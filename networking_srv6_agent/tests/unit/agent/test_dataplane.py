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

Everything that touches the kernel is mocked, so what is under test is the
*shape* of the calls: which objects get created, with which names, in which
order. That is precisely where this layer went wrong during bring-up -- the
kernel calls themselves were fine, the arguments and the ordering were not.

Ported near-verbatim from networking-bgpvpn (srv6-te); the classes after
TestMtu cover what P5 added (P5-PLAN.md 4).
"""

from unittest import mock

from neutron.tests import base
from oslo_utils import uuidutils

from networking_srv6_agent.agent import dataplane


LOCATOR = 'fc00:0:1::/48'
UNDERLAY = 'eth0'
FUNCTION_ID = 7
VRF_TABLE = 10007
VRF_NAME = 'sv6vrf-7'
PORT_NAME = 'svp-7-100'


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
        # Sysctls are exercised by their own test case; everywhere else they
        # would only reach into the test machine's /proc.
        sysctl = mock.patch.object(dataplane.Srv6LinuxDataplane,
                                   '_set_sysctl')
        self.set_sysctl = sysctl.start()
        self.addCleanup(sysctl.stop)

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

    def test_end_sid_falls_back_to_lo_without_an_underlay(self):
        # An unset underlay_interface is only a warning at config
        # validation, so it must not stop this node being a transit node.
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
        self.priv_seg6.delete_route.assert_called_once_with(
            'fc00:0:1:7::/128', 'main', family_v6=True)
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
        self.assertEqual([mock.call('10.0.0.1/24'),
                          mock.call('2001:db8::1/64')],
                         self.device.addr.add.call_args_list)

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
        self.dp.reconcile_gateway_ports(self.domain_id, {100}, True)
        self.bridge.delete_port.assert_called_once_with('svp-7-101')

    def test_the_networks_that_stayed_keep_theirs(self):
        self.dp.reconcile_gateway_ports(self.domain_id, {100, 101}, True)
        self.bridge.delete_port.assert_not_called()

    def test_another_domains_ports_are_never_touched(self):
        # svp-9-100 belongs to function id 9. Matching on the bare prefix
        # would delete it.
        self.dp.reconcile_gateway_ports(self.domain_id, {100, 101}, True)
        self.dp.reconcile_gateway_ports(self.domain_id, set(), False)
        for call in self.bridge.delete_port.call_args_list:
            self.assertTrue(call[0][0].startswith('svp-7-'))

    def test_the_last_network_leaving_empties_the_domain(self):
        # The case in the evidence file: the domain survives with no
        # networks.
        self.dp.reconcile_gateway_ports(self.domain_id, set(), False)
        self.assertEqual(
            sorted([mock.call('svp-7-100'), mock.call('svp-7-101')], key=str),
            sorted(self.bridge.delete_port.call_args_list, key=str))
        # The domain itself still exists, so its VRF and SID stay.
        self.priv_ip_lib.delete_interface.assert_not_called()
        self.priv_seg6.delete_route.assert_not_called()

    def test_ports_left_behind_by_a_previous_agent_are_collected(self):
        """The reason the bridge is asked instead of self.gateway_ports.

        Measured on node 1 on 2026-09-03: after a restart the in-memory map
        is empty while the OVS ports are all still there, so a map-based
        reconciliation deleted nothing and the leak survived every restart.
        """
        self.assertEqual({}, self.dp.gateway_ports[self.domain_id])
        self.dp.reconcile_gateway_ports(self.domain_id, {100}, True)
        self.bridge.delete_port.assert_called_once_with('svp-7-101')

    def test_a_vlan_map_that_is_not_ready_yet_deletes_nothing(self):
        """Every agent restart passes through this state.

        _apply_domain runs from initialize() before the OVS agent has built
        its VLAN mapping, so every network resolves to None. Reconciling
        then would tear down every gateway and rebuild it on the first port
        event.
        """
        self.dp.reconcile_gateway_ports(self.domain_id, set(), True)
        self.bridge.delete_port.assert_not_called()

    def test_the_stale_entry_is_dropped_from_the_map_too(self):
        self.dp.gateway_ports[self.domain_id] = {'net1': 'svp-7-100',
                                                 'net2': 'svp-7-101'}
        self.dp.reconcile_gateway_ports(self.domain_id, {100}, True)
        self.assertEqual({'net1': 'svp-7-100'},
                         self.dp.gateway_ports[self.domain_id])

    def test_reconciling_an_unknown_domain_is_a_noop(self):
        self.dp.reconcile_gateway_ports('no-such-domain', set(), False)
        self.bridge.delete_port.assert_not_called()

    def test_reconciliation_is_idempotent(self):
        self.dp.reconcile_gateway_ports(self.domain_id, {100}, True)
        self.bridge.reset_mock()
        self.bridge.get_port_name_list.return_value = ['svp-7-100']
        self.dp.reconcile_gateway_ports(self.domain_id, {100}, True)
        self.bridge.delete_port.assert_not_called()

    def test_a_bridge_that_cannot_be_listed_deletes_nothing(self):
        self.bridge.get_port_name_list.side_effect = RuntimeError('boom')
        self.dp.reconcile_gateway_ports(self.domain_id, set(), False)
        self.bridge.delete_port.assert_not_called()


class TestRouteReconciliation(DataplaneTestCaseBase):
    """Regression: encap routes accumulated exactly as the ports did.

    Found on node 1 on 2026-09-03 while verifying the gateway-port fix:
    the port for a detached network was gone, but table 13783 still held
    `10.90.2.21 encap seg6 ... segs [fc00:0:2:ec7::]` for a workload on a
    network that had left the domain.
    """

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.locators = {'node2': 'fc00:0:2::/48'}
        self.priv_seg6.list_seg6_routes.side_effect = \
            lambda table, family_v6=False: (
                [] if family_v6 else ['10.90.1.11', '10.90.2.21'])

    def _sync(self, routes):
        self.dp.sync_routes(self.domain_id, routes, self.locators, 'node1')

    def test_a_prefix_that_left_the_domain_loses_its_route(self):
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'}])
        self.priv_seg6.delete_route.assert_called_once_with(
            '10.90.2.21', VRF_TABLE, family_v6=False)

    def test_a_host_route_is_not_deleted_by_its_own_prefix_length(self):
        # `ip route show` prints 10.90.2.21 where the payload says
        # 10.90.2.21/32. Comparing the strings deletes the whole table.
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node2'}])
        self.priv_seg6.delete_route.assert_not_called()

    def test_a_workload_that_moved_here_loses_its_encap_route(self):
        """The withdraw half of a migration.

        add_routes skips a route whose host is this node rather than
        removing it, so the old encap route survived the move and kept
        sending local traffic out to the underlay and back.
        """
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node1'}])
        self.priv_seg6.delete_route.assert_called_once_with(
            '10.90.2.21', VRF_TABLE, family_v6=False)

    def test_a_domain_with_no_routes_left_keeps_none(self):
        self._sync([])
        self.assertEqual(
            sorted([mock.call('10.90.1.11', VRF_TABLE, family_v6=False),
                    mock.call('10.90.2.21', VRF_TABLE, family_v6=False)],
                   key=str),
            sorted(self.priv_seg6.delete_route.call_args_list, key=str))

    def test_both_families_are_reconciled(self):
        self._sync([])
        self.assertEqual(
            [False, True],
            [c[1]['family_v6']
             for c in self.priv_seg6.list_seg6_routes.call_args_list])

    def test_a_route_that_failed_to_install_is_still_wanted(self):
        # Otherwise a transient install failure deletes what is there.
        self.priv_seg6.replace_seg6_route.side_effect = RuntimeError('boom')
        self._sync([{'prefix': '10.90.1.11/32', 'host': 'node2'},
                    {'prefix': '10.90.2.21/32', 'host': 'node2'}])
        self.priv_seg6.delete_route.assert_not_called()

    def test_a_table_that_cannot_be_read_deletes_nothing(self):
        self.priv_seg6.list_seg6_routes.side_effect = RuntimeError('boom')
        self._sync([])
        self.priv_seg6.delete_route.assert_not_called()

    def test_reconciling_an_unknown_domain_is_a_noop(self):
        self.dp.sync_routes('no-such-domain', [], self.locators, 'node1')
        self.priv_seg6.delete_route.assert_not_called()


class TestRoutes(DataplaneTestCaseBase):

    def setUp(self):
        super().setUp()
        self._with_domain()
        self.locators = {'node2': 'fc00:0:2::/48'}

    def _route(self, prefix='10.0.0.5/32', host='node2'):
        return {'prefix': prefix, 'host': host, 'port_id': 'p1'}

    def test_remote_port_gets_an_encap_route(self):
        # The remote node's locator with *this domain's* function id: the
        # SID differs per node, the function id does not.
        self.dp.add_routes(self.domain_id, [self._route()], self.locators,
                           'node1')
        self.priv_seg6.replace_seg6_route.assert_called_once_with(
            '10.0.0.5/32', ['fc00:0:2:7::'], UNDERLAY, VRF_TABLE,
            family_v6=False)

    def test_ipv6_prefix_selects_the_v6_family(self):
        self.dp.add_routes(self.domain_id, [self._route('2001:db8::5/128')],
                           self.locators, 'node1')
        self.assertTrue(
            self.priv_seg6.replace_seg6_route.call_args[1]['family_v6'])

    def test_local_port_is_skipped(self):
        # It is already reachable through the gateway port's connected
        # route; encapsulating would send local traffic out and back.
        self.dp.add_routes(self.domain_id, [self._route(host='node1')],
                           {'node1': LOCATOR}, 'node1')
        self.priv_seg6.replace_seg6_route.assert_not_called()

    def test_host_without_a_locator_is_skipped(self):
        self.dp.add_routes(self.domain_id, [self._route(host='node9')],
                           self.locators, 'node1')
        self.priv_seg6.replace_seg6_route.assert_not_called()

    def test_one_failed_route_does_not_stop_the_rest(self):
        self.priv_seg6.replace_seg6_route.side_effect = [
            RuntimeError('boom'), None]
        self.dp.add_routes(
            self.domain_id,
            [self._route('10.0.0.5/32'), self._route('10.0.0.6/32')],
            self.locators, 'node1')
        self.assertEqual(2, self.priv_seg6.replace_seg6_route.call_count)

    def test_routes_for_an_unknown_domain_are_ignored(self):
        self.dp.add_routes('no-such-domain', [self._route()], self.locators,
                           'node1')
        self.priv_seg6.replace_seg6_route.assert_not_called()

    def test_remove_routes_targets_the_domain_table(self):
        self.dp.remove_routes(self.domain_id, [self._route()])
        self.priv_seg6.delete_route.assert_called_once_with(
            '10.0.0.5/32', VRF_TABLE, family_v6=False)

    def test_add_routes_reports_what_it_wants(self):
        self.assertEqual(
            {'10.0.0.5/32'},
            self.dp.add_routes(self.domain_id,
                               [{'prefix': '10.0.0.5/32', 'host': 'node2'}],
                               {'node2': 'fc00:0:2::/48'}, 'node1'))

    def test_remove_routes_for_an_unknown_domain_is_a_noop(self):
        self.dp.remove_routes('no-such-domain', [self._route()])
        self.priv_seg6.delete_route.assert_not_called()


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
# P5 additions (P5-PLAN.md 4)
# ----------------------------------------------------------------------
class TestSeal(DataplaneTestCaseBase):
    """4.1 / MIGRATION-PLAN.md 8.6: no lookup may fall through to main.

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
    """4.2: deleting the VRF device does not empty its table."""

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
    """4.3: delete used to find gateway ports in memory only."""

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
        self.priv_seg6.delete_route.assert_called_once_with(
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


class TestSegments(DataplaneTestCaseBase):
    """4.5: the agent half of TE, moved here from P6.

    Until P6 no payload carries `segments`, so every route is exactly the
    depth-1 route TestRoutes pins.
    """

    def setUp(self):
        super().setUp()
        self._with_domain()

    def _segs(self, segments):
        self.dp.add_routes(
            self.domain_id,
            [{'prefix': '10.0.0.5/32', 'host': 'node2', 'port_id': 'p1',
              'segments': segments}],
            {'node2': 'fc00:0:2::/48'}, 'node1')
        return self.priv_seg6.replace_seg6_route.call_args[0][1]

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
