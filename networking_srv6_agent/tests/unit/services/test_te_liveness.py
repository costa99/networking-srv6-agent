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

"""The TE liveness monitor: when it pushes, and when it must not.

The plugin is a mock here. What it computes for a dead node (the dead set,
the routes built around it) is covered in test_te.TestDeadWaypoint; these
cases pin only the monitor's own decisions: which transitions cause a push,
to which domains, and that a failure is retried rather than swallowed.
"""

from unittest import mock

from neutron.tests import base

from networking_srv6_agent.services import te_liveness


def _path(path_id, domain_id, *via):
    return {'id': path_id, 'domain_id': domain_id, 'via_hosts': list(via)}


class TestTeLivenessMonitor(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(te_liveness, 'te_db')
        self.te_db = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(te_liveness.n_context,
                                    'get_admin_context')
        self.ctx = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.plugin = mock.Mock()
        self.plugin._dead_hosts.return_value = frozenset()
        self.monitor = te_liveness.TeLivenessMonitor(self.plugin)
        self.te_db.get_all_te_paths.return_value = [
            _path('a', 'd1', 'node1'),
            _path('b', 'd2', 'node1', 'node3'),
            _path('c', 'd3', 'node3')]

    def _dead(self, *hosts):
        self.plugin._dead_hosts.return_value = frozenset(hosts)

    def _pushed(self):
        return sorted(call[0][1] for call
                      in self.plugin._push_domain_by_id.call_args_list)

    def test_nothing_dead_at_the_start_pushes_nothing(self):
        self.monitor.check()
        self.assertEqual([], self._pushed())

    def test_a_node_found_dead_at_the_start_is_pushed(self):
        # After a server restart agents may hold routes through it.
        self._dead('node1')
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())

    def test_a_death_pushes_the_domains_whose_paths_cross_the_node(self):
        self.monitor.check()
        self._dead('node1')
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())

    def test_an_unchanged_set_pushes_nothing_more(self):
        self._dead('node1')
        self.monitor.check()
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())

    def test_the_return_of_the_node_pushes_again(self):
        self._dead('node1')
        self.monitor.check()
        self.plugin._push_domain_by_id.reset_mock()
        self._dead()
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())

    def test_a_second_death_pushes_only_what_it_touches(self):
        self._dead('node1')
        self.monitor.check()
        self.plugin._push_domain_by_id.reset_mock()
        self._dead('node1', 'node3')
        self.monitor.check()
        self.assertEqual(['d2', 'd3'], self._pushed())

    def test_a_dead_node_no_path_crosses_pushes_nothing(self):
        self.monitor.check()
        self._dead('node2')
        self.monitor.check()
        self.assertEqual([], self._pushed())

    def test_a_failed_push_is_retried_at_the_next_pass(self):
        self.monitor.check()
        self._dead('node1')
        self.plugin._push_domain_by_id.side_effect = RuntimeError('rpc')
        self.monitor.check()
        self.plugin._push_domain_by_id.reset_mock(side_effect=True)
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())
        self.plugin._push_domain_by_id.reset_mock()
        self.monitor.check()
        self.assertEqual([], self._pushed())

    def test_a_failing_check_does_not_end_the_loop(self):
        self.te_db.get_all_te_paths.side_effect = RuntimeError('db')
        self.monitor.check()
        self.te_db.get_all_te_paths.side_effect = None
        self._dead('node1')
        self.monitor.check()
        self.assertEqual(['d1', 'd2'], self._pushed())
