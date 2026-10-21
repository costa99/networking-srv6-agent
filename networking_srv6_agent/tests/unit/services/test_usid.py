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

"""The server half of microSID (USID-PLAN.md).

What [srv6] dataplane_encoding = usid changes on the server: the function
id ranges it accepts (D2), the locators it registers (D5), the transit SIDs
it resolves via hosts to (D10), the `encoded_segments` it shows (D11), and
the capabilities it checks agents for (D3).
"""

from unittest import mock

from neutron_lib import exceptions as n_exc
from oslo_config import cfg
from oslo_db import exception as db_exc

from networking_srv6_agent.common import config
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_plugin
from networking_srv6_agent.tests.unit.services import test_te


LOCATORS = {'node1': test_te.LOC1, 'node2': test_te.LOC2,
            'node3': test_te.LOC3}


class UsidPluginTestCaseBase(test_plugin.PluginUnitTestCaseBase):

    encoding = 'usid'
    ranges = ['0xe000:0xefff']

    def setUp(self):
        super().setUp()
        config.register_server_opts()
        self.config(group=config.SRV6_SERVER_GROUP,
                    dataplane_encoding=self.encoding,
                    function_id_ranges=self.ranges)
        # srv6_db is mocked wholesale by the base; the range check needs
        # the real parser.
        self.srv6_db.parse_function_id_ranges.side_effect = \
            srv6_db.parse_function_id_ranges
        self.plugin = plugin.Srv6Plugin()
        self.srv6_db.get_host_locators.return_value = dict(LOCATORS)
        self.srv6_db.get_function_id.return_value = 0xe005


class TestConfigValidation(UsidPluginTestCaseBase):

    def test_the_lib_is_accepted(self):
        self.assertTrue(self.plugin.usid)

    def test_a_range_outside_the_lib_is_refused(self):
        self.config(group=config.SRV6_SERVER_GROUP,
                    function_id_ranges=['1:4095'])
        self.assertRaises(n_exc.InvalidConfigurationOption,
                          plugin.Srv6Plugin)

    def test_a_range_reaching_past_the_lib_is_refused(self):
        self.config(group=config.SRV6_SERVER_GROUP,
                    function_id_ranges=['0xe000:0xefff', '0xef00:0xefff',
                                        '0xd000:0xe000'])
        self.assertRaises(n_exc.InvalidConfigurationOption,
                          plugin.Srv6Plugin)

    def test_full_sids_accept_a_range_outside_the_lib(self):
        self.config(group=config.SRV6_SERVER_GROUP,
                    dataplane_encoding='encap',
                    function_id_ranges=['1:4095'])
        self.assertFalse(plugin.Srv6Plugin().usid)

    def test_the_default_range_works_with_both_encodings(self):
        # Switching the encoding must not need a range change too.
        cfg.CONF.clear_override('function_id_ranges',
                                group=config.SRV6_SERVER_GROUP)
        self.assertEqual(['0xe000:0xefff'],
                         cfg.CONF.srv6.function_id_ranges)
        for encoding in ('usid', 'encap'):
            self.config(group=config.SRV6_SERVER_GROUP,
                        dataplane_encoding=encoding)
            self.assertEqual(encoding == 'usid', plugin.Srv6Plugin().usid)


class TestPayloadAndPaths(UsidPluginTestCaseBase):

    def test_the_payload_names_the_encoding(self):
        self.core.get_ports.return_value = []
        payload = self.plugin._build_domain_payload(
            self.ctx, test_plugin._domain())
        self.assertEqual('usid', payload['encoding'])

    def test_transit_sids_are_un(self):
        self.assertEqual(['fc00:0:3::', 'fc00:0:1::'],
                         self.plugin._transit_sids(['node3', 'node1'],
                                                   LOCATORS))

    def test_a_path_shows_its_container(self):
        path = self.plugin._resolve_te_path(
            self.ctx, test_te._db_path(destination_host='node2',
                                       via_hosts=['node3', 'node1']),
            LOCATORS, 0xe005)
        self.assertEqual(['fc00:0:3::', 'fc00:0:1::', 'fc00:0:2:e005::'],
                         path['segments'])
        self.assertEqual(['fc00:0:3:1:2:e005::'], path['encoded_segments'])

    def test_a_source_entry_is_un(self):
        self.te_db.get_te_paths_for_domain.return_value = {
            'by_port': {}, 'by_host': {},
            'by_source': {'node1': {'by_port': {},
                                    'by_host': {'node2': ['node3']}}}}
        route = self.plugin._attach_segments(
            self.ctx, 'd1', [test_te._route('p1')], LOCATORS)[0]
        self.assertEqual({'node1': ['fc00:0:3::']},
                         route['segments_by_source'])

    def test_a_source_path_container_leaves_out_the_source(self):
        # Ingress node1, first waypoint node1: its uN is dropped, so the
        # container starts at node3.
        path = self.plugin._resolve_te_path(
            self.ctx, test_te._db_path(destination_host='node2',
                                       source_host='node1',
                                       via_hosts=['node1', 'node3']),
            LOCATORS, 0xe005)
        self.assertEqual(['fc00:0:3:2:e005::'], path['encoded_segments'])

    def test_a_degraded_path_pushes_nothing(self):
        path = self.plugin._resolve_te_path(
            self.ctx, test_te._db_path(destination_host='nowhere'),
            LOCATORS, 0xe005)
        self.assertEqual(([], []), (path['segments'],
                                    path['encoded_segments']))


