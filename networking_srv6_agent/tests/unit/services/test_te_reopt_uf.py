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

"""PLAN-UNRESERVED-STEERING steps 6-7: the re-optimisation steers VM pairs.

The harness of test_te_reopt: node1 - la - {s1, s2} - lb - node2, 100 Mbit/s
spine links, la's link state posted every 5 s, the server clock pinned.
The VM pairs the fabric would report (srv6_aggregate_loads) are given
directly as the plugin's aggregate view: its own tests are in
test_aggregate_load. The ports are real, for the auto paths' foreign keys.
"""

from unittest import mock

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.db import te_db
from networking_srv6_agent.services import pce
from networking_srv6_agent.services import te_reopt
from networking_srv6_agent.tests.unit.services import test_te_reopt

S1, S2 = test_te_reopt.S1, test_te_reopt.S2
VIA_S2 = ['la', 's2', 'lb']


class SteerTestCase(test_te_reopt.ReoptTestCase):

    def setUp(self):
        super().setUp()
        self.pairs = []
        view = mock.patch.object(self.srv6, '_aggregate_view',
                                 side_effect=lambda ctx: list(self.pairs))
        view.start()
        self.addCleanup(view.stop)
        self.vms = [self._vm_port()['id'] for _ in range(4)]

    def _pair(self, src, dst, mbps, links=(S1,), src_host='node1',
              dst_host='node2'):
        """The entries of one pair: on each link, and on lb>node2 (all)."""
        for link in list(links) + [('lb', dst_host)]:
            self.pairs.append({
                'from_node': link[0], 'to_node': link[1],
                'mbps': float(mbps) / (len(links) if link[1] != dst_host
                                       else 1),
                'domain_id': self.domain['id'],
                'source_port_id': self.vms[src],
                'destination_port_id': self.vms[dst],
                'source_host': src_host, 'destination_host': dst_host,
                'steered': False})

    def _autos(self):
        return te_db.get_all_te_paths(self.admin_ctx,
                                      {'origin': [te_def.ORIGIN_AUTO]})

    def _congest_s1(self, mbps=95, also=None):
        loads = {S1: mbps}
        loads.update(also or {})
        self._samples(3, loads)


