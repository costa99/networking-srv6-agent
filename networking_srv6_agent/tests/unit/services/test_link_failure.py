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

"""Silent fabric nodes, end to end.

The two-spine fabric of test_te (node1 - la - s1|s2 - lb - node2, spine
links 100 Mbit/s), a real database, link state posted through the API as
the fabric agents post it, and the TE liveness worker's pass run by hand.
The server's clock is a mock, so "silent for 15 s" is exact. A fabric node
that went silent is dead: a path through it goes DEGRADED (direct).
"""

import datetime
from unittest import mock

from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.services import te_liveness
from networking_srv6_agent.tests.unit.services import test_te

LS = 'srv6_link_states'
T0 = datetime.datetime(2026, 11, 1, 12, 0, 0)
FABRIC_NODES = ('la', 'lb', 's1', 's2')


class LinkFailureTestCase(test_te.TeRestTestCase):

    def setUp(self):
        super().setUp()
        self.srv6.fabric_locators = dict(test_te.FABRIC)
        self.srv6.capacity = dict(test_te.CAPACITY)
        self.now = T0
        clock = mock.patch('oslo_utils.timeutils.utcnow',
                           side_effect=lambda *a, **kw: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.monitor = te_liveness.TeLivenessMonitor(self.srv6)

    def _at(self, seconds):
        self.now = T0 + datetime.timedelta(seconds=seconds)

    def _report(self, node, down=()):
        """Post node's batch, as its agent would.

        Both directions of each of its links; the directed links in down
        reported down.
        """
        items = [{'reporter': node, 'from_node': a, 'to_node': b,
                  'up': (a, b) not in down, 'mbps': 0.0, 'drops': 0}
                 for a, b in sorted(self.srv6.capacity) if node in (a, b)]
        req = self.new_create_request(LS, {LS: items}, as_admin=True)
        self._check(req.get_response(self.ext_api), 201)

    def _all_report(self, down=(), but=()):
        for node in FABRIC_NODES:
            if node not in but:
                self._report(node, down)

    def _via_s1(self):
        """An operator path node1 -> node2 through the spine s1."""
        return self._path(destination_host='node2', source_host='node1',
                          via_hosts=['s1'])

    def _status(self, path):
        return self._show(path['id'], as_admin=True)['status']

    def _pass(self):
        """One pass of the TE liveness worker, as neutron runs it."""
        self.monitor.check()


class TestSilentNodes(LinkFailureTestCase):

    def test_a_silent_spine_is_down(self):
        path = self._via_s1()
        self._all_report()
        self._at(20)                           # beyond stale_after (15 s)
        self._all_report(but={'s1'})           # s1 went silent
        self.assertIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self.agent_rpc.reset_mock()
        self._pass()
        self.assertEqual('DEGRADED', self._status(path))
        # the domain was pushed: the agents route it direct
        self.assertTrue(self.agent_rpc.method_calls)

    def test_an_unknown_link_in_its_batch_does_not_silence_a_node(self):
        # s1's link map names a link topology_file does not have: the batch
        # used to be refused whole, so s1 went silent and read as dead
        path = self._via_s1()
        self._all_report()
        self._at(20)
        self._all_report(but={'s1'})
        items = [{'reporter': 's1', 'from_node': a, 'to_node': b,
                  'up': True, 'mbps': 0.0, 'drops': 0}
                 for a, b in sorted(self.srv6.capacity) if 's1' in (a, b)]
        items.append({'reporter': 's1', 'from_node': 's1', 'to_node': 'lc',
                      'up': True, 'mbps': 0.0, 'drops': 0})
        req = self.new_create_request(LS, {LS: items}, as_admin=True)
        self._check(req.get_response(self.ext_api), 201)
        self.assertNotIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self.assertEqual('ACTIVE', self._status(path))

    def test_silent_for_less_than_stale_after_is_alive(self):
        path = self._via_s1()
        self._all_report()
        self._at(15)
        self._all_report(but={'s1'})
        self.assertNotIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self.assertEqual('ACTIVE', self._status(path))

    def test_a_node_that_never_reported_is_not_down(self):
        path = self._via_s1()
        self._all_report(but={'s1'})           # no agent on s1 at all
        self.assertNotIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self.assertEqual('ACTIVE', self._status(path))

    def test_every_node_silent_is_an_outage_nothing_is_down(self):
        path = self._via_s1()
        self._all_report(down={('la', 's1')})
        self._at(20)                           # nobody posts any more
        self.assertEqual(frozenset(), self.srv6._dead_hosts(self.admin_ctx))
        self.assertEqual('ACTIVE', self._status(path))

    def test_no_link_state_at_all_changes_nothing(self):
        path = self._via_s1()
        self.assertEqual([], link_state_db.get_link_states(self.admin_ctx))
        self._pass()
        self.assertEqual('ACTIVE', self._status(path))
