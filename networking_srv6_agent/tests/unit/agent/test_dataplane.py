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

"""Unit tests for Srv6LinuxDataplane.

Everything that touches the kernel is mocked. The routes, steer tables and
SID rules sync_routes reconciles go through FakeKernel, so those tests assert
what the kernel ends up holding, and FakeKernel itself flags a route whose
packets would meet the seal; the rest assert the shape of the calls.

Ported near-verbatim from networking-bgpvpn (srv6-te); the classes after
TestMtu cover what P5 added.
"""

import ipaddress
from unittest import mock

from neutron.tests import base
from oslo_utils import uuidutils

from networking_srv6_agent.agent import dataplane
from networking_srv6_agent.common import constants


LOCATOR = 'fc00:0:1::/48'
UNDERLAY = 'eth0'
FUNCTION_ID = 7
VRF_TABLE = 10007
VRF_NAME = 'sv6vrf-7'
PORT_NAME = 'svp-7-100'


def _printed(prefix):
    """A prefix as `ip route/rule show` prints it: a host route bare."""
    net = ipaddress.ip_network(prefix, strict=False)
    if net.prefixlen == net.max_prefixlen:
        return str(net.network_address)
    return str(net)


class FakeKernel:
    """The routes and rules priv_seg6 manages, kept as kernel state.

    Installed as the side effects of the autospec priv_seg6 mock, so the
    signatures are still checked and the calls still recorded, while tests
    assert on what the kernel ends up holding. After every write it checks
    what call-order mocks could not see: a route traffic can reach (any
    route of a VRF table, a steer table's once a steer rule sends traffic
    there) whose first segment its VRF has no SID rule for drops every
    packet at the seal. Each such window lands in `blackholes`; a sealed
    path (a transit SID the edge filter does not cover) lands there too, by
    design, so only tests that expect every path open assert it empty.
    """

    FUNCTIONS = ('replace_seg6_route', 'delete_route', 'list_seg6_routes',
                 'flush_table', 'add_sid_rule', 'delete_sid_rule',
                 'list_sid_rules', 'add_steer_rule', 'delete_steer_rule',
                 'list_steer_rules')

    def __init__(self, priv, vrfs):
        self.vrfs = dict(vrfs)        # VRF table -> VRF name
        self.routes = {}              # (table, v6) -> {printed: segments}
        self.sid_rules = set()        # (vrf, sid)
        self.steer_rules = set()      # (printed, iif, table, v6)
        self.failing = set()          # prefixes / SIDs whose write fails
        self.blackholes = set()
        for name in self.FUNCTIONS:
            getattr(priv, name).side_effect = getattr(self, name)

    def table(self, table, family_v6=False):
        return self.routes.get((table, family_v6), {})

    def replace_seg6_route(self, prefix, segments, dev, table,
                           family_v6=False, mode=None):
        if prefix in self.failing:
            raise RuntimeError('boom')
        self.routes.setdefault((table, family_v6), {})[_printed(prefix)] = \
            list(segments)
        self._check()

    def delete_route(self, prefix, table, family_v6=False):
        self.routes.get((table, family_v6), {}).pop(_printed(prefix), None)

    def list_seg6_routes(self, table, family_v6=False):
        return list(self.table(table, family_v6))

    def flush_table(self, table, family_v6=False):
        self.routes.pop((table, family_v6), None)

    def add_sid_rule(self, vrf_name, sid_address):
        if sid_address in self.failing:
            raise RuntimeError('boom')
        self.sid_rules.add((vrf_name, sid_address))

    def delete_sid_rule(self, vrf_name, sid_address):
        self.sid_rules.discard((vrf_name, sid_address))
        self._check()

    def list_sid_rules(self):
        return sorted(self.sid_rules)

    def add_steer_rule(self, prefix, iif, table, family_v6=False):
        self.steer_rules.add((_printed(prefix), iif, table, family_v6))
        self._check()

    def delete_steer_rule(self, prefix, iif, table, family_v6=False):
        self.steer_rules.discard((_printed(prefix), iif, table, family_v6))

    def list_steer_rules(self):
        return sorted(self.steer_rules)

    def _check(self):
        live = dict(self.vrfs)
        for _p, iif, table, _v in self.steer_rules:
            function_id = iif[len(constants.GW_PORT_PREFIX):].split('-')[0]
            live[table] = constants.VRF_PREFIX + function_id
        for (table, _v6), routes in self.routes.items():
            if table not in live:
                continue
            for prefix, segments in routes.items():
                if (live[table], _sid(segments[0])) not in self.sid_rules:
                    self.blackholes.add((table, prefix, segments[0]))


def _sid(address):
    try:
        return str(ipaddress.IPv6Address(address))
    except ValueError:
        return address


class InterfaceAlreadyExists(Exception):
    pass


class DataplaneTestCaseBase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.ip_lib = self._patch('ip_lib')
        self.priv_ip_lib = self._patch('priv_ip_lib')
        self.priv_ip_lib.InterfaceAlreadyExists = InterfaceAlreadyExists
        # autospec: without it the mock accepts ANY signature, so changing
        # the argument order of a priv_seg6 helper -- which produces a
        # silently wrong `ip` command line -- would not fail a single test.
        # These assertions are the only check on those call sites.
        self.priv_seg6 = self._patch('priv_seg6', autospec=True)
        # Unpatched, initialize_node would start a real privsep daemon.
        self.priv_nft = self._patch('priv_nft', autospec=True)
        # Sysctls are exercised by their own test case; everywhere else they
        # would only reach into the test machine's /proc.
        sysctl = mock.patch.object(dataplane.Srv6LinuxDataplane,
                                   '_set_sysctl')
        self.set_sysctl = sysctl.start()
        self.addCleanup(sysctl.stop)

        self.priv_seg6.list_sid_rules.return_value = []
        self.priv_seg6.list_vrf_names.return_value = []
        # The kernel's default rule order, which the dataplane changes.
        self.priv_seg6.local_rule_prefs.return_value = [0]
        # A node fresh from a full-SID build: the `local` locator route.
        self.priv_seg6.show_main_route.return_value = [
            'local %s dev lo metric 1024 pref medium' % LOCATOR]
        self.priv_seg6.probe_capabilities.return_value = [
            'encap-red', 'next-csid']
        self.bridge = mock.Mock()
        self.bridge.get_port_name_list.return_value = []
        self.device = self.ip_lib.IPDevice.return_value
        self.device.addr.list.return_value = []
        self.domain_id = uuidutils.generate_uuid()
        self.dp = dataplane.Srv6LinuxDataplane(
            locator=LOCATOR, underlay_interface=UNDERLAY,
            vrf_table_base=10000, bridge=self.bridge)

    def _patch(self, name, autospec=False):
        patcher = mock.patch.object(dataplane, name, autospec=autospec)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def _with_domain(self):
        self.dp.domains[self.domain_id] = dataplane.DomainState(
            self.domain_id, FUNCTION_ID, VRF_TABLE, VRF_NAME)

    def _fake_kernel(self):
        self.kernel = FakeKernel(self.priv_seg6, {VRF_TABLE: VRF_NAME})


class TestNodeInitialisation(DataplaneTestCaseBase):

    def test_initialize_node(self):
        self.dp.initialize_node()
        self.priv_seg6.modprobe_vrf.assert_called_once_with()
        self.priv_seg6.replace_locator_route.assert_called_once_with(LOCATOR)

    def test_underlay_sysctls_are_asserted(self):
        # seg6_enabled counts on the interface the packet ARRIVES on, and
        # `all` does not apply to interfaces that already exist.
        self.dp.initialize_node()
        keys = [call[0][0] for call in self.set_sysctl.call_args_list]
        self.assertIn('net.ipv6.conf.eth0.seg6_enabled', keys)
        self.assertIn('net.ipv4.conf.eth0.rp_filter', keys)

    def test_modprobe_failure_is_not_fatal(self):
        self.priv_seg6.modprobe_vrf.side_effect = RuntimeError('no module')
        self.dp.initialize_node()
        self.assertTrue(self.priv_seg6.replace_locator_route.called)


class TestLocalRuleOrder(DataplaneTestCaseBase):
    """The local rule goes after l3mdev, so a VRF cannot reach the host.

    Otherwise, from inside a VRF the management address answered ping and a
    forged ICMPv6 to the underlay address was delivered, because
    `0: lookup local` precedes `1000: l3mdev`.
    """

    def _calls(self):
        return [(c[0], c[1], c[2]) for c in self.priv_seg6.method_calls
                if c[0] in ('add_local_rule', 'delete_local_rule')]

    def test_both_families_are_moved_add_before_delete(self):
        # Added first: the host is never without a local rule.
        self.dp.initialize_node()
        self.assertEqual(
            [('add_local_rule', (32765,), {'family_v6': False}),
             ('delete_local_rule', (0,), {'family_v6': False}),
             ('add_local_rule', (32765,), {'family_v6': True}),
             ('delete_local_rule', (0,), {'family_v6': True})],
            self._calls())

    def test_the_new_place_is_after_l3mdev_and_before_main(self):
        self.assertGreater(dataplane.constants.LOCAL_RULE_PRIORITY, 1000)
        self.assertLess(dataplane.constants.LOCAL_RULE_PRIORITY, 32766)

    def test_an_already_moved_rule_is_left_alone(self):
        self.priv_seg6.local_rule_prefs.return_value = [32765]
        self.dp.initialize_node()
        self.priv_seg6.add_local_rule.assert_not_called()
        self.priv_seg6.delete_local_rule.assert_not_called()

    def test_a_half_done_move_is_finished(self):
        # Both present: a crash between the add and the delete.
        self.priv_seg6.local_rule_prefs.return_value = [0, 32765]
        self.dp.initialize_node()
        self.priv_seg6.add_local_rule.assert_not_called()
        self.assertEqual(
            [mock.call(0, family_v6=False), mock.call(0, family_v6=True)],
            self.priv_seg6.delete_local_rule.call_args_list)

    def test_a_failure_does_not_stop_the_node(self):
        self.priv_seg6.local_rule_prefs.side_effect = RuntimeError('boom')
        self.dp.initialize_node()
        self.priv_seg6.replace_locator_route.assert_called_once_with(LOCATOR)

    def test_l3mdev_accept_stays_off(self):
        # At 1 a socket of the host answers connections from every VRF.
        self.assertIn(('net.ipv4.tcp_l3mdev_accept', '0'),
                      dataplane.GLOBAL_SYSCTLS)
        self.assertIn(('net.ipv4.udp_l3mdev_accept', '0'),
                      dataplane.GLOBAL_SYSCTLS)

    def test_flow_label_carries_entropy(self):
        # At 0 every flow between two nodes has one outer header, and the
        # underlay's ECMP puts them all on one path.
        self.assertIn(('net.ipv6.seg6_flowlabel', '1'),
                      dataplane.GLOBAL_SYSCTLS)
        self.dp.initialize_node()
        self.set_sysctl.assert_any_call('net.ipv6.seg6_flowlabel', '1')


class TestNodeEndSid(DataplaneTestCaseBase):
    """The node's own transit SID, which makes traffic engineering possible.

    It is node state, not domain state: another node may route through this
    one while this one holds no domain at all.
    """

    def test_end_sid_is_installed_without_a_vrf_table(self):
        # `action End vrftable <n>` is rejected outright by iproute2 -- the
        # behaviour forwards by a fresh FIB lookup, it does not terminate in
        # a VRF. Passing no vrf_table is the whole point.
        self.dp.initialize_node()
        self.priv_seg6.replace_seg6local_route.assert_called_once_with(
            'fc00:0:1:fff0::', 'End', UNDERLAY)

    def test_end_sid_comes_after_the_locator_route(self):
        # Order is load-bearing: the locator route is `local <loc> ... table
        # main`, and this /128 must beat it by longest prefix within main.
        self.dp.initialize_node()
        names = [c[0] for c in self.priv_seg6.method_calls]
        self.assertLess(names.index('replace_locator_route'),
                        names.index('replace_seg6local_route'))

    def test_end_sid_starts_on_lo_without_an_underlay(self):
        # Before the first sync no peer is known; refresh_underlay moves it
        # to a real device (TestUnderlayAutodetect).
        dp = dataplane.Srv6LinuxDataplane(
            locator=LOCATOR, underlay_interface=None,
            vrf_table_base=10000, bridge=self.bridge)
        dp.initialize_node()
        self.priv_seg6.replace_seg6local_route.assert_called_once_with(
            'fc00:0:1:fff0::', 'End', 'lo')

    def test_end_sid_failure_is_not_fatal(self):
        self.priv_seg6.replace_seg6local_route.side_effect = \
            RuntimeError('no seg6local')
        # Must not raise: a node that cannot be a transit is still a
        # perfectly good ingress and egress.
        self.dp.initialize_node()

    def test_delete_domain_does_not_remove_the_end_sid(self):
        # The End SID outlives every domain. Removing it here would silently
        # break traffic engineering for OTHER domains routed through here.
        self.dp.initialize_node()
        self._with_domain()
        self.priv_seg6.reset_mock()
        self.dp.delete_domain(self.domain_id)
        deleted = [c[0][0] for c in
                   self.priv_seg6.delete_route.call_args_list]
        self.assertNotIn('fc00:0:1:fff0::/128', deleted)


