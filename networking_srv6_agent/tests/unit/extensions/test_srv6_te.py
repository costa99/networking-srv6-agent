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

"""Invariants of the srv6-te definition and descriptor (P6-PLAN.md 1, 5)."""

from neutron.tests import base
from neutron_lib import exceptions as n_exc

from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.extensions import srv6_te as te_ext
from networking_srv6_agent.services import plugin


ATTRS = te_def.RESOURCE_ATTRIBUTE_MAP[te_def.COLLECTION_NAME]


class TestTePathDefinition(base.BaseTestCase):

    def test_the_domain_and_the_selectors_are_create_only(self):
        # A path that changed what it selects is a different path.
        for name in ('domain_id', 'destination_host', 'destination_port_id'):
            self.assertTrue(ATTRS[name]['allow_post'], name)
            self.assertFalse(ATTRS[name]['allow_put'], name)

    def test_each_selector_is_optional_on_its_own(self):
        # "Exactly one" is the plugin's check; here each may be absent.
        self.assertIsNone(ATTRS['destination_host']['default'])
        self.assertIsNone(ATTRS['destination_port_id']['default'])

    def test_via_hosts_is_put_able(self):
        # PUT replaces the list: re-steering with no delete/create window.
        attr = ATTRS['via_hosts']
        self.assertTrue(attr['allow_post'])
        self.assertTrue(attr['allow_put'])
        self.assertEqual([], attr['default'])
        self.assertIn('type:list_of_unique_strings', attr['validate'])

    def test_via_hosts_accepts_a_single_host_or_null(self):
        convert = ATTRS['via_hosts']['convert_to']
        self.assertEqual(['node2'], convert('node2'))
        self.assertEqual([], convert(None))

    def test_segments_and_status_are_server_resolved(self):
        for name in ('segments', 'status'):
            self.assertFalse(ATTRS[name]['allow_post'], name)
            self.assertFalse(ATTRS[name]['allow_put'], name)

    def test_the_topology_attributes_are_policy_enforced(self):
        for name in ('destination_host', 'destination_port_id', 'via_hosts',
                     'segments'):
            self.assertTrue(ATTRS[name].get('enforce_policy'), name)

    def test_project_id_is_accepted_so_the_plugin_can_force_it(self):
        # Read-only would break neutron's populate_project_id.
        self.assertTrue(ATTRS['project_id']['allow_post'])
        self.assertTrue(ATTRS['project_id']['required_by_policy'])

    def test_top_level_not_a_child_of_the_domain(self):
        # A child of a tenant-readable object would leak the host inventory.
        self.assertEqual({}, te_def.SUB_RESOURCE_ATTRIBUTE_MAP)
        self.assertEqual('', te_def.API_PREFIX)
        self.assertEqual('srv6_te_paths', te_def.COLLECTION_NAME)

    def test_requires_the_srv6_extension(self):
        self.assertEqual([srv6_def.ALIAS], te_def.REQUIRED_EXTENSIONS)


class TestTeDescriptor(base.BaseTestCase):

    def test_the_class_name_survives_neutrons_capitalize(self):
        self.assertEqual('Srv6_te', 'srv6_te'.capitalize())
        self.assertTrue(hasattr(te_ext, 'Srv6_te'))

    def test_metadata(self):
        self.assertEqual('srv6-te', te_ext.Srv6_te.get_alias())
        self.assertIn('srv6_te_paths',
                      te_ext.Srv6_te.get_extended_resources('2.0'))
        self.assertEqual(['srv6'], te_ext.Srv6_te.get_required_extensions())

    def test_the_plugin_serves_it(self):
        self.assertIn('srv6-te',
                      plugin.Srv6Plugin.supported_extension_aliases)
        for verb in ('create', 'get', 'update', 'delete'):
            self.assertTrue(hasattr(plugin.Srv6Plugin,
                                    '%s_srv6_te_path' % verb), verb)
        self.assertTrue(hasattr(plugin.Srv6Plugin, 'get_srv6_te_paths'))

    def test_exceptions_carry_their_http_codes(self):
        self.assertTrue(issubclass(te_ext.Srv6TePathNotFound, n_exc.NotFound))
        self.assertTrue(issubclass(te_ext.Srv6TePathInvalid,
                                   n_exc.BadRequest))
        self.assertTrue(issubclass(te_ext.Srv6TePathExists, n_exc.Conflict))
        self.assertIn('no such host', str(te_ext.Srv6TePathInvalid(
            reason='no such host')))