class TestSteer(SteerTestCase):

    def test_the_largest_pair_gets_an_auto_path_off_the_hot_spine(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.agent_rpc.reset_mock()
        with mock.patch.object(te_reopt.LOG, 'warning') as warn:
            self.reopt.check()
        [auto] = self._autos()
        self.assertEqual((self.vms[0], self.vms[1], VIA_S2),
                         (auto[te_def.SOURCE_PORT_ID],
                          auto[te_def.DESTINATION_PORT_ID],
                          auto[te_def.VIA_HOSTS]))
        self.assertIn('automatic path', warn.call_args[0][0] %
                      warn.call_args[0][1])
        self.assertTrue(self.agent_rpc.method_calls)        # pushed

    def test_one_move_is_enough_when_it_clears_the_link(self):
        # 95 - 50 = 45 <= 70: the second pair stays on ECMP
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))

    def test_until_below_low_within_the_budget(self):
        # limit 60: one move leaves 78, a second (s2 then holds 20 + 20 =
        # 40 <= 60, the first counted as pending) leaves 58: stop at two
        self._conf(te_reopt_max_moves=3, te_reopt_low=0.6)
        self._pair(0, 1, 20)
        self._pair(2, 3, 20)
        self._pair(1, 2, 20)
        self._congest_s1(98)
        self.reopt.check()
        self.assertEqual(2, len(self._autos()))

    def test_a_link_with_one_pair_only_is_left(self):
        self._pair(0, 1, 95)
        self._congest_s1()
        with mock.patch.object(te_reopt.LOG, 'info') as info:
            self.reopt.check()
        self.assertEqual([], self._autos())
        self.assertIn('one VM pair only', info.call_args_list[0][0][0])

    def test_mice_stay_on_ecmp(self):
        self._pair(0, 1, 9)              # < 0.1 x 100
        self._pair(2, 3, 9)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_the_floor_is_read_from_config(self):
        self._conf(te_auto_min_share=0.05)
        self._pair(0, 1, 9)
        self._pair(2, 3, 9)
        self._congest_s1()
        self.reopt.check()
        # both qualify; 95 - 9 = 86 is still above 70, so both move
        self.assertEqual(2, len(self._autos()))

    def test_a_pair_a_user_path_covers_is_never_steered(self):
        self._path(destination_host='node2', via_hosts=['node1'])
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_the_what_if_check_keeps_the_target_below_low(self):
        # s2 holds 40: 40 + 50 > 70, so pair 0>1 cannot go; 40 + 25 <= 70
        self._pair(0, 1, 50)
        self._pair(2, 3, 25)
        self._congest_s1(also={S2: 40})
        self.reopt.check()
        [auto] = self._autos()
        self.assertEqual(self.vms[2], auto[te_def.SOURCE_PORT_ID])

    def test_te_auto_fit_1_drops_the_margin(self):
        # same loads: 40 + 50 <= 100, so the largest pair moves after all
        self._conf(te_auto_fit=1.0)
        self._pair(0, 1, 50)
        self._pair(2, 3, 25)
        self._congest_s1(also={S2: 40})
        self.reopt.check()
        [auto] = self._autos()
        self.assertEqual(self.vms[0], auto[te_def.SOURCE_PORT_ID])

    def test_no_room_anywhere_nothing_moves(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1(also={S2: 69})
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_off_when_disabled(self):
        self._conf(te_auto_steering=False)
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_the_caps(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._conf(te_auto_max_paths=0)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual([], self._autos())
        self._conf(te_auto_max_paths=64, te_auto_max_rules_per_host=0)
        self._congest_s1()
        self.reopt.check()
        self.assertEqual([], self._autos())


class TestTrigger(SteerTestCase):
    """When a link counts as congested (te_reopt_high, te_reopt_samples)."""

    def setUp(self):
        super().setUp()
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)

    def test_two_samples_are_not_enough(self):
        self._samples(2, {S1: 95})
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_one_sample_below_breaks_the_run(self):
        for mbps in (95, 50, 95, 95):
            self._sample({S1: mbps})
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_at_or_below_high_never_moves(self):
        self._samples(5, {S1: 90})                       # not above 90
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_stale_samples_are_not_congestion(self):
        self._samples(3, {S1: 95})
        self._at(self.t + 16)                            # la went silent
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_high_is_read_from_config(self):
        self._conf(te_reopt_high=0.96)
        self._samples(3, {S1: 95})
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_samples_is_read_from_config(self):
        self._conf(te_reopt_samples=2)
        self._samples(2, {S1: 95})
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))

    def test_a_congested_link_with_no_pair_is_logged(self):
        self.pairs = []
        self._samples(3, {S1: 95})
        with mock.patch.object(te_reopt.LOG, 'info') as info:
            self.reopt.check()
        self.assertEqual([], self._autos())
        self.assertIn('no VM pair to steer', info.call_args[0][0])

    def test_an_outage_of_the_collection_steers_nothing(self):
        self._samples(3, {S1: 95})
        self._at(self.t + 16)                   # every reporter silent
        with mock.patch.object(te_reopt.LOG, 'error') as error:
            self.reopt.check()
        self.assertIn('outage', error.call_args[0][0])
        self.assertEqual([], self._autos())


