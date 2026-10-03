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

"""Srv6AgentExtension -- runs inside the OVS L2 agent.

Delivered as an L2 agent extension (entry point group
`neutron.agent.l2.extensions`, enabled with `[agent] extensions = srv6`),
the mechanism networking-bagpipe uses. Three things come for free that way
and are the reason for the choice: per-port
handle_port/delete_port callbacks, a handle on br-int, and the network ->
local-VLAN mapping the gateway port needs, which is not available outside
the agent. The price is that this runs under ML2/OVS only -- under ML2/OVN
there is no L2 agent to host it -- and initialize() refuses anything else.
"""

import threading

from neutron.agent import firewall
from neutron.agent.linux import openvswitch_firewall
from neutron.conf.agent import securitygroups_rpc as sg_conf
from neutron.plugins.ml2.drivers.openvswitch.agent import vlanmanager
from neutron_lib.agent import l2_extension
from neutron_lib.agent import topics
from neutron_lib import context as n_context
from neutron_lib.plugins.ml2 import ovs_constants as ovs_const
from neutron_lib import rpc as n_rpc
from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging
from oslo_service import loopingcall

from networking_srv6_agent._i18n import _
from networking_srv6_agent.agent import dataplane as dp
from networking_srv6_agent.common import config
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid


LOG = logging.getLogger(__name__)