class TestSysctls(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(dataplane, 'ip_lib')
        self.ip_lib = patcher.start()
        self.addCleanup(patcher.stop)
        self.dp = dataplane.Srv6LinuxDataplane(
            locator=LOCATOR, underlay_interface=UNDERLAY, vrf_table_base=10000)

    def test_absent_sysctl_is_reported_not_written(self):
        with mock.patch('builtins.open', side_effect=OSError):
            self.dp._set_sysctl('net.vrf.strict_mode', '1')
        self.ip_lib.sysctl.assert_not_called()

    def test_correct_value_is_left_alone(self):
        with mock.patch('builtins.open', mock.mock_open(read_data='1\n')):
            self.dp._set_sysctl('net.vrf.strict_mode', '1')
        self.ip_lib.sysctl.assert_not_called()

    def test_wrong_value_is_written(self):
        self.ip_lib.sysctl.return_value = 0
        with mock.patch('builtins.open', mock.mock_open(read_data='0\n')):
            self.dp._set_sysctl('net.vrf.strict_mode', '1')
        # neutron's ip_lib.sysctl takes the argv AFTER "sysctl".
        self.ip_lib.sysctl.assert_called_once_with(
            ['-w', 'net.vrf.strict_mode=1'])

    def test_wrong_value_is_only_reported_when_not_managing_sysctls(self):
        self.dp.apply_sysctls = False
        with mock.patch('builtins.open', mock.mock_open(read_data='0\n')):
            self.dp._set_sysctl('net.vrf.strict_mode', '1')
        self.ip_lib.sysctl.assert_not_called()


class TestDomainLifecycle(DataplaneTestCaseBase):

    def test_ensure_domain_creates_the_vrf(self):
        self.ip_lib.device_exists.return_value = False
        state = self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.priv_ip_lib.create_interface.assert_called_once_with(
            VRF_NAME, None, 'vrf', vrf_table=VRF_TABLE)
        self.assertEqual(VRF_TABLE, state.vrf_table)
        self.assertEqual(VRF_NAME, state.vrf_name)
        self.assertEqual(state, self.dp.domains[self.domain_id])

    def test_ensure_domain_installs_the_decap_sid(self):
        self.ip_lib.device_exists.return_value = False
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.priv_seg6.replace_seg6local_route.assert_called_once_with(
            'fc00:0:1:7::', 'End.DT46', VRF_NAME, vrf_table=VRF_TABLE)

    def test_ensure_domain_is_idempotent(self):
        self.ip_lib.device_exists.return_value = True
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.priv_ip_lib.create_interface.assert_not_called()
        # The SID is re-asserted anyway: `replace` is how a resync repairs
        # state somebody else changed.
        self.assertTrue(self.priv_seg6.replace_seg6local_route.called)

    def test_ensure_domain_tolerates_a_racing_creation(self):
        self.ip_lib.device_exists.return_value = False
        self.priv_ip_lib.create_interface.side_effect = \
            InterfaceAlreadyExists()
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.assertTrue(self.priv_seg6.replace_seg6local_route.called)

    def test_strict_mode_is_set_before_the_vrf_exists(self):
        # End.DT46 is refused outright without it, and the table<->VRF
        # mapping is registered at VRF creation time.
        self.ip_lib.device_exists.return_value = False
        calls = []
        self.set_sysctl.side_effect = lambda k, v: calls.append(('sysctl', k))
        self.priv_ip_lib.create_interface.side_effect = \
            lambda *a, **kw: calls.append(('create', a[0]))
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.assertLess(calls.index(('sysctl', 'net.vrf.strict_mode')),
                        calls.index(('create', VRF_NAME)))

    def test_delete_domain_removes_everything(self):
        self._with_domain()
        self.dp.gateway_ports[self.domain_id]['net1'] = PORT_NAME
        self.ip_lib.device_exists.return_value = True
        self.dp.delete_domain(self.domain_id)
        self.bridge.delete_port.assert_called_once_with(PORT_NAME)
        # Both decap lengths, whatever the encoding: a /64 left from usid
        # mode must not outlive the domain either.
        self.assertEqual(
            [mock.call('fc00:0:1:7::/128', 'main', family_v6=True),
             mock.call('fc00:0:1:7::/64', 'main', family_v6=True)],
            self.priv_seg6.delete_route.call_args_list)
        self.priv_ip_lib.delete_interface.assert_called_once_with(
            VRF_NAME, None)
        self.assertNotIn(self.domain_id, self.dp.domains)

    def test_delete_domain_uses_the_ids_the_server_sent(self):
        # After an agent restart the domain is unknown locally, so the
        # message has to carry enough to name the kernel objects.
        self.ip_lib.device_exists.return_value = True
        self.dp.delete_domain(self.domain_id, function_id=FUNCTION_ID,
                              vrf_table=VRF_TABLE)
        self.priv_ip_lib.delete_interface.assert_called_once_with(
            VRF_NAME, None)

    def test_delete_domain_without_any_id_does_nothing(self):
        self.dp.delete_domain(self.domain_id)
        self.priv_ip_lib.delete_interface.assert_not_called()
        self.priv_seg6.delete_route.assert_not_called()
        self.priv_seg6.flush_table.assert_not_called()

    def test_delete_domain_survives_a_missing_vrf(self):
        self._with_domain()
        self.ip_lib.device_exists.return_value = False
        self.dp.delete_domain(self.domain_id)
        self.priv_ip_lib.delete_interface.assert_not_called()


class TestGatewayPort(DataplaneTestCaseBase):

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        self.subnets = [{'id': 'sub1', 'cidr': '10.0.0.0/24',
                         'gateway_ip': '10.0.0.1'}]

    def test_tag_is_a_port_column_not_an_interface_column(self):
        """Regression: passing tag to add_port aborts the OVS transaction.

        add_port sends its tuples to a db_set against the Interface table.
        `tag` lives on Port, so the whole transaction was rejected, the port
        was never created, and the only symptom was a much later
        "interface svp-... not found".
        """
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.bridge.add_port.assert_called_once_with(PORT_NAME,
                                                     ('type', 'internal'))
        self.bridge.set_db_attribute.assert_called_once_with(
            'Port', PORT_NAME, 'tag', 100)

    def test_tag_is_reasserted_on_every_call(self):
        # The OVS agent reallocates local VLANs when ports rebind; a stale
        # tag puts the gateway in the wrong broadcast domain.
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 101, self.subnets)
        self.assertEqual(
            [mock.call('Port', 'svp-7-100', 'tag', 100),
             mock.call('Port', 'svp-7-101', 'tag', 101)],
            self.bridge.set_db_attribute.call_args_list)

    def test_enslaved_before_addressed(self):
        # An address added first lands in the main table and does not move
        # when the interface is later enslaved.
        order = []
        self.priv_seg6.set_link_master.side_effect = \
            lambda *a: order.append('master')
        self.device.addr.add.side_effect = lambda *a: order.append('addr')
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.assertEqual(['master', 'addr'], order)

    def test_gateway_address_is_added_with_the_subnet_prefixlen(self):
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.device.addr.add.assert_called_once_with('10.0.0.1/24')

    def test_existing_address_is_not_added_again(self):
        # Asking beats adding and interpreting the failure: resync runs this
        # repeatedly.
        self.device.addr.list.return_value = [{'cidr': '10.0.0.1/24'}]
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.device.addr.add.assert_not_called()

    def test_an_address_of_no_subnet_is_removed(self):
        # A deleted subnet, or a changed gateway; link-local stays, and the
        # kernel's spelling of a wanted address is not a difference.
        self.device.addr.list.return_value = [
            {'cidr': '10.0.0.1/24'}, {'cidr': '10.0.9.1/24'},
            {'cidr': 'fe80::1/64'}, {'cidr': 'fd98:1:0::1/64'}]
        self.dp.ensure_gateway_port(
            self.domain_id, 'net1', 100, self.subnets + [
                {'id': 'sub6', 'cidr': 'fd98:1::/64',
                 'gateway_ip': 'fd98:1::1'}])
        self.device.addr.delete.assert_called_once_with('10.0.9.1/24')

    def test_subnet_without_a_gateway_is_skipped(self):
        self.dp.ensure_gateway_port(
            self.domain_id, 'net1', 100,
            [{'id': 'sub1', 'cidr': '10.0.0.0/24', 'gateway_ip': None}])
        self.device.addr.add.assert_not_called()

    def test_dual_stack_subnets(self):
        self.dp.ensure_gateway_port(
            self.domain_id, 'net1', 100,
            self.subnets + [{'id': 'sub2', 'cidr': '2001:db8::/64',
                             'gateway_ip': '2001:db8::1'}])
        self.assertEqual([mock.call('10.0.0.1/24')],
                         self.device.addr.add.call_args_list)
        # IPv6 without DAD: TestGatewayIpv6NoDad.
        self.priv_seg6.add_address_nodad.assert_called_once_with(
            PORT_NAME, '2001:db8::1/64')

    def test_port_is_recorded_for_teardown(self):
        name = self.dp.ensure_gateway_port(self.domain_id, 'net1', 100,
                                           self.subnets)
        self.assertEqual(PORT_NAME, name)
        self.assertEqual({'net1': PORT_NAME},
                         self.dp.gateway_ports[self.domain_id])

    def test_absent_netdev_defers_instead_of_failing(self):
        """Regression: ovs-vsctl returns before the netdev exists.

        ovs-vswitchd creates the kernel device after the row lands in the
        database, so enslaving immediately failed. The port is left for the
        next resync rather than half-programmed.
        """
        with mock.patch.object(self.dp, '_wait_for_device',
                               return_value=False):
            result = self.dp.ensure_gateway_port(self.domain_id, 'net1', 100,
                                                 self.subnets)
        self.assertIsNone(result)
        self.priv_seg6.set_link_master.assert_not_called()
        self.assertEqual({}, self.dp.gateway_ports[self.domain_id])

    def test_wait_for_device_present(self):
        self.ip_lib.device_exists.return_value = True
        self.assertTrue(self.dp._wait_for_device(PORT_NAME, timeout=0))

    def test_wait_for_device_absent(self):
        self.ip_lib.device_exists.return_value = False
        self.assertFalse(self.dp._wait_for_device(PORT_NAME, timeout=0))

    def test_failure_to_enslave_leaves_nothing_half_programmed(self):
        self.priv_seg6.set_link_master.side_effect = RuntimeError('boom')
        self.assertIsNone(self.dp.ensure_gateway_port(
            self.domain_id, 'net1', 100, self.subnets))
        self.device.addr.add.assert_not_called()

    def test_unknown_domain_is_ignored(self):
        self.assertIsNone(self.dp.ensure_gateway_port(
            'no-such-domain', 'net1', 100, self.subnets))
        self.bridge.add_port.assert_not_called()

    def test_network_without_a_local_vlan_is_ignored(self):
        self.assertIsNone(self.dp.ensure_gateway_port(
            self.domain_id, 'net1', None, self.subnets))
        self.bridge.add_port.assert_not_called()

    def test_a_reallocated_vlan_does_not_orphan_the_previous_port(self):
        """Regression: the port name embeds the VLAN, so the name changes.

        The OVS agent reallocates local VLANs when ports rebind. Recording
        the new name overwrote the only reference to the old port, which
        then survived every teardown path there is.
        """
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 101, self.subnets)
        self.bridge.delete_port.assert_called_once_with('svp-7-100')
        self.assertEqual({'net1': 'svp-7-101'},
                         self.dp.gateway_ports[self.domain_id])

    def test_the_same_vlan_twice_deletes_nothing(self):
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100, self.subnets)
        self.bridge.delete_port.assert_not_called()