class TestHoldDownAndMoves(SteerTestCase):

    def test_a_steered_pair_is_not_moved_again_in_the_hold_down(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        [first] = self._autos()
        # s2 now hot, the pair still reported there (as it would be)
        self.pairs = []
        self._pair(0, 1, 50, links=(S2,))
        self._pair(2, 3, 40, links=(S2,))
        with mock.patch.object(self.srv6, '_place_auto_path') as place:
            self._samples(3, {S2: 95})
            self.reopt.check()
        for call in place.call_args_list:
            self.assertNotEqual(self.vms[0],
                                call[0][1]['source_port_id'])
        self.assertEqual(first['id'], self._autos()[0]['id'])

    def test_after_the_hold_down_an_auto_path_moves_in_place(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        [first] = self._autos()
        self.pairs = []
        self._pair(0, 1, 50, links=(S2,))
        self._pair(2, 3, 40, links=(S2,))
        self.t += 60
        self._samples(3, {S2: 95})
        self.reopt.check()
        [moved] = self._autos()
        self.assertEqual(first['id'], moved['id'])
        self.assertEqual(test_te_reopt.VIA_S1, moved[te_def.VIA_HOSTS])


class TestRelease(SteerTestCase):

    def _steered(self):
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        self.reopt.check()
        [auto] = self._autos()
        return auto

    def test_an_idle_pair_is_released_after_the_timer(self):
        self._steered()
        self.pairs = []                                 # it stopped
        self.t += 61
        self._samples(1, {})
        self.reopt.check()                              # idle from now
        self.assertEqual(1, len(self._autos()))
        self.t += 110                                   # + 5 per sample
        self._samples(1, {})
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))         # 115 s: not yet
        self._samples(1, {})                            # 120 s
        with mock.patch.object(te_reopt.LOG, 'warning') as warn:
            self.reopt.check()
        self.assertEqual([], self._autos())
        self.assertIn('idle', warn.call_args[0][1]['why'])

    def test_room_again_releases_after_the_hold_down(self):
        self._steered()
        self.pairs = []
        self._pair(0, 1, 30, links=(S2,))               # still sending
        self._samples(1, {S2: 30})
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))         # hold-down
        self.t += 60
        self._samples(1, {S2: 30})
        self.reopt.check()
        self.assertEqual([], self._autos())

    def test_kept_while_a_default_link_has_no_room(self):
        self._steered()
        self.pairs = []
        self._pair(0, 1, 30, links=(S2,))
        self.t += 60
        self._samples(1, {S1: 50, S2: 30})              # 50 + 30 > 70
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))

    def test_released_pair_not_re_steered_inside_the_hold_down(self):
        self._steered()
        self.pairs = []
        self._pair(0, 1, 30, links=(S2,))
        self.t += 60
        self._samples(1, {S2: 30})
        self.reopt.check()                              # released
        self.pairs = []
        self._pair(0, 1, 50)
        self._pair(2, 3, 40)
        self._congest_s1()
        with mock.patch.object(self.srv6, '_place_auto_path',
                               return_value=None) as place:
            self.reopt.check()
        self.assertNotIn(self.vms[0], [c[0][1]['source_port_id']
                                       for c in place.call_args_list])


class TestShortestLinks(test_te_reopt.ReoptTestCase):

    def test_both_spines_on_a_two_tier_clos(self):
        cap = dict(self.srv6.capacity)
        self.assertEqual(
            {('node1', 'la'), ('la', 's1'), ('la', 's2'), ('s1', 'lb'),
             ('s2', 'lb'), ('lb', 'node2')},
            pce.shortest_links(cap, 'node1', 'node2'))

    def test_unreachable_or_itself_is_empty(self):
        cap = {('a', 'b'): 1, ('b', 'a'): 1}
        self.assertEqual(set(), pce.shortest_links(cap, 'a', 'c'))
        self.assertEqual(set(), pce.shortest_links(cap, 'a', 'a'))


class TestNoPileUp(SteerTestCase):
    """Eval 2026-11-02 (c4-on-2): three pairs piled onto spine s2.

    Each what-if read the fabric's measurements, which did not show the
    pairs placed a moment before, in the same pass or the previous one.
    """

    def test_two_pairs_do_not_both_go_onto_the_idle_spine(self):
        # s1 at 98 with 40 + 40 + 18; low 0.5: after one move s1 is at 58,
        # still above 50, but s2 now holds 40 and 40 + 40 > 50
        self._conf(te_reopt_low=0.5)
        self._pair(0, 1, 40)
        self._pair(2, 3, 40)
        self._pair(1, 2, 18)
        self._congest_s1(98)
        self.reopt.check()
        autos = self._autos()
        self.assertEqual(1, len([a for a in autos
                                 if a[te_def.VIA_HOSTS] == VIA_S2 and
                                 a[te_def.SOURCE_PORT_ID] in
                                 (self.vms[0], self.vms[2])]))

    def test_the_next_pass_still_counts_it(self):
        self._conf(te_reopt_low=0.5, te_reopt_max_moves=1)
        self._pair(0, 1, 40)
        self._pair(2, 3, 40)
        self._congest_s1(98)
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))
        # 10 s later, the fabric has not posted anything new about s2
        self.t += 10
        self._at(self.t)
        self.reopt.check()
        self.assertEqual(1, len(self._autos()))

    def test_forgotten_once_the_window_has_seen_it(self):
        self._conf(te_reopt_low=0.5, te_reopt_max_moves=1)
        self._pair(0, 1, 40)
        self._pair(2, 3, 40)
        self._congest_s1(98)
        self.reopt.check()
        [placed] = self.reopt._pending
        self.t += 31
        self._samples(3, {S1: 98})
        self.reopt.check()
        self.assertNotIn(placed, self.reopt._pending)