class Srv6AgentExtension(l2_extension.L2AgentExtension):

    SUPPORTED_RESOURCE_TYPES = []

    def consume_api(self, agent_api):
        """Handed to us by the OVS agent before initialize()."""
        self.agent_api = agent_api

    def initialize(self, connection, driver_type):
        # First, before any sysctl or kernel write: on another agent there
        # is no br-int and no local VLAN map, and the failure would surface
        # much later as an AttributeError nobody could read.
        self._check_driver_type(driver_type)

        config.register_agent_opts()
        self.conf = cfg.CONF[config.SRV6_AGENT_GROUP]
        # The SERVER group's vrf_table_base: agent and server must derive
        # the same table from a function id, so a compute-only node needs
        # [srv6] vrf_table_base too (the devstack plugin writes it on every
        # node).
        config.register_server_opts()
        self.vrf_table_base = cfg.CONF[
            config.SRV6_SERVER_GROUP].vrf_table_base

        self._validate_config()

        self.host = cfg.CONF.host
        self.context = n_context.get_admin_context_without_session()
        # domain_id -> payload, so handle_port can answer "is this network
        # in a domain?" without an RPC round trip on every port event.
        self.domains = {}
        self.locators = {}
        self._lock = threading.Lock()

        self.vlan_manager = vlanmanager.LocalVlanManager()
        bridge = None
        if getattr(self, 'agent_api', None) is not None:
            bridge = self.agent_api.request_int_br()
        self.dataplane = dp.Srv6LinuxDataplane(
            locator=self.conf.locator,
            underlay_interface=self.conf.underlay_interface,
            vrf_table_base=self.vrf_table_base,
            bridge=bridge,
            apply_sysctls=self.conf.apply_sysctls,
            ovs_firewall=bridge is not None and self._ovs_firewall_active())
        self.dataplane.initialize_node()

        self._setup_rpc(connection)
        self._sync_state()
        self._start_resync_loop()
        LOG.info("SRv6 agent extension initialised on %(host)s "
                 "(locator %(loc)s)",
                 {'host': self.host, 'loc': self.conf.locator})

    # ------------------------------------------------------------------
    @staticmethod
    def _check_driver_type(driver_type):
        if driver_type != ovs_const.EXTENSION_DRIVER_TYPE:
            raise SystemExit(
                _("The srv6 L2 agent extension needs the Open vSwitch agent "
                  "(it programs br-int and reads the OVS local VLAN map), "
                  "but it was loaded by the %r agent. Remove `srv6` from "
                  "[agent] extensions on this node.") % driver_type)

    @staticmethod
    def _ovs_firewall_active():
        """Whether this agent filters ports with the OVS firewall driver.

        Only that driver leaves the gateway ports outside the firewall (F1,
        F2), and only its tables 73 and 94 exist for the flows the dataplane
        adds. Under iptables_hybrid table 73 is empty, so the same flows
        would DROP every frame from the gateway; under noop there is
        nothing to join. The driver is resolved the way the agent resolves
        it (alias or class path, unset meaning noop), so a subclass counts.
        """
        sg_conf.register_securitygroups_opts()
        conf = cfg.CONF.SECURITYGROUP
        if not conf.enable_security_group or not conf.firewall_driver:
            return False
        try:
            driver = firewall.load_firewall_driver_class(conf.firewall_driver)
        except Exception as e:
            LOG.warning("SRv6: could not resolve firewall_driver %(d)r "
                        "(%(e)s); gateway ports get no firewall flows",
                        {'d': conf.firewall_driver, 'e': e})
            return False
        return issubclass(driver, openvswitch_firewall.OVSFirewallDriver)

    def _validate_config(self):
        if not self.conf.locator:
            raise SystemExit(
                _("[%s] locator is required: this node cannot participate "
                  "in an SRv6 domain without an address block of its own.")
                % config.SRV6_AGENT_GROUP)
        try:
            sid.parse_locator(self.conf.locator)
        except sid.InvalidLocator as e:
            raise SystemExit(_("[%(g)s] locator: %(e)s")
                             % {'g': config.SRV6_AGENT_GROUP, 'e': e})
        if not self.conf.underlay_interface:
            LOG.warning("[%s] underlay_interface is unset; encap routes will "
                        "be installed without a nexthop device and may fail",
                        config.SRV6_AGENT_GROUP)

    def _setup_rpc(self, connection):
        """Join the server's fanout topic.

        The one services/rpc.py sends on, and no BGPVPN driver's.
        """
        topic = topics.get_topic_name(topics.AGENT, constants.TOPIC_SRV6,
                                      topics.UPDATE)
        endpoints = [self]
        connection.create_consumer(topic, endpoints, fanout=True)

        target = oslo_messaging.Target(
            topic=constants.TOPIC_SRV6_PLUGIN, version='1.0')
        self.server_rpc = n_rpc.get_client(target)

    def _sync_state(self):
        """Lock-acquiring wrapper. Use from callers that hold no lock."""
        with self._lock:
            self._do_sync_state()

    def _do_sync_state(self):
        """Register our locator and converge to whatever the server says.

        Caller MUST already hold self._lock. Split from _sync_state because
        handle_port and routes_updated need to resync while holding the
        lock; calling the locking version from there deadlocked the agent's
        RPC thread, which also blocked graceful shutdown.
        """
        try:
            cctxt = self.server_rpc.prepare()
            payloads = cctxt.call(
                self.context, constants.SYNC_STATE,
                host=self.host, locator=self.conf.locator,
                payload_version=constants.PAYLOAD_VERSION,
                capabilities=self.dataplane.capabilities)
        except Exception as e:
            LOG.error("SRv6: sync_state failed (%s); will retry on the next "
                      "resync interval", e)
            return
        wanted = {}
        for payload in payloads or []:
            wanted[payload['id']] = payload
            self.locators.update(payload.get('locators') or {})
        # Every resync, changed or not: a full replace is idempotent and
        # restores a table somebody deleted by hand. Before any domain is
        # applied, so the End-SID rules see the current filter.
        self.dataplane.ensure_edge_filter(sorted(self.locators.values()))
        # Domains this process applied that the server no longer has.
        for stale_id in set(self.domains) - set(wanted):
            LOG.info("SRv6: domain %s no longer present; removing", stale_id)
            self.dataplane.delete_domain(stale_id)
            self.domains.pop(stale_id, None)
        self._collect_kernel_orphans(wanted)
        for payload in wanted.values():
            self._apply_domain(payload)

    def _collect_kernel_orphans(self, wanted):
        """Delete VRFs in the kernel that no domain on the server owns.

        The in-memory diff above cannot do this after a restart:
        self.domains starts empty, so a domain deleted while the agent was
        down -- or state left by an earlier build -- was never collected,
        whatever the old comment here claimed. The kernel is asked instead,
        by the rule the gateway ports already follow. Only after a
        SUCCESSFUL sync_state (the caller returns on failure): an empty
        answer from a working server means "no domains", and everything
        goes; an unreadable VRF list means "delete nothing".
        """
        present = self.dataplane.present_function_ids()
        if present is None:
            return
        wanted_fids = {p['function_id'] for p in wanted.values()}
        for function_id in sorted(present - wanted_fids):
            LOG.info("SRv6: function id %s has kernel state but no domain "
                     "on the server; removing it", function_id)
            try:
                self.dataplane.delete_domain(None, function_id=function_id)
            except Exception:
                LOG.exception("SRv6: could not remove orphaned function id "
                              "%s", function_id)

    def _start_resync_loop(self):
        """Periodic full resync.

        oslo_service's looping call rather than threading.Timer: the OVS
        agent runs on oslo.service's own threading backend, and a raw Timer
        thread is not part of that lifecycle -- it neither participates in
        graceful shutdown nor is visible to the agent's thread management.
        """
        interval = self.conf.resync_interval
        if not interval:
            return
        self._resync_loop = loopingcall.FixedIntervalLoopingCall(
            self._sync_state)
        self._resync_loop.start(interval=interval, initial_delay=interval,
                                stop_on_exception=False)

    # ------------------------------------------------------------------
    # applying state
    # ------------------------------------------------------------------
    def _local_vlan_for(self, network_id):
        """The br-int local VLAN, or None if the network is not on this node.

        None is the normal case for a network whose instances all live
        elsewhere: this node then holds the domain's VRF and its remote
        routes but no gateway port for that network. That is correct, not a
        degraded state -- a gateway with no local instances behind it would
        answer ARP for an address it has no reason to own.

        The VLAN is looked up rather than remembered because the OVS agent
        reallocates it whenever ports rebind, and a stale tag silently puts
        the gateway in the wrong broadcast domain.
        """
        try:
            segments = self.vlan_manager.get_segments(network_id)
        except vlanmanager.MappingNotFound:
            return None
        except Exception:
            LOG.debug("SRv6: could not resolve local VLAN for network %s",
                      network_id)
            return None
        if not segments:
            return None
        if len(segments) > 1:
            LOG.warning("SRv6: network %(n)s has %(c)d segments on this "
                        "node; using the first. Multi-segment networks are "
                        "not supported.",
                        {'n': network_id, 'c': len(segments)})
        return list(segments.values())[0].vlan

    def _merge_locators(self, locators):
        """Merge a payload's locators. Returns whether anything changed."""
        changed = any(self.locators.get(host) != locator
                      for host, locator in (locators or {}).items())
        self.locators.update(locators or {})
        return changed

    @staticmethod
    def _wanted_vlans(payload, present):
        """The VLANs on which this domain's networks have a live port here.

        present is dataplane.local_network_vlans(): network -> VLANs, from
        OVSDB. Anything of the domain on another VLAN is a leftover.
        """
        wanted = set()
        for network in payload.get('networks') or []:
            wanted |= present.get(network['network_id'], set())
        return wanted

    def _reconcile_domain(self, domain_id, payload, present):
        """Remove the domain's gateway ports that no live port backs.

        present None means OVSDB could not be read: delete nothing, the next
        trigger tries again.
        """
        if present is None:
            LOG.warning("SRv6: br-int ports unreadable; not reconciling the "
                        "gateway ports of domain %s", domain_id)
            return
        self.dataplane.reconcile_gateway_ports(
            domain_id, self._wanted_vlans(payload, present))

    def _reconcile_all_gateways(self):
        """Reconcile every known domain's gateway ports. Caller holds the lock.

        One OVSDB read for all of them; a domain that fails is logged and
        does not stop the others.
        """
        present = self.dataplane.local_network_vlans()
        for domain_id, payload in list(self.domains.items()):
            try:
                self._reconcile_domain(domain_id, payload, present)
            except Exception:
                LOG.exception("SRv6: reconciling the gateway ports of "
                              "domain %s", domain_id)

    @staticmethod
    def _network_mtus(payload):
        """network_id -> MTU, for the per-route check."""
        return {n['network_id']: n.get('mtu')
                for n in payload.get('networks') or []}

    def _apply_domain(self, payload):
        """Converge this node to the payload. Reconciles, does not accumulate.

        The payload is the server's whole view of the domain, so a network
        missing from it has left the domain and its gateway port has to go.
        Both callers land here -- the domain_updated fanout and the
        periodic resync -- so reconciling here covers the missed-message
        case as well.

        Returns whether the payload switched the node's dataplane encoding.
        That happens before this domain is programmed, so this domain is
        already encoded the new way; every OTHER domain needs a resync,
        which domain_updated does. A full sync needs none: it applies every
        domain after this one anyway.
        """
        domain_id = payload['id']
        # Absent from an older server's payload, which only knew full SIDs.
        encoding_changed = self.dataplane.ensure_encoding(
            payload.get('encoding') or constants.DEFAULT_ENCODING)
        if self._merge_locators(payload.get('locators')):
            # A new node's SID space is filtered before any route of this
            # payload can lead a rule toward its End SID.
            self.dataplane.ensure_edge_filter(sorted(self.locators.values()))
        self.dataplane.ensure_domain(domain_id, payload['function_id'],
                                     payload['behavior'])
        networks = payload.get('networks') or []
        # What to KEEP comes from OVSDB (local_network_vlans): it is right
        # even during a restart, when the in-memory map below is empty.
        # Reconcile before ensuring, or a port (re-)created in this pass
        # could be deleted by it.
        self._reconcile_domain(domain_id, payload,
                               self.dataplane.local_network_vlans())
        # What to CREATE still comes from the OVS agent's own map: a gateway
        # is made only once the agent has bound the network on this node.
        vlans = {n['network_id']: self._local_vlan_for(n['network_id'])
                 for n in networks}
        for network in networks:
            network_id = network['network_id']
            local_vlan = vlans[network_id]
            if local_vlan is None:
                continue
            self.dataplane.check_mtu(network.get('mtu'))
            self.dataplane.ensure_gateway_port(
                domain_id, network_id, local_vlan,
                network.get('subnets') or [], mtu=network.get('mtu'))
        self.dataplane.sync_routes(domain_id, payload.get('routes') or [],
                                   self.locators, self.host,
                                   mtus=self._network_mtus(payload))
        self.domains[domain_id] = payload
        return encoding_changed

    # ------------------------------------------------------------------
    # RPC handlers (server -> agent fanout)
    #
    # The parameter names ARE the wire contract: a kwarg
    # the server sends that is not named here lands in **kwargs and the
    # handler returns without a word. TestRpcContract binds every cast of
    # services/rpc.py to these signatures.
    # ------------------------------------------------------------------
    def _check_version(self, payload_version):
        if payload_version != constants.PAYLOAD_VERSION:
            LOG.warning("SRv6: ignoring RPC with payload version %(got)s "
                        "(this agent speaks %(want)s)",
                        {'got': payload_version,
                         'want': constants.PAYLOAD_VERSION})
            return False
        return True

    def domain_updated(self, context, domain=None, payload_version=1,
                       **kwargs):
        if not self._check_version(payload_version) or not domain:
            return
        LOG.debug("SRv6: domain_updated %s", domain.get('id'))
        with self._lock:
            try:
                encoding_changed = self._apply_domain(domain)
            except Exception:
                LOG.exception("SRv6: failed to apply domain_updated for %s",
                              domain.get('id'))
                return
            if encoding_changed:
                LOG.info("SRv6: dataplane encoding changed; re-encoding "
                         "every domain")
                self._do_sync_state()

    def routes_updated(self, context, domain_id=None, add=None, remove=None,
                       payload_version=1, **kwargs):
        if not self._check_version(payload_version):
            return
        with self._lock:
            if domain_id not in self.domains:
                # A route for a domain we have never heard of means our view
                # is stale; a full sync is cheaper to reason about than
                # building partial state from an incremental message.
                LOG.debug("SRv6: routes for unknown domain %s; resyncing",
                          domain_id)
                self._do_sync_state()
                return
            try:
                if remove:
                    self.dataplane.remove_routes(domain_id, remove)
                if add:
                    # The MTUs come from the cached domain payload: an
                    # incremental message carries routes, not networks.
                    self.dataplane.add_routes(
                        domain_id, add, self.locators, self.host,
                        mtus=self._network_mtus(self.domains[domain_id]))
            except Exception:
                LOG.exception("SRv6: failed to apply routes_updated for %s",
                              domain_id)

    def domain_deleted(self, context, domain_id=None, function_id=None,
                       vrf_table=None, payload_version=1, **kwargs):
        if not self._check_version(payload_version):
            return
        LOG.info("SRv6: domain_deleted %s", domain_id)
        with self._lock:
            try:
                self.dataplane.delete_domain(domain_id, function_id,
                                             vrf_table)
            except Exception:
                LOG.exception("SRv6: failed to delete domain %s", domain_id)
            self.domains.pop(domain_id, None)

    # ------------------------------------------------------------------
    # L2 agent extension interface
    # ------------------------------------------------------------------
    def handle_port(self, context, port):
        """A port appeared on this node.

        The gateway port for its network can only be created once the OVS
        agent has assigned the network a local VLAN, which is exactly when
        this fires -- so this, not the RPC, is where a network first becomes
        programmable on a node.
        """
        network_id = port.get('network_id')
        if not network_id:
            return
        with self._lock:
            # First, so no leftover gateway can share the VLAN this port's
            # network has just been given (F8). delete_port normally removed
            # it already; this covers a removal the agent never saw.
            self._reconcile_all_gateways()
            known = False
            for domain_id, payload in self.domains.items():
                for network in payload.get('networks') or []:
                    if network['network_id'] != network_id:
                        continue
                    known = True
                    local_vlan = self._local_vlan_for(network_id)
                    if local_vlan is None:
                        continue
                    try:
                        self.dataplane.ensure_gateway_port(
                            domain_id, network_id, local_vlan,
                            network.get('subnets') or [],
                            mtu=network.get('mtu'))
                    except Exception:
                        LOG.exception("SRv6: gateway port for network %s",
                                      network_id)
            if not known:
                LOG.debug("SRv6: port on unknown network %s; resyncing",
                          network_id)
                self._do_sync_state()

    def delete_port(self, context, port):
        """A port left this node: drop the gateways no live port backs now.

        This is the fix for F8. When a network's last port leaves, the OVS
        agent reclaims its local VLAN right after this call
        (treat_devices_removed calls the extensions before port_unbound),
        and it processes added ports before removed ones, so the VLAN can
        reach another network no earlier than its next iteration. Removing
        the gateway here therefore leaves no moment in which a leftover
        shares a VLAN with somebody else's network. Before this, nothing
        reacted, and t9 had an unrelated network inherit the VLAN and inject
        into the domain (REPORT-ISOLATION.md 4.7).

        The port is already gone from OVSDB when this runs, so
        local_network_vlans() no longer counts it. Routes are still left
        alone: the instance's address is the *server's* to withdraw, through
        routes_updated(remove=...), and removing it here would race that
        message. Only the gateway is node-local state.
        """
        with self._lock:
            self._reconcile_all_gateways()