class TestGatewayFirewallFlows(DataplaneTestCaseBase):
    """The OVS firewall has to see the gateway port.

    Without it every routed leg into an instance with port security on lost
    ICMP and TCP (the reply was +inv in table 72) while a port the security
    group does not open still received SYNs, and an instance with port
    security on could not open TCP to anything routed. The two flows below,
    the firewall's own pair for an unfiltered port plus ct_clear, made all
    of it pass.
    """

    OFPORT = 42

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        self.subnets = [{'id': 'sub1', 'cidr': '10.0.0.0/24',
                         'gateway_ip': '10.0.0.1'}]
        self.dp.ovs_firewall = True
        self.bridge.get_port_ofport.return_value = self.OFPORT

    def _ensure(self, vlan=100):
        return self.dp.ensure_gateway_port(self.domain_id, 'net1', vlan,
                                           self.subnets)

    def _expected_flows(self, ofport=OFPORT, vlan=100):
        return [
            mock.call(table=60, priority=100, in_port=ofport,
                      actions='ct_clear,set_field:%d->reg5,'
                              'set_field:%d->reg6,resubmit(,73)'
                              % (ofport, vlan)),
            mock.call(table=73, priority=80, reg5=ofport,
                      actions='resubmit(,94)'),
        ]

    def test_the_gateway_is_given_the_unfiltered_port_flows(self):
        # Table 60 tags the frame with port and network (the network is
        # also the conntrack zone) and hands it to table 73, which sends a
        # frame for a filtered port into its ingress pipeline and anything
        # else to NORMAL (94) -- what the firewall does for a Neutron port
        # with port security disabled.
        self._ensure()
        self.assertEqual(self._expected_flows(),
                         self.bridge.add_flow.call_args_list)
        self.assertEqual({PORT_NAME: self.OFPORT}, self.dp.gateway_flows)

    def test_ct_clear_comes_before_anything_else(self):
        """Regression: the pair alone is not enough.

        Without ct_clear frames from the gateway still carry
        the host's conntrack entry, OVS takes them as +trk, table 81 skips
        the instance's zone and the reply dies as not established: Rl and
        Rl' lost everything, Rc lost TCP and UDP.
        """
        self._ensure()
        actions = self.bridge.add_flow.call_args_list[0][1]['actions']
        self.assertTrue(actions.startswith('ct_clear,'), actions)

    def test_the_flows_follow_the_ports_vlan(self):
        self._ensure(vlan=101)
        self.assertEqual(self._expected_flows(vlan=101),
                         self.bridge.add_flow.call_args_list)

    def test_no_flows_without_the_ovs_firewall(self):
        # Under iptables_hybrid table 73 is empty: the flows would drop
        # every frame from the gateway.
        self.dp.ovs_firewall = False
        self._ensure()
        self.bridge.add_flow.assert_not_called()
        self.bridge.get_port_ofport.assert_not_called()

    def test_flows_are_installed_before_the_gateway_answers(self):
        # Before the address, so no routed frame meets the port while the
        # firewall cannot see it.
        order = []
        self.bridge.add_flow.side_effect = lambda **kw: order.append('flow')
        self.device.addr.add.side_effect = lambda *a: order.append('addr')
        self._ensure()
        self.assertEqual(['flow', 'flow', 'addr'], order)

    def test_flows_are_reasserted_on_every_call(self):
        # The OVS agent deletes every flow of another cookie at its first
        # iteration after a restart; handle_port and
        # the resync put them back by calling this again.
        self._ensure()
        self._ensure()
        self.assertEqual(self._expected_flows() * 2,
                         self.bridge.add_flow.call_args_list)
        self.bridge.delete_flows.assert_not_called()

    def test_a_new_ofport_removes_the_flows_of_the_old_one(self):
        # Left in place they would apply to whatever port OVS numbers 42
        # next.
        self._ensure()
        self.bridge.get_port_ofport.return_value = 43
        self._ensure()
        self.assertEqual([mock.call(table=60, in_port=42),
                          mock.call(table=73, reg5=42)],
                         self.bridge.delete_flows.call_args_list)
        self.assertEqual(self._expected_flows(ofport=43),
                         self.bridge.add_flow.call_args_list[2:])
        self.assertEqual({PORT_NAME: 43}, self.dp.gateway_flows)

    def test_a_port_without_an_ofport_still_routes(self):
        # Today's behaviour, not a failure: the next resync adds the flows.
        for no_ofport in (-1, [], None):
            self.bridge.get_port_ofport.return_value = no_ofport
            self.assertEqual(PORT_NAME, self._ensure())
        self.bridge.add_flow.assert_not_called()
        self.assertEqual({}, self.dp.gateway_flows)
        self.device.addr.add.assert_called_with('10.0.0.1/24')

    def test_a_failed_install_is_not_recorded(self):
        self.bridge.add_flow.side_effect = RuntimeError('boom')
        self.assertEqual(PORT_NAME, self._ensure())
        self.assertEqual({}, self.dp.gateway_flows)

    def test_deleting_the_port_deletes_its_flows_first(self):
        # Once the port is gone OVS may reuse its ofport.
        self._ensure()
        order = []
        self.bridge.delete_flows.side_effect = \
            lambda **kw: order.append('flows')
        self.bridge.delete_port.side_effect = lambda *a: order.append('port')
        self.dp._delete_gateway_port(PORT_NAME)
        self.assertEqual(['flows', 'flows', 'port'], order)
        self.assertEqual({}, self.dp.gateway_flows)

    def test_only_this_processes_flows_are_deleted(self):
        # No cookie argument: the cookie bridge adds its own, so a firewall
        # flow that took over the same match survives.
        self._ensure()
        self.dp._delete_gateway_port(PORT_NAME)
        for call in self.bridge.delete_flows.call_args_list:
            self.assertNotIn('cookie', call[1])

    def test_a_port_with_no_recorded_flows_deletes_none(self):
        # After a restart: the earlier process's flows have its cookie, and
        # cleanup_stale_flows removes them.
        self.dp._delete_gateway_port(PORT_NAME)
        self.bridge.delete_flows.assert_not_called()
        self.bridge.delete_port.assert_called_once_with(PORT_NAME)

    def test_a_reallocated_vlan_takes_the_flows_with_the_old_port(self):
        self._ensure(vlan=100)
        self.bridge.get_port_ofport.return_value = 43
        self._ensure(vlan=101)
        self.assertEqual([mock.call(table=60, in_port=42),
                          mock.call(table=73, reg5=42)],
                         self.bridge.delete_flows.call_args_list)
        self.assertEqual({'svp-7-101': 43}, self.dp.gateway_flows)

    def test_reconciliation_removes_the_flows_of_a_leftover(self):
        self._ensure()
        self.bridge.get_port_name_list.return_value = [PORT_NAME]
        self.dp.reconcile_gateway_ports(self.domain_id, set())
        self.assertEqual(2, self.bridge.delete_flows.call_count)
        self.assertEqual({}, self.dp.gateway_flows)

    def test_deleting_the_domain_removes_the_flows(self):
        self._ensure()
        self.dp.delete_domain(self.domain_id)
        self.assertEqual(2, self.bridge.delete_flows.call_count)
        self.assertEqual({}, self.dp.gateway_flows)

    def test_a_failed_delete_does_not_stop_the_port_delete(self):
        self._ensure()
        self.bridge.delete_flows.side_effect = RuntimeError('boom')
        self.dp._delete_gateway_port(PORT_NAME)
        self.bridge.delete_port.assert_called_once_with(PORT_NAME)


class TestGatewayIpv6NoDad(DataplaneTestCaseBase):
    """An IPv6 gateway address is added without DAD.

    Every node's gateway of a network holds it on one L2 segment, so DAD
    disabled it on all nodes but the first, and a guest there lost IPv6
    routing for good when the first one's gateway went.
    """

    V4 = {'id': 's4', 'cidr': '10.0.0.0/24', 'gateway_ip': '10.0.0.1'}
    V6 = {'id': 's6', 'cidr': 'fd98:1::/64', 'gateway_ip': 'fd98:1::'}

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.ip_lib.device_exists.return_value = True

    def _ensure(self):
        self.dp.ensure_gateway_port(self.domain_id, 'net1', 100,
                                    [self.V4, self.V6])

    def test_ipv6_is_nodad_and_ipv4_is_unchanged(self):
        self._ensure()
        self.priv_seg6.add_address_nodad.assert_called_once_with(
            PORT_NAME, 'fd98:1::/64')
        self.device.addr.add.assert_called_once_with('10.0.0.1/24')

    def test_a_dadfailed_address_is_re_added_without_dad(self):
        # What every node upgraded from an older build holds.
        self.device.addr.list.return_value = [
            {'cidr': '10.0.0.1/24', 'dadfailed': False},
            {'cidr': 'fd98:1::/64', 'dadfailed': True, 'tentative': True}]
        self._ensure()
        self.device.addr.delete.assert_called_once_with('fd98:1::/64')
        self.priv_seg6.add_address_nodad.assert_called_once_with(
            PORT_NAME, 'fd98:1::/64')
        self.device.addr.add.assert_not_called()

    def test_healthy_addresses_are_left_alone(self):
        self.device.addr.list.return_value = [
            {'cidr': '10.0.0.1/24', 'dadfailed': False},
            {'cidr': 'fd98:1::/64', 'dadfailed': False}]
        self._ensure()
        self.device.addr.delete.assert_not_called()
        self.device.addr.add.assert_not_called()
        self.priv_seg6.add_address_nodad.assert_not_called()


class TestGatewayNdFlows(DataplaneTestCaseBase):
    """A gateway's ARP and ND do not cross nodes.

    The flows drop, on the way in from patch-tun, ARP and ND frames naming
    the gateway on the network's VLAN: a second node's gateway then passes
    DAD although another node holds the address, and every guest resolves
    the gateway to its own node's port.
    """

    PATCH_OFPORT = 2

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        self.dp.tunnel_patch_port = 'patch-tun'
        self.bridge.get_port_ofport.side_effect = \
            lambda name: {'patch-tun': self.PATCH_OFPORT}.get(name, 42)
        self.subnets = [
            {'id': 's4', 'cidr': '10.0.0.0/24', 'gateway_ip': '10.0.0.1'},
            {'id': 's6', 'cidr': 'fd98:1::/64', 'gateway_ip': 'fd98:1::'}]

    def _ensure(self, vlan=100, subnets=None):
        self.dp.ensure_gateway_port(self.domain_id, 'net1', vlan,
                                    self.subnets if subnets is None
                                    else subnets)

    @staticmethod
    def _flow(vlan=100, **match):
        return mock.call(table=0, priority=100, in_port=2, dl_vlan=vlan,
                         actions='drop', **match)

    def _added(self):
        return [c for c in self.bridge.add_flow.call_args_list
                if c[1].get('table') == 0]

    def test_arp_and_nd_for_the_gateway_from_the_tunnel_are_dropped(self):
        self._ensure()
        self.assertCountEqual(
            [self._flow(proto='arp', arp_tpa='10.0.0.1'),
             self._flow(proto='arp', arp_spa='10.0.0.1'),
             self._flow(proto='icmp6', icmp_type=135, nd_target='fd98:1::'),
             self._flow(proto='icmp6', icmp_type=136, nd_target='fd98:1::')],
            self._added())

    def test_no_flows_without_tunnels(self):
        self.dp.tunnel_patch_port = None
        self._ensure()
        self.assertEqual([], self._added())

    def test_no_flows_without_a_patch_ofport(self):
        self.bridge.get_port_ofport.side_effect = None
        self.bridge.get_port_ofport.return_value = -1
        self._ensure()
        self.assertEqual([], self._added())
        # the gateway is still programmed
        self.priv_seg6.set_link_master.assert_called_once()

    def test_the_flows_come_before_the_address(self):
        order = []
        self.bridge.add_flow.side_effect = \
            lambda **kw: order.append('flow') if kw['table'] == 0 else None
        self.priv_seg6.add_address_nodad.side_effect = \
            lambda *a: order.append('address')
        self._ensure(subnets=self.subnets[1:])
        self.assertEqual(['flow', 'flow', 'address'], order)

    def test_they_are_re_asserted_on_every_call(self):
        # The OVS agent sweeps them at its first iteration after a restart.
        self._ensure()
        self._ensure()
        self.assertEqual(8, len(self._added()))
        self.bridge.delete_flows.assert_not_called()

    def test_a_removed_subnet_takes_its_flows_along(self):
        self._ensure()
        self._ensure(subnets=self.subnets[:1])
        deleted = [c[1] for c in self.bridge.delete_flows.call_args_list]
        self.assertCountEqual(
            [dict(table=0, in_port=2, dl_vlan=100, proto='icmp6',
                  icmp_type=t, nd_target='fd98:1::') for t in (135, 136)],
            deleted)

    def test_deleting_the_gateway_deletes_its_flows_first(self):
        self._ensure()
        order = []
        self.bridge.delete_flows.side_effect = \
            lambda **kw: order.append('flow') if kw['table'] == 0 else None
        self.bridge.delete_port.side_effect = \
            lambda *a: order.append('port')
        self.dp._delete_gateway_port(PORT_NAME)
        self.assertEqual(['flow'] * 4 + ['port'], order)
        self.assertEqual({}, self.dp.gateway_nd_flows)

    def test_only_this_processes_flows_are_deleted(self):
        # No cookie argument: the bridge matches its own cookie.
        self._ensure()
        self.dp._delete_gateway_port(PORT_NAME)
        for call in self.bridge.delete_flows.call_args_list:
            self.assertNotIn('cookie', call[1])

    def test_a_vlan_change_moves_the_flows(self):
        self._ensure(vlan=100)
        self._ensure(vlan=101)
        deleted_vlans = {c[1]['dl_vlan']
                         for c in self.bridge.delete_flows.call_args_list
                         if c[1]['table'] == 0}
        self.assertEqual({100}, deleted_vlans)
        self.assertIn(self._flow(vlan=101, proto='arp', arp_tpa='10.0.0.1'),
                      self._added())


