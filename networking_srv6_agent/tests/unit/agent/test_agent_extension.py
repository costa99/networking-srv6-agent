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

"""Unit tests for Srv6AgentExtension.

The extension is built field by field rather than through initialize(),
which would need a live RPC connection and a bridge. What is under test is
the concurrency discipline and the RPC handling -- the two things that broke
during bring-up, both silently.

Ported near-verbatim from networking-bgpvpn (srv6-te); the classes after
TestConfigValidation cover what P5 added.
"""

import inspect
import threading
from unittest import mock

from neutron.plugins.ml2.drivers.openvswitch.agent import vlanmanager
from neutron.tests import base
from neutron_lib.plugins.ml2 import ovs_constants as ovs_const
from oslo_utils import uuidutils

from networking_srv6_agent.agent import agent_extension
from networking_srv6_agent.services import rpc as server_rpc


LOCATOR = 'fc00:0:1::/48'
HOST = 'node1'


def _segment(vlan):
    return mock.Mock(vlan=vlan)


def _payload(domain_id, networks=None, routes=None, locators=None,
             function_id=7):
    return {'id': domain_id,
            'function_id': function_id,
            'behavior': 'End.DT46',
            'vrf_table': 10000 + function_id,
            'networks': networks if networks is not None else [],
            'routes': routes or [],
            'locators': locators if locators is not None else {}}


def _network(network_id='net1', mtu=1450):
    return {'network_id': network_id, 'mtu': mtu,
            'subnets': [{'id': 'sub1', 'cidr': '10.0.0.0/24',
                         'gateway_ip': '10.0.0.1'}]}


class AgentExtensionTestCaseBase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.ext = agent_extension.Srv6AgentExtension()
        self.ext.conf = mock.Mock(locator=LOCATOR,
                                  underlay_interface='eth0',
                                  resync_interval=0,
                                  apply_sysctls=True)
        self.ext.vrf_table_base = 10000
        self.ext.host = HOST
        self.ext.context = mock.Mock()
        self.ext.domains = {}
        self.ext.locators = {}
        self.ext._lock = threading.Lock()
        self.ext.vlan_manager = mock.Mock()
        self.ext.dataplane = mock.Mock()
        self.ext.dataplane.present_function_ids.return_value = set()
        self.ext.server_rpc = mock.Mock()
        self.cctxt = self.ext.server_rpc.prepare.return_value
        self.cctxt.call.return_value = []
        self.domain_id = uuidutils.generate_uuid()

    def _run_without_deadlocking(self, what, func, *args, **kwargs):
        """Run func in a thread and fail loudly if it does not return.

        A regression in the locking discipline blocks forever rather than
        raising, so a plain call would hang the whole test run instead of
        failing one test.
        """
        done = threading.Event()
        errors = []

        def run():
            try:
                func(*args, **kwargs)
            except Exception as e:      # noqa: BLE001 - reported below
                errors.append(e)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(done.wait(5),
                        "%s did not return within 5s -- the agent's RPC "
                        "thread is deadlocked on self._lock" % what)
        if errors:
            raise errors[0]
        self.assertTrue(self.ext._lock.acquire(blocking=False),
                        "%s returned without releasing self._lock" % what)
        self.ext._lock.release()


