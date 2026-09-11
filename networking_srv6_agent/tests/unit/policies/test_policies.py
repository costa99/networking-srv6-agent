# Copyright 2026 networking-srv6-agent contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Policy coverage: the only cheap defence against an invisible 403.

oslo.policy fails closed on a rule it never registered, and neutron builds
`<action>:<attribute>` rule names for every enforce_policy attribute it
sees. So an attribute flagged in an API definition with no default here
makes every call touching it return 403 -- admin included -- with nothing
in the log naming the rule. This walks the attribute map of EVERY descriptor
under extensions/, so a resource added later (P6's TE paths) is covered the
day it lands.
"""

import importlib
from importlib import metadata
import pkgutil

from neutron.tests import base
from neutron_lib.policy import rules as lib_rules

from networking_srv6_agent import extensions as srv6_extensions
from networking_srv6_agent import policies


def _definitions():
    for module_info in pkgutil.iter_modules(srv6_extensions.__path__):
        module = importlib.import_module(
            'networking_srv6_agent.extensions.' + module_info.name)
        yield getattr(module, module_info.name.capitalize()).api_definition


def _operations(attr):
    ops = ['get']
    if attr.get('allow_post'):
        ops.append('create')
    if attr.get('allow_put'):
        ops.append('update')
    return ops


class TestPolicyCoverage(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.rules = {rule.name: rule for rule in policies.list_rules()}

    def test_every_enforce_policy_attribute_has_a_rule(self):
        missing = []
        for definition in _definitions():
            for collection, attrs in (
                    definition.RESOURCE_ATTRIBUTE_MAP.items()):
                member = collection[:-1]
                for name, attr in attrs.items():
                    if not attr.get('enforce_policy'):
                        continue
                    missing += ['%s_%s:%s' % (op, member, name)
                                for op in _operations(attr)
                                if '%s_%s:%s' % (op, member, name)
                                not in self.rules]
            for collection, sub in (
                    (definition.SUB_RESOURCE_ATTRIBUTE_MAP or {}).items()):
                member = '%s_%s' % (sub['parent']['member_name'],
                                    collection[:-1])
                for name, attr in sub['parameters'].items():
                    if not attr.get('enforce_policy'):
                        continue
                    missing += ['%s_%s:%s' % (op, member, name)
                                for op in _operations(attr)
                                if '%s_%s:%s' % (op, member, name)
                                not in self.rules]
        self.assertEqual([], missing)

    def test_every_operation_has_a_base_rule(self):
        for name in ('create_srv6_domain', 'get_srv6_domain',
                     'update_srv6_domain', 'delete_srv6_domain',
                     'create_srv6_domain_network_association',
                     'get_srv6_domain_network_association',
                     'delete_srv6_domain_network_association',
                     'get_srv6_locator',
                     'create_srv6_te_path', 'get_srv6_te_path',
                     'update_srv6_te_path', 'delete_srv6_te_path'):
            self.assertIn(name, self.rules)

    def test_te_paths_are_admin_only_for_every_verb_and_attribute(self):
        # MIGRATION-PLAN.md 8.5: steering is the admin's, and the read side
        # names hosts and underlay SIDs (P6-PLAN.md 1).
        te_rules = [r for r in self.rules.values()
                    if r.name.split(':')[0].endswith('_srv6_te_path')]
        self.assertEqual(12, len(te_rules))
        for rule in te_rules:
            self.assertEqual(lib_rules.ADMIN, rule.check_str, rule.name)

    def test_sid_function_is_admin_only(self):
        self.assertEqual(lib_rules.ADMIN,
                         self.rules['get_srv6_domain:sid_function'].check_str)

    def test_locators_are_admin_only_and_read_only(self):
        self.assertEqual(lib_rules.ADMIN,
                         self.rules['get_srv6_locator'].check_str)
        for verb in ('create', 'update', 'delete'):
            self.assertNotIn('%s_srv6_locator' % verb, self.rules)

    def test_domains_belong_to_their_project(self):
        self.assertEqual(lib_rules.ADMIN_OR_PROJECT_MEMBER,
                         self.rules['create_srv6_domain'].check_str)
        self.assertEqual(lib_rules.ADMIN_OR_PROJECT_READER,
                         self.rules['get_srv6_domain'].check_str)

    def test_no_parent_owner_persona(self):
        """No ext_parent_owner persona anywhere.

        Neutron resolves ext_parent_owner only for the parents in
        neutron-lib's EXT_PARENT_RESOURCE_MAPPING, and srv6_domain is not
        one of them: such a rule could never pass for a tenant.
        """
        for rule in self.rules.values():
            self.assertNotIn('ext_parent', rule.check_str, rule.name)

    def test_no_deprecated_persona(self):
        for rule in self.rules.values():
            self.assertNotIn('admin_or_owner', rule.check_str, rule.name)

    def test_every_rule_is_project_scoped(self):
        for rule in self.rules.values():
            self.assertEqual(['project'], rule.scope_types, rule.name)


class TestEntryPoints(base.BaseTestCase):
    """What neutron-server loads, read from the installed metadata.

    The only thing that proves pyproject.toml's tables made it through the
    build -- `pip install -e .` succeeding proves nothing.
    """

    def _load(self, group, name):
        for entry_point in metadata.entry_points(group=group):
            if entry_point.name == name:
                return entry_point.load()
        self.fail("no %s entry point named %s" % (group, name))

    def test_policies(self):
        self.assertIs(policies.list_rules,
                      self._load('neutron.policies', 'networking-srv6-agent'))
        self.assertIs(policies.list_rules,
                      self._load('oslo.policy.policies',
                                 'networking-srv6-agent'))

    def test_service_plugin(self):
        from networking_srv6_agent.services import plugin
        self.assertIs(plugin.Srv6Plugin,
                      self._load('neutron.service_plugins', 'srv6'))

    def test_l2_agent_extension(self):
        from networking_srv6_agent.agent import agent_extension
        self.assertIs(agent_extension.Srv6AgentExtension,
                      self._load('neutron.agent.l2.extensions', 'srv6'))
