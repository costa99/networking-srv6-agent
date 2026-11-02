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

"""PLAN-UNRESERVED-STEERING step 2: /v2.0/srv6_aggregate_loads.

Three layers: the REST resource end to end (Neutron's harness, a real
database, the policy from the entry point, the server clock pinned); the
resolver from (SID, inner addresses) to domain, ports and hosts, alone;
and the plugin's address map of a domain, with a mocked core plugin.
"""

import datetime
import ipaddress
from unittest import mock

from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const
from oslo_utils import uuidutils
import testtools

from networking_srv6_agent.common import config
from networking_srv6_agent.db import aggregate_load_db
from networking_srv6_agent.services import aggregates
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_plugin


AG = 'srv6_aggregate_loads'
T0 = datetime.datetime(2026, 11, 2, 12, 0, 0)
CAPACITY = {('s2', 'l3'): 80, ('l3', 's2'): 80,
            ('l1', 's2'): 80, ('s2', 'l1'): 80}
# the GATE A run: pa1 -> pb3 steered via s2, pa1 -> pb4 direct
DECAP3 = 'fc00:0:3:e300::'
PA1, PB3, PB4 = '10.91.1.11', '10.91.2.13', '10.91.2.14'


def _m(a='l1', b='s2', sid=DECAP3, src=PA1, dst=PB3, mbps=20.0,
       reporter=None):
    return {'reporter': reporter or a, 'from_node': a, 'to_node': b,
            'sid': sid, 'source_address': src, 'destination_address': dst,
            'mbps': mbps}