class TestLockDiscipline(AgentExtensionTestCaseBase):
    """Regression tests for the non-reentrant lock.

    handle_port and routes_updated take self._lock and then need a resync.
    Calling the lock-acquiring _sync_state from there wedged the agent's RPC
    thread on the first port event: domains were received and logged but
    nothing was programmed, with no exception anywhere, and graceful
    shutdown blocked too -- which is why restarting the agent hung in
    `deactivating`.
    """

    def test_handle_port_on_an_unknown_network_resyncs(self):
        self._run_without_deadlocking(
            'handle_port', self.ext.handle_port, self.ext.context,
            {'network_id': 'net-unknown', 'port_id': 'p1'})
        self.assertTrue(self.cctxt.call.called)

    def test_routes_updated_for_an_unknown_domain_resyncs(self):
        # Building partial state from an incremental message for a domain
        # we have never heard of is harder to reason about than a full sync.
        self._run_without_deadlocking(
            'routes_updated', self.ext.routes_updated, self.ext.context,
            domain_id='no-such-domain', add=[{'prefix': '10.0.0.5/32'}])
        self.assertTrue(self.cctxt.call.called)
        self.ext.dataplane.add_routes.assert_not_called()

    def test_domain_updated_releases_the_lock(self):
        self._run_without_deadlocking(
            'domain_updated', self.ext.domain_updated, self.ext.context,
            domain=_payload(self.domain_id))

    def test_domain_updated_releases_the_lock_after_a_failure(self):
        self.ext.dataplane.ensure_domain.side_effect = RuntimeError('boom')
        self._run_without_deadlocking(
            'domain_updated', self.ext.domain_updated, self.ext.context,
            domain=_payload(self.domain_id))

    def test_domain_deleted_releases_the_lock(self):
        self._run_without_deadlocking(
            'domain_deleted', self.ext.domain_deleted, self.ext.context,
            domain_id=self.domain_id, function_id=7, vrf_table=10007)

    def test_sync_state_is_reentrant_by_split_not_by_luck(self):
        # _do_sync_state assumes the lock is held; _sync_state takes it.
        with self.ext._lock:
            self.ext._do_sync_state()
        self.assertTrue(self.cctxt.call.called)


