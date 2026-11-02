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

"""PLAN-LINK-STATE-TE step 5: moving reservations off congested links.

The two-spine fabric of test_te (node1 - la - s1|s2 - lb - node2; spine
links 100 Mbit/s, host links 1000; capacity used as is, so te_reopt_high
0.9 is 90 Mbit/s on a spine link). Measurements are posted through the
API as leaf la's agent would, one batch per 5 s of the mocked clock, and
the re-optimiser's pass is run by hand.
"""

from unittest import mock

from networking_srv6_agent.common import config
from networking_srv6_agent.services import plugin
from networking_srv6_agent.services import te_reopt
from networking_srv6_agent.tests.unit.services import test_link_failure

LS = test_link_failure.LS
S1, S2 = ('la', 's1'), ('la', 's2')
VIA_S1, VIA_S2 = ['la', 's1', 'lb'], ['la', 's2', 'lb']


class ReoptTestCase(test_link_failure.LinkFailureTestCase):

    def setUp(self):
        super().setUp()
        self.reopt = te_reopt.TeReoptimiser(self.srv6)
        self.t = 0

    def _conf(self, **kw):
        self.config(group=config.SRV6_SERVER_GROUP, **kw)

    def _sample(self, loads):
        """Post la's next batch, one period later.

        Every link of la's; loads gives Mbit/s for some directed links,
        the others carry 0.
        """
        self.t += 5
        self._at(self.t)
        items = [{'reporter': 'la', 'from_node': a, 'to_node': b, 'up': True,
                  'mbps': float(loads.get((a, b), 0)), 'drops': 0}
                 for a, b in sorted(self.srv6.capacity) if 'la' in (a, b)]
        req = self.new_create_request(LS, {LS: items}, as_admin=True)
        self._check(req.get_response(self.ext_api), 201)

    def _samples(self, n, loads):
        for _ in range(n):
            self._sample(loads)

    def _via(self, path):
        return self._show(path['id'], as_admin=True)['via_hosts']

    def _on_s1(self, *bandwidths):
        """Reservations all placed on s1.

        la>s2 is reported down while they are admitted (admission avoids
        dead links), then up again.
        """
        self._sample({})
        self._report('la', down={S2})
        paths = [self._reserve(bandwidth=bw, domain=self._domain()['id'])
                 for bw in bandwidths]
        self.assertEqual([VIA_S1] * len(paths), [p['via_hosts']
                                                 for p in paths])
        self._sample({})                          # la>s2 up again
        return paths


class TestTrigger(ReoptTestCase):

    def test_a_congested_link_moves_its_reservation(self):
        path = self._reserve(bandwidth=30)
        self.assertEqual(VIA_S1, path['via_hosts'])
        self._samples(3, {S1: 95})
        self.agent_rpc.reset_mock()
        with mock.patch.object(te_reopt.LOG, 'warning') as warn:
            self.reopt.check()
        self.assertEqual(VIA_S2, self._via(path))
        self.assertIn('moved from', warn.call_args[0][0])
        self.assertTrue(self.agent_rpc.method_calls)     # domain pushed

    def test_two_samples_are_not_enough(self):
        path = self._reserve(bandwidth=30)
        self._samples(2, {S1: 95})
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_one_sample_below_breaks_the_run(self):
        path = self._reserve(bandwidth=30)
        for mbps in (95, 50, 95, 95):
            self._sample({S1: mbps})
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_at_or_below_high_never_moves(self):
        path = self._reserve(bandwidth=30)
        self._samples(5, {S1: 90})                       # not above 90
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_stale_samples_are_not_congestion(self):
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95})
        self._at(self.t + 16)                            # la went silent
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_traffic_without_reservations_is_left(self):
        self._samples(3, {S1: 95})
        with mock.patch.object(te_reopt.LOG, 'info') as info, \
                mock.patch.object(self.srv6, '_reroute_reservation') as mv:
            self.reopt.check()
        mv.assert_not_called()
        self.assertIn('no reservation', info.call_args[0][0])


class TestDamping(ReoptTestCase):

    def test_hysteresis_moves_the_fewest_largest_first(self):
        # 95 - 30 = 65 <= 70: one move, the largest, is enough
        big, mid, small = self._on_s1(30, 20, 10)
        self._samples(3, {S1: 95})
        self.reopt.check()
        self.assertEqual([VIA_S2, VIA_S1, VIA_S1],
                         [self._via(p) for p in (big, mid, small)])

    def test_moves_continue_until_below_low(self):
        # 99 - 20 = 79 > 70, - 20 = 59: two moves
        a, b = self._on_s1(20, 20)
        self._samples(3, {S1: 99})
        self.reopt.check()
        self.assertEqual([VIA_S2, VIA_S2], [self._via(p) for p in (a, b)])

    def test_the_budget_per_pass_is_read_from_config(self):
        self._conf(te_reopt_max_moves=1)
        a, b = self._on_s1(20, 20)
        self._samples(3, {S1: 99})
        self.reopt.check()
        self.assertEqual(1, [self._via(p) for p in (a, b)].count(VIA_S2))

    def test_the_link_is_judged_again_only_on_newer_samples(self):
        self._conf(te_reopt_max_moves=1)
        a, b = self._on_s1(20, 20)
        self._samples(3, {S1: 99})
        self.reopt.check()
        self.reopt.check()                 # same samples: no second move
        self.assertEqual(1, [self._via(p) for p in (a, b)].count(VIA_S2))
        self._samples(3, {S1: 99})         # still hot after the move
        self.reopt.check()
        self.assertEqual([VIA_S2, VIA_S2], [self._via(p) for p in (a, b)])

    def test_hold_down_then_it_may_move_again(self):
        self._conf(te_reopt_hold_down=30)
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95})
        self.reopt.check()
        self.assertEqual(VIA_S2, self._via(path))
        self._samples(3, {S2: 95})             # now s2 is hot, s1 is free
        self.reopt.check()                     # t = 30 - 15 s since the move
        self.assertEqual(VIA_S2, self._via(path))
        self._samples(3, {S2: 95})             # 30 s after the move
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_the_memory_of_moves_forgets_what_the_hold_down_no_longer_holds(
            self):
        # audit 2026-11-02 finding 6: _moved kept every reservation ever
        # moved, deleted ones included
        self._conf(te_reopt_hold_down=30)
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95})
        self.reopt.check()
        self.assertEqual({path['id']}, set(self.reopt._moved))
        self._delete_path(path['id'])
        self._samples(6, {S2: 95})             # a pass 30 s after the move
        self.reopt.check()
        self.assertEqual({}, self.reopt._moved)

    def test_oscillating_load_does_not_make_it_flap(self):
        # The hot spine flips every 15 s for 3 minutes. The reservation
        # follows the first change, then never again: the hold-down (60 s)
        # stops it for a minute, and after that the other spine was hot
        # within link_state_window (30 s), so its peak leaves no room.
        path = self._reserve(bandwidth=30)
        moves, where = 0, self._via(path)
        for cycle in range(12):
            self._samples(3, {S1 if cycle % 2 == 0 else S2: 95})
            self.reopt.check()
            now = self._via(path)
            moves += now != where
            where = now
        self.assertEqual(1, moves)