class AggregateRestTestCase(test_plugin.Srv6RestTestCase):

    def setUp(self):
        super().setUp()
        self.srv6.capacity = dict(CAPACITY)
        self.now = T0
        clock = mock.patch('oslo_utils.timeutils.utcnow',
                           side_effect=lambda *a, **kw: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def _post(self, items, expected=201, as_admin=True):
        req = self.new_create_request(AG, {AG: items}, as_admin=as_admin)
        return self._check(req.get_response(self.ext_api), expected)

    def _list(self, as_admin=True, **params):
        query = '&'.join('%s=%s' % kv for kv in params.items()) or None
        req = self.new_list_request(AG, as_admin=as_admin, params=query)
        return self._check(req.get_response(self.ext_api), 200)[AG]

    def _show(self, entry_id, expected=200):
        req = self.new_show_request(AG, entry_id, as_admin=True)
        return self._check(req.get_response(self.ext_api), expected)

    def _at(self, seconds):
        self.now = T0 + datetime.timedelta(seconds=seconds)

    def _rows(self):
        return aggregate_load_db.get_aggregate_loads(
            self.admin_ctx, datetime.datetime(2000, 1, 1))


class TestIngestion(AggregateRestTestCase):

    def test_a_batch_is_stored_normalised_with_the_server_time(self):
        res = self._post([_m(sid='FC00:0:3:E300:0:0:0:0'),
                          _m(dst=PB4, sid='fc00:0:4:e300::', mbps=10)])[AG]
        self.assertEqual(2, len(res))
        self.assertEqual(DECAP3, res[0]['sid'])
        self.assertEqual(T0.isoformat(), res[0]['measured_at'])
        rows = self._rows()
        self.assertEqual({(DECAP3, PB3, 20.0), ('fc00:0:4:e300::', PB4, 10.0)},
                         {(r['sid'], r['destination_address'], r['mbps'])
                          for r in rows})
        self.assertEqual({T0}, {r['measured_at'] for r in rows})

    def test_inner_ipv6_is_normalised_too(self):
        res = self._post([_m(src='2001:DB8:A:0::10', dst='2001:db8:b::5')])
        self.assertEqual('2001:db8:a::10', res[AG][0]['source_address'])

    def test_a_single_measurement_without_the_list(self):
        req = self.new_create_request(AG, {'srv6_aggregate_load': _m()},
                                      as_admin=True)
        res = self._check(req.get_response(self.ext_api), 201)
        self.assertEqual(PB3, res['srv6_aggregate_load'][
            'destination_address'])

    def test_admin_only(self):
        self._post([_m()], expected=403, as_admin=False)
        self._post([_m()])
        self.assertEqual([], self._list(as_admin=False))

    def test_the_reporter_must_be_the_sending_end(self):
        self._post([_m(reporter='s2')], expected=400)

    def test_an_unknown_link_is_skipped_with_one_warning(self):
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            self._post([_m(), _m(a='l1', b='s1')])
            self._post([_m(), _m(a='l1', b='s1')])
        self.assertEqual(1, warn.call_count)
        self.assertEqual(2, len(self._rows()))

    def test_a_batch_of_unknown_links_only_is_refused(self):
        self._post([_m(a='l1', b='s1')], expected=400)

    def test_no_topology_no_aggregate_loads(self):
        self.srv6.capacity = {}
        self._post([_m()], expected=400)

    def test_bad_values_are_refused(self):
        for bad in (_m(sid='10.0.0.1'), _m(sid='nonsense'),
                    _m(src='not-an-ip'), _m(dst='2001:db8::1'),
                    _m(mbps=-1), _m(mbps=True), _m(mbps='5'), _m(mbps=1e7),
                    _m(mbps=None)):
            self._post([bad], expected=400)
        self.assertEqual([], self._rows())

    def test_a_key_twice_is_refused(self):
        self._post([_m(), _m(sid='FC00:0:3:E300::')], expected=400)

    def test_at_most_max_pairs_per_link(self):
        pairs = [_m(src=str(ipaddress.ip_address('10.0.0.1') + i))
                 for i in range(aggregates.MAX_PAIRS_PER_LINK + 1)]
        self._post(pairs[:-1])
        self._post(pairs, expected=400)

    def test_unknown_attributes_are_refused(self):
        self._post([dict(_m(), flow_label=1)], expected=400)

    def test_a_batch_prunes_only_its_own_reporters_old_rows(self):
        self._post([_m()])                                  # l1 at 0
        self._post([_m(a='s2', b='l3')])                    # s2 at 0
        self._at(2 * 30 + 5)                                # two windows on
        self._post([_m(mbps=1)])                            # l1 prunes l1
        self.assertEqual(
            [('l1', T0 + datetime.timedelta(seconds=65)), ('s2', T0)],
            sorted((r['reporter'], r['measured_at']) for r in self._rows()))


class TestView(AggregateRestTestCase):

    def test_latest_rate_and_peak_of_the_window(self):
        for t, mbps in ((0, 30.0), (5, 50.0), (10, 20.0)):
            self._at(t)
            self._post([_m(mbps=mbps)])
        self._at(12)
        [entry] = self._list()
        self.assertEqual(20.0, entry['mbps'])
        self.assertEqual(50.0, entry['peak_mbps'])
        self.assertEqual(2, entry['age'])
        self.assertEqual('l1', entry['reporter'])

    def test_the_peak_forgets_what_left_the_window(self):
        self._post([_m(mbps=90.0)])
        self._at(25)
        self._post([_m(mbps=10.0)])
        self._at(31)
        self.assertEqual(10.0, self._list()[0]['peak_mbps'])

    def test_stale_pairs_are_not_listed(self):
        self._post([_m()])
        self._at(15)
        self.assertEqual(1, len(self._list()))
        self._at(16)
        self.assertEqual([], self._list())

    def test_the_stale_after_option_is_read(self):
        self.config(group=config.SRV6_SERVER_GROUP,
                    link_state_stale_after=60)
        self._post([_m()])
        self._at(40)
        self.assertEqual(1, len(self._list()))

    def test_a_pair_missing_from_its_reporters_last_report_is_gone(self):
        # GATE B: ECMP moved pb1>pa4 off l1>s2; l1 stopped reporting it,
        # but its last row was still fresh.
        def report(*pairs):
            req = self.new_create_request('srv6_link_states', {
                'srv6_link_states': [{'reporter': 'l1', 'from_node': 'l1',
                                      'to_node': 's2', 'up': True,
                                      'mbps': 30.0, 'drops': 0}]},
                as_admin=True)
            self._check(req.get_response(self.ext_api), 201)
            if pairs:
                self._post(list(pairs))
        report(_m(), _m(dst=PB4))
        self.assertEqual(2, len(self._list()))
        self._at(5)
        report(_m())                                  # PB4 left the link
        self.assertEqual([PB3], [e['destination_address']
                                 for e in self._list()])
        self._at(10)
        report()                                      # nothing above floor
        self.assertEqual([], self._list())

    def test_a_reporter_without_link_states_keeps_the_fresh_rule(self):
        self._post([_m()])
        self._at(10)
        self.assertEqual(1, len(self._list()))

    def test_unresolved_without_domains(self):
        self._post([_m()])
        entry = self._list()[0]
        self.assertIsNone(entry['domain_id'])
        self.assertIsNone(entry['source_port_id'])
        self.assertTrue(entry['steered'])           # not a known decap SID

    def test_show_and_filters(self):
        self._post([_m(), _m(dst=PB4)])
        self._post([_m(a='s2', b='l3')])
        listed = self._list()
        self.assertEqual(3, len(listed))
        self.assertEqual(3, len({e['id'] for e in listed}))
        self.assertEqual({'s2'}, {e['from_node']
                                  for e in self._list(from_node='s2')})
        self.assertEqual(2, len(self._list(to_node='s2')))
        entry = listed[0]
        self.assertEqual(entry, self._show(entry['id'])['srv6_aggregate_load'])
        self._show(uuidutils.generate_uuid(), expected=404)

    def test_the_id_is_stable_across_periods(self):
        self._post([_m(mbps=1)])
        first = self._list()[0]['id']
        self._at(5)
        self._post([_m(mbps=2)])
        self.assertEqual([first], [e['id'] for e in self._list()])


# ----------------------------------------------------------------------
# resolver: (SID, inner addresses) -> domain, ports, hosts
# ----------------------------------------------------------------------
LOCATORS = {'OVS1': 'fc00:0:1::/48', 'OVS3': 'fc00:0:3::/48',
            'OVS4': 'fc00:0:4::/48'}
FID = 0xe300


def _domains(*specs):
    """[(domain, function id)], specs: (id, function id)."""
    return [({'id': d, 'networks': ['net-%s' % d]}, fid) for d, fid in specs]


class TestResolver(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.ports = {'D': {PA1: ('port-pa1', 'OVS1'),
                            PB3: ('port-pb3', 'OVS3'),
                            PB4: ('port-pb4', 'OVS4')},
                      'E': {PA1: ('port-e1', 'OVS1'),
                            PB3: ('port-e3', 'OVS3')}}
        self.asked = []

    def _resolver(self, domains, usid=True):
        def ports_of(domain):
            self.asked.append(domain['id'])
            return self.ports[domain['id']]
        return aggregates.Resolver(domains, LOCATORS, usid, ports_of)

    def test_a_decap_sid_names_the_domain_and_the_destination(self):
        r = self._resolver(_domains(('D', FID), ('E', 0xe301)))
        self.assertEqual({'domain_id': 'D', 'steered': False,
                          'source_port_id': 'port-pa1', 'source_host': 'OVS1',
                          'destination_port_id': 'port-pb3',
                          'destination_host': 'OVS3'},
                         r(DECAP3, PA1, PB3))
        self.assertEqual(['D'], self.asked)        # E's ports never read

    def test_a_usid_container_is_steered_and_resolved_by_its_tail(self):
        r = self._resolver(_domains(('D', FID), ('E', 0xe301)))
        for container in ('fc00:0:a2:b3:3:e300::', 'fc00:0:b3:3:e300::'):
            out = r(container, PA1, PB3)
            self.assertTrue(out['steered'])
            self.assertEqual(('D', 'OVS3', 'port-pa1'),
                             (out['domain_id'], out['destination_host'],
                              out['source_port_id']))

    def test_an_end_sid_falls_back_to_the_addresses(self):
        r = self._resolver(_domains(('D', FID)), usid=False)
        out = r('fc00:0:a2::', PA1, PB4)              # a waypoint's End SID
        self.assertTrue(out['steered'])
        self.assertEqual(('D', 'port-pb4', 'OVS4'),
                         (out['domain_id'], out['destination_port_id'],
                          out['destination_host']))

    def test_addresses_in_two_domains_resolve_nothing(self):
        r = self._resolver(_domains(('D', FID), ('E', 0xe301)), usid=False)
        out = r('fc00:0:a2::', PA1, PB3)
        self.assertIsNone(out['domain_id'])
        self.assertTrue(out['steered'])

    def test_an_address_without_a_port_leaves_that_side_empty(self):
        r = self._resolver(_domains(('D', FID)))
        out = r(DECAP3, '10.91.1.99', PB3)
        self.assertEqual('D', out['domain_id'])
        self.assertIsNone(out['source_port_id'])
        self.assertEqual('port-pb3', out['destination_port_id'])

    def test_a_locator_that_cannot_hold_the_function_is_skipped(self):
        r = aggregates.Resolver(_domains(('D', FID)),
                                dict(LOCATORS, bad='fc00::/120'), True,
                                lambda d: self.ports['D'])
        self.assertEqual('D', r(DECAP3, PA1, PB3)['domain_id'])


class TestViewFunction(testtools.TestCase):

    def test_the_heartbeat_hides_rows_before_it(self):
        rows = [dict(_m(mbps=9.0), measured_at=T0)]
        out = aggregates.view(rows, T0, fresh=T0, window_start=T0,
                              resolve=lambda *a: {},
                              current={'l1': T0 + datetime.timedelta(
                                  seconds=1)})
        self.assertEqual([], out)

    def test_only_fresh_keys_with_their_newest_rate(self):
        def row(t, mbps, dst=PB3):
            return dict(_m(dst=dst, mbps=mbps),
                        measured_at=T0 + datetime.timedelta(seconds=t))
        rows = [row(0, 40.0), row(0, 5.0, dst=PB4), row(10, 12.0)]
        now = T0 + datetime.timedelta(seconds=12)
        out = aggregates.view(
            rows, now, fresh=T0 + datetime.timedelta(seconds=3),
            window_start=T0 - datetime.timedelta(seconds=1),
            resolve=lambda *a: {'domain_id': 'D'})
        self.assertEqual(1, len(out))
        self.assertEqual((12.0, 40.0, 2, 'D'),
                         (out[0]['mbps'], out[0]['peak_mbps'], out[0]['age'],
                          out[0]['domain_id']))


class TestDomainAddresses(test_plugin.PluginUnitTestCaseBase):

    def test_bound_active_workload_ports_only(self):
        ok = test_plugin._port(fixed_ips=[{'ip_address': '10.91.1.11'},
                                          {'ip_address': '2001:DB8::1'}],
                               **{portbindings.HOST_ID: 'OVS1'})
        unbound = test_plugin._port(fixed_ips=[{'ip_address': '10.91.1.12'}],
                                    **{portbindings.HOST_ID: ''})
        down = test_plugin._port(fixed_ips=[{'ip_address': '10.91.1.13'}],
                                 status=n_const.PORT_STATUS_DOWN)
        dhcp = test_plugin._port(fixed_ips=[{'ip_address': '10.91.1.2'}],
                                 device_owner=n_const.DEVICE_OWNER_DHCP)
        self.core.get_ports.return_value = [ok, unbound, down, dhcp]
        out = self.plugin._domain_addresses(
            self.ctx, test_plugin._domain(networks=['net1']))
        self.assertEqual({'10.91.1.11': (ok['id'], 'OVS1'),
                          '2001:db8::1': (ok['id'], 'OVS1')}, out)
        self.core.get_ports.assert_called_once_with(
            self.ctx, filters={'network_id': ['net1']})

    def test_a_domain_without_networks_asks_nothing(self):
        self.assertEqual({}, self.plugin._domain_addresses(
            self.ctx, test_plugin._domain()))
        self.core.get_ports.assert_not_called()