class TestSyncState(AgentExtensionTestCaseBase):

    def test_state_is_applied(self):
        self.cctxt.call.return_value = [_payload(self.domain_id)]
        self.ext._sync_state()
        self.ext.dataplane.ensure_domain.assert_called_once_with(
            self.domain_id, 7, 'End.DT46')
        self.assertIn(self.domain_id, self.ext.domains)

    def test_domain_that_disappeared_while_we_ran_is_collected(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        self.cctxt.call.return_value = []
        self.ext._sync_state()
        self.ext.dataplane.delete_domain.assert_called_once_with(
            self.domain_id)
        self.assertEqual({}, self.ext.domains)

    def test_rpc_failure_leaves_state_alone(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        self.cctxt.call.side_effect = Exception('no route to server')
        self.ext._sync_state()
        self.ext.dataplane.delete_domain.assert_not_called()
        self.assertIn(self.domain_id, self.ext.domains)

    def test_locator_is_reported(self):
        self.ext._sync_state()
        kwargs = self.cctxt.call.call_args[1]
        self.assertEqual(HOST, kwargs['host'])
        self.assertEqual(LOCATOR, kwargs['locator'])


class TestApplyDomain(AgentExtensionTestCaseBase):

    def test_network_with_a_local_vlan_gets_a_gateway_port(self):
        self.ext.vlan_manager.get_segments.return_value = {1: _segment(100)}
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.ext.dataplane.ensure_gateway_port.assert_called_once_with(
            self.domain_id, 'net1', 100,
            [{'id': 'sub1', 'cidr': '10.0.0.0/24',
              'gateway_ip': '10.0.0.1'}], mtu=1450)

    def test_network_that_is_not_on_this_node_gets_no_gateway_port(self):
        # Normal, not degraded: a gateway with no local instances behind it
        # would answer ARP for an address it has no reason to own.
        self.ext.vlan_manager.get_segments.side_effect = \
            vlanmanager.MappingNotFound(net_id='net1', seg_id='<all>')
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.ext.dataplane.ensure_gateway_port.assert_not_called()
        # The VRF and the remote routes are still programmed.
        self.assertTrue(self.ext.dataplane.ensure_domain.called)
        self.assertTrue(self.ext.dataplane.sync_routes.called)

    def test_mtu_is_checked_per_network(self):
        self.ext.vlan_manager.get_segments.return_value = {1: _segment(100)}
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.ext.dataplane.check_mtu.assert_called_once_with(1450)

    def test_locators_are_merged_and_passed_to_the_routes(self):
        self.ext.locators = {'node2': 'fc00:0:2::/48'}
        routes = [{'prefix': '10.0.0.5/32', 'host': 'node3'}]
        self.ext._apply_domain(_payload(self.domain_id, routes=routes,
                                        locators={'node3': 'fc00:0:3::/48'}))
        self.ext.dataplane.sync_routes.assert_called_once_with(
            self.domain_id, routes,
            {'node2': 'fc00:0:2::/48', 'node3': 'fc00:0:3::/48'}, HOST)

    def test_multi_segment_network_uses_the_first_segment(self):
        self.ext.vlan_manager.get_segments.return_value = {
            1: _segment(100), 2: _segment(101)}
        self.assertEqual(100, self.ext._local_vlan_for('net1'))

    def test_local_vlan_of_an_unmapped_network(self):
        self.ext.vlan_manager.get_segments.side_effect = \
            vlanmanager.MappingNotFound(net_id='net1', seg_id='<all>')
        self.assertIsNone(self.ext._local_vlan_for('net1'))

    def test_local_vlan_when_the_lookup_explodes(self):
        self.ext.vlan_manager.get_segments.side_effect = RuntimeError('boom')
        self.assertIsNone(self.ext._local_vlan_for('net1'))

    def test_local_vlan_of_a_network_with_no_segments(self):
        self.ext.vlan_manager.get_segments.return_value = {}
        self.assertIsNone(self.ext._local_vlan_for('net1'))


class TestApplyDomainReconciles(AgentExtensionTestCaseBase):
    """Regression: _apply_vpn only added, so association deletes leaked.

    The payload is the server's whole view of the domain, so a network
    absent from it has left. Both callers land here -- the domain_updated
    fanout and the periodic resync -- so reconciling here also repairs a
    lost message. See implementation/srv6-plugin/evidence/
    assoc-delete-leak.txt.
    """

    def setUp(self):
        super().setUp()
        self.ext.vlan_manager.get_segments.return_value = {1: _segment(100)}

    def test_the_resolved_vlans_are_what_is_kept(self):
        self.ext.vlan_manager.get_segments.side_effect = \
            lambda n: {1: _segment(100 if n == 'net1' else 101)}
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network('net1'),
                                                  _network('net2')]))
        self.ext.dataplane.reconcile_gateway_ports.assert_called_once_with(
            self.domain_id, {100, 101}, has_networks=True)

    def test_a_domain_with_no_networks_left_keeps_nothing(self):
        self.ext._apply_domain(_payload(self.domain_id, networks=[]))
        self.ext.dataplane.reconcile_gateway_ports.assert_called_once_with(
            self.domain_id, set(), has_networks=False)

    def test_reconciliation_precedes_the_ports_it_would_otherwise_delete(self):
        # Reconciling after ensure_gateway_port would delete a port that had
        # just been (re-)created in the same pass.
        order = []
        self.ext.dataplane.reconcile_gateway_ports.side_effect = \
            lambda *a, **kw: order.append('reconcile')
        self.ext.dataplane.ensure_gateway_port.side_effect = \
            lambda *a, **kw: order.append('ensure')
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.assertEqual(['reconcile', 'ensure'], order)

    def test_an_unresolvable_network_still_reports_that_it_exists(self):
        # has_networks is what stops the dataplane deleting on a VLAN map
        # that is merely not ready.
        self.ext.vlan_manager.get_segments.side_effect = \
            vlanmanager.MappingNotFound(net_id='net1', seg_id='<all>')
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.ext.dataplane.reconcile_gateway_ports.assert_called_once_with(
            self.domain_id, set(), has_networks=True)

    def test_the_vlan_is_resolved_once_per_network(self):
        # Reconciliation and ensure_gateway_port must agree on the VLAN; two
        # lookups are two chances to disagree while ports are rebinding.
        self.ext._apply_domain(_payload(self.domain_id,
                                        networks=[_network()]))
        self.assertEqual(1, self.ext.vlan_manager.get_segments.call_count)

    def test_the_resync_reconciles_too(self):
        self.cctxt.call.return_value = [
            _payload(self.domain_id, networks=[_network('net1')])]
        self.ext._sync_state()
        self.ext.dataplane.reconcile_gateway_ports.assert_called_once_with(
            self.domain_id, {100}, has_networks=True)

    def test_the_fanout_reconciles_too(self):
        self.ext.domain_updated(
            mock.Mock(),
            domain=_payload(self.domain_id, networks=[_network('net1')]))
        self.ext.dataplane.reconcile_gateway_ports.assert_called_once_with(
            self.domain_id, {100}, has_networks=True)


