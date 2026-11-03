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

"""The harness of the TE re-optimisation, its options and its worker.

What a pass does with VM pairs is in test_te_reopt_uf. The two-spine
fabric of test_te (node1 - la - s1|s2 - lb - node2; spine
links 100 Mbit/s, host links 1000; capacity used as is, so te_reopt_high
0.72 is 72 Mbit/s on a spine link). Measurements are posted through the
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


class TestOptions(ReoptTestCase):

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

    def test_off_with_interval_zero_steering_off_or_no_topology(self):
        self._conf(te_reopt_interval=0)
        with mock.patch.object(plugin.neutron_worker, 'PeriodicWorker'):
            self.assertEqual(1, len(self.srv6.get_workers()))
        self._conf(te_reopt_interval=10, te_auto_steering=False)
        with mock.patch.object(plugin.neutron_worker, 'PeriodicWorker'):
            self.assertEqual(1, len(self.srv6.get_workers()))
        self._conf(te_auto_steering=True)
        self.srv6.capacity = {}
        with mock.patch.object(plugin.neutron_worker, 'PeriodicWorker'):
            self.assertEqual(1, len(self.srv6.get_workers()))

    def test_a_failing_pass_does_not_raise(self):
        with mock.patch.object(self.srv6, '_load_series',
                               side_effect=RuntimeError('db')), \
                mock.patch.object(te_reopt.LOG, 'exception') as exc:
            self.reopt.check()
        exc.assert_called_once()
