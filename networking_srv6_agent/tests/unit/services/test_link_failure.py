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

"""PLAN-LINK-STATE-TE step 4: failures of the underlay, end to end.

The two-spine fabric of test_te (node1 - la - s1|s2 - lb - node2, spine
links 100 Mbit/s), a real database, link state posted through the API as
the fabric agents post it, and the TE liveness worker's pass run by hand.
The server's clock is a mock, so "silent for 15 s" is exact.
"""

import datetime
from unittest import mock

from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.services import plugin
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
        self.srv6.link_capacity = dict(test_te.CAPACITY)
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

    def _reserve(self, expected=201, domain=None, **attrs):
        body = dict({'destination_host': 'node2', 'source_host': 'node1',
                     'bandwidth': 60}, **attrs)
        if domain:
            body['domain_id'] = domain
        return self._path(expected, **body)

    def _pass(self):
        """One pass of the TE liveness worker, as neutron runs it."""
        self.monitor.check()


class TestReroute(LinkFailureTestCase):

    def test_a_reservation_off_a_down_link_moves_to_the_other_spine(self):
        path = self._reserve()
        self.assertEqual(['la', 's1', 'lb'], path['via_hosts'])
        self._all_report(down={('la', 's1')})
        self.agent_rpc.reset_mock()
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            self._pass()
        self.assertEqual(['la', 's2', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])
        self.assertIn('moved from', warn.call_args_list[0][0][0])
        # the domain was pushed: the agents swap the head-end route
        self.assertTrue(self.agent_rpc.method_calls)

    def test_down_at_the_far_end_only_is_enough(self):
        # s1 reports its receive side of la>s1 down, la says up (L8)
        path = self._reserve()
        self._all_report()
        self._report('s1', down={('la', 's1')})
        self._pass()
        self.assertEqual(['la', 's2', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])

    def test_one_that_does_not_fit_is_deleted_with_a_warning(self):
        first = self._reserve()
        second = self._reserve(domain=self._domain()['id'])
        self.assertEqual(['la', 's2', 'lb'], second['via_hosts'])
        self._all_report(down={('s2', 'lb')})
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            self._pass()
        # 60 + 60 > 100 on s1
        self._show(second['id'], expected=404, as_admin=True)
        self.assertEqual(['la', 's1', 'lb'], self._show(
            first['id'], as_admin=True)['via_hosts'])
        self.assertIn('deleted', warn.call_args_list[0][0][0])

    def test_a_link_back_up_moves_nothing_back(self):
        path = self._reserve()
        self._all_report(down={('la', 's1')})
        self._pass()
        self._at(5)
        self._all_report()                     # la>s1 up again
        self._pass()
        self.assertEqual(['la', 's2', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])

    def test_a_reservation_crossing_nothing_dead_is_untouched(self):
        path = self._reserve()
        self._all_report(down={('la', 's2')})
        with mock.patch.object(self.srv6, '_reroute_reservation') as move:
            self._pass()
        move.assert_not_called()
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])

    def test_admission_avoids_a_down_link(self):
        self._all_report(down={('la', 's1')})
        self.assertEqual(['la', 's2', 'lb'], self._reserve()['via_hosts'])


class TestSilentNodes(LinkFailureTestCase):

    def test_a_silent_spine_is_down(self):
        path = self._reserve()
        operator = self._path(destination_host='node2', via_hosts=['s1'],
                              source_host='node1',
                              domain_id=self._domain()['id'])
        self._all_report()
        self._at(20)                           # beyond stale_after (15 s)
        self._all_report(but={'s1'})           # s1 went silent
        self.assertIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self._pass()
        self.assertEqual(['la', 's2', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])
        # the operator path through it: DEGRADED, direct
        self.assertEqual('DEGRADED', self._show(
            operator['id'], as_admin=True)['status'])

    def test_an_unknown_link_in_its_batch_does_not_silence_a_node(self):
        # s1's link map names a link topology_file does not have (audit
        # 2026-11-02 finding 2): the batch used to be refused whole, so s1
        # went silent, read as dead, and the reservation left it
        path = self._reserve()
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
        self._pass()
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])

    def test_silent_for_less_than_stale_after_is_alive(self):
        path = self._reserve()
        self._all_report()
        self._at(15)
        self._all_report(but={'s1'})
        self.assertNotIn('s1', self.srv6._dead_hosts(self.admin_ctx))
        self._pass()
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])

    def test_a_node_that_never_reported_is_not_down(self):
        path = self._reserve()
        self._all_report(but={'s1'})           # no agent on s1 at all
        self._pass()
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])
        self.assertNotIn('s1', self.srv6._dead_hosts(self.admin_ctx))

    def test_every_node_silent_is_an_outage_nothing_moves(self):
        path = self._reserve()
        self._all_report(down={('la', 's1')})
        self._at(20)                           # nobody posts any more
        with mock.patch.object(plugin.LOG, 'error') as error:
            self._pass()
        self.assertIn('outage', error.call_args[0][0])
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])
        self.assertEqual(frozenset(), self.srv6._dead_hosts(self.admin_ctx))

    def test_during_an_outage_admission_uses_every_link(self):
        # an empty fabric: s1 wins the tie only if la>s1 is not excluded
        self._all_report(down={('la', 's1')})
        self._at(20)
        self.assertEqual(['la', 's1', 'lb'], self._reserve()['via_hosts'])

    def test_no_link_state_at_all_changes_nothing(self):
        path = self._reserve()
        self.assertEqual([], link_state_db.get_link_states(self.admin_ctx))
        with mock.patch.object(plugin.LOG, 'error') as error:
            self._pass()
        error.assert_not_called()
        self.assertEqual(['la', 's1', 'lb'], self._show(
            path['id'], as_admin=True)['via_hosts'])