class TestFullSidPaths(UsidPluginTestCaseBase):

    encoding = 'encap'
    ranges = ['1:4095']

    def test_encoded_is_the_same_list(self):
        path = self.plugin._resolve_te_path(
            self.ctx, test_te._db_path(destination_host='node2',
                                       via_hosts=['node1']),
            LOCATORS, 7)
        self.assertEqual(['fc00:0:1:fff0::', 'fc00:0:2:7::'],
                         path['encoded_segments'])
        self.assertEqual(path['segments'], path['encoded_segments'])

    def test_full_sids_need_no_capability(self):
        with mock.patch.object(plugin, 'LOG') as log:
            self.plugin._check_capabilities('node1', None)
        log.warning.assert_not_called()
        log.error.assert_not_called()


class TestSyncState(UsidPluginTestCaseBase):

    def setUp(self):
        super().setUp()
        admin = mock.patch.object(plugin.n_context, 'get_admin_context',
                                  return_value=mock.Mock())
        admin.start()
        self.addCleanup(admin.stop)
        self.domain_db.get_domains.return_value = []

    def _sync(self, locator, host='node4', capabilities=('encap-red',
                                                         'next-csid')):
        return self.plugin.handle_sync_state(
            self.ctx, host, locator,
            capabilities=list(capabilities)
            if capabilities is not None else None)

    def test_an_f3216_locator_in_the_block_is_registered(self):
        self._sync('fc00:0:4::/48')
        self.srv6_db.set_host_locator.assert_called_once()

    def test_a_64_is_refused(self):
        self.assertEqual([], self._sync('fc00:0:4:1::/64'))
        self.srv6_db.set_host_locator.assert_not_called()

    def test_another_block_is_refused(self):
        self._sync('fc01:0:4::/48')
        self.srv6_db.set_host_locator.assert_not_called()

    def test_a_node_id_in_the_lib_is_refused(self):
        self._sync('fc00:0:e001::/48')
        self.srv6_db.set_host_locator.assert_not_called()

    def test_re_registering_ones_own_locator_is_fine(self):
        self._sync(test_te.LOC1, host='node1')
        self.srv6_db.set_host_locator.assert_called_once()

    def test_a_node_lacking_the_flavor_is_named(self):
        with mock.patch.object(plugin, 'LOG') as log:
            self._sync('fc00:0:4::/48', capabilities=['encap-red'])
        self.assertIn('next-csid', log.error.call_args[0][1]['missing'])

    def test_an_agent_that_says_nothing_is_warned_about(self):
        with mock.patch.object(plugin, 'LOG') as log:
            self._sync('fc00:0:4::/48', capabilities=None)
        log.warning.assert_called()


class TestPoolRace(test_plugin.PluginUnitTestCaseBase):
    """Two first sync_states after a range change insert the same new ids."""

    def test_a_concurrent_insert_is_retried_once(self):
        self.srv6_db.sync_allocation_pool.side_effect = [
            db_exc.DBDuplicateEntry(columns=['function_id']), None]
        self.plugin._ensure_pool(mock.Mock())
        self.assertEqual(2, self.srv6_db.sync_allocation_pool.call_count)
        self.assertTrue(self.plugin._pool_synced)

    def test_a_second_failure_is_not_swallowed(self):
        self.srv6_db.sync_allocation_pool.side_effect = \
            db_exc.DBDuplicateEntry(columns=['function_id'])
        self.assertRaises(db_exc.DBDuplicateEntry,
                          self.plugin._ensure_pool, mock.Mock())
        self.assertFalse(self.plugin._pool_synced)