class TestRoom(ReoptTestCase):

    def test_no_room_on_the_other_spine_it_stays(self):
        # s2: max(0 reserved, 75 measured) leaves 25 < 30
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95, S2: 75})
        with mock.patch.object(te_reopt.LOG, 'info') as info:
            self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))
        self.assertIn('no path with room', info.call_args[0][0])

    def test_room_is_counted_with_the_measured_load(self):
        # s2 measures 60: 100 - 60 = 40 >= 30
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95, S2: 60})
        self.reopt.check()
        self.assertEqual(VIA_S2, self._via(path))

    def test_room_is_counted_with_the_reservations(self):
        # s2 idle but holding a 75 reservation: 25 < 30
        held = self._reserve(bandwidth=75)
        self.assertEqual(VIA_S1, held['via_hosts'])
        path = self._reserve(bandwidth=20, domain=self._domain()['id'])
        self.assertEqual(VIA_S2, path['via_hosts'])
        mover = self._reserve(bandwidth=30, domain=self._domain()['id'])
        self.assertEqual(VIA_S2, mover['via_hosts'])   # 25 left on s1
        self._samples(3, {S2: 95})
        self.reopt.check()
        # largest first: 30 does not fit on s1 (100 - 75 held = 25), the 20
        # does and moves
        self.assertEqual(VIA_S2, self._via(mover))
        self.assertEqual(VIA_S1, self._via(path))

    def test_own_traffic_is_subtracted_on_shared_links(self):
        # L10: the host uplink node1>la carries 950, 30 of it the mover's
        # own. Without subtracting it: 1000 - 950 = 50 >= 30 fits anyway,
        # so use 980: 20 < 30 without, 1000 - 950 = 50 with.
        self._conf(te_reopt_high=0.99)
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 120, ('node1', 'la'): 980})
        self.reopt.check()
        self.assertEqual(VIA_S2, self._via(path))


class TestOptions(ReoptTestCase):

    def test_high_is_read_from_config(self):
        self._conf(te_reopt_high=0.96)
        path = self._reserve(bandwidth=30)
        self._samples(3, {S1: 95})
        self.reopt.check()
        self.assertEqual(VIA_S1, self._via(path))

    def test_samples_is_read_from_config(self):
        self._conf(te_reopt_samples=2)
        path = self._reserve(bandwidth=30)
        self._samples(2, {S1: 95})
        self.reopt.check()
        self.assertEqual(VIA_S2, self._via(path))

    def test_low_is_read_from_config(self):
        # low 0.8: 99 - 20 = 79 <= 80 after one move
        self._conf(te_reopt_low=0.8)
        a, b = self._on_s1(20, 20)
        self._samples(3, {S1: 99})
        self.reopt.check()
        self.assertEqual(1, [self._via(p) for p in (a, b)].count(VIA_S2))

    def test_low_must_be_below_high(self):
        self._conf(te_reopt_low=0.9, te_reopt_high=0.9)
        self.assertRaises(plugin.n_exc.InvalidConfigurationOption,
                          plugin.Srv6Plugin)


class TestWorkers(ReoptTestCase):

    def test_one_worker_each(self):
        with mock.patch.object(plugin.neutron_worker,
                               'PeriodicWorker') as worker:
            workers = self.srv6.get_workers()
        self.assertEqual(2, len(workers))
        names = [c[1]['desc'] for c in worker.call_args_list]
        self.assertEqual(['srv6-te-liveness', 'srv6-te-reopt'], names)
        self.assertEqual(10, worker.call_args_list[1][0][1])

    def test_off_with_interval_zero_or_without_topology(self):
        self._conf(te_reopt_interval=0)
        with mock.patch.object(plugin.neutron_worker, 'PeriodicWorker'):
            self.assertEqual(1, len(self.srv6.get_workers()))
        self._conf(te_reopt_interval=10)
        self.srv6.capacity = {}
        with mock.patch.object(plugin.neutron_worker, 'PeriodicWorker'):
            self.assertEqual(1, len(self.srv6.get_workers()))

    def test_a_failing_pass_does_not_raise(self):
        with mock.patch.object(self.srv6, '_load_series',
                               side_effect=RuntimeError('db')), \
                mock.patch.object(te_reopt.LOG, 'exception') as exc:
            self.reopt.check()
        exc.assert_called_once()
