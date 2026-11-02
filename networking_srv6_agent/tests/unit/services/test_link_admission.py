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

"""PLAN-LINK-STATE-TE step 6: admission against max(reserved, measured).

The fixture of test_te_reopt: two spines, 100 Mbit/s each way, la's
measurements posted through the API every 5 s of the mocked clock. With
nothing measured, two equal paths tie and the lexicographically smaller
one (via s1) wins, so "via s2" shows the measurement was counted.
"""

from unittest import mock

from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_te_reopt

S1, S2 = test_te_reopt.S1, test_te_reopt.S2
VIA_S1, VIA_S2 = test_te_reopt.VIA_S1, test_te_reopt.VIA_S2


class TestAdmission(test_te_reopt.ReoptTestCase):

    def _new(self, bandwidth, expected=201):
        return self._reserve(expected, domain=self._domain()['id'],
                             bandwidth=bandwidth)

    def test_without_link_state_the_books_only(self):
        self.assertEqual(VIA_S1, self._new(30)['via_hosts'])

    def test_control_chatter_does_not_break_ties(self):
        # idle links carry ~0.03 Mbit/s of control traffic: counted in
        # whole Mbit/s it is 0, so the two spines still tie
        self._sample({S1: 0.4, S2: 0.02})
        self.assertEqual(VIA_S1, self._new(30)['via_hosts'])

    def test_the_measured_load_counts(self):
        self._sample({S1: 80})                 # s1: 20 free
        self.assertEqual(VIA_S2, self._new(30)['via_hosts'])

    def test_an_idle_reservation_still_counts(self):
        self._sample({})                       # every link measured idle
        self.assertEqual(VIA_S1, self._new(70)['via_hosts'])
        self.assertEqual(VIA_S2, self._new(40)['via_hosts'])
        # s1: 100 - max(70 reserved, 0 measured) = 30, s2: 60
        self.assertEqual(VIA_S2, self._new(31)['via_hosts'])
        self.assertEqual(VIA_S1, self._new(29)['via_hosts'])

    def test_both_spines_measured_full_is_409(self):
        self._sample({S1: 80, S2: 80})
        res = self._new(30, expected=409)
        self.assertIn('30 Mbit/s', self._message(res))
        self.assertEqual(VIA_S1, self._new(20)['via_hosts'])

    def test_the_peak_counts_not_the_latest(self):
        self._sample({S1: 85})
        self._sample({S1: 10})                 # 85 is still in the window
        self.assertEqual(VIA_S2, self._new(30)['via_hosts'])

    def test_the_window_is_read_from_config(self):
        self._conf(link_state_window=5)
        self._sample({S1: 85, S2: 10})
        self._samples(2, {S1: 10, S2: 10})     # 85 left the 5 s window
        self.assertEqual(VIA_S1, self._new(30)['via_hosts'])

    def test_stale_link_state_falls_back_to_the_books_with_a_log(self):
        self._sample({S1: 95})
        self._at(self.t + 16)                  # older than stale_after
        with mock.patch.object(plugin.LOG, 'info') as info:
            path = self._new(30)
        self.assertEqual(VIA_S1, path['via_hosts'])
        self.assertTrue(any('no fresh link state' in c[0][0]
                            for c in info.call_args_list))


class TestFailureReroute(test_te_reopt.ReoptTestCase):
    """A failure moves a reservation by the admission's rule (L4).

    Audit 2026-11-02 finding 1: the failure reroute counted the books
    only, so it moved a reservation onto a link that unreserved traffic
    already filled, where a new request for the same path was refused.
    """

    def test_not_onto_a_link_measured_full(self):
        path = self._reserve(bandwidth=60)
        self.assertEqual(VIA_S1, path['via_hosts'])
        self._sample({S2: 80})                 # unreserved: s2 has 20 free
        self._report('la', down={S1})
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            self._pass()
        # a new 60 Mbit/s request is refused now, so the move is too: L6
        self._reserve(409, domain=self._domain()['id'], bandwidth=60)
        self._show(path['id'], expected=404, as_admin=True)
        self.assertIn('deleted', warn.call_args_list[0][0][0])

    def test_onto_a_loaded_link_that_still_has_room(self):
        path = self._reserve(bandwidth=60)
        self._sample({S2: 30})                 # s2 has 70 free
        self._report('la', down={S1})
        self._pass()
        self.assertEqual(VIA_S2, self._via(path))
