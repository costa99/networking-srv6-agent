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

"""PLAN-LINK-STATE-TE step 2: /v2.0/srv6_link_states, end to end.

Neutron's own REST harness with a real database and the policy from the
entry point, on a two-link topology: spine s1 - leaf l1 - host ml1. The
server's clock (timeutils.utcnow) is pinned with a mock, so ages, staleness and
the peak window are exact.
"""

import datetime
from unittest import mock

from networking_srv6_agent.common import config
from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_plugin


LS = 'srv6_link_states'
T0 = datetime.datetime(2026, 11, 1, 12, 0, 0)
CAPACITY = {('s1', 'l1'): 100, ('l1', 's1'): 100,
            ('l1', 'ml1'): 1000, ('ml1', 'l1'): 1000}


def _m(reporter, a, b, mbps=10.0, up=True, drops=0):
    return {'reporter': reporter, 'from_node': a, 'to_node': b, 'up': up,
            'mbps': mbps, 'drops': drops}


class LinkStateRestTestCase(test_plugin.Srv6RestTestCase):

    def setUp(self):
        super().setUp()
        self.srv6.capacity = dict(CAPACITY)
        self.now = T0
        clock = mock.patch('oslo_utils.timeutils.utcnow',
                           side_effect=lambda *a, **kw: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def _post(self, items, expected=201, as_admin=True):
        req = self.new_create_request(LS, {LS: items}, as_admin=as_admin)
        return self._check(req.get_response(self.ext_api), expected)

    def _list(self, as_admin=True, **params):
        query = '&'.join('%s=%s' % kv for kv in params.items()) or None
        req = self.new_list_request(LS, as_admin=as_admin, params=query)
        return {(e['from_node'], e['to_node']): e
                for e in self._check(req.get_response(self.ext_api), 200)[LS]}

    def _show(self, link_id, expected=200):
        req = self.new_show_request(LS, link_id, as_admin=True)
        return self._check(req.get_response(self.ext_api), expected)

    def _at(self, seconds):
        self.now = T0 + datetime.timedelta(seconds=seconds)

    def _l1_batch(self, tx=50.0, rx=100.0, up=True):
        """What leaf l1's agent posts about its link to s1: both ways."""
        return [_m('l1', 'l1', 's1', tx, up), _m('l1', 's1', 'l1', rx, up)]


class TestIngestion(LinkStateRestTestCase):

    def test_a_batch_is_stored_with_the_server_time(self):
        res = self._post(self._l1_batch())[LS]
        self.assertEqual(2, len(res))
        self.assertEqual({'l1:s1', 's1:l1'}, {r['id'] for r in res})
        self.assertEqual({T0.isoformat()}, {r['measured_at'] for r in res})
        rows = link_state_db.get_link_states(self.admin_ctx)
        self.assertEqual([T0, T0], [r.measured_at for r in rows])

    def test_the_time_is_stored_in_whole_seconds(self):
        # MySQL DATETIME rounds a fraction (.7 would be stored as T0 + 1)
        self.now = T0 + datetime.timedelta(seconds=0.7)
        res = self._post(self._l1_batch())[LS]
        self.assertEqual(T0.isoformat(), res[0]['measured_at'])
        self.assertEqual(T0, link_state_db.get_link_states(
            self.admin_ctx)[0].measured_at)

    def test_a_single_measurement_without_the_list(self):
        req = self.new_create_request(
            LS, {'srv6_link_state': _m('l1', 'l1', 'ml1')}, as_admin=True)
        self.assertEqual('l1:ml1', self._check(
            req.get_response(self.ext_api), 201)['srv6_link_state']['id'])

    def test_admin_only(self):
        self._post(self._l1_batch(), expected=403, as_admin=False)
        self._post(self._l1_batch())
        self.assertEqual({}, self._list(as_admin=False))

    def test_an_unknown_link_is_skipped_and_the_rest_stored(self):
        # a link map one line ahead of topology_file must not silence the
        # node: a silent node reads as dead (L8)
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            res = self._post(self._l1_batch() + [_m('l1', 'l1', 's2')])[LS]
            self._post(self._l1_batch() + [_m('l1', 'l1', 's2')])
        self.assertEqual({'l1:s1', 's1:l1'}, {r['id'] for r in res})
        self.assertEqual(4, len(link_state_db.get_link_states(
            self.admin_ctx)))
        # once per reporter and link, not once per batch
        self.assertEqual(1, warn.call_count)
        self.assertIn('l1>s2', warn.call_args[0][1]['l'])

    def test_a_batch_of_unknown_links_only_is_refused(self):
        res = self._post([_m('l1', 'l1', 's2'), _m('l1', 's2', 'l1')], 400)
        self.assertIn('l1>s2, s2>l1: not a link',
                      res['NeutronError']['message'])
        self.assertEqual([], link_state_db.get_link_states(self.admin_ctx))

    def test_the_reporter_must_be_an_end_of_the_link(self):
        res = self._post([_m('ml1', 'l1', 's1')], 400)
        self.assertIn('not an end of l1>s1', res['NeutronError']['message'])

    def test_a_link_twice_from_one_reporter_is_refused(self):
        self._post([_m('l1', 'l1', 's1'), _m('l1', 'l1', 's1', 20.0)], 400)

    def test_both_ends_may_report_the_same_link(self):
        self._post([_m('l1', 'l1', 's1'), _m('s1', 'l1', 's1')])

    def test_values_are_null_or_non_negative_numbers(self):
        for bad in ({'mbps': -1}, {'mbps': 'fast'}, {'mbps': True},
                    {'mbps': 1e7}, {'drops': 1.5}, {'drops': -2}):
            self._post([dict(_m('l1', 'l1', 's1'), **bad)], 400)
        self._post([dict(_m('l1', 'l1', 's1'), mbps=None, drops=None)])
        self._post([dict(_m('l1', 'l1', 'ml1'), mbps=0)])

    def test_unknown_attributes_are_refused(self):
        # the reporter's own clock is never stored (L8)
        self._post([dict(_m('l1', 'l1', 's1'), t=1793570523.3)], 400)

    def test_no_topology_no_link_state(self):
        self.srv6.capacity = {}
        res = self._post(self._l1_batch(), 400)
        self.assertIn('topology_file', res['NeutronError']['message'])

    def test_a_batch_prunes_only_its_own_reporters_old_rows(self):
        self._post(self._l1_batch())
        self._post([_m('s1', 's1', 'l1'), _m('s1', 'l1', 's1')])
        self._at(61)                    # beyond 2 x link_state_window
        self._post(self._l1_batch())
        rows = link_state_db.get_link_states(self.admin_ctx)
        self.assertEqual(
            sorted([('l1', 61), ('l1', 61), ('s1', 0), ('s1', 0)]),
            sorted((r.reporter, (r.measured_at - T0).seconds)
                   for r in rows))

    def test_rows_inside_two_windows_are_kept(self):
        self._post(self._l1_batch())
        self._at(59)
        self._post(self._l1_batch())
        self.assertEqual(4, len(link_state_db.get_link_states(
            self.admin_ctx)))


class TestView(LinkStateRestTestCase):

    def test_the_sender_gives_the_load_the_receiver_what_got_through(self):
        self._post(self._l1_batch(tx=150.0, rx=60.0))
        self._post([_m('s1', 'l1', 's1', 100.0), _m('s1', 's1', 'l1', 61.0)])
        view = self._list()
        up = view['l1', 's1']           # l1 sent 150, s1 received 100
        self.assertEqual((150.0, 100.0, 'l1'),
                         (up['mbps'], up['delivered_mbps'], up['reporter']))
        down = view['s1', 'l1']         # s1 sent 61, l1 received 60
        self.assertEqual((61.0, 60.0, 's1'),
                         (down['mbps'], down['delivered_mbps'],
                          down['reporter']))
        self.assertEqual((0, False, True), (up['age'], up['stale'], up['up']))

    def test_a_history_read_once_gives_the_same_view_and_series(self):
        # PLAN-C4-OPTIMISATION O1: the re-optimisation passes one history
        # to every caller instead of each reading its own
        self._post(self._l1_batch(tx=150.0, rx=60.0))
        self._post([_m('s1', 'l1', 's1', 100.0), _m('s1', 's1', 'l1', 61.0)])
        self._at(5)
        self._post(self._l1_batch(tx=120.0, rx=40.0))
        hist = self.srv6._history(self.admin_ctx)
        self.assertEqual(self.srv6._link_state_view(self.admin_ctx),
                         self.srv6._link_state_view(self.admin_ctx, hist))
        self.assertEqual(self.srv6._load_series(self.admin_ctx),
                         self.srv6._load_series(self.admin_ctx, hist))
        series = self.srv6._load_series(self.admin_ctx, hist)
        self.assertEqual([150.0, 120.0], [m for _t, m in series[('l1', 's1')]])

    def test_a_link_one_end_reports_has_that_end_only(self):
        self._post([_m('l1', 'ml1', 'l1', 30.0)])     # the host runs no agent
        entry = self._list()['ml1', 'l1']
        self.assertEqual((30.0, 30.0, 'l1'),
                         (entry['mbps'], entry['delivered_mbps'],
                          entry['reporter']))

    def test_stale_after_link_state_stale_after(self):
        self._post(self._l1_batch())
        self._at(15)
        self.assertFalse(self._list()['l1', 's1']['stale'])
        self._at(16)
        entry = self._list()['l1', 's1']
        self.assertEqual((True, 16, 50.0),
                         (entry['stale'], entry['age'], entry['mbps']))
        self.assertIsNone(entry['delivered_mbps'])

    def test_the_stale_after_option_is_read(self):
        self.config(link_state_stale_after=5,
                    group=config.SRV6_SERVER_GROUP)
        self._post(self._l1_batch())
        self._at(6)
        self.assertTrue(self._list()['l1', 's1']['stale'])

    def test_a_silent_sender_falls_back_to_the_receiver(self):
        self._post(self._l1_batch(tx=50.0))
        self._at(20)
        self._post([_m('s1', 'l1', 's1', 45.0)])
        entry = self._list()['l1', 's1']
        self.assertEqual((45.0, 's1', False, 0),
                         (entry['mbps'], entry['reporter'], entry['stale'],
                          entry['age']))

    def test_the_peak_is_the_maximum_of_the_window(self):
        # the series of the G5 discussion: 90, then 80 x 5, then 20
        for t, mbps in ((0, 90.0), (5, 80.0), (10, 80.0), (15, 80.0),
                        (20, 80.0), (25, 80.0), (30, 20.0)):
            self._at(t)
            self._post([_m('l1', 'l1', 's1', mbps)])
        entry = self._list()['l1', 's1']
        self.assertEqual((20.0, 80.0), (entry['mbps'], entry['peak_mbps']))
        self._at(55)
        self._post([_m('l1', 'l1', 's1', 10.0)])
        self.assertEqual(20.0, self._list()['l1', 's1']['peak_mbps'])

    def test_null_rates_do_not_hide_the_peak(self):
        self._post([_m('l1', 'l1', 's1', 70.0)])
        self._at(5)
        self._post([_m('l1', 'l1', 's1', None)])
        entry = self._list()['l1', 's1']
        self.assertEqual((None, 70.0), (entry['mbps'], entry['peak_mbps']))

    def test_down_if_either_fresh_end_says_so(self):
        self._post(self._l1_batch())
        self._post([_m('s1', 'l1', 's1', 0.0, up=False)])
        self.assertFalse(self._list()['l1', 's1']['up'])
        # once that report is stale, only l1's fresh "up" counts
        self._at(10)
        self._post(self._l1_batch())
        self._at(20)
        self.assertTrue(self._list()['l1', 's1']['up'])

    def test_show_and_filters(self):
        self._post(self._l1_batch() + [_m('l1', 'l1', 'ml1')])
        self.assertEqual(
            'l1', self._show('l1:s1')['srv6_link_state']['from_node'])
        self._show('l1:s2', expected=404)
        self._show('nonsense', expected=404)
        self.assertEqual({('l1', 's1'), ('l1', 'ml1')},
                         set(self._list(from_node='l1')))
        # every topology link is listed, reported or not
        self.assertEqual({('s1', 'l1'), ('ml1', 'l1')},
                         set(self._list(to_node='l1')))

    def test_an_unreported_link_is_listed_stale(self):
        self._post(self._l1_batch())
        view = self._list()
        self.assertEqual(4, len(view))
        entry = view['ml1', 'l1']
        self.assertEqual((True, None, None, None, None),
                         (entry['stale'], entry['up'], entry['mbps'],
                          entry['measured_at'], entry['age']))

    def test_capacity_next_to_the_measurement(self):
        view = self._list()
        self.assertEqual(CAPACITY[('l1', 's1')],
                         view['l1', 's1']['capacity_mbps'])
        self.assertNotIn('reserved_mbps', view['l1', 's1'])