class TestLocalNetworkVlans(DataplaneTestCaseBase):
    """Which networks have a live port on br-int, and on which VLAN.

    From OVSDB (Port.other_config:net_uuid + Port.tag), which the OVS agent
    writes when it binds a port and keeps across restarts -- unlike its
    in-memory VLAN map, empty during every restart.
    """

    def _rows(self, *rows):
        self.bridge.get_ports_attributes.return_value = [
            {'name': n, 'tag': t, 'other_config': oc} for n, t, oc in rows]

    def test_bound_ports_map_their_network_to_their_vlan(self):
        self._rows(('tapa', 3, {'net_uuid': 'net1', 'tag': '3'}),
                   ('tapb', 4, {'net_uuid': 'net2', 'tag': '4'}))
        self.assertEqual({'net1': {3}, 'net2': {4}},
                         self.dp.local_network_vlans())
        self.bridge.get_ports_attributes.assert_called_once_with(
            'Port', columns=['name', 'tag', 'other_config'], if_exists=True)

    def test_two_ports_of_one_network_are_one_entry(self):
        self._rows(('tapa', 3, {'net_uuid': 'net1'}),
                   ('tapb', 3, {'net_uuid': 'net1'}))
        self.assertEqual({'net1': {3}}, self.dp.local_network_vlans())

    def test_gateway_ports_are_not_a_networks_ports(self):
        self._rows(('svp-7-3', 3, {'net_uuid': 'net1'}))
        self.assertEqual({}, self.dp.local_network_vlans())

    def test_ports_without_a_network_are_skipped(self):
        # patch ports, an internal port the agent has not bound yet
        self._rows(('patch-tun', [], {}), ('tapnew', 5, {}))
        self.assertEqual({}, self.dp.local_network_vlans())

    def test_the_dead_vlan_is_not_live(self):
        # admin-down or unbound ports sit in VLAN 4095
        self._rows(('tapa', 4095, {'net_uuid': 'net1'}))
        self.assertEqual({}, self.dp.local_network_vlans())

    def test_an_untagged_port_is_skipped(self):
        self._rows(('tapa', [], {'net_uuid': 'net1'}))
        self.assertEqual({}, self.dp.local_network_vlans())

    def test_a_one_element_list_tag_is_accepted(self):
        self._rows(('tapa', [3], {'net_uuid': 'net1'}))
        self.assertEqual({'net1': {3}}, self.dp.local_network_vlans())

    def test_an_empty_bridge(self):
        self.bridge.get_ports_attributes.return_value = []
        self.assertEqual({}, self.dp.local_network_vlans())

    def test_an_unreadable_bridge_is_none(self):
        # None, not {}: {} would mean "nothing is live" and delete it all
        self.bridge.get_ports_attributes.side_effect = RuntimeError('boom')
        self.assertIsNone(self.dp.local_network_vlans())

    def test_no_bridge_is_none(self):
        self.dp.bridge = None
        self.assertIsNone(self.dp.local_network_vlans())


class TestGatewayPortReconciliation(DataplaneTestCaseBase):
    """Regression: a network leaving a domain leaked its gateway port.

    ensure_gateway_port only ever added, and delete_domain only fired when
    the node left the domain entirely -- so an association delete left the
    OVS port and its VRF routes in place permanently.
    See implementation/srv6-plugin/evidence/assoc-delete-leak.txt.
    """

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        # The bridge is the source of truth, not self.gateway_ports: the
        # ports outlive the agent process and the map starts empty.
        self.bridge.get_port_name_list.return_value = [
            'svp-7-100', 'svp-7-101', 'svp-9-100', 'tap-something']

    def test_a_network_that_left_the_domain_loses_its_port(self):
        self.dp.reconcile_gateway_ports(self.domain_id, {100})
        self.bridge.delete_port.assert_called_once_with('svp-7-101')

    def test_the_networks_that_stayed_keep_theirs(self):
        self.dp.reconcile_gateway_ports(self.domain_id, {100, 101})
        self.bridge.delete_port.assert_not_called()

    def test_another_domains_ports_are_never_touched(self):
        # svp-9-100 belongs to function id 9. Matching on the bare prefix
        # would delete it.
        self.dp.reconcile_gateway_ports(self.domain_id, {100, 101})
        self.dp.reconcile_gateway_ports(self.domain_id, set())
        for call in self.bridge.delete_port.call_args_list:
            self.assertTrue(call[0][0].startswith('svp-7-'))

    def test_the_last_network_leaving_empties_the_domain(self):
        # The case in the evidence file: the domain survives with no
        # networks.
        self.dp.reconcile_gateway_ports(self.domain_id, set())
        self.assertEqual(
            sorted([mock.call('svp-7-100'), mock.call('svp-7-101')], key=str),
            sorted(self.bridge.delete_port.call_args_list, key=str))
        # The domain itself still exists, so its VRF and SID stay.
        self.priv_ip_lib.delete_interface.assert_not_called()
        self.priv_seg6.delete_route.assert_not_called()

    def test_ports_left_behind_by_a_previous_agent_are_collected(self):
        """The reason the bridge is asked instead of self.gateway_ports.

        After a restart the in-memory map is empty while the OVS ports are
        all still there, so a map-based reconciliation deleted nothing and
        the leak survived every restart.
        """
        self.assertEqual({}, self.dp.gateway_ports[self.domain_id])
        self.dp.reconcile_gateway_ports(self.domain_id, {100})
        self.bridge.delete_port.assert_called_once_with('svp-7-101')

    def test_an_empty_wanted_set_deletes_every_port_of_the_domain(self):
        """The domain has networks, none with a live port here.

        This used to be the early return for "the VLAN map is not built
        yet", which kept every leftover: after a detach, and after a
        network's last port left the node, when the reclaimed VLAN could
        reach another network that then injected into the domain. The
        wanted set now comes from OVSDB, so empty means empty.
        """
        self.dp.reconcile_gateway_ports(self.domain_id, set())
        self.assertEqual(
            sorted([mock.call('svp-7-100'), mock.call('svp-7-101')], key=str),
            sorted(self.bridge.delete_port.call_args_list, key=str))

    def test_the_stale_entry_is_dropped_from_the_map_too(self):
        self.dp.gateway_ports[self.domain_id] = {'net1': 'svp-7-100',
                                                 'net2': 'svp-7-101'}
        self.dp.reconcile_gateway_ports(self.domain_id, {100})
        self.assertEqual({'net1': 'svp-7-100'},
                         self.dp.gateway_ports[self.domain_id])

    def test_reconciling_an_unknown_domain_is_a_noop(self):
        self.dp.reconcile_gateway_ports('no-such-domain', set())
        self.bridge.delete_port.assert_not_called()

    def test_reconciliation_is_idempotent(self):
        self.dp.reconcile_gateway_ports(self.domain_id, {100})
        self.bridge.reset_mock()
        self.bridge.get_port_name_list.return_value = ['svp-7-100']
        self.dp.reconcile_gateway_ports(self.domain_id, {100})
        self.bridge.delete_port.assert_not_called()

    def test_a_bridge_that_cannot_be_listed_deletes_nothing(self):
        self.bridge.get_port_name_list.side_effect = RuntimeError('boom')
        self.dp.reconcile_gateway_ports(self.domain_id, set())
        self.bridge.delete_port.assert_not_called()


class TestRouteReconciliation(DataplaneTestCaseBase):
    """Regression: encap routes accumulated exactly as the ports did.

    Found while verifying the gateway-port fix: the port for a detached
    network was gone, but table 13783 still held
    `10.90.2.21 encap seg6 ... segs [fc00:0:2:ec7::]` for a workload on a
    network that had left the domain.
    """

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2}
        self.kernel.routes[(VRF_TABLE, False)] = {
            '10.90.1.11': ['fc00:0:2:7::'], '10.90.2.21': ['fc00:0:2:7::']}

    def _sync(self, routes):
        self.dp.sync_routes(self.domain_id, routes, self.locators, 'node1')

    def _held(self, family_v6=False):
        return sorted(self.kernel.table(VRF_TABLE, family_v6))

    def test_a_prefix_that_left_the_domain_loses_its_route(self):
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'}])
        self.assertEqual(['10.90.1.11'], self._held())

    def test_a_host_route_is_not_deleted_by_its_own_prefix_length(self):
        # `ip route show` prints 10.90.2.21 where the payload says
        # 10.90.2.21/32. Comparing the strings deletes the whole table.
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node2'}])
        self.assertEqual(['10.90.1.11', '10.90.2.21'], self._held())
        self.priv_seg6.delete_route.assert_not_called()

    def test_a_workload_that_moved_here_loses_its_encap_route(self):
        """The withdraw half of a migration.

        A live migration to this node sends no withdrawal of the old host;
        the route now names this node, and its encap route must go or it
        keeps sending local traffic out to the underlay and back.
        """
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node1'}])
        self.assertEqual(['10.90.1.11'], self._held())

    def test_a_domain_with_no_routes_left_keeps_none(self):
        self._sync([])
        self.assertEqual([], self._held())

    def test_both_families_are_reconciled(self):
        self.kernel.routes[(VRF_TABLE, True)] = {'fd00::5': ['fc00:0:2:7::']}
        self._sync([])
        self.assertEqual([], self._held(True))

    def test_a_route_that_failed_to_install_is_still_wanted(self):
        # Otherwise a transient install failure deletes what is there.
        self.kernel.failing.add('10.90.2.21/32')
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node2'}])
        self.assertEqual(['10.90.1.11', '10.90.2.21'], self._held())

    def test_a_route_with_no_underlay_device_is_kept(self):
        # It cannot be rewritten; a failed lookup must not delete it.
        self.dp.underlay_interface = None
        self._sync([{'prefix': '10.90.2.21/32', 'host': 'node2'}])
        self.assertEqual(['10.90.2.21'], self._held())
        self.priv_seg6.replace_seg6_route.assert_not_called()

    def test_a_table_that_cannot_be_read_deletes_nothing(self):
        self.priv_seg6.list_seg6_routes.side_effect = RuntimeError('boom')
        self._sync([])
        self.assertEqual(['10.90.1.11', '10.90.2.21'], self._held())

    def test_reconciling_an_unknown_domain_is_a_noop(self):
        self.dp.sync_routes('no-such-domain', [], self.locators, 'node1')
        self.assertEqual(['10.90.1.11', '10.90.2.21'], self._held())


class TestRoutes(DataplaneTestCaseBase):

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2}

    def _route(self, prefix='10.0.0.5/32', host='node2'):
        return {'prefix': prefix, 'host': host, 'port_id': 'p1'}

    def _sync(self, *routes, locators=None):
        self.dp.sync_routes(self.domain_id, list(routes),
                            locators or self.locators, 'node1')

    def test_remote_port_gets_an_encap_route(self):
        # The remote node's locator with *this domain's* function id: the
        # SID differs per node, the function id does not.
        self._sync(self._route())
        self.priv_seg6.replace_seg6_route.assert_called_once_with(
            '10.0.0.5/32', ['fc00:0:2:7::'], UNDERLAY, VRF_TABLE,
            family_v6=False)
        self.assertEqual({'10.0.0.5': ['fc00:0:2:7::']},
                         self.kernel.table(VRF_TABLE))

    def test_ipv6_prefix_selects_the_v6_family(self):
        self._sync(self._route('2001:db8::5/128'))
        self.assertEqual(['2001:db8::5'],
                         list(self.kernel.table(VRF_TABLE, True)))

    def test_local_port_is_skipped(self):
        # It is already reachable through the gateway port's connected
        # route; encapsulating would send local traffic out and back.
        self._sync(self._route(host='node1'), locators={'node1': LOCATOR})
        self.assertEqual({}, self.kernel.table(VRF_TABLE))

    def test_host_without_a_locator_is_skipped(self):
        self._sync(self._route(host='node9'))
        self.assertEqual({}, self.kernel.table(VRF_TABLE))

    def test_one_failed_route_does_not_stop_the_rest(self):
        self.kernel.failing.add('10.0.0.5/32')
        self._sync(self._route('10.0.0.5/32'), self._route('10.0.0.6/32'))
        self.assertEqual(['10.0.0.6'], list(self.kernel.table(VRF_TABLE)))

    def test_routes_for_an_unknown_domain_are_ignored(self):
        self.dp.sync_routes('no-such-domain', [self._route()], self.locators,
                            'node1')
        self.priv_seg6.replace_seg6_route.assert_not_called()

    def test_a_route_that_goes_leaves_the_domains_steer_tables_too(self):
        # A path to a deleted port goes with it and nothing else is pushed;
        # left there it would capture a reused address. Another domain's
        # steer table (svp-8-*) is not touched.
        self.kernel.steer_rules = {('10.0.1.9', 'svp-7-100', 70001, False),
                                   ('10.0.2.9', 'svp-8-100', 70002, False)}
        self.kernel.routes[(70001, False)] = {'10.0.0.5': ['fc00:0:2:7::']}
        self.kernel.routes[(70002, False)] = {'10.0.0.5': ['fc00:0:2:8::']}
        self._sync()
        self.assertEqual({}, self.kernel.table(70001))
        self.assertEqual({('10.0.2.9', 'svp-8-100', 70002, False)},
                         self.kernel.steer_rules)
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(70002)))