class TestRpcHandlers(AgentExtensionTestCaseBase):

    def test_domain_updated_is_recorded(self):
        self.ext.domain_updated(self.ext.context,
                                domain=_payload(self.domain_id))
        self.assertIn(self.domain_id, self.ext.domains)

    def test_domain_updated_without_a_payload_is_ignored(self):
        self.ext.domain_updated(self.ext.context, domain=None)
        self.ext.dataplane.ensure_domain.assert_not_called()

    def test_routes_are_removed_before_they_are_added(self):
        # The reverse order would leave a window in which a moved address
        # is claimed by two nodes.
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        order = []
        self.ext.dataplane.remove_routes.side_effect = \
            lambda *a: order.append('remove')
        self.ext.dataplane.add_routes.side_effect = \
            lambda *a: order.append('add')
        self.ext.routes_updated(self.ext.context, domain_id=self.domain_id,
                                add=[{'prefix': '10.0.0.5/32'}],
                                remove=[{'prefix': '10.0.0.6/32'}])
        self.assertEqual(['remove', 'add'], order)

    def test_routes_updated_failure_is_contained(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        self.ext.dataplane.add_routes.side_effect = RuntimeError('boom')
        self.ext.routes_updated(self.ext.context, domain_id=self.domain_id,
                                add=[{'prefix': '10.0.0.5/32'}])

    def test_domain_deleted_forgets_the_domain(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        self.ext.domain_deleted(self.ext.context, domain_id=self.domain_id,
                                function_id=7, vrf_table=10007)
        self.ext.dataplane.delete_domain.assert_called_once_with(
            self.domain_id, 7, 10007)
        self.assertEqual({}, self.ext.domains)

    def test_domain_deleted_failure_still_forgets_the_domain(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id)
        self.ext.dataplane.delete_domain.side_effect = RuntimeError('boom')
        self.ext.domain_deleted(self.ext.context, domain_id=self.domain_id,
                                function_id=7, vrf_table=10007)
        self.assertEqual({}, self.ext.domains)

    def test_unknown_payload_version_is_ignored_everywhere(self):
        # Half-applying a message whose shape cannot be relied on is worse
        # than ignoring it.
        self.ext.domain_updated(self.ext.context,
                                domain=_payload(self.domain_id),
                                payload_version=99)
        self.ext.routes_updated(self.ext.context, domain_id=self.domain_id,
                                add=[{'prefix': '10.0.0.5/32'}],
                                payload_version=99)
        self.ext.domain_deleted(self.ext.context, domain_id=self.domain_id,
                                function_id=7, vrf_table=10007,
                                payload_version=99)
        self.assertFalse(self.ext.dataplane.method_calls)
        self.assertFalse(self.cctxt.call.called)


class TestPortEvents(AgentExtensionTestCaseBase):

    def test_port_on_a_known_network_programs_its_gateway(self):
        """This, not the RPC, is when a network becomes programmable.

        The gateway port needs the local VLAN, which the OVS agent only
        assigns when a port for the network appears on this node.
        """
        self.ext.domains[self.domain_id] = _payload(self.domain_id,
                                                    networks=[_network()])
        self.ext.vlan_manager.get_segments.return_value = {1: _segment(100)}
        self.ext.handle_port(self.ext.context,
                             {'network_id': 'net1', 'port_id': 'p1'})
        self.ext.dataplane.ensure_gateway_port.assert_called_once_with(
            self.domain_id, 'net1', 100,
            [{'id': 'sub1', 'cidr': '10.0.0.0/24',
              'gateway_ip': '10.0.0.1'}], mtu=1450)
        self.assertFalse(self.cctxt.call.called)

    def test_port_on_a_network_with_no_vlan_yet_is_left_for_the_resync(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id,
                                                    networks=[_network()])
        self.ext.vlan_manager.get_segments.side_effect = \
            vlanmanager.MappingNotFound(net_id='net1', seg_id='<all>')
        self.ext.handle_port(self.ext.context,
                             {'network_id': 'net1', 'port_id': 'p1'})
        self.ext.dataplane.ensure_gateway_port.assert_not_called()

    def test_port_on_a_network_in_no_domain_resyncs(self):
        self.ext.domains[self.domain_id] = _payload(
            self.domain_id, networks=[_network('net1')])
        self.ext.handle_port(self.ext.context,
                             {'network_id': 'net2', 'port_id': 'p1'})
        self.assertTrue(self.cctxt.call.called)

    def test_port_without_a_network_is_ignored(self):
        self.ext.handle_port(self.ext.context, {'port_id': 'p1'})
        self.assertFalse(self.cctxt.call.called)
        self.assertFalse(self.ext.dataplane.method_calls)

    def test_gateway_port_failure_is_contained(self):
        self.ext.domains[self.domain_id] = _payload(self.domain_id,
                                                    networks=[_network()])
        self.ext.vlan_manager.get_segments.return_value = {1: _segment(100)}
        self.ext.dataplane.ensure_gateway_port.side_effect = \
            RuntimeError('boom')
        self.ext.handle_port(self.ext.context,
                             {'network_id': 'net1', 'port_id': 'p1'})

    def test_delete_port_does_nothing(self):
        # The server withdraws the address through routes_updated; removing
        # anything here would race that message.
        self.ext.delete_port(self.ext.context,
                             {'network_id': 'net1', 'port_id': 'p1'})
        self.assertFalse(self.ext.dataplane.method_calls)


class TestConfigValidation(AgentExtensionTestCaseBase):

    def test_missing_locator_refuses_to_start(self):
        # A node without an address block of its own cannot take part.
        self.ext.conf = mock.Mock(locator=None, underlay_interface='eth0')
        self.assertRaises(SystemExit, self.ext._validate_config)

    def test_unusable_locator_refuses_to_start(self):
        self.ext.conf = mock.Mock(locator='not-a-locator',
                                  underlay_interface='eth0')
        self.assertRaises(SystemExit, self.ext._validate_config)

    def test_locator_too_long_for_the_function_field_refuses_to_start(self):
        self.ext.conf = mock.Mock(locator='fc00:0:1:2:3:4:5::/120',
                                  underlay_interface='eth0')
        self.assertRaises(SystemExit, self.ext._validate_config)

    def test_missing_underlay_interface_is_only_a_warning(self):
        self.ext.conf = mock.Mock(locator=LOCATOR, underlay_interface=None)
        self.ext._validate_config()


# ----------------------------------------------------------------------
# P5 additions (P5-PLAN.md 4, 5)
# ----------------------------------------------------------------------
class TestKernelGarbageCollection(AgentExtensionTestCaseBase):
    """4.3: the old GC diffed in-memory state, empty after every restart.

    A domain deleted while the agent was down -- or kernel state left by an
    earlier build -- was therefore never collected. The old test passed
    only because it pre-seeded self.vpns.
    """

    def test_an_orphan_found_after_a_restart_is_collected(self):
        self.assertEqual({}, self.ext.domains)       # just restarted
        self.ext.dataplane.present_function_ids.return_value = {7, 9}
        self.cctxt.call.return_value = [_payload(self.domain_id)]
        self.ext._sync_state()
        self.ext.dataplane.delete_domain.assert_called_once_with(
            None, function_id=9)

    def test_a_wanted_function_id_is_never_collected(self):
        self.ext.dataplane.present_function_ids.return_value = {7}
        self.cctxt.call.return_value = [_payload(self.domain_id)]
        self.ext._sync_state()
        self.ext.dataplane.delete_domain.assert_not_called()

    def test_an_empty_answer_from_a_working_server_collects_everything(self):
        # The re-stack case: the old build's VRFs, and a fresh database.
        self.ext.dataplane.present_function_ids.return_value = {3579, 3783}
        self.ext._sync_state()
        self.assertEqual(
            [mock.call(None, function_id=3579),
             mock.call(None, function_id=3783)],
            self.ext.dataplane.delete_domain.call_args_list)

    def test_a_failed_sync_collects_nothing(self):
        self.cctxt.call.side_effect = Exception('no route to server')
        self.ext._sync_state()
        self.ext.dataplane.present_function_ids.assert_not_called()
        self.ext.dataplane.delete_domain.assert_not_called()

    def test_an_unreadable_kernel_collects_nothing(self):
        self.ext.dataplane.present_function_ids.return_value = None
        self.ext._sync_state()
        self.ext.dataplane.delete_domain.assert_not_called()

    def test_one_failure_does_not_stop_the_rest_or_the_apply(self):
        self.ext.dataplane.present_function_ids.return_value = {8, 9}
        self.ext.dataplane.delete_domain.side_effect = [RuntimeError('boom'),
                                                        None]
        self.cctxt.call.return_value = [_payload(self.domain_id)]
        self.ext._sync_state()
        self.assertEqual(2, self.ext.dataplane.delete_domain.call_count)
        self.assertTrue(self.ext.dataplane.ensure_domain.called)


class TestDriverType(base.BaseTestCase):
    """4.4 / MIGRATION-PLAN.md 8.4."""

    def test_the_ovs_agent_is_accepted(self):
        agent_extension.Srv6AgentExtension._check_driver_type(
            ovs_const.EXTENSION_DRIVER_TYPE)

    def test_another_agent_is_refused_before_anything_is_touched(self):
        with mock.patch.object(agent_extension.config,
                               'register_agent_opts') as register, \
                mock.patch.object(agent_extension.dp,
                                  'Srv6LinuxDataplane') as dataplane:
            ext = agent_extension.Srv6AgentExtension()
            self.assertRaises(SystemExit, ext.initialize, mock.Mock(),
                              'macvtap')
        register.assert_not_called()
        dataplane.assert_not_called()


class TestRpcSetup(base.BaseTestCase):

    def test_topics(self):
        ext = agent_extension.Srv6AgentExtension()
        connection = mock.Mock()
        with mock.patch.object(agent_extension.n_rpc,
                               'get_client') as get_client:
            ext._setup_rpc(connection)
        # The fanout the server's notifier sends on -- and no BGPVPN
        # driver's.
        connection.create_consumer.assert_called_once_with(
            'q-agent-notifier-srv6-update', [ext], fanout=True)
        # Never topics.PLUGIN (see constants.TOPIC_SRV6_PLUGIN).
        self.assertEqual('srv6-plugin', get_client.call_args[0][0].topic)


class TestRpcContract(base.BaseTestCase):
    """P5-PLAN.md 5: every cast the server sends binds to named parameters.

    A kwarg the handler does not name lands in **kwargs, the named one
    defaults to None, and the handler returns without a word. This is the
    only test that sees both sides of the wire at once.
    """

    def _casts(self):
        with mock.patch.object(server_rpc.n_rpc, 'get_client') as get_client:
            notifier = server_rpc.Srv6AgentNotifyAPI()
            notifier.domain_updated(mock.sentinel.ctx, {'id': 'd1'})
            notifier.routes_updated(mock.sentinel.ctx, 'd1', add=[],
                                    remove=[])
            notifier.domain_deleted(mock.sentinel.ctx, 'd1', 7, 10007)
        return get_client.return_value.prepare.return_value.cast.call_args_list

    @staticmethod
    def _unnamed(method, ctx, kwargs):
        handler = getattr(agent_extension.Srv6AgentExtension, method)
        bound = inspect.signature(handler).bind(None, ctx, **kwargs)
        return bound.arguments.get('kwargs', {})

    def test_every_cast_binds_to_named_parameters(self):
        casts = self._casts()
        self.assertEqual(['domain_updated', 'routes_updated',
                          'domain_deleted'],
                         [call.args[1] for call in casts])
        for call in casts:
            ctx, method = call.args
            self.assertEqual({}, self._unnamed(method, ctx, call.kwargs),
                             method)

    def test_the_check_would_catch_the_old_kwarg(self):
        # What the old build sent. It must NOT bind cleanly.
        self.assertEqual({'vpn': {'id': 'd1'}},
                         self._unnamed('domain_updated', mock.sentinel.ctx,
                                       {'vpn': {'id': 'd1'},
                                        'payload_version': 1}))
