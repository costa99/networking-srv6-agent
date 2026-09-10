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

"""The wire contract, from the server's side.

The agent side of the same contract -- that every kwarg sent here binds to a
named parameter of the agent's handler -- is in
tests/unit/agent/test_agent_extension.py (TestRpcContract).
"""

from unittest import mock

from neutron.tests import base

from networking_srv6_agent.services import rpc


class TestNotifier(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        get_client = mock.patch.object(rpc.n_rpc, 'get_client')
        self.client = get_client.start().return_value
        self.addCleanup(get_client.stop)
        self.notifier = rpc.Srv6AgentNotifyAPI()
        self.cctxt = self.client.prepare.return_value

    def test_fanout_topic_is_this_packages_own(self):
        # Distinct from every networking-bgpvpn driver's, so both plugins
        # can run in one deployment without their fanouts colliding.
        self.assertEqual('q-agent-notifier-srv6-update',
                         self.notifier.topic_srv6_update)

    def test_domain_updated(self):
        self.notifier.domain_updated(mock.sentinel.ctx, {'id': 'd1'})
        self.client.prepare.assert_called_once_with(
            topic='q-agent-notifier-srv6-update', fanout=True)
        self.cctxt.cast.assert_called_once_with(
            mock.sentinel.ctx, 'domain_updated', payload_version=1,
            domain={'id': 'd1'})

    def test_routes_updated_defaults_to_empty_lists(self):
        self.notifier.routes_updated(mock.sentinel.ctx, 'd1',
                                     add=[{'prefix': '10.0.0.5/32'}])
        self.cctxt.cast.assert_called_once_with(
            mock.sentinel.ctx, 'routes_updated', payload_version=1,
            domain_id='d1', add=[{'prefix': '10.0.0.5/32'}], remove=[])

    def test_domain_deleted_carries_what_the_agent_cannot_look_up(self):
        self.notifier.domain_deleted(mock.sentinel.ctx, 'd1', 7, 10007)
        self.cctxt.cast.assert_called_once_with(
            mock.sentinel.ctx, 'domain_deleted', payload_version=1,
            domain_id='d1', function_id=7, vrf_table=10007)


class TestServerRpcCallback(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.plugin = mock.Mock()
        self.callback = rpc.Srv6ServerRpcCallback(self.plugin)

    def test_delegates_to_the_plugin(self):
        self.callback.sync_state(mock.sentinel.ctx, host='node1',
                                 locator='fc00:0:1::/48', payload_version=1)
        self.plugin.handle_sync_state.assert_called_once_with(
            mock.sentinel.ctx, 'node1', 'fc00:0:1::/48')

    def test_refuses_an_unknown_payload_version(self):
        # Half-applying a message whose shape cannot be relied on is worse
        # than refusing it.
        self.assertEqual([], self.callback.sync_state(
            mock.sentinel.ctx, host='node1', locator='fc00:0:1::/48',
            payload_version=99))
        self.plugin.handle_sync_state.assert_not_called()