class TestMtu(DataplaneTestCaseBase):

    def _underlay_mtu(self, value):
        self.ip_lib.IPDevice.return_value.link.mtu = value

    def test_tenant_mtu_plus_encapsulation_fits(self):
        self._underlay_mtu(1500)
        self.assertTrue(self.dp.check_mtu(1400))

    def test_tenant_mtu_plus_encapsulation_does_not_fit(self):
        # 1450 + 64 bytes of encapsulation exceeds a 1500-byte underlay.
        self._underlay_mtu(1500)
        self.assertFalse(self.dp.check_mtu(1450))

    def test_exactly_at_the_limit_fits(self):
        self._underlay_mtu(1500)
        self.assertTrue(self.dp.check_mtu(1500 - 64))

    def test_deeper_segment_lists_cost_more(self):
        self._underlay_mtu(1500)
        self.assertFalse(self.dp.check_mtu(1500 - 64, segment_count=2))

    def test_unknown_underlay_mtu_is_not_an_error(self):
        self.ip_lib.IPDevice.side_effect = RuntimeError('no such device')
        self.assertTrue(self.dp.check_mtu(1450))

    def test_no_network_mtu_is_not_an_error(self):
        self.assertTrue(self.dp.check_mtu(None))


# ----------------------------------------------------------------------
# P5 additions
# ----------------------------------------------------------------------
class TestSeal(DataplaneTestCaseBase):
    """No lookup may fall through to main.

    Measured on node 1 before this existed (evidence/vrf-fallthrough.txt):
    from a tenant's gateway port, another tenant's SID resolved to its
    End.DT46 decap -- a route into the other tenant's VRF.
    """

    def setUp(self):
        super().setUp()
        self.ip_lib.device_exists.return_value = True

    def test_both_families_are_sealed(self):
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.assertEqual(
            [mock.call(VRF_TABLE, family_v6=False),
             mock.call(VRF_TABLE, family_v6=True)],
            self.priv_seg6.replace_unreachable_default.call_args_list)

    def test_sealed_before_the_decap_sid_exists(self):
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        names = [c[0] for c in self.priv_seg6.method_calls]
        self.assertLess(names.index('replace_unreachable_default'),
                        names.index('replace_seg6local_route'))

    def test_every_resync_reasserts_it(self):
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.dp.ensure_domain(self.domain_id, FUNCTION_ID, 'End.DT46')
        self.assertEqual(
            4, self.priv_seg6.replace_unreachable_default.call_count)

    def test_a_vrf_that_cannot_be_sealed_is_not_programmed(self):
        # Fail closed: no SID, no recorded state -- and therefore no gateway
        # port, because ensure_gateway_port refuses an unrecorded domain.
        self.priv_seg6.replace_unreachable_default.side_effect = \
            RuntimeError('boom')
        self.assertRaises(dataplane.Srv6DataplaneError,
                          self.dp.ensure_domain,
                          self.domain_id, FUNCTION_ID, 'End.DT46')
        self.priv_seg6.replace_seg6local_route.assert_not_called()
        self.assertNotIn(self.domain_id, self.dp.domains)
        self.assertIsNone(self.dp.ensure_gateway_port(
            self.domain_id, 'net1', 100, []))


class TestDeleteFlushesTheTable(DataplaneTestCaseBase):
    """Deleting the VRF device does not empty its table."""

    def test_both_families_are_flushed(self):
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        self.dp.delete_domain(self.domain_id)
        self.assertEqual(
            [mock.call(VRF_TABLE, family_v6=False),
             mock.call(VRF_TABLE, family_v6=True)],
            self.priv_seg6.flush_table.call_args_list)

    def test_after_the_device_is_gone(self):
        self._with_domain()
        self.ip_lib.device_exists.return_value = True
        self.dp.delete_domain(self.domain_id)
        order = [c[0] for c in self.priv_seg6.method_calls] + []
        self.assertEqual('flush_table', order[-1])
        self.assertTrue(self.priv_ip_lib.delete_interface.called)

    def test_even_when_the_device_is_already_gone(self):
        # The routes outlive the device; that is the whole point.
        self._with_domain()
        self.ip_lib.device_exists.return_value = False
        self.dp.delete_domain(self.domain_id)
        self.assertEqual(2, self.priv_seg6.flush_table.call_count)


class TestDeleteAfterRestart(DataplaneTestCaseBase):
    """Delete used to find gateway ports in memory only."""

    def setUp(self):
        super().setUp()
        self.ip_lib.device_exists.return_value = True
        self.bridge.get_port_name_list.return_value = [
            'svp-7-100', 'svp-7-101', 'svp-9-100', 'tap-something']

    def test_the_ports_are_found_on_the_bridge(self):
        # Nothing in memory: exactly the state after a restart.
        self.dp.delete_domain(self.domain_id, function_id=FUNCTION_ID)
        self.assertEqual(
            [mock.call('svp-7-100'), mock.call('svp-7-101')],
            self.bridge.delete_port.call_args_list)

    def test_an_orphan_is_named_by_its_function_id_alone(self):
        self.dp.delete_domain(None, function_id=9)
        self.bridge.delete_port.assert_called_once_with('svp-9-100')
        self.priv_ip_lib.delete_interface.assert_called_once_with(
            'sv6vrf-9', None)
        self.priv_seg6.delete_route.assert_any_call(
            'fc00:0:1:9::/128', 'main', family_v6=True)
        self.priv_seg6.flush_table.assert_any_call(10009, family_v6=True)

    def test_a_port_known_both_ways_is_deleted_once(self):
        self._with_domain()
        self.dp.gateway_ports[self.domain_id]['net1'] = 'svp-7-100'
        self.dp.delete_domain(self.domain_id)
        self.assertEqual(2, self.bridge.delete_port.call_count)


class TestPresentFunctionIds(DataplaneTestCaseBase):

    def test_only_this_packages_vrfs_count(self):
        self.priv_seg6.list_vrf_names.return_value = [
            'sv6vrf-7', 'sv6vrf-3783', 'mgmt', 'sv6vrf-x']
        self.assertEqual({7, 3783}, self.dp.present_function_ids())

    def test_none_is_not_the_same_as_empty(self):
        # The garbage collector deletes what is present and not wanted; an
        # unreadable list must never read as "nothing is present".
        self.priv_seg6.list_vrf_names.side_effect = RuntimeError('boom')
        self.assertIsNone(self.dp.present_function_ids())
        self.priv_seg6.list_vrf_names.side_effect = None
        self.priv_seg6.list_vrf_names.return_value = []
        self.assertEqual(set(), self.dp.present_function_ids())


class TestSidRules(DataplaneTestCaseBase):
    """The seal must not stop the domain's OWN encapsulated traffic.

    Found at the two-node gate: after seg6
    encapsulates a packet that arrived on a gateway port, the kernel routes
    the outer IPv6 header in the same VRF, hit the unreachable default, and
    dropped every packet -- Ip6InNoRoutes one per echo request, on both
    nodes. Call-order mocks could not model where that lookup happens;
    FakeKernel's blackhole check does.
    """

    RED = 'sv6vrf-7'

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def _route(self, prefix, host, segments=None):
        route = {'prefix': prefix, 'host': host, 'port_id': 'p'}
        if segments is not None:
            route['segments'] = segments
        return route

    def _sync(self, *routes, locators=None):
        self.dp.sync_routes(self.domain_id, list(routes),
                            locators or self.locators, 'node1')

    def test_one_rule_per_remote_node_not_per_route(self):
        self._sync(self._route('10.0.0.5/32', 'node2'),
                   self._route('10.0.0.6/32', 'node2'),
                   self._route('10.0.0.7/32', 'node3'))
        self.assertEqual({(self.RED, 'fc00:0:2:7::'),
                          (self.RED, 'fc00:0:3:7::')}, self.kernel.sid_rules)
        self.assertEqual(2, self.priv_seg6.add_sid_rule.call_count)

    def test_no_route_ever_lacks_its_rule(self):
        # Make before break: a route re-steered through a new first segment
        # dropped its packets in the VRF until the rule toward that segment
        # followed, ~0.4 s per drain on the Clos.
        self._sync(self._route('10.0.0.5/32', 'node2'))
        self._sync(self._route('10.0.0.5/32', 'node3'))
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertEqual({(self.RED, 'fc00:0:3:7::')}, self.kernel.sid_rules)

    def test_a_local_route_needs_no_rule(self):
        self._sync(self._route('10.0.0.5/32', 'node1'),
                   locators={'node1': LOCATOR})
        self.assertEqual(set(), self.kernel.sid_rules)

    def test_an_existing_rule_is_not_added_again(self):
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:7::')}
        self._sync(self._route('10.0.0.5/32', 'node2'))
        self.priv_seg6.add_sid_rule.assert_not_called()

    def test_the_kernel_spelling_matches_ours(self):
        # `ip rule show` may print a SID differently from format_sid.
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:7:0:0:0:0')}
        self._sync(self._route('10.0.0.5/32', 'node2'))
        self.priv_seg6.add_sid_rule.assert_not_called()
        self.priv_seg6.delete_sid_rule.assert_not_called()

    def test_a_transit_first_segment_gets_no_rule(self):
        # Letting a VRF reach an End SID lets a tenant craft an SRH through
        # it toward another domain. Fail closed while the edge filter is not
        # active -- and nothing applied it here (TestEndSidRules has both).
        self._sync(self._route('10.0.0.5/32', 'node2', ['fc00:0:3:fff0::']))
        self.assertEqual(set(), self.kernel.sid_rules)
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(VRF_TABLE)))

    def test_an_add_failure_is_contained(self):
        self.kernel.failing.add('fc00:0:2:7::')
        self._sync(self._route('10.0.0.5/32', 'node2'),
                   self._route('10.0.0.7/32', 'node3'))
        self.assertEqual({(self.RED, 'fc00:0:3:7::')}, self.kernel.sid_rules)

    def test_rules_no_route_uses_are_pruned(self):
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:7::'),
                                 (self.RED, 'fc00:0:3:7::'),
                                 ('sv6vrf-9', 'fc00:0:3:9::')}
        self._sync(self._route('10.0.0.5/32', 'node2'))
        # Only this VRF's stale rule; another domain's is never touched.
        self.assertEqual({(self.RED, 'fc00:0:2:7::'),
                          ('sv6vrf-9', 'fc00:0:3:9::')}, self.kernel.sid_rules)

    def test_an_unreadable_rule_list_prunes_nothing(self):
        self.priv_seg6.list_sid_rules.side_effect = RuntimeError('boom')
        self._sync()
        self.priv_seg6.delete_sid_rule.assert_not_called()

    def test_unreadable_steer_rules_prune_no_rule(self):
        # A steer table may be the one user of a rule; unseen, it is kept.
        self.kernel.sid_rules = {(self.RED, 'fc00:0:3:fff0::')}
        self.priv_seg6.list_steer_rules.side_effect = RuntimeError('boom')
        self._sync(self._route('10.0.0.5/32', 'node2'))
        self.assertIn((self.RED, 'fc00:0:3:fff0::'), self.kernel.sid_rules)

    def test_delete_removes_only_this_vrfs_rules_before_the_device(self):
        self.ip_lib.device_exists.return_value = True
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:7::'),
                                 ('sv6vrf-9', 'fc00:0:2:9::')}
        at_vrf_delete = []
        self.priv_ip_lib.delete_interface.side_effect = \
            lambda *a: at_vrf_delete.append(set(self.kernel.sid_rules))
        self.dp.delete_domain(self.domain_id)
        self.assertEqual([{('sv6vrf-9', 'fc00:0:2:9::')}], at_vrf_delete)

    def test_a_rule_without_its_vrf_is_still_an_orphan(self):
        # `[detached]` after the VRF device went: GC must still see it.
        self.priv_seg6.list_vrf_names.return_value = ['sv6vrf-7']
        self.kernel.sid_rules = {('sv6vrf-9', 'fc00:0:2:9::')}
        self.assertEqual({7, 9}, self.dp.present_function_ids())

    def test_an_unreadable_rule_list_still_reports_the_vrfs(self):
        self.priv_seg6.list_vrf_names.return_value = ['sv6vrf-7']
        self.priv_seg6.list_sid_rules.side_effect = RuntimeError('boom')
        self.assertEqual({7}, self.dp.present_function_ids())


