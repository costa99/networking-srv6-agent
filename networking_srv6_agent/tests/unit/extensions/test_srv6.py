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

"""Invariants of the API definitions and their descriptors.

Each of these is a decision recorded in MIGRATION-PLAN.md section 8 or
P4-PLAN.md section 3, stated as something a later edit would have to break
a test to undo.
"""

import importlib
import pkgutil

from neutron.tests import base

from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def
from networking_srv6_agent import extensions as srv6_extensions
from networking_srv6_agent.extensions import srv6 as srv6_ext
from networking_srv6_agent.extensions import srv6_locators as loc_ext


DOMAIN_ATTRS = srv6_def.RESOURCE_ATTRIBUTE_MAP[srv6_def.COLLECTION_NAME]
ASSOC = srv6_def.SUB_RESOURCE_ATTRIBUTE_MAP[srv6_def.NET_ASSOC_COLLECTION_NAME]


class TestDomainDefinition(base.BaseTestCase):

    def test_behavior_offers_exactly_end_dt46(self):
        # 8.3: the API advertises only the choice the dataplane has.
        attr = DOMAIN_ATTRS[srv6_def.SRV6_BEHAVIOR]
        self.assertEqual(['End.DT46'], attr['validate']['type:values'])
        self.assertEqual('End.DT46', attr['default'])

    def test_behavior_is_create_only(self):
        attr = DOMAIN_ATTRS[srv6_def.SRV6_BEHAVIOR]
        self.assertTrue(attr['allow_post'])
        self.assertFalse(attr['allow_put'])

    def test_sid_function_is_server_allocated_and_policy_enforced(self):
        attr = DOMAIN_ATTRS[srv6_def.SID_FUNCTION]
        self.assertFalse(attr['allow_post'])
        self.assertFalse(attr['allow_put'])
        # enforce_policy is what makes get_srv6_domain:sid_function apply.
        self.assertTrue(attr['enforce_policy'])

    def test_no_federation_vocabulary(self):
        for gone in ('route_targets', 'import_targets', 'export_targets',
                     'route_distinguishers', 'type', 'routers', 'vni'):
            self.assertNotIn(gone, DOMAIN_ATTRS)

    def test_project_id_not_tenant_id(self):
        self.assertIn('project_id', DOMAIN_ATTRS)
        self.assertNotIn('tenant_id', DOMAIN_ATTRS)
        self.assertTrue(DOMAIN_ATTRS['project_id']['required_by_policy'])


class TestAssociationDefinition(base.BaseTestCase):

    def test_parent_is_the_domain(self):
        self.assertEqual({'collection_name': 'srv6_domains',
                          'member_name': 'srv6_domain'}, ASSOC['parent'])

    def test_nothing_on_an_association_is_updatable(self):
        for name, attr in ASSOC['parameters'].items():
            self.assertFalse(attr['allow_put'], name)

    def test_network_associations_only(self):
        # 8.1: router associations are not implemented at all -- no
        # endpoint rather than an endpoint that only raises.
        self.assertEqual([srv6_def.NET_ASSOC_COLLECTION_NAME],
                         list(srv6_def.SUB_RESOURCE_ATTRIBUTE_MAP))
        for method in dir(srv6_ext.Srv6PluginBase):
            self.assertNotIn('router_association', method)

    def test_no_association_update_method(self):
        self.assertFalse(hasattr(
            srv6_ext.Srv6PluginBase,
            'update_srv6_domain_network_association'))


class TestLocatorDefinition(base.BaseTestCase):

    def test_every_attribute_is_read_only(self):
        attrs = loc_def.RESOURCE_ATTRIBUTE_MAP[loc_def.COLLECTION_NAME]
        for name, attr in attrs.items():
            self.assertFalse(attr['allow_post'], name)
            self.assertFalse(attr['allow_put'], name)

    def test_requires_the_srv6_extension(self):
        self.assertEqual([srv6_def.ALIAS], loc_def.REQUIRED_EXTENSIONS)


class TestDescriptors(base.BaseTestCase):

    def test_class_names_survive_neutrons_capitalize(self):
        """neutron/api/extensions.py: ext_name = mod_name.capitalize().

        A descriptor named anything else is silently skipped and its API
        answers 404 with nothing in the log.
        """
        for module_info in pkgutil.iter_modules(srv6_extensions.__path__):
            module = importlib.import_module(
                'networking_srv6_agent.extensions.' + module_info.name)
            self.assertTrue(
                hasattr(module, module_info.name.capitalize()),
                "%s.py must define %s" % (module_info.name,
                                          module_info.name.capitalize()))

    def test_srv6_descriptor_metadata(self):
        self.assertEqual('srv6', srv6_ext.Srv6.get_alias())
        self.assertTrue(srv6_ext.Srv6.get_name())
        self.assertTrue(srv6_ext.Srv6.get_description())
        self.assertTrue(srv6_ext.Srv6.get_updated())
        self.assertEqual([], srv6_ext.Srv6.get_required_extensions())
        extended = srv6_ext.Srv6.get_extended_resources('2.0')
        self.assertIn('srv6_domains', extended)
        self.assertIn('network_associations', extended)
        self.assertEqual({}, srv6_ext.Srv6.get_extended_resources('1.0'))

    def test_locators_descriptor_metadata(self):
        self.assertEqual('srv6-locators', loc_ext.Srv6_locators.get_alias())
        self.assertIn('srv6_locators',
                      loc_ext.Srv6_locators.get_extended_resources('2.0'))

    def test_plugin_interface(self):
        self.assertIs(srv6_ext.Srv6PluginBase,
                      srv6_ext.Srv6.get_plugin_interface())