class TestSegments(DataplaneTestCaseBase):
    """The agent half of TE, moved here from P6.

    A payload carries `segments` only on a route an admin TE path steers;
    every other route is exactly the depth-1 route TestRoutes pins.
    """

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()

    def _segs(self, segments):
        self.dp.sync_routes(
            self.domain_id,
            [{'prefix': '10.0.0.5/32', 'host': 'node2', 'port_id': 'p1',
              'segments': segments}],
            {'node2': LOCATOR2}, 'node1')
        return self.kernel.table(VRF_TABLE)['10.0.0.5']

    def test_transit_segments_go_before_the_decap_sid(self):
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:2:7::'],
                         self._segs(['fc00:0:3:fff0::']))

    def test_a_leading_own_end_sid_is_dropped(self):
        # The payload fans out to every node; the server cannot know which
        # one is the ingress.
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:2:7::'],
                         self._segs(['fc00:0:1:fff0::', 'fc00:0:3:fff0::']))

    def test_compared_as_addresses_not_strings(self):
        self.assertEqual(['fc00:0:2:7::'],
                         self._segs(['fc00:0:1:fff0:0:0:0:0']))

    def test_this_node_later_in_the_path_is_a_real_waypoint(self):
        self.assertEqual(
            ['fc00:0:3:fff0::', 'fc00:0:1:fff0::', 'fc00:0:2:7::'],
            self._segs(['fc00:0:3:fff0::', 'fc00:0:1:fff0::']))

    def test_via_the_destination_itself_is_kept(self):
        # The only depth-2 SRH two nodes can produce (TE-DEPTH2 evidence).
        self.assertEqual(['fc00:0:2:fff0::', 'fc00:0:2:7::'],
                         self._segs(['fc00:0:2:fff0::']))

    def test_an_unparseable_segment_is_left_for_ip_to_refuse(self):
        self.assertEqual(['bogus', 'fc00:0:2:7::'], self._segs(['bogus']))

    def test_no_segments_is_the_depth_one_route(self):
        self.assertEqual(['fc00:0:2:7::'], self._segs(None))


# ----------------------------------------------------------------------
# P6 additions
# ----------------------------------------------------------------------
LOCATOR2 = 'fc00:0:2::/48'
LOCATOR3 = 'fc00:0:3::/48'


class TestEdgeFilterRendering(base.BaseTestCase):
    """The ruleset text. Checked with `nft -c` against nftables 1.0.9."""

    def _render(self, *locators):
        return dataplane.render_edge_ruleset(
            dataplane._locator_networks(locators))

    def test_verbatim(self):
        self.assertEqual(
            'add table inet srv6_edge\n'
            'delete table inet srv6_edge\n'
            'table inet srv6_edge {\n'
            '  set sid_space {\n'
            '    type ipv6_addr\n'
            '    flags interval\n'
            '    auto-merge\n'
            '    elements = { fc00:0:1::/48, fc00:0:2::/48 }\n'
            '  }\n'
            '  chain prerouting {\n'
            '    type filter hook prerouting priority raw; policy accept;\n'
            '    iifname "svp-*" ip6 daddr @sid_space counter drop\n'
            '    iifname "svp-*" rt type 4 counter drop\n'
            '  }\n'
            '}\n',
            self._render(LOCATOR, LOCATOR2))

    def test_the_table_is_replaced_in_one_transaction(self):
        # `delete` alone fails on a table that does not exist yet.
        self.assertTrue(self._render(LOCATOR).startswith(
            'add table inet srv6_edge\ndelete table inet srv6_edge\n'))

    def test_the_same_set_renders_the_same_text(self):
        self.assertEqual(self._render(LOCATOR, LOCATOR2, LOCATOR3),
                         self._render(LOCATOR3, LOCATOR, LOCATOR2, LOCATOR))

    def test_locators_are_sorted_and_deduplicated(self):
        self.assertIn('elements = { fc00:0:1::/48, fc00:0:3::/48 }',
                      self._render(LOCATOR3, LOCATOR, LOCATOR3))

    def test_the_drops_match_only_the_gateway_ports(self):
        # Never the underlay: the agent's own encapsulated traffic and every
        # other node's arrive there.
        for line in self._render(LOCATOR).splitlines():
            if line.strip().endswith('drop'):
                self.assertTrue(line.strip().startswith('iifname "svp-*" '),
                                line)

    def test_an_empty_set_has_no_elements_line(self):
        # nft refuses `elements = { }`.
        self.assertNotIn('elements', dataplane.render_edge_ruleset([]))

    def test_an_unparseable_locator_is_left_out(self):
        self.assertIn('elements = { fc00:0:1::/48 }',
                      self._render(LOCATOR, 'bogus', None))


class TestEdgeFilter(DataplaneTestCaseBase):
    """Applied at initialize_node, active only after it succeeds."""

    def _applied(self):
        return self.priv_nft.apply_ruleset.call_args[0][0]

    def test_inactive_until_applied(self):
        self.assertFalse(self.dp.edge_filter_active)

    def test_applied_at_initialize_node_with_this_nodes_locator(self):
        self.dp.initialize_node()
        self.priv_nft.apply_ruleset.assert_called_once_with(mock.ANY)
        self.assertIn('elements = { fc00:0:1::/48 }', self._applied())
        self.assertTrue(self.dp.edge_filter_active)

    def test_this_nodes_locator_is_always_included(self):
        self.dp.ensure_edge_filter([LOCATOR2])
        self.assertIn('elements = { fc00:0:1::/48, fc00:0:2::/48 }',
                      self._applied())

    def test_every_call_replaces_it(self):
        # Idempotent, and it restores a table somebody deleted by hand.
        self.dp.ensure_edge_filter([LOCATOR2])
        self.dp.ensure_edge_filter([LOCATOR2])
        self.assertEqual(2, self.priv_nft.apply_ruleset.call_count)

    def test_a_failed_apply_leaves_it_inactive(self):
        self.priv_nft.apply_ruleset.side_effect = RuntimeError('boom')
        self.assertFalse(self.dp.ensure_edge_filter([LOCATOR2]))
        self.assertFalse(self.dp.edge_filter_active)

    def test_a_missing_nft_is_a_failure_too(self):
        self.priv_nft.apply_ruleset.side_effect = FileNotFoundError('nft')
        self.assertFalse(self.dp.ensure_edge_filter([]))
        self.assertFalse(self.dp.edge_filter_active)

    def test_a_later_failure_deactivates_it(self):
        self.assertTrue(self.dp.ensure_edge_filter([LOCATOR2]))
        self.priv_nft.apply_ruleset.side_effect = RuntimeError('boom')
        self.dp.ensure_edge_filter([LOCATOR2])
        self.assertFalse(self.dp.edge_filter_active)

    def test_a_failure_does_not_stop_initialize_node(self):
        self.priv_nft.apply_ruleset.side_effect = RuntimeError('boom')
        self.dp.initialize_node()
        self.assertTrue(self.priv_seg6.replace_locator_route.called)


class TestEndSidRules(DataplaneTestCaseBase):
    """A steered route's first End SID gets a rule -- behind the filter.

    A rule toward an End SID lets the VRF's outer headers reach it, and
    without the filter a tenant could send its own SRH that way into
    another domain.
    """

    RED = 'sv6vrf-7'

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def _route(self, prefix='10.0.0.5/32', segments=None):
        route = {'prefix': prefix, 'host': 'node2', 'port_id': 'p'}
        if segments is not None:
            route['segments'] = segments
        return route

    def _sync(self, *routes):
        self.dp.sync_routes(self.domain_id, list(routes), self.locators,
                            'node1')

    def test_no_filter_no_rule(self):
        self._sync(self._route(segments=['fc00:0:2:fff0::']))
        self.assertEqual(set(), self.kernel.sid_rules)

    def test_a_failed_filter_no_rule(self):
        self.priv_nft.apply_ruleset.side_effect = RuntimeError('boom')
        self.dp.ensure_edge_filter([LOCATOR2])
        self._sync(self._route(segments=['fc00:0:2:fff0::']))
        self.assertEqual(set(), self.kernel.sid_rules)

    def test_via_the_destination_gets_its_end_sid_rule(self):
        # The G2 shape: the only depth-2 SRH two nodes can produce.
        self.dp.ensure_edge_filter([LOCATOR2])
        self._sync(self._route(segments=['fc00:0:2:fff0::']))
        self.assertEqual({(self.RED, 'fc00:0:2:fff0::')},
                         self.kernel.sid_rules)
        self.priv_seg6.replace_seg6_route.assert_called_once_with(
            '10.0.0.5/32', ['fc00:0:2:fff0::', 'fc00:0:2:7::'], UNDERLAY,
            VRF_TABLE, family_v6=False)

    def test_only_the_first_transit_gets_a_rule(self):
        # Later segments are reached from main by each waypoint's End.
        self.dp.ensure_edge_filter([LOCATOR2, LOCATOR3])
        self._sync(self._route(segments=['fc00:0:3:fff0::',
                                         'fc00:0:2:fff0::']))
        self.assertEqual({(self.RED, 'fc00:0:3:fff0::')},
                         self.kernel.sid_rules)

    def test_a_neighbour_keeps_its_decap_rule(self):
        self.dp.ensure_edge_filter([LOCATOR2])
        self._sync(self._route('10.0.0.5/32', ['fc00:0:2:fff0::']),
                   self._route('10.0.0.6/32'))
        self.assertEqual({(self.RED, 'fc00:0:2:fff0::'),
                          (self.RED, 'fc00:0:2:7::')}, self.kernel.sid_rules)
        self.assertEqual(set(), self.kernel.blackholes)

    def test_a_leading_own_end_sid_is_not_the_rule(self):
        self.dp.ensure_edge_filter([LOCATOR3])
        self._sync(self._route(segments=['fc00:0:1:fff0::',
                                         'fc00:0:3:fff0::']))
        self.assertEqual({(self.RED, 'fc00:0:3:fff0::')},
                         self.kernel.sid_rules)

    def test_an_end_sid_the_filter_does_not_cover_gets_no_rule(self):
        # A routes_updated racing the domain_updated that carries node3's
        # locator: the filter is active, but not over node3 yet.
        self.dp.ensure_edge_filter([LOCATOR2])
        self._sync(self._route(segments=['fc00:0:3:fff0::']))
        self.assertEqual(set(), self.kernel.sid_rules)

    def test_removing_the_path_prunes_the_rule(self):
        # G4: PUT via_hosts [] -> the next full sync carries no segments.
        self.dp.ensure_edge_filter([LOCATOR2])
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:fff0::'),
                                 (self.RED, 'fc00:0:2:7::')}
        self._sync(self._route())
        self.assertEqual({(self.RED, 'fc00:0:2:7::')}, self.kernel.sid_rules)

    def test_a_filter_that_goes_inactive_prunes_the_rule(self):
        self.dp.ensure_edge_filter([LOCATOR2])
        self.priv_nft.apply_ruleset.side_effect = RuntimeError('boom')
        self.dp.ensure_edge_filter([LOCATOR2])
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:fff0::')}
        self._sync(self._route(segments=['fc00:0:2:fff0::']))
        self.assertEqual(set(), self.kernel.sid_rules)
        # The route itself stays, sealed: fail closed, not fail open.
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(VRF_TABLE)))


class TestSourceSegments(DataplaneTestCaseBase):
    """A source-specific TE path: each ingress takes its own entry.

    The payload is the same on every node; `segments_by_source` holds the
    transit list of each ingress a source path names, and `segments` the
    list for every other ingress. The server decides precedence; the
    agent only picks the entry of its own host (_segments_for).
    """

    RED = 'sv6vrf-7'

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def _route(self, host='node2', segments=None, by_source=None):
        route = {'prefix': '10.0.0.5/32', 'host': host, 'port_id': 'p'}
        if segments is not None:
            route['segments'] = segments
        if by_source is not None:
            route['segments_by_source'] = by_source
        return route

    def _segs(self, route, local_host='node1'):
        self.dp.sync_routes(self.domain_id, [route], self.locators,
                            local_host)
        return self.kernel.table(VRF_TABLE)['10.0.0.5']

    def test_the_own_entry_is_used(self):
        # node1 is the source the path names: its list, not the default.
        self.assertEqual(
            ['fc00:0:3:fff0::', 'fc00:0:2:7::'],
            self._segs(self._route(by_source={'node1': ['fc00:0:3:fff0::']})))

    def test_without_an_own_entry_the_default_is_used(self):
        # The path names node9 as its source; node1 is another ingress.
        self.assertEqual(
            ['fc00:0:2:fff0::', 'fc00:0:2:7::'],
            self._segs(self._route(segments=['fc00:0:2:fff0::'],
                                   by_source={'node9': ['fc00:0:3:fff0::']})))

    def test_an_empty_own_entry_is_direct_over_the_default(self):
        # An explicit "direct from node1": membership, not truth, decides.
        self.assertEqual(
            ['fc00:0:2:7::'],
            self._segs(self._route(segments=['fc00:0:3:fff0::'],
                                   by_source={'node1': []})))

    def test_the_own_entry_wins_over_the_default(self):
        self.assertEqual(
            ['fc00:0:3:fff0::', 'fc00:0:2:7::'],
            self._segs(self._route(segments=['fc00:0:2:fff0::'],
                                   by_source={'node1': ['fc00:0:3:fff0::']})))

    def test_a_leading_own_sid_in_the_own_entry_is_dropped(self):
        # via == source: the same strip as for any-source segments.
        self.assertEqual(
            ['fc00:0:3:fff0::', 'fc00:0:2:7::'],
            self._segs(self._route(by_source={
                'node1': ['fc00:0:1:fff0::', 'fc00:0:3:fff0::']})))

    def test_a_local_route_is_skipped_even_with_an_own_entry(self):
        # The destination's own node never encapsulates its VMs.
        self.dp.sync_routes(
            self.domain_id,
            [self._route(host='node1',
                         by_source={'node1': ['fc00:0:3:fff0::']})],
            dict(self.locators, node1=LOCATOR), 'node1')
        self.assertEqual({}, self.kernel.table(VRF_TABLE))

    def test_the_rule_follows_the_chosen_first_segment(self):
        self.dp.ensure_edge_filter([LOCATOR2, LOCATOR3])
        self._segs(self._route(segments=['fc00:0:2:fff0::'],
                               by_source={'node1': ['fc00:0:3:fff0::']}))
        self.assertEqual({(self.RED, 'fc00:0:3:fff0::')},
                         self.kernel.sid_rules)

    def test_the_rule_of_the_old_list_is_pruned(self):
        # The source path appeared: the any-source End SID rule is no
        # longer used by this node.
        self.dp.ensure_edge_filter([LOCATOR2, LOCATOR3])
        self.kernel.sid_rules = {(self.RED, 'fc00:0:2:fff0::')}
        self._segs(self._route(segments=['fc00:0:2:fff0::'],
                               by_source={'node1': ['fc00:0:3:fff0::']}))
        self.assertEqual({(self.RED, 'fc00:0:3:fff0::')},
                         self.kernel.sid_rules)


class TestRouteMtu(DataplaneTestCaseBase):
    """A steered route's MTU, from its network, warned once."""

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.device.link.mtu = 1500
        check = mock.patch.object(self.dp, 'check_mtu',
                                  wraps=self.dp.check_mtu)
        self.check_mtu = check.start()
        self.addCleanup(check.stop)

    def _route(self, segments=None):
        route = {'prefix': '10.0.0.5/32', 'host': 'node2', 'port_id': 'p',
                 'network_id': 'net1'}
        if segments:
            route['segments'] = segments
        return route

    def _sync(self, route, mtu):
        self.dp.sync_routes(self.domain_id, [route], {'node2': LOCATOR2},
                            'node1', mtus={'net1': mtu})

    def test_a_steered_route_that_does_not_fit_warns_once(self):
        # 1436 fits at depth 1 (+64) and not at depth 2 (+80).
        for _ in range(3):
            self._sync(self._route(['fc00:0:2:fff0::']), 1436)
        self.check_mtu.assert_called_once_with(1436, segment_count=2,
                                               prefix='10.0.0.5/32',
                                               dev=UNDERLAY)

    def test_a_steered_route_that_fits_is_checked_and_quiet(self):
        self._sync(self._route(['fc00:0:2:fff0::']), 1400)
        self.check_mtu.assert_called_once_with(1400, segment_count=2,
                                               prefix='10.0.0.5/32',
                                               dev=UNDERLAY)
        self.assertEqual(set(), self.dp._mtu_warned)

    def test_a_deeper_path_warns_again(self):
        self._sync(self._route(['fc00:0:2:fff0::']), 1436)
        self._sync(self._route(['fc00:0:3:fff0::', 'fc00:0:2:fff0::']),
                   1436)
        self.assertEqual({('10.0.0.5/32', 2), ('10.0.0.5/32', 3)},
                         self.dp._mtu_warned)

    def test_depth_one_is_left_to_the_per_network_check(self):
        self._sync(self._route(), 1450)
        self.check_mtu.assert_not_called()

    def test_a_network_with_no_known_mtu_is_not_checked(self):
        self.dp.sync_routes(self.domain_id,
                            [self._route(['fc00:0:2:fff0::'])],
                            {'node2': LOCATOR2}, 'node1')
        self.check_mtu.assert_not_called()


# ----------------------------------------------------------------------
# microSID
# ----------------------------------------------------------------------
UN_LINE = ('fc00:0:1::/48  encap seg6local action End flavors next-csid '
           'lblen 32 nflen 16 dev eth0 metric 1024 pref medium')
LIB = 'fc00:0:1:e000::/52'


class TestUsidNode(DataplaneTestCaseBase):
    """Node state per encoding: the /48, the LIB seal."""

    def _un_installed(self):
        self.priv_seg6.replace_seg6local_route.assert_any_call(
            'fc00:0:1::', 'End', UNDERLAY, prefixlen=48, next_csid=(32, 16))

    def test_a_fresh_node_starts_on_full_sids(self):
        self.dp.initialize_node()
        self.assertEqual('encap', self.dp.encoding)
        self.priv_seg6.delete_main_route.assert_called_once_with(
            LIB, route_type='unreachable')
        self.priv_seg6.replace_locator_route.assert_called_once_with(LOCATOR)

    def test_a_restart_in_a_usid_deployment_keeps_the_un(self):
        # No `local` route put back for the seconds until the first payload.
        self.priv_seg6.show_main_route.return_value = [UN_LINE]
        self.dp.initialize_node()
        self.assertEqual('usid', self.dp.encoding)
        self.priv_seg6.replace_locator_route.assert_not_called()
        self._un_installed()

    def test_capabilities_are_probed(self):
        self.dp.initialize_node()
        self.assertEqual(['encap-red', 'next-csid'], self.dp.capabilities)

    def test_a_failed_probe_reports_none(self):
        self.priv_seg6.probe_capabilities.side_effect = RuntimeError('x')
        self.dp.initialize_node()
        self.assertEqual([], self.dp.capabilities)

    def test_switch_to_usid_replaces_the_local_route(self):
        self.assertTrue(self.dp.ensure_encoding('usid'))
        self.priv_seg6.delete_main_route.assert_called_once_with(
            LOCATOR, route_type='local')
        self._un_installed()
        self.priv_seg6.replace_unreachable_route.assert_called_once_with(LIB)
        self.priv_seg6.replace_locator_route.assert_not_called()

    def test_switch_back_restores_the_local_route(self):
        self.dp.ensure_encoding('usid')
        self.priv_seg6.reset_mock()
        self.priv_seg6.show_main_route.return_value = [UN_LINE]
        self.assertTrue(self.dp.ensure_encoding('encap'))
        self.assertEqual(
            [mock.call(LOCATOR), mock.call(LIB, route_type='unreachable')],
            self.priv_seg6.delete_main_route.call_args_list)
        self.priv_seg6.replace_locator_route.assert_called_once_with(LOCATOR)

    def test_the_same_encoding_is_a_no_op(self):
        self.assertFalse(self.dp.ensure_encoding('encap'))
        self.priv_seg6.show_main_route.assert_not_called()

    def test_an_unknown_encoding_is_refused(self):
        self.assertFalse(self.dp.ensure_encoding('replace-csid'))
        self.assertEqual('encap', self.dp.encoding)

    def test_a_locator_that_is_not_f3216_stays_on_full_sids(self):
        self.dp.locator = 'fc00:0:1:2::/64'
        self.assertFalse(self.dp.ensure_encoding('usid'))
        self.assertEqual('encap', self.dp.encoding)
        self.priv_seg6.replace_seg6local_route.assert_not_called()

    def test_a_switch_moves_known_decap_sids_to_the_new_length(self):
        self._with_domain()
        self.dp.ensure_encoding('usid')
        self.priv_seg6.delete_route.assert_called_once_with(
            'fc00:0:1:7::/128', 'main', family_v6=True)

    def test_transit_sid_is_the_un(self):
        self.assertEqual('fc00:0:1:fff0::', self.dp.own_transit_sid)
        self.dp.ensure_encoding('usid')
        self.assertEqual('fc00:0:1::', self.dp.own_transit_sid)


class TestUsidDomain(DataplaneTestCaseBase):

    def setUp(self):
        super().setUp()
        self.dp.ensure_encoding('usid')
        self.priv_seg6.reset_mock()
        self.ip_lib.device_exists.return_value = True

    def test_the_decap_sid_is_a_64(self):
        self.dp.ensure_domain(self.domain_id, 0xe005, 'End.DT46')
        self.priv_seg6.replace_seg6local_route.assert_called_once_with(
            'fc00:0:1:e005::', 'End.DT46', 'sv6vrf-57349',
            vrf_table=10000 + 0xe005, prefixlen=64)


class TestUsidRoutes(DataplaneTestCaseBase):
    """Packing at the ingress, the rule on the first container."""

    LOCATORS = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def setUp(self):
        super().setUp()
        self.dp.ensure_encoding('usid')
        self.dp.domains[self.domain_id] = dataplane.DomainState(
            self.domain_id, 0xe005, 10000 + 0xe005, 'sv6vrf-57349')
        self.dp.ensure_edge_filter(sorted(self.LOCATORS.values()))
        self.priv_seg6.reset_mock()
        self.priv_seg6.list_sid_rules.return_value = []

    def _route(self, segments=None):
        self.dp.sync_routes(
            self.domain_id,
            [{'prefix': '10.0.0.5/32', 'host': 'node3', 'port_id': 'p1',
              'segments': segments}],
            self.LOCATORS, 'node1')
        args, kwargs = self.priv_seg6.replace_seg6_route.call_args
        return args[1], kwargs.get('mode')

    def test_direct_pushes_the_decap_sid_reduced(self):
        self.assertEqual((['fc00:0:3:e005::'], 'encap.red'), self._route())

    def test_a_te_path_is_one_container(self):
        self.assertEqual((['fc00:0:2:3:e005::'], 'encap.red'),
                         self._route(['fc00:0:2::']))

    def test_the_rule_is_on_the_container(self):
        self._route(['fc00:0:2::'])
        self.priv_seg6.add_sid_rule.assert_called_once_with(
            'sv6vrf-57349', 'fc00:0:2:3:e005::')

    def test_a_source_entry_is_packed_too(self):
        self.dp.sync_routes(
            self.domain_id,
            [{'prefix': '10.0.0.5/32', 'host': 'node3', 'port_id': 'p1',
              'segments_by_source': {'node1': ['fc00:0:2::']}}],
            self.LOCATORS, 'node1')
        args, kwargs = self.priv_seg6.replace_seg6_route.call_args
        self.assertEqual((['fc00:0:2:3:e005::'], 'encap.red'),
                         (args[1], kwargs.get('mode')))

    def test_a_leading_own_un_is_dropped(self):
        self.assertEqual((['fc00:0:2:3:e005::'], 'encap.red'),
                         self._route(['fc00:0:1::', 'fc00:0:2::']))

    def test_an_unpackable_list_goes_out_as_full_sids(self):
        self.assertEqual((['fc01:0:9::', 'fc00:0:3:e005::'], 'encap.red'),
                         self._route(['fc01:0:9::']))

    def test_a_one_container_te_path_costs_a_direct_route(self):
        self.ip_lib.IPDevice.return_value.link.mtu = 1500
        self.assertTrue(self.dp.check_mtu(1460))
        self.assertFalse(self.dp.check_mtu(1461))


class TestUnderlayAutodetect(DataplaneTestCaseBase):
    """underlay_interface unset: the device comes from `ip route get`."""

    DEVS = {'fc00:0:2::': 'wlan0', 'fc00:0:3::': 'eth1'}
    LOCATORS = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def setUp(self):
        super().setUp()
        self.priv_seg6.route_get_dev.side_effect = self.DEVS.get
        self.dp = dataplane.Srv6LinuxDataplane(
            locator=LOCATOR, underlay_interface=None,
            vrf_table_base=10000, bridge=self.bridge)

    def _route_devs(self, routes):
        self.dp.refresh_underlay([LOCATOR, LOCATOR2, LOCATOR3])
        self.priv_seg6.replace_seg6_route.reset_mock()
        self.dp.sync_routes(self.domain_id, routes, self.LOCATORS, 'node1')
        return {c[0][0]: c[0][2] for c in
                self.priv_seg6.replace_seg6_route.call_args_list}

    def test_the_override_skips_detection(self):
        dp = dataplane.Srv6LinuxDataplane(
            locator=LOCATOR, underlay_interface=UNDERLAY,
            vrf_table_base=10000, bridge=self.bridge)
        dp.refresh_underlay([LOCATOR2])
        self.priv_seg6.route_get_dev.assert_not_called()

    def test_peers_are_resolved_but_not_this_node(self):
        self.dp.refresh_underlay([LOCATOR, LOCATOR2])
        self.priv_seg6.route_get_dev.assert_called_once_with('fc00:0:2::')
        self.assertEqual({LOCATOR2: 'wlan0'}, self.dp.underlay_devs)

    def test_every_device_gets_the_sysctls(self):
        self.dp.refresh_underlay([LOCATOR2, LOCATOR3])
        keys = [c[0][0] for c in self.set_sysctl.call_args_list]
        for dev in ('wlan0', 'eth1'):
            self.assertIn('net.ipv6.conf.%s.seg6_enabled' % dev, keys)
            self.assertIn('net.ipv4.conf.%s.rp_filter' % dev, keys)

    def test_each_route_leaves_by_its_peers_device(self):
        self._with_domain()
        self.assertEqual(
            {'10.0.0.2/32': 'wlan0', '10.0.0.3/32': 'eth1'},
            self._route_devs([
                {'prefix': '10.0.0.2/32', 'host': 'node2', 'port_id': 'p2'},
                {'prefix': '10.0.0.3/32', 'host': 'node3', 'port_id': 'p3'},
            ]))

    def test_a_usid_te_path_leaves_towards_its_first_waypoint(self):
        self.dp.ensure_encoding('usid')
        self._with_domain()
        self.assertEqual({'10.0.0.3/32': 'wlan0'}, self._route_devs([
            {'prefix': '10.0.0.3/32', 'host': 'node3', 'port_id': 'p3',
             'segments': ['fc00:0:2::']}]))

    def test_a_peer_without_a_route_is_skipped_alone(self):
        self.priv_seg6.route_get_dev.side_effect = {
            'fc00:0:2::': 'wlan0'}.get
        self._with_domain()
        self.assertEqual({'10.0.0.2/32': 'wlan0'}, self._route_devs([
            {'prefix': '10.0.0.2/32', 'host': 'node2', 'port_id': 'p2'},
            {'prefix': '10.0.0.3/32', 'host': 'node3', 'port_id': 'p3'},
        ]))

    def test_the_sids_move_off_lo_once_a_device_is_known(self):
        # A seg6local route via lo is a reject route: the SID would drop
        # everything (measured, kernel 6.8).
        self.dp.initialize_node()
        self.priv_seg6.replace_seg6local_route.reset_mock()
        self.dp.refresh_underlay([LOCATOR2, LOCATOR3])
        self.priv_seg6.replace_seg6local_route.assert_called_once_with(
            'fc00:0:1:fff0::', 'End', 'eth1')
        # Unchanged on the next resync: not reinstalled.
        self.priv_seg6.replace_seg6local_route.reset_mock()
        self.dp.refresh_underlay([LOCATOR2, LOCATOR3])
        self.priv_seg6.replace_seg6local_route.assert_not_called()

    def test_mtu_is_the_smallest_over_the_devices(self):
        mtus = {'wlan0': 1500, 'eth1': 9000}
        self.ip_lib.IPDevice.side_effect = \
            lambda d: mock.Mock(**{'link.mtu': mtus[d]})
        self.dp.refresh_underlay([LOCATOR2, LOCATOR3])
        self.assertFalse(self.dp.check_mtu(1450))
        self.assertTrue(self.dp.check_mtu(1450, dev='eth1'))


class TestSteering(DataplaneTestCaseBase):
    """Steer tables and rules for source VMs.

    The routes carry segments_by_source_port; the VM is on node1 (here),
    its network net1's gateway port is svp-7-100, and the steer tables
    start at vrf_table_base + 0x10000 = 75536.
    """

    GW = 'svp-7-100'
    BASE = 10000 + 0x10000

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.locators = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def _route(self, prefix='10.0.0.5/32', host='node2', vm_host='node1',
               segments=('fc00:0:3:fff0::',), port='vm1',
               prefixes=('10.0.1.9/32',)):
        return {'prefix': prefix, 'host': host, 'port_id': 'p',
                'segments_by_source_port': {port: {
                    'host': vm_host, 'network_id': 'net1',
                    'prefixes': list(prefixes),
                    'segments': list(segments)}}}

    def _sync(self, *routes, ports=None):
        self.dp.sync_routes(self.domain_id, list(routes), self.locators,
                            'node1', gateway_ports={'net1': self.GW}
                            if ports is None else ports)

    def _filter_on(self):
        self.dp.ensure_edge_filter([LOCATOR2, LOCATOR3])

    def test_a_local_vm_gets_a_table_its_route_and_a_rule(self):
        self._sync(self._route())
        # the VRF route is untouched by the VM path: direct to node2
        self.assertEqual({'10.0.0.5': ['fc00:0:2:7::']},
                         self.kernel.table(VRF_TABLE))
        self.assertEqual({'10.0.0.5': ['fc00:0:3:fff0::', 'fc00:0:2:7::']},
                         self.kernel.table(self.BASE))
        self.assertEqual({('10.0.1.9', self.GW, self.BASE, False)},
                         self.kernel.steer_rules)

    def test_no_steered_packet_meets_the_seal(self):
        # The steer table's route and SID rule exist before the steer
        # rule that sends traffic there.
        self._filter_on()
        self._sync(self._route())
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertIn((VRF_NAME, 'fc00:0:3:fff0::'), self.kernel.sid_rules)

    def test_the_steer_sid_rule_is_not_pruned(self):
        # Without the rule the steered flow never connected
        self._filter_on()
        self.kernel.sid_rules = {(VRF_NAME, 'fc00:0:2:7::'),
                                 (VRF_NAME, 'fc00:0:3:fff0::')}
        self._sync(self._route())
        self.priv_seg6.delete_sid_rule.assert_not_called()

    def test_a_vm_on_another_node_or_without_a_gateway_port_here(self):
        self._sync(self._route(vm_host='node3'))
        self._sync(self._route(), ports={})
        self.assertEqual(set(), self.kernel.steer_rules)
        self.assertEqual([(VRF_TABLE, False)], list(self.kernel.routes))

    def test_two_addresses_two_rules_one_table(self):
        self._sync(self._route(prefixes=('10.0.1.9/32', 'fd00::9/128')))
        self.assertEqual({('10.0.1.9', self.GW, self.BASE, False),
                          ('fd00::9', self.GW, self.BASE, True)},
                         self.kernel.steer_rules)

    def test_two_vms_two_tables(self):
        self._sync(self._route(),
                   self._route(prefix='10.0.0.6/32', port='vm2',
                               prefixes=('10.0.1.10/32',)))
        self.assertEqual({self.BASE, self.BASE + 1},
                         {t for _p, _i, t, _v in self.kernel.steer_rules})

    def test_a_new_destination_joins_the_vms_table(self):
        self._sync(self._route())
        self._sync(self._route(), self._route(prefix='10.0.0.6/32'))
        self.assertEqual(['10.0.0.5', '10.0.0.6'],
                         sorted(self.kernel.table(self.BASE)))
        self.assertEqual({('10.0.1.9', self.GW, self.BASE, False)},
                         self.kernel.steer_rules)

    def test_a_vms_existing_table_is_reused_after_a_restart(self):
        self.kernel.steer_rules = {('10.0.1.9', self.GW, self.BASE + 7,
                                    False)}
        self._sync(self._route())
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(self.BASE + 7)))
        self.priv_seg6.add_steer_rule.assert_not_called()   # already there
        self.priv_seg6.delete_steer_rule.assert_not_called()

    def test_tables_of_other_domains_are_skipped(self):
        theirs = ('10.9.9.9', 'svp-8-100', self.BASE, False)
        self.kernel.steer_rules = {theirs}
        self._sync(self._route())
        self.assertEqual({theirs, ('10.0.1.9', self.GW, self.BASE + 1,
                                   False)}, self.kernel.steer_rules)

    def test_a_vm_no_longer_steered_loses_its_rules_and_table(self):
        self.kernel.steer_rules = {('10.0.1.9', self.GW, self.BASE, False),
                                   ('fd00::9', self.GW, self.BASE, True)}
        self.kernel.routes[(self.BASE, False)] = {
            '10.0.0.5': ['fc00:0:3:fff0::', 'fc00:0:2:7::']}
        self._sync({'prefix': '10.0.0.5/32', 'host': 'node2',
                    'port_id': 'p'})
        self.assertEqual(set(), self.kernel.steer_rules)
        self.assertEqual({}, self.kernel.table(self.BASE))

    def test_a_stale_route_and_rule_in_a_kept_table_are_pruned(self):
        self.kernel.steer_rules = {('10.0.1.9', self.GW, self.BASE, False),
                                   ('10.0.1.8', self.GW, self.BASE, False)}
        self.kernel.routes[(self.BASE, False)] = {
            '10.0.0.5': ['fc00:0:2:7::'], '10.0.0.99': ['fc00:0:2:7::']}
        self._sync(self._route())
        self.assertEqual({('10.0.1.9', self.GW, self.BASE, False)},
                         self.kernel.steer_rules)
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(self.BASE)))

    def test_a_steered_route_with_no_underlay_device_is_kept(self):
        self.kernel.steer_rules = {('10.0.1.9', self.GW, self.BASE, False)}
        self.kernel.routes[(self.BASE, False)] = {
            '10.0.0.5': ['fc00:0:3:fff0::', 'fc00:0:2:7::']}
        self.dp.underlay_interface = None
        self._sync(self._route())
        self.assertEqual(['10.0.0.5'], list(self.kernel.table(self.BASE)))

    def test_unreadable_rules_change_nothing(self):
        self.priv_seg6.list_steer_rules.side_effect = RuntimeError('boom')
        self._sync(self._route())
        self.priv_seg6.add_steer_rule.assert_not_called()

    def test_no_free_table_is_logged(self):
        self.kernel.steer_rules = {
            ('10.9.9.%d' % (i % 250), 'svp-8-1', self.BASE + i, False)
            for i in range(dataplane.constants.STEER_TABLE_SLOTS)}
        with mock.patch.object(dataplane.LOG, 'error') as error:
            self._sync(self._route())
        self.assertIn('no free steer table', error.call_args[0][0])
        self.priv_seg6.add_steer_rule.assert_not_called()

    def test_deleting_the_domain_removes_only_its_steering(self):
        theirs = ('10.9.9.9', 'svp-8-100', self.BASE + 1, False)
        self.kernel.steer_rules = {('10.0.1.9', self.GW, self.BASE, False),
                                   theirs}
        self.kernel.routes[(self.BASE, False)] = {'10.0.0.5': ['x']}
        self.kernel.routes[(self.BASE + 1, False)] = {'10.0.0.5': ['y']}
        self.dp.delete_domain(self.domain_id)
        self.assertEqual({theirs}, self.kernel.steer_rules)
        self.assertEqual({}, self.kernel.table(self.BASE))
        self.assertEqual({'10.0.0.5': ['y']},
                         self.kernel.table(self.BASE + 1))


class TestMakeBeforeBreak(DataplaneTestCaseBase):
    """Every change of a domain leaves no reachable route without its rule.

    The edge filter covers every node, so no path is sealed by design and
    any window FakeKernel records is a real one.
    """

    GW = 'svp-7-100'

    def setUp(self):
        super().setUp()
        self._with_domain()
        self._fake_kernel()
        self.dp.ensure_edge_filter([LOCATOR2, LOCATOR3])
        self.locators = {'node2': LOCATOR2, 'node3': LOCATOR3}

    def _sync(self, segments=None, vm_segments=None, host='node2'):
        route = {'prefix': '10.0.0.5/32', 'host': host, 'port_id': 'p'}
        if segments is not None:
            route['segments'] = segments
        if vm_segments is not None:
            route['segments_by_source_port'] = {'vm1': {
                'host': 'node1', 'network_id': 'net1',
                'prefixes': ['10.0.1.9/32'], 'segments': vm_segments}}
        self.dp.sync_routes(self.domain_id, [route],
                            dict(self.locators, node1=LOCATOR), 'node1',
                            gateway_ports={'net1': self.GW})

    def test_a_route_re_steered_and_back(self):
        self._sync()
        self._sync(segments=['fc00:0:3:fff0::'])
        self._sync(segments=['fc00:0:2:fff0::'])
        self._sync()
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertEqual({(VRF_NAME, 'fc00:0:2:7::')}, self.kernel.sid_rules)

    def test_a_source_vm_path_that_moves_then_goes(self):
        self._sync(vm_segments=['fc00:0:3:fff0::'])
        self._sync(vm_segments=['fc00:0:2:fff0::'])
        self._sync()
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertEqual(set(), self.kernel.steer_rules)

    def test_a_workload_that_moves_here(self):
        self._sync(vm_segments=['fc00:0:3:fff0::'])
        self._sync(host='node1')
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertEqual({}, self.kernel.table(VRF_TABLE))
        self.assertEqual(set(), self.kernel.sid_rules)

    def test_a_domain_torn_down(self):
        self._sync(segments=['fc00:0:3:fff0::'],
                   vm_segments=['fc00:0:2:fff0::'])
        self.dp.delete_domain(self.domain_id)
        self.assertEqual(set(), self.kernel.blackholes)
        self.assertEqual(set(), self.kernel.sid_rules)
        self.assertEqual(set(), self.kernel.steer_rules)
