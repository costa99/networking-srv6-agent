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

"""Srv6Plugin -- the service plugin behind the srv6 API.

Enabled with `service_plugins = ...,srv6`. It owns three things: allocation
of each domain's SID function id, translation of Neutron state into the
payload agents consume -- admin TE paths included, resolved to SIDs here --
and the decision of *when* to tell agents anything. It programs no kernel
state itself -- that is entirely the agent's job.

This is the old networking-bgpvpn SRv6 driver with the driver/plugin split
removed. The precommit/postcommit discipline
survives without the hook framework: write inside the transaction, fan out
RPC after it commits, and read anything the commit destroys BEFORE it (delta
D5 -- a postcommit hook that looked up the function id found it already
released, and every agent was silently left holding a stale VRF).

Never decorate the CRUD methods themselves with CONTEXT_WRITER: the fan-out
would then run inside the transaction and agents could be told about state
that later rolls back.
"""

import collections
import datetime
import ipaddress

from neutron import worker as neutron_worker
from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from neutron_lib import rpc as n_rpc
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import timeutils

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_link_states as ls_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def
from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.common import config
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.db import te_db
from networking_srv6_agent.extensions import srv6 as srv6_ext
from networking_srv6_agent.extensions import srv6_link_states as ls_ext
from networking_srv6_agent.extensions import srv6_te as te_ext
from networking_srv6_agent.services import pce
from networking_srv6_agent.services import rpc
from networking_srv6_agent.services import te_liveness
from networking_srv6_agent.services import te_reopt


LOG = logging.getLogger(__name__)


def _prefix_for(address):
    return '%s/%d' % (address, 128 if ':' in address else 32)


def _route_for(port, address, host):
    """One payload route. The full sync and the incremental path share it.

    network_id is for the agent's per-route MTU check;
    port_id is what per-VM TE precedence resolves on.
    """
    return {'prefix': _prefix_for(address),
            'host': host,
            'port_id': port['id'],
            'network_id': port.get('network_id')}


def _normalise_filters(filters):
    """Accept the legacy tenant_id filter as project_id."""
    filters = dict(filters or {})
    if 'tenant_id' in filters and 'project_id' not in filters:
        filters['project_id'] = filters.pop('tenant_id')
    return filters


def _only(res, fields):
    if fields:
        return {k: v for k, v in res.items() if k in fields}
    return res


@registry.has_registry_receivers
class Srv6Plugin(srv6_ext.Srv6PluginBase):

    supported_extension_aliases = [srv6_def.ALIAS, loc_def.ALIAS,
                                   te_def.ALIAS, ls_def.ALIAS]
    # A POST of a list reaches create_<resource>_bulk in one call, so a
    # link-state batch is stored in one transaction. Only srv6_link_states
    # allows bulk at all (its get_resources); the others refuse a list.
    __native_bulk_support = True

    def __init__(self):
        super().__init__()
        config.register_server_opts()
        self.conf = cfg.CONF[config.SRV6_SERVER_GROUP]
        self.agent_rpc = rpc.Srv6AgentNotifyAPI()
        self._pool_synced = False
        self.encoding = self.conf.dataplane_encoding
        self._validate_function_id_ranges()
        self._load_topology()
        LOG.info("SRv6 service plugin initialised (vrf_table_base=%(base)s, "
                 "function_id_ranges=%(ranges)s, dataplane_encoding=%(enc)s, "
                 "reservable_fraction=%(frac)s)",
                 {'base': self.conf.vrf_table_base,
                  'ranges': self.conf.function_id_ranges,
                  'enc': self.encoding,
                  'frac': self.conf.reservable_fraction})

    def _load_topology(self):
        """The underlay for bandwidth-reserved TE paths, read once.

        fabric_locators {node: locator} and capacity {(a, b): Mbit/s}, empty
        without [srv6] topology_file. capacity is what admission may fill:
        the file's capacity times reservable_fraction, so the rest of every
        link stays free for traffic without a reservation.
        """
        fraction = self.conf.reservable_fraction
        if not 0 < fraction <= 1:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] reservable_fraction', opt_value=fraction)
        if not 0 < self.conf.te_reopt_low < self.conf.te_reopt_high:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] te_reopt_low/te_reopt_high',
                opt_value='%s/%s (need 0 < low < high)' % (
                    self.conf.te_reopt_low, self.conf.te_reopt_high))
        # link_capacity: the file's Mbit/s, for the link-state view
        self.fabric_locators, self.capacity, self.link_capacity = {}, {}, {}
        if not self.conf.topology_file:
            return
        try:
            self.fabric_locators, raw = pce.load(self.conf.topology_file)
        except (OSError, pce.TopologyError) as e:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] topology_file', opt_value=str(e))
        self.link_capacity = raw
        # round() first: 100 * 0.29 is 28.999999999999996, not 29
        self.capacity = {link: int(round(mbps * fraction, 6))
                         for link, mbps in raw.items()}

    @property
    def usid(self):
        return self.encoding == constants.ENCODING_USID

    def _validate_function_id_ranges(self):
        """Refuse to start with function ids outside the LIB.

        At startup rather than on the first API call. Ids already
        allocated outside the range are kept and warned about by
        sync_allocation_pool, as for any range change.
        """
        try:
            srv6_db.parse_function_id_ranges(self.conf.function_id_ranges)
        except ValueError as e:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] function_id_ranges', opt_value=str(e))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _ensure_pool(self, context):
        """Populate the function id pool once, lazily.

        Lazily rather than in __init__ because the plugin is constructed
        before the database is necessarily reachable, and a server that
        cannot start because of an unreachable DB is harder to diagnose
        than one that fails on the first API call.
        """
        if self._pool_synced:
            return
        ranges = srv6_db.parse_function_id_ranges(self.conf.function_id_ranges)
        try:
            srv6_db.sync_allocation_pool(context, ranges)
        except db_exc.DBDuplicateEntry:
            # Two sync_state calls landing together -- every agent's first
            # one after a server restart -- both find the newly configured
            # ids missing and both insert them; the second fails on the
            # primary key (seen on the testbed when function_id_ranges moved
            # to the microSID LIB). The rows are there now, so one more
            # pass inserts nothing and completes the sync.
            LOG.debug("SRv6 function id pool populated concurrently; "
                      "re-synchronising")
            srv6_db.sync_allocation_pool(context, ranges)
        self._pool_synced = True

    @staticmethod
    def _core_plugin():
        return directory.get_plugin()

    def _vrf_table(self, function_id):
        return sid.vrf_table(function_id, self.conf.vrf_table_base)

    @staticmethod
    def _owns(context, domain):
        return context.is_admin or domain['project_id'] == context.project_id

    def _get_domain_checked(self, context, domain_id):
        """The domain, if the caller may act on it; else NotFound.

        Association requests are authorised by neutron against the
        association's own project_id. That says nothing about the PARENT, so
        the parent's ownership is checked here. NotFound rather than
        NotAuthorized, so another project's domain id reveals nothing.

        (ADMIN_OR_PARENT_OWNER_* cannot do this: neutron resolves
        ext_parent_owner only for the parents listed in neutron-lib's
        EXT_PARENT_RESOURCE_MAPPING -- floatingip, router, local_ip, qos
        policy -- so for srv6_domain the rule could never pass.)
        """
        domain = domain_db.get_domain(context, domain_id)
        if domain is None or not self._owns(context, domain):
            raise srv6_ext.Srv6DomainNotFound(id=domain_id)
        return domain

    # ------------------------------------------------------------------
    # payload construction
    # ------------------------------------------------------------------
    def _network_info(self, context, network_id):
        """The subnets and MTU an agent needs to build a gateway port."""
        plugin = self._core_plugin()
        network = plugin.get_network(context, network_id)
        subnets = []
        for subnet_id in network.get('subnets', []):
            subnet = plugin.get_subnet(context, subnet_id)
            if subnet.get('ip_version') not in (4, 6):
                continue
            subnets.append({
                'id': subnet['id'],
                'cidr': subnet['cidr'],
                'ip_version': subnet['ip_version'],
                'gateway_ip': subnet.get('gateway_ip'),
            })
        return {
            'network_id': network_id,
            'mtu': network.get('mtu'),
            'subnets': subnets,
        }

    def _routes_for_domain(self, context, network_ids, locators):
        """One route per port that is bound to a host with a known locator.

        A port on a host that has never called sync_state is skipped rather
        than advertised: guessing a SID for an unknown locator would install
        a blackhole on every other node. Each route keeps its port_id --
        per-VM TE precedence resolves on it -- and its network_id. TE
        segments are attached afterwards, by _attach_segments.
        """
        if not network_ids:
            return []
        plugin = self._core_plugin()
        routes = []
        ports = plugin.get_ports(
            context, filters={'network_id': list(network_ids)})
        for port in ports:
            if not self._port_is_relevant(port):
                continue
            host = port.get(portbindings.HOST_ID) or port.get('host')
            if not host:
                continue
            if not locators.get(host):
                LOG.debug("skipping port %(p)s: host %(h)s has not "
                          "registered an SRv6 locator yet",
                          {'p': port['id'], 'h': host})
                continue
            for fixed_ip in port.get('fixed_ips', []):
                address = fixed_ip.get('ip_address')
                if not address:
                    continue
                routes.append(_route_for(port, address, host))
        return routes

    @staticmethod
    def _port_is_relevant(port):
        """Only real, bound, up workload ports carry tenant traffic."""
        device_owner = port.get('device_owner', '')
        if device_owner.startswith(n_const.DEVICE_OWNER_NETWORK_PREFIX):
            # DHCP, router and other infrastructure ports are not workloads.
            return False
        return port.get('status') == n_const.PORT_STATUS_ACTIVE

    def _build_domain_payload(self, context, domain, locators=None,
                              dead_hosts=None):
        """Full desired state of one domain, as agents consume it.

        Built with an ELEVATED context whoever triggered it. A shared network
        is allowed in a domain, and the instances on it may
        belong to other projects: under the owner's own context their ports
        are invisible, so their routes would silently be missing.

        dead_hosts only steers TE paths around dead waypoints; the payload's
        `locators` stay complete, because a dead node's VMs keep their
        routes and decap SIDs on every other node.
        """
        admin_context = context.elevated()
        if locators is None:
            locators = srv6_db.get_host_locators(admin_context)
        function_id = srv6_db.get_function_id(admin_context, domain['id'])
        if function_id is None:
            LOG.warning("SRv6 domain %s has no function id allocated; "
                        "skipping", domain['id'])
            return None
        network_ids = domain.get(srv6_def.NETWORKS) or []
        return {
            'id': domain['id'],
            'function_id': function_id,
            'behavior': (domain.get(srv6_def.SRV6_BEHAVIOR) or
                         constants.DEFAULT_BEHAVIOR),
            'vrf_table': self._vrf_table(function_id),
            'networks': [self._network_info(admin_context, net_id)
                         for net_id in network_ids],
            'routes': self._attach_segments(
                admin_context, domain['id'],
                self._routes_for_domain(admin_context, network_ids,
                                        locators),
                locators, dead_hosts),
            # Fabric nodes too (topology_file): their uN may be the first
            # segment of a computed path, and the agents' edge filter only
            # admits SIDs inside a locator it knows. Registered hosts win.
            'locators': {**self.fabric_locators, **locators},
            # Additive, so PAYLOAD_VERSION stays 1: an older agent ignores
            # it, and a missing key means full SIDs to a newer one.
            'encoding': self.encoding,
        }

    def _push_domain(self, context, domain):
        payload = self._build_domain_payload(context, domain)
        if payload is not None:
            self.agent_rpc.domain_updated(context, payload)

    def _push_domain_by_id(self, context, domain_id):
        domain = domain_db.get_domain(context.elevated(), domain_id)
        if domain is None:
            LOG.debug("SRv6 domain %s vanished before it could be pushed",
                      domain_id)
            return
        self._push_domain(context, domain)

    # ------------------------------------------------------------------
    # domains
    # ------------------------------------------------------------------
    def create_srv6_domain(self, context, srv6_domain):
        body = srv6_domain[srv6_def.RESOURCE_NAME]
        self._ensure_pool(context)
        try:
            # One transaction for both: the two DB helpers carry their own
            # CONTEXT_WRITER, which JOINS this one. Without the block they
            # would commit separately, and an exhausted pool would leave a
            # committed domain holding no function id.
            with db_api.CONTEXT_WRITER.using(context):
                domain = domain_db.create_domain(context, body)
                domain[srv6_def.SID_FUNCTION] = srv6_db.allocate_function_id(
                    context, domain['id'])
        except srv6_db.NoFunctionIdAvailable:
            raise srv6_ext.Srv6NoFunctionIdAvailable()
        LOG.info("SRv6 domain %(d)s allocated function id %(f)s (vrf table "
                 "%(t)s)", {'d': domain['id'],
                            'f': domain[srv6_def.SID_FUNCTION],
                            't': self._vrf_table(
                                domain[srv6_def.SID_FUNCTION])})
        # No RPC: a domain with no network has no presence on any node
        # (F11: handle_sync_state and the agents now agree).
        return domain

    def get_srv6_domain(self, context, id, fields=None):
        domain = domain_db.get_domain(context, id, fields)
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=id)
        return domain

    def get_srv6_domains(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        return domain_db.get_domains(context, _normalise_filters(filters),
                                     fields)

    def update_srv6_domain(self, context, id, srv6_domain):
        domain = domain_db.update_domain(
            context, id, srv6_domain[srv6_def.RESOURCE_NAME])
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=id)
        self._push_domain(context, domain)
        return domain

    def delete_srv6_domain(self, context, id):
        # BEFORE the delete (D5): the allocation FK is ON DELETE SET NULL
        # and the TE paths' FK is ON DELETE CASCADE, so after the commit
        # there is nothing left to read either from.
        function_id = srv6_db.get_function_id(context, id)
        te_paths = te_db.get_te_paths(context.elevated(), id)
        with db_api.CONTEXT_WRITER.using(context):
            if domain_db.delete_domain(context, id) is None:
                raise srv6_ext.Srv6DomainNotFound(id=id)
            srv6_db.release_function_id(context, id)
        if te_paths:
            # A tenant action erasing admin configuration leaves a trace.
            LOG.warning("SRv6 domain %(d)s deleted by project %(p)s; its "
                        "%(n)d admin TE path(s) went with it: %(ids)s",
                        {'d': id, 'p': context.project_id,
                         'n': len(te_paths),
                         'ids': ', '.join(p['id'] for p in te_paths)})
        if function_id is None:
            LOG.warning("SRv6 domain %s had no function id; agents were not "
                        "told to tear down (nothing to name)", id)
            return
        LOG.info("SRv6 domain %(d)s deleted; telling agents to remove "
                 "function id %(f)s (vrf table %(t)s)",
                 {'d': id, 'f': function_id,
                  't': self._vrf_table(function_id)})
        self.agent_rpc.domain_deleted(context, id, function_id,
                                      self._vrf_table(function_id))

    # ------------------------------------------------------------------
    # network associations
    # ------------------------------------------------------------------
    def _validate_net_assoc(self, context, domain, network_id):
        plugin = self._core_plugin()
        # The caller's context: a network they cannot see is NotFound, so
        # the request cannot probe for other projects' networks.
        network = plugin.get_network(context, network_id)
        if network['project_id'] != domain['project_id']:
            raise srv6_ext.Srv6NetworkNotOwned(network_id=network_id,
                                               domain_id=domain['id'])
        if network.get('router:external'):
            raise srv6_ext.Srv6ExternalNetworkNotAllowed(network_id=network_id)
        # Elevated: a router interface is refused whoever owns the router.
        # ROUTER_INTERFACE_OWNERS, not the bare router_interface owner: the
        # tuple also covers the L3HA and DVR interface owners, and without
        # them the check passes on a network that is in fact routed.
        router_ports = plugin.get_ports(context.elevated(), filters={
            'network_id': [network_id],
            'device_owner': list(n_const.ROUTER_INTERFACE_OWNERS)})
        if router_ports:
            raise srv6_ext.Srv6RouteredNetworkNotAllowed(
                network_id=network_id,
                router_id=router_ports[0]['device_id'])
        self._check_one_domain(context, domain, network_id)
        self._check_no_overlap(
            context, domain, network_id,
            [s['cidr'] for s in self._subnets_of(context, network_id)])

    def _check_one_domain(self, context, domain, network_id):
        """A network belongs to one domain (F3).

        Two domains holding one network would each put a gateway with the
        same address on the same L2 segment, and the network's instances
        would be routed into whichever domain answered ARP -- t9 I8 saw
        red's r1 reach blue's instance through such a network. The unique
        constraint on network_id closes the race this check leaves open;
        create_srv6_domain_network_association maps it back here.
        """
        for other in domain_db.get_domain_ids_for_network(
                context.elevated(), network_id):
            if other == domain['id']:
                raise srv6_ext.Srv6DomainNetAssocAlreadyExists(
                    network_id=network_id, domain_id=domain['id'])
            raise srv6_ext.Srv6NetworkInAnotherDomain(
                network_id=network_id, other_domain_id=other)

    def _subnets_of(self, context, network_id):
        """id, cidr and ip_version of every subnet; elevated, read only."""
        return self._core_plugin().get_subnets(
            context.elevated(), filters={'network_id': [network_id]},
            fields=['id', 'cidr', 'ip_version'])

    def _check_no_overlap(self, context, domain, network_id, cidrs):
        """No two networks of a domain may have overlapping subnets (F10).

        They share one routing table: the second connected route would
        either be refused by the kernel or shadow the first, and the
        domain would route to whichever gateway port came up last. Checked
        per IP version; elevated, because a shared network's subnets may
        belong to the owner only in name.
        """
        new = []
        for cidr in cidrs:
            try:
                new.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        if not new:
            return
        for other_id in domain.get(srv6_def.NETWORKS) or []:
            if other_id == network_id:
                continue
            for subnet in self._subnets_of(context, other_id):
                try:
                    other = ipaddress.ip_network(subnet['cidr'],
                                                 strict=False)
                except ValueError:
                    continue
                for net in new:
                    if net.version == other.version and net.overlaps(other):
                        raise srv6_ext.Srv6OverlappingSubnets(
                            cidr=str(net), network_id=network_id,
                            other_cidr=str(other),
                            other_network_id=other_id,
                            domain_id=domain['id'])

    def create_srv6_domain_network_association(self, context, srv6_domain_id,
                                               network_association):
        body = network_association[srv6_def.NET_ASSOC_RESOURCE_NAME]
        domain = self._get_domain_checked(context, srv6_domain_id)
        self._validate_net_assoc(context, domain, body['network_id'])
        # Forced, never the caller's: an admin acting on a tenant's behalf
        # still produces the tenant's association.
        body = dict(body, project_id=domain['project_id'])
        try:
            assoc = domain_db.create_net_assoc(context, srv6_domain_id, body)
        except db_exc.DBDuplicateEntry:
            # A concurrent attach got there first: to this domain or, since
            # the network_id unique constraint (F3), to another one.
            self._check_one_domain(context, domain, body['network_id'])
            raise srv6_ext.Srv6DomainNetAssocAlreadyExists(
                network_id=body['network_id'], domain_id=srv6_domain_id)
        self._push_domain_by_id(context, srv6_domain_id)
        return assoc

    def get_srv6_domain_network_association(self, context, id,
                                            srv6_domain_id, fields=None):
        self._get_domain_checked(context, srv6_domain_id)
        assoc = domain_db.get_net_assoc(context, id, srv6_domain_id, fields)
        if assoc is None:
            raise srv6_ext.Srv6DomainNetAssocNotFound(
                id=id, domain_id=srv6_domain_id)
        return assoc

    def get_srv6_domain_network_associations(self, context, srv6_domain_id,
                                             filters=None, fields=None,
                                             sorts=None, limit=None,
                                             marker=None, page_reverse=False):
        self._get_domain_checked(context, srv6_domain_id)
        return domain_db.get_net_assocs(context, srv6_domain_id,
                                        _normalise_filters(filters), fields)

    def delete_srv6_domain_network_association(self, context, id,
                                               srv6_domain_id):
        self._get_domain_checked(context, srv6_domain_id)
        if domain_db.delete_net_assoc(context, id, srv6_domain_id) is None:
            raise srv6_ext.Srv6DomainNetAssocNotFound(
                id=id, domain_id=srv6_domain_id)
        self._push_domain_by_id(context, srv6_domain_id)

    @registry.receives(resources.SUBNET, [events.BEFORE_CREATE])
    def _refuse_overlapping_subnet_in_domain(self, resource, event, trigger,
                                             payload=None):
        """F10 again, through the other door: a subnet added after attach.

        Only a request that names its cidr can be checked here: Neutron
        publishes no precommit event for subnets, and a subnet allocated
        from a pool gets its cidr after this event.
        """
        subnet = payload.latest_state or {}
        network_id = subnet.get('network_id')
        cidr = subnet.get('cidr')
        if not network_id or not isinstance(cidr, str):
            return
        admin_context = payload.context.elevated()
        for domain_id in domain_db.get_domain_ids_for_network(
                admin_context, network_id):
            domain = domain_db.get_domain(admin_context, domain_id)
            if domain is not None:
                self._check_no_overlap(admin_context, domain, network_id,
                                       [cidr])

    @registry.receives(resources.ROUTER_INTERFACE, [events.BEFORE_CREATE])
    def _refuse_router_interface_on_domain_network(self, resource, event,
                                                   trigger, payload=None):
        """Close the race the association check alone leaves open.

        Without this, attaching the network first and adding the router
        interface second bypasses _validate_net_assoc entirely. The L3
        plugin turns the exception into RouterInterfaceAttachmentConflict.
        """
        network_id = (payload.metadata or {}).get('network_id')
        if not network_id:
            return
        domain_ids = domain_db.get_domain_ids_for_network(
            payload.context.elevated(), network_id)
        if domain_ids:
            raise srv6_ext.Srv6NetworkInDomain(network_id=network_id,
                                               domain_id=domain_ids[0])

    # ------------------------------------------------------------------
    # locators (read-only, admin-only)
    # ------------------------------------------------------------------
    @staticmethod
    def _make_locator_dict(row, fields=None):
        updated_at = row['updated_at']
        res = {
            'id': row['host'],
            'host': row['host'],
            'locator': row['locator'],
            'updated_at': updated_at.isoformat() if updated_at else None,
        }
        if fields:
            return {k: v for k, v in res.items() if k in fields}
        return res

    def get_srv6_locators(self, context, filters=None, fields=None,
                          sorts=None, limit=None, marker=None,
                          page_reverse=False):
        hosts = set((filters or {}).get('host') or
                    (filters or {}).get('id') or [])
        return [self._make_locator_dict(row, fields)
                for row in srv6_db.get_host_locator_details(context)
                if not hosts or row['host'] in hosts]

    def get_srv6_locator(self, context, id, fields=None):
        for row in srv6_db.get_host_locator_details(context):
            if row['host'] == id:
                return self._make_locator_dict(row, fields)
        raise srv6_ext.Srv6LocatorNotFound(id=id)

    # ------------------------------------------------------------------
    # measured link state (PLAN-LINK-STATE-TE)
    # ------------------------------------------------------------------
    def create_srv6_link_state_bulk(self, context, srv6_link_states):
        return self._add_link_states(
            context, [item[ls_def.RESOURCE_NAME]
                      for item in srv6_link_states[ls_def.COLLECTION_NAME]])

    def create_srv6_link_state(self, context, srv6_link_state):
        return self._add_link_states(
            context, [srv6_link_state[ls_def.RESOURCE_NAME]])[0]

    def _add_link_states(self, context, items):
        """Validate and store one batch of measurements, whole or not at all.

        Every item must be a link of the topology file, measured by one of
        its two ends, at most once per batch; mbps and drops are null or a
        non-negative finite number (the collector's data moves traffic, so
        nothing else is trusted). The server's clock stamps the batch, and
        the batch deletes its reporters' rows older than two windows.
        """
        if not self.capacity:
            raise ls_ext.Srv6LinkStateInvalid(
                reason=_("link state needs an underlay topology "
                         "([srv6] topology_file)"))
        rows, seen = [], set()
        for item in items:
            row = {k: item[k] for k in ('reporter', 'from_node', 'to_node',
                                        'up', 'mbps', 'drops')}
            link = (row['from_node'], row['to_node'])
            name = '%s>%s' % link
            if link not in self.capacity:
                raise ls_ext.Srv6LinkStateInvalid(
                    reason=_("%s is not a link of the underlay topology")
                    % name)
            if row['reporter'] not in link:
                raise ls_ext.Srv6LinkStateInvalid(
                    reason=_("reporter %(r)s is not an end of %(l)s")
                    % {'r': row['reporter'], 'l': name})
            if (row['reporter'], link) in seen:
                raise ls_ext.Srv6LinkStateInvalid(
                    reason=_("%(r)s reported %(l)s twice")
                    % {'r': row['reporter'], 'l': name})
            seen.add((row['reporter'], link))
            # 1e7 Mbit/s = 10 Tbit/s: no link carries more; also NaN/inf
            for attr, kinds, top in (('mbps', (int, float), 1e7),
                                     ('drops', (int,), 2 ** 63)):
                v = row[attr]
                if v is not None and (isinstance(v, bool) or
                                      not isinstance(v, kinds) or
                                      not 0 <= v < top):
                    raise ls_ext.Srv6LinkStateInvalid(
                        reason=_("%(a)s of %(l)s must be null or a number "
                                 "from 0 to %(t)g") %
                        {'a': attr, 'l': name, 't': top})
            rows.append(row)
        # whole seconds: MySQL DATETIME rounds the fraction, which could
        # store a measurement up to half a second in the future
        now = timeutils.utcnow().replace(microsecond=0)
        link_state_db.add_link_states(
            context, rows, now, now - datetime.timedelta(
                seconds=2 * self.conf.link_state_window))
        return [dict(r, id='%s:%s' % (r['from_node'], r['to_node']),
                     measured_at=now.isoformat()) for r in rows]

    def _link_state_view(self, context):
        """{(from, to): entry} of every link of the topology file.

        capacity_mbps is the file's capacity, reserved_mbps what the
        bandwidth reservations hold on the link (the books), shown next to
        what is measured (U-a). A link nobody has reported yet has null
        measurements and stale=true.

        Per directed link, the sender's measurement is the load (what was
        offered to the link) and the receiver's is what got through
        (delivered_mbps); a link only one end reports (leaf-host) has just
        that one. mbps comes from the sender if it is fresh, else from the
        receiver if fresh, else from whichever is newer (then stale). A
        measurement is fresh for link_state_stale_after seconds;
        peak_mbps is the highest mbps of that end in link_state_window
        (L4, L9). up is False if a fresh report from either end says so
        (L8; a silent node is step 4's).
        """
        now = timeutils.utcnow()
        fresh = now - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        start = now - datetime.timedelta(seconds=self.conf.link_state_window)
        hist = collections.defaultdict(list)      # (from, to, reporter)
        for r in link_state_db.get_link_states(context):   # oldest first
            hist[(r['from_node'], r['to_node'], r['reporter'])].append(r)
        books = self._books(context)
        view = {}
        for a, b in sorted(self.capacity):
            entry = {'id': '%s:%s' % (a, b), 'from_node': a, 'to_node': b,
                     'capacity_mbps': self.link_capacity.get((a, b)),
                     'reserved_mbps': books.get((a, b), 0)}
            view[(a, b)] = entry
            ends = [h for h in (hist.get((a, b, a)), hist.get((a, b, b)))
                    if h]
            if not ends:
                entry.update(dict.fromkeys(
                    ('reporter', 'up', 'mbps', 'drops', 'delivered_mbps',
                     'peak_mbps', 'measured_at', 'age')), stale=True)
                continue
            live = [h for h in ends if h[-1]['measured_at'] >= fresh]
            main = live[0] if live else max(
                ends, key=lambda h: h[-1]['measured_at'])
            last = main[-1]
            rx = hist.get((a, b, b))
            entry.update({
                'reporter': last['reporter'],
                'up': (all(h[-1]['up'] for h in live) if live
                       else last['up']),
                'mbps': last['mbps'],
                'drops': last['drops'],
                'delivered_mbps': (rx[-1]['mbps'] if rx and
                                   rx[-1]['measured_at'] >= fresh else None),
                'peak_mbps': max((r['mbps'] for r in main
                                  if r['measured_at'] > start and
                                  r['mbps'] is not None), default=None),
                'measured_at': last['measured_at'].isoformat(),
                'age': int((now - last['measured_at']).total_seconds()),
                'stale': not live,
            })
        return view

    def get_srv6_link_states(self, context, filters=None, fields=None,
                             sorts=None, limit=None, marker=None,
                             page_reverse=False):
        filters = filters or {}
        return [_only(entry, fields)
                for entry in self._link_state_view(context).values()
                if all(entry[k] in filters[k]
                       for k in ('id', 'from_node', 'to_node')
                       if filters.get(k))]

    def get_srv6_link_state(self, context, id, fields=None):
        entry = self._link_state_view(context).get(tuple(id.split(':', 1)))
        if entry is None:
            raise ls_ext.Srv6LinkStateNotFound(id=id)
        return _only(entry, fields)

    def _check_capabilities(self, host, capabilities):
        """Name a node that cannot honour dataplane_encoding. Logs only.

        Deployment-wide by design (USID-PLAN.md D3): the server does not
        route around such a node, but it does not let it fail in silence.
        """
        required = constants.ENCODING_REQUIRES[self.encoding]
        if not required:
            return
        if capabilities is None:
            LOG.warning("SRv6 agent on %(host)s did not report its "
                        "capabilities (an older agent?); it may not support "
                        "dataplane_encoding = %(enc)s",
                        {'host': host, 'enc': self.encoding})
            return
        missing = [c for c in required if c not in capabilities]
        if missing:
            LOG.error("SRv6 agent on %(host)s lacks %(missing)s, which "
                      "dataplane_encoding = %(enc)s needs (kernel >= 6.1 and "
                      "iproute2 >= 6.1). Traffic through and to that node "
                      "will fail.",
                      {'host': host, 'missing': ', '.join(missing),
                       'enc': self.encoding})

    @staticmethod
    def _check_usid_locator(admin_context, host, locator):
        """F3216, a node C-SID in the GIB, and the block everyone else uses.

        Every container begins with ONE block, so a path through a node
        in another block cannot be packed; refusing the locator is clearer
        than routes that silently fall back to full SIDs.
        """
        sid.check_usid_locator(locator)
        block = sid.usid_block(locator)
        for other_host, other in srv6_db.get_host_locators(
                admin_context).items():
            if other_host == host or not other:
                continue
            try:
                other_block = sid.usid_block(other)
            except sid.InvalidLocator:
                continue
            if other_block != block:
                raise sid.InvalidLocator(
                    _("it is in block %(b)s but %(h)s's locator %(o)s is in "
                      "%(ob)s; microSID needs one block for every node")
                    % {'b': block, 'h': other_host, 'o': other,
                       'ob': other_block})

    # ------------------------------------------------------------------
    # traffic engineering (P6, admin-only)
    # ------------------------------------------------------------------
    def _dead_hosts(self, admin_context):
        """The hosts whose node is down, judged by Neutron's agent liveness.

        A node is dead when its Open vSwitch agent -- the process that hosts
        the SRv6 extension -- has missed its heartbeats for agent_down_time.
        The locator registry cannot say this: its rows are never expired.

        Computed from the database each time, never cached in the process:
        API workers resolve paths too, and one worker's memory would
        disagree with another's. A host with no agent row is not dead, and
        so is every host when the check is off (te_liveness_interval = 0)
        or the question cannot be answered: an unanswerable query leaves the
        paths steered, as before this check existed, instead of rerouting
        every one of them on a database hiccup.

        Fabric nodes (spines, leaves) run no agent: one is dead when its
        link-state reports stopped (L8, _silent_fabric), so an operator
        path through a dead spine goes DEGRADED too. The two sources are
        read apart: either failing leaves the other's answer.
        """
        if not self.conf.te_liveness_interval:
            return frozenset()
        dead = set()
        get_agents = getattr(self._core_plugin(), 'get_agents', None)
        try:
            if get_agents is not None:
                agents = get_agents(
                    admin_context,
                    filters={'agent_type': [n_const.AGENT_TYPE_OVS]})
                dead.update(agent['host'] for agent in agents
                            if not agent['alive'])
        except Exception:
            LOG.exception("SRv6: cannot read agent liveness; treating "
                          "every TE waypoint as alive")
        try:
            dead.update(self._silent_fabric(admin_context)[0])
        except Exception:
            LOG.exception("SRv6: cannot read the fabric link state; "
                          "treating every fabric node as alive")
        return frozenset(dead)

    def _transit_sids(self, via_hosts, locators, dead_hosts=()):
        """The transit SIDs of via_hosts in order, or None if one is unusable.

        A host is unusable when it has no registered locator or when its
        node is dead (dead_hosts, see _dead_hosts): a dead waypoint
        blackholes the path exactly as a missing one cannot be encoded.

        Each host's End SID, <locator>:fff0::, or in usid mode its uN, the
        locator's own address -- the agent packs those into containers.

        None, not a shorter list: a path missing a waypoint is not the path
        the admin asked for, and quietly dropping a hop is the silent
        partial steering the locator API exists to prevent. The caller
        falls back to the direct route instead, and the path reports
        DEGRADED.

        A host with no registered locator may be a fabric node of the
        topology file (a spine or leaf, which runs no agent): its locator
        comes from there.
        """
        locators = {**self.fabric_locators, **locators}
        if any(not locators.get(host) or host in dead_hosts
               for host in via_hosts):
            return None
        if self.usid:
            try:
                return [sid.node_sid(locators[host]) for host in via_hosts]
            except sid.InvalidLocator as e:
                LOG.error("SRv6: a via host's locator is not usable for "
                          "microSID: %s", e)
                return None
        return [sid.format_sid(locators[host],
                               constants.NODE_END_FUNCTION_ID)
                for host in via_hosts]

    def _route_transit(self, domain_id, route, by_port, by_host, locators,
                       source=None, dead_hosts=()):
        """The transit SIDs one set of paths gives a route, or None.

        by_port and by_host are the paths of one rank pair: the any-source
        paths, or the paths of one source. Port path > host path, and a
        port path with no vias is an explicit "direct" that still wins
        over a host path.

            None   no path of this set applies to the route
            []     a path applies and says direct: no vias, or DEGRADED (a
                   via host has no registered locator or its node is dead,
                   or the source has no registered locator).
                   Degraded is direct, not "fall through to the next path
                   down", which the admin did not ask for either.
            [...]  the transit SIDs, in traversal order
        """
        via_hosts = by_port.get(route.get('port_id'))
        if via_hosts is None:
            via_hosts = by_host.get(route.get('host'))
        if via_hosts is None:
            return None
        if not via_hosts:
            return []
        transit = None
        if source is None or locators.get(source):
            transit = self._transit_sids(via_hosts, locators, dead_hosts)
        if transit is None:
            LOG.warning("SRv6 domain %(d)s: the TE path toward %(p)s%(s)s "
                        "names a host with no registered locator or a dead "
                        "node (via %(v)s, dead: %(x)s); routing it directly",
                        {'d': domain_id, 'p': route['prefix'],
                         's': ' from %s' % source if source else '',
                         'v': ', '.join(via_hosts),
                         'x': ', '.join(sorted(
                             set(via_hosts) & set(dead_hosts))) or 'none'})
            return []
        return transit

    def _attach_segments(self, admin_context, domain_id, routes, locators,
                         dead_hosts=None):
        """Give each steered route its transit SIDs. Mutates `routes`.

        One payload goes to every node, so a route carries the answer for
        every ingress at once:

            segments             the any-source answer: what every ingress
                                 pushes unless it has an entry below
            segments_by_source   {source host: transit SIDs}, one entry per
                                 source whose paths apply to the route;
                                 the agent of that host uses it instead

        Precedence per route and ingress, highest first (FEATURE-TE-SOURCE-
        HOST, user decision U1):

            (port, source) > (host, source) > (port, any) > (host, any)
            > direct

        The two source ranks live in segments_by_source, the two any-source
        ranks in segments; the agent's choice of its own entry over
        `segments` is what puts every source path above every any-source
        path. An entry may be [] -- an explicit or degraded "direct from
        this source" -- and must still be sent, or that ingress would fall
        back to `segments`.

        A source that is the route's own host gets no entry: that node never
        encapsulates its own VMs' traffic (the agent skips the route). Both
        keys are added only when non-empty, so a route nothing steers stays
        byte-for-byte the P5 route.

        Transit SIDs only: the agent appends the decap SID, from the locator
        of the route's host, as it always has.

        dead_hosts is what _dead_hosts returned; None asks it here, after the
        no-paths return, so a domain with no TE path costs no agent query.
        A path through a dead host is direct, like one through an unknown
        host: [] for every route it covers.
        """
        paths = te_db.get_te_paths_for_domain(admin_context, domain_id)
        by_port, by_host = paths['by_port'], paths['by_host']
        by_source = paths.get('by_source') or {}
        # by_source too: a domain whose only paths name a source must not
        # leave here unsteered.
        if not by_port and not by_host and not by_source:
            return routes
        if dead_hosts is None:
            dead_hosts = self._dead_hosts(admin_context)
        for route in routes:
            transit = self._route_transit(domain_id, route, by_port, by_host,
                                          locators, dead_hosts=dead_hosts)
            if transit:
                route['segments'] = transit
            per_source = {}
            for source in sorted(by_source):
                if source == route.get('host'):
                    continue
                transit = self._route_transit(
                    domain_id, route, by_source[source]['by_port'],
                    by_source[source]['by_host'], locators, source=source,
                    dead_hosts=dead_hosts)
                if transit is not None:
                    per_source[source] = transit
            if per_source:
                route['segments_by_source'] = per_source
        return routes

    def _destination_of(self, admin_context, path):
        """The compute host a path's traffic is headed for, or None."""
        port_id = path.get(te_def.DESTINATION_PORT_ID)
        if not port_id:
            return path.get(te_def.DESTINATION_HOST)
        try:
            port = self._core_plugin().get_port(admin_context, port_id)
        except n_exc.PortNotFound:
            return None
        return port.get(portbindings.HOST_ID) or None

    @staticmethod
    def _drop_leading(transit, own_sid):
        """transit without its leading occurrences of own_sid.

        The server-side twin of the agent's _transit_segments, for a path
        with a source: that node is the ingress, and a first waypoint equal
        to itself is dropped before the push. Compared as addresses.
        """
        own = ipaddress.IPv6Address(own_sid)
        transit = list(transit)
        while transit and ipaddress.IPv6Address(transit[0]) == own:
            transit.pop(0)
        return transit

    def _resolve_te_path(self, context, path, locators, function_id,
                         dead_hosts=()):
        """Attach `segments` and `status` -- what the server resolved.

        ACTIVE:   [<via End SIDs>..., <destination decap SID>]
        DEGRADED: [], because a via host or the destination has no locator,
                  a via host's node is dead (dead_hosts), or the port is
                  unbound; the routes go direct meanwhile.
                  For a path with a source_host, also when the source has
                  no locator, or the destination is the source itself (a
                  port path whose VM has moved onto it): its agent never
                  encapsulates traffic to its own VMs, so the path steers
                  nothing.

        For a path with a source_host the source is the only ingress, so
        `segments` leaves out leading SIDs of that node, exactly as its
        agent drops them: the list shown is the list pushed.

        `encoded_segments` is what the ingress will actually push: the
        same list with full SIDs, the C-SID containers in usid mode --
        computed by the same sid.pack_next_csid the agent calls, so the
        two cannot disagree.

        Server resolution, not an agent acknowledgement: ACTIVE says the
        payload carries this list, not that any kernel holds it.
        """
        path = dict(path)
        source = path.get(te_def.SOURCE_HOST)
        destination = self._destination_of(context.elevated(), path)
        transit = self._transit_sids(path[te_def.VIA_HOSTS], locators,
                                     dead_hosts)
        active = bool(destination and locators.get(destination) and
                      transit is not None and function_id is not None)
        if active and source:
            active = bool(locators.get(source)) and destination != source
        if active:
            own = self._transit_sids([source], locators) if source else None
            if own:
                transit = self._drop_leading(transit, own[0])
            segments = transit + [
                sid.format_sid(locators[destination], function_id)]
            path[te_def.SEGMENTS] = segments
            path[te_def.ENCODED_SEGMENTS] = self._encoded(segments)
            path[te_def.STATUS] = te_def.STATUS_ACTIVE
            return path
        LOG.warning("SRv6 TE path %(id)s is DEGRADED: destination %(dst)s, "
                    "source %(src)s, via %(via)s; not every host on it has "
                    "a registered locator or is dead, the port is unbound, "
                    "or the destination is the source itself, so its "
                    "routes go direct", {'id': path['id'], 'dst': destination,
                                         'src': source or 'any',
                                         'via': path[te_def.VIA_HOSTS]})
        path[te_def.SEGMENTS] = []
        path[te_def.ENCODED_SEGMENTS] = []
        path[te_def.STATUS] = te_def.STATUS_DEGRADED
        return path

    def _encoded(self, segments):
        """The pushed form of a segment list; the agent's _encode twin."""
        if not self.usid:
            return list(segments)
        try:
            return sid.pack_next_csid(segments)
        except sid.InvalidLocator:
            return list(segments)

    def _resolved(self, context, path, locators=None):
        admin_context = context.elevated()
        if locators is None:
            locators = srv6_db.get_host_locators(admin_context)
        return self._resolve_te_path(
            context, path, locators,
            srv6_db.get_function_id(admin_context, path['domain_id']),
            self._dead_hosts(admin_context))

    @staticmethod
    def _validate_via_hosts(via_hosts, locators):
        """A 400, never a silent `segments: []`."""
        if len(via_hosts) > constants.MAX_TE_VIA_HOSTS:
            raise te_ext.Srv6TePathInvalid(
                reason=_("at most %(max)d via_hosts are allowed, got %(n)d")
                % {'max': constants.MAX_TE_VIA_HOSTS, 'n': len(via_hosts)})
        unknown = [host for host in via_hosts if host not in locators]
        if unknown:
            raise te_ext.Srv6TePathInvalid(
                reason=_("via_hosts %s have no registered SRv6 locator (see "
                         "GET /v2.0/srv6_locators)") % ', '.join(unknown))

    def _validate_destination_port(self, admin_context, domain, port_id):
        """The port, if it may be a destination of this domain; else 400."""
        try:
            port = self._core_plugin().get_port(admin_context, port_id)
        except n_exc.PortNotFound:
            raise te_ext.Srv6TePathInvalid(
                reason=_("destination_port_id %s does not exist") % port_id)
        if port['network_id'] not in (domain.get(srv6_def.NETWORKS) or []):
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %(p)s is on network %(n)s, which is not "
                         "attached to SRv6 domain %(d)s")
                % {'p': port_id, 'n': port['network_id'],
                   'd': domain['id']})
        return port

    @staticmethod
    def _validate_source_host(source, locators, host, port):
        """A 400 for a source that could never steer anything.

        The source must be an SRv6 node: its agent is the one that would
        push the path. And it must not be the destination: a node never
        encapsulates traffic toward its own VMs, so such a path would be
        accepted and do nothing. For a port path that is checked against
        the port's binding NOW; a later migration onto the source makes the
        path inert, and _resolve_te_path then reports it DEGRADED.
        """
        if source not in locators:
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_host %s has no registered SRv6 locator "
                         "(see GET /v2.0/srv6_locators)") % source)
        if host and host == source:
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_host and destination_host are both %s: a "
                         "node never encapsulates the traffic toward its own "
                         "VMs, so the path would steer nothing") % source)
        if port and port.get(portbindings.HOST_ID) == source:
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %(p)s is bound on the source_host %(s)s: a "
                         "node never encapsulates the traffic toward its own "
                         "VMs, so the path would steer nothing")
                % {'p': port['id'], 's': source})

    @staticmethod
    def _books(context, exclude=None):
        """{(a, b): Mbit/s} every reservation holds, in every domain.

        Built from reserved_host, the host the hops were computed for,
        never from where a port is now (see _compute_hops). exclude: the
        id of a reservation being moved, which must not compete with
        itself (G1).
        """
        held = []
        for p in te_db.get_bandwidth_paths(context):
            if p['id'] == exclude:
                continue
            # rows from before reserved_host existed are all to a host
            end = p['reserved_host'] or p[te_def.DESTINATION_HOST]
            if end:
                held.append(([p[te_def.SOURCE_HOST]] + p[te_def.VIA_HOSTS] +
                             [end], p[te_def.BANDWIDTH]))
        return pce.reserved(held)

    # ------------------------------------------------------------------
    # failures of the underlay (PLAN-LINK-STATE-TE step 4)
    # ------------------------------------------------------------------
    def _silent_fabric(self, context):
        """(fabric nodes down, outage) from the link-state reporters (L8).

        A node that has posted link state and has posted nothing for
        link_state_stale_after seconds is down: its agent runs on the node
        itself, and a powered-off node's neighbours still see their NICs
        up, so silence is the only signal. A node that never posted is
        not down. Every reporter silent at once is an outage of the
        collection (expired credentials, the API unreachable), not a
        fabric failure: nothing is down, and outage is True.
        """
        last = link_state_db.get_last_seen(context)
        fresh = timeutils.utcnow() - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        silent = frozenset(r for r, t in last.items() if t < fresh)
        if last and silent == frozenset(last):
            return frozenset(), True
        return silent, False

    def _dead_links(self, context, silent, view=None):
        """The directed topology links down now.

        Reported down by a fresh end (or last seen down), or touching a
        silent node (L8). view: a _link_state_view already read.
        """
        view = self._link_state_view(context) if view is None else view
        return frozenset(link for link, entry in view.items()
                         if entry['up'] is False or
                         not silent.isdisjoint(link))

    def _live_capacity(self, context, view=None):
        """self.capacity without the links down now.

        All of it during an outage of the collection.
        """
        silent, outage = self._silent_fabric(context)
        dead = (frozenset() if outage else
                self._dead_links(context, silent, view))
        return {link: mbps for link, mbps in self.capacity.items()
                if link not in dead}

    def _measured_load(self, context, view=None):
        """{(a, b): Mbit/s}: each fresh link's peak in link_state_window.

        What the free capacity counts beside the books (L4): a link
        holds max(reserved, measured), so idle reservations still count
        and traffic without one counts too. A stale link is not here: it
        falls back to the books only. view: a _link_state_view already
        read.
        """
        view = self._link_state_view(context) if view is None else view
        # whole Mbit/s, the unit of reservations and capacities: idle
        # links' control chatter (~0.03) counts as 0, so equal paths still
        # tie and the choice stays deterministic (found live, step 6)
        return {link: round(entry['peak_mbps'])
                for link, entry in view.items()
                if not entry['stale'] and entry['peak_mbps'] is not None}

    def _load_series(self, context):
        """{(a, b): [(measured_at, mbps), ...]} oldest first, per link.

        The same end as the view's mbps: the sender's (offered load) if
        fresh, else the receiver's if fresh; a link with neither is not
        here. What the re-optimisation judges congestion on.
        """
        fresh = timeutils.utcnow() - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        hist = collections.defaultdict(list)
        for r in link_state_db.get_link_states(context):   # oldest first
            hist[(r['from_node'], r['to_node'], r['reporter'])].append(r)
        series = {}
        for a, b in self.capacity:
            for end in (a, b):
                h = hist.get((a, b, end))
                if h and h[-1]['measured_at'] >= fresh:
                    series[(a, b)] = [(r['measured_at'], r['mbps'])
                                      for r in h]
                    break
        return series

    def _reroute_reservation(self, context, path_id, cap, delete=False,
                             measured=None):
        """Move one reservation, in place, onto a path with room in cap (G1).

        Under the admission lock, like an admission: the books of every
        OTHER reservation, a new path from the same source to the same
        reserved host for the same bandwidth, the hops replaced in the
        row. measured ({link: Mbit/s}, re-optimisation): each link then
        holds max(reserved, measured) (L4), the measured load minus this
        reservation's own bandwidth on the links it crosses now (L10: its
        traffic is still there, and would be counted twice). The agents
        then swap the head-end route (ip route replace): make-before-
        break, no window without a route. No path: the
        reservation is deleted if delete (a failure, L6), else left.
        Returns (the reservation before, the new node path or None);
        (None, None) if it is gone.
        """
        with db_api.CONTEXT_WRITER.using(context):
            domain_db.lock_all_domains(context)
            old = next((p for p in te_db.get_bandwidth_paths(context)
                        if p['id'] == path_id), None)
            if old is None:
                return None, None
            end = old['reserved_host'] or old[te_def.DESTINATION_HOST]
            bw = old[te_def.BANDWIDTH]
            used = self._books(context, exclude=path_id)
            if measured:
                # shortcut (L10 caveat): only the reserved part of the own
                # traffic is subtracted; per-path counters would give it all
                own = set(pce.links_of([old[te_def.SOURCE_HOST]] +
                                       old[te_def.VIA_HOSTS] + [end]))
                for link, mbps in measured.items():
                    mine = bw if link in own else 0
                    used[link] = max(used.get(link, 0), max(0, mbps - mine))
            path = pce.compute(cap, used, old[te_def.SOURCE_HOST], end, bw)
            if path is not None:
                te_db.update_te_path(context, path_id, None,
                                     {te_def.VIA_HOSTS: path[1:-1]})
            elif delete:
                te_db.delete_te_path(context, path_id)
            else:
                return old, None
        self._push_domain_by_id(context, old['domain_id'])
        return old, path

    def _reroute_failed_reservations(self, context):
        """Move every reservation off the links and nodes that are down.

        Run by the TE liveness worker. A reservation that no longer fits
        is deleted with a WARNING (L6), like one whose port migrated. One
        that crosses nothing dead is never touched, so a link coming back
        moves nothing back by itself. During an outage of the collection
        nothing moves (ERROR logged each pass).
        """
        if not self.capacity:
            return
        silent, outage = self._silent_fabric(context)
        if outage:
            LOG.error("SRv6 TE: no fabric node has posted link state for "
                      "%ss: treated as an outage of the collection, not "
                      "of the fabric; no reservation is moved",
                      self.conf.link_state_stale_after)
            return
        dead = self._dead_links(context, silent)
        if not dead:
            return
        cap = {link: mbps for link, mbps in self.capacity.items()
               if link not in dead}
        for p in te_db.get_bandwidth_paths(context):
            end = p['reserved_host'] or p[te_def.DESTINATION_HOST]
            hops = [p[te_def.SOURCE_HOST]] + p[te_def.VIA_HOSTS] + [end]
            crossed = sorted('%s>%s' % link for link in pce.links_of(hops)
                             if link in dead)
            if not end or not crossed:
                continue
            old, path = self._reroute_reservation(context, p['id'], cap,
                                                  delete=True)
            if old is None:
                continue
            info = {'id': p['id'], 'bw': p[te_def.BANDWIDTH],
                    's': p[te_def.SOURCE_HOST], 'd': end,
                    'l': ', '.join(crossed), 'old': '-'.join(hops)}
            if path:
                info['new'] = '-'.join(path)
                LOG.warning("SRv6 TE: reservation %(id)s (%(bw)d Mbit/s "
                            "%(s)s -> %(d)s) crossed %(l)s, which is down: "
                            "moved from %(old)s to %(new)s", info)
            else:
                LOG.warning("SRv6 TE: reservation %(id)s (%(bw)d Mbit/s "
                            "%(s)s -> %(d)s) crossed %(l)s, which is down, "
                            "and no other path has room: deleted, its "
                            "creator must ask again", info)

    def _compute_hops(self, context, body, source, host, port, via_hosts):
        """(via_hosts, reserved host) for a bandwidth reservation, or 400/409.

        Online CSPF (services/pce.py) over the topology file minus the
        links down now, against what each link holds: the larger of what
        every existing reservation reserves on it, in every domain (the
        links are shared), and its measured peak load when the link state
        is fresh (PLAN-LINK-STATE-TE L4). The result pins every hop,
        leaves and spines included, so the reservation sits on exactly
        the links the traffic takes.

        The destination is a host, or the host a port is bound to now. The
        hops are computed for it, and it is stored as reserved_host. The
        books are built from reserved_host, never from where a port is
        later: after a migration the hops still end at that host's leaf,
        and reading the port's new host made the books charge a link that
        does not exist (live-migration over-commit, review of 0e282a3).
        """
        bandwidth = body[te_def.BANDWIDTH]
        if not self.capacity:
            raise te_ext.Srv6TePathInvalid(
                reason=_("bandwidth needs an underlay topology "
                         "([srv6] topology_file)"))
        if not source:
            raise te_ext.Srv6TePathInvalid(
                reason=_("bandwidth needs source_host: a path is computed "
                         "from the ingress it starts at"))
        if via_hosts:
            raise te_ext.Srv6TePathInvalid(
                reason=_("bandwidth and via_hosts exclude each other: with "
                         "bandwidth the hops are computed"))
        destination = host or (port or {}).get(portbindings.HOST_ID)
        if not destination:
            raise te_ext.Srv6TePathInvalid(
                reason=_("bandwidth needs a bound port: the hops are "
                         "computed for the host it is on"))
        for end in (source, destination):
            if not any(end == a for a, _b in self.capacity):
                raise te_ext.Srv6TePathInvalid(
                    reason=_("%s is not a node of the underlay topology")
                    % end)
        books = self._books(context)
        view = self._link_state_view(context)
        cap = self._live_capacity(context, view)
        over = sorted('%s>%s' % link for link, mbps in books.items()
                      if mbps > self.capacity.get(link, mbps))
        if over:
            # reservable_fraction was lowered below what is already held
            LOG.warning("SRv6 TE: reservations exceed the reservable "
                        "capacity (reservable_fraction %(f)s) on %(l)s; new "
                        "requests there are refused until some are deleted",
                        {'f': self.conf.reservable_fraction,
                         'l': ', '.join(over)})
        # L4: a link holds max(reserved, measured peak): an idle
        # reservation still counts, traffic without one counts too
        measured = self._measured_load(context, view)
        used = {link: max(books.get(link, 0), measured.get(link, 0))
                for link in set(books) | set(measured)}
        stale = sorted('%s>%s' % link for link, entry in view.items()
                       if entry['stale'] and entry['measured_at'])
        if stale:
            LOG.info("SRv6 TE: no fresh link state for %(l)s: admission "
                     "counts only their reservations there", {
                         'l': ', '.join(stale)})
        # The caller holds the admission lock (create_srv6_te_path).
        path = pce.compute(cap, used, source, destination, bandwidth)
        if path is None:
            raise te_ext.Srv6TePathNoCapacity(
                source=source, destination=destination, bandwidth=bandwidth)
        LOG.info("SRv6 TE: %(bw)d Mbit/s %(s)s -> %(d)s computed as %(p)s",
                 {'bw': bandwidth, 's': source, 'd': destination,
                  'p': '-'.join(path)})
        return path[1:-1], destination

    def create_srv6_te_path(self, context, srv6_te_path):
        body = srv6_te_path[te_def.RESOURCE_NAME]
        admin_context = context.elevated()
        domain_id = body[te_def.DOMAIN_ID]
        domain = domain_db.get_domain(admin_context, domain_id)
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=domain_id)

        host = body.get(te_def.DESTINATION_HOST)
        port_id = body.get(te_def.DESTINATION_PORT_ID)
        if bool(host) == bool(port_id):
            raise te_ext.Srv6TePathInvalid(
                reason=_("exactly one of destination_host and "
                         "destination_port_id must be set"))
        locators = srv6_db.get_host_locators(admin_context)
        if host and host not in locators:
            raise te_ext.Srv6TePathInvalid(
                reason=_("destination_host %s has no registered SRv6 "
                         "locator (see GET /v2.0/srv6_locators)") % host)
        port = None
        if port_id:
            port = self._validate_destination_port(admin_context, domain,
                                                   port_id)
        # '' is "any ingress" too: string_or_none lets it through, and it
        # is what the column stores for that.
        source = body.get(te_def.SOURCE_HOST) or None
        if source:
            self._validate_source_host(source, locators, host, port)
        # via == destination is accepted on purpose: a harmless detour, and
        # on two nodes the only depth-2 SRH there is (TE-DEPTH2).
        via_hosts = body.get(te_def.VIA_HOSTS) or []
        self._validate_via_hosts(via_hosts,
                                 {**self.fabric_locators, **locators})
        selector = ('port %s' % port_id) if port_id else ('host %s' % host)
        if source:
            selector += ' from %s' % source
        try:
            with db_api.CONTEXT_WRITER.using(context):
                if body.get(te_def.BANDWIDTH) is not None:
                    # One admission at a time: reading what is reserved and
                    # inserting the new reservation must be one step, or
                    # concurrent creates all see the same free links and
                    # over-commit them (Phase C, PLAN-PATH-COMPUTATION LOG
                    # op 21). Every domain row is locked, in key order, for
                    # the rest of this transaction.
                    domain_db.lock_all_domains(context)
                    via_hosts, reserved = self._compute_hops(
                        context, body, source, host, port, via_hosts)
                else:
                    reserved = None
                # Forced, never the caller's, as for associations: the path
                # belongs with the domain it steers.
                body = dict(body, project_id=domain['project_id'],
                            via_hosts=via_hosts, source_host=source,
                            reserved_host=reserved)
                path = te_db.create_te_path(context, domain_id, body)
        except db_exc.DBDuplicateEntry:
            raise te_ext.Srv6TePathExists(domain_id=domain_id,
                                          selector=selector)
        except db_exc.DBReferenceError:
            # The port went between the check and the insert.
            raise te_ext.Srv6TePathInvalid(
                reason=_("destination_port_id %s does not exist") % port_id)
        LOG.info("SRv6 TE path %(id)s created in domain %(d)s: %(s)s via "
                 "%(v)s", {'id': path['id'], 'd': domain_id, 's': selector,
                           'v': via_hosts})
        self._push_domain(context, domain)
        return self._resolved(context, path, locators)

    def get_srv6_te_path(self, context, id, fields=None):
        path = te_db.get_te_path(context, id)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        return _only(self._resolved(context, path), fields)

    def get_srv6_te_paths(self, context, filters=None, fields=None,
                          sorts=None, limit=None, marker=None,
                          page_reverse=False):
        paths = te_db.get_all_te_paths(context, _normalise_filters(filters))
        if not paths:
            return []
        admin_context = context.elevated()
        locators = srv6_db.get_host_locators(admin_context)
        dead_hosts = self._dead_hosts(admin_context)
        function_ids = {}
        res = []
        for path in paths:
            domain_id = path['domain_id']
            if domain_id not in function_ids:
                function_ids[domain_id] = srv6_db.get_function_id(
                    admin_context, domain_id)
            res.append(_only(self._resolve_te_path(
                context, path, locators, function_ids[domain_id],
                dead_hosts), fields))
        return res

    def update_srv6_te_path(self, context, id, srv6_te_path):
        body = srv6_te_path[te_def.RESOURCE_NAME]
        locators = srv6_db.get_host_locators(context.elevated())
        if te_def.VIA_HOSTS in body:
            # PUT replaces the list: re-steering with no delete/create
            # window. [] un-steers.
            body = dict(body, via_hosts=body[te_def.VIA_HOSTS] or [])
            self._validate_via_hosts(body[te_def.VIA_HOSTS],
                                     {**self.fabric_locators, **locators})
            old = te_db.get_te_path(context, id)
            if old and old.get(te_def.BANDWIDTH) is not None:
                # The reservation is accounted on the computed hops: other
                # hops would hold capacity on links the traffic avoids.
                raise te_ext.Srv6TePathInvalid(
                    reason=_("via_hosts of a path with a bandwidth "
                             "reservation are computed; delete and "
                             "re-create the path to move it"))
        path = te_db.update_te_path(context, id, None, body)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        LOG.info("SRv6 TE path %(id)s now via %(v)s",
                 {'id': id, 'v': path[te_def.VIA_HOSTS]})
        self._push_domain_by_id(context, path['domain_id'])
        return self._resolved(context, path, locators)

    def delete_srv6_te_path(self, context, id):
        path = te_db.delete_te_path(context, id)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        LOG.info("SRv6 TE path %(id)s deleted from domain %(d)s",
                 {'id': id, 'd': path['domain_id']})
        self._push_domain_by_id(context, path['domain_id'])

    # ------------------------------------------------------------------
    # port events -- the incremental path
    # ------------------------------------------------------------------
    def _notify_port(self, context, port, removed=False):
        if not port:
            return
        network_id = port.get('network_id')
        if not network_id:
            return
        # The full payload's rule (_routes_for_domain): a DHCP or router
        # port is never advertised. Without it an incremental add put a
        # route on every node that the next full sync took away again.
        # Removals pass: withdrawing a route nobody holds is harmless.
        if not removed and not self._port_is_relevant(port):
            return
        # Runs on every port update in the cloud: one indexed query, and an
        # early return for the common case of a network in no domain.
        domain_ids = domain_db.get_domain_ids_for_network(
            context.elevated(), network_id)
        if not domain_ids:
            return
        host = port.get(portbindings.HOST_ID) or port.get('host')
        if not host:
            return
        locators = srv6_db.get_host_locators(context.elevated())
        if host not in locators and not removed:
            LOG.debug("port %(p)s is on host %(h)s which has no SRv6 "
                      "locator; not advertising it",
                      {'p': port['id'], 'h': host})
            return
        routes = [_route_for(port, fixed_ip['ip_address'], host)
                  for fixed_ip in port.get('fixed_ips', [])
                  if fixed_ip.get('ip_address')]
        if not routes:
            return
        for domain_id in domain_ids:
            if srv6_db.get_function_id(context.elevated(), domain_id) is None:
                continue
            if removed:
                self.agent_rpc.routes_updated(context, domain_id,
                                              remove=routes)
            else:
                # Segments per domain, on copies: TE paths belong to a
                # domain. Without them a steered VM's route would arrive
                # at depth 1 and stay so until the next full sync.
                self.agent_rpc.routes_updated(
                    context, domain_id,
                    add=self._attach_segments(
                        context.elevated(), domain_id,
                        [dict(r) for r in routes], locators))

    @staticmethod
    def _addresses(port):
        return {fixed_ip['ip_address']
                for fixed_ip in (port or {}).get('fixed_ips') or []
                if fixed_ip.get('ip_address')}

    def _notify_fixed_ip_change(self, context, original, port):
        """A live port that kept its host but changed addresses (F14).

        None of the status or host branches matches this case, so until the
        next periodic resync (300 s) other nodes had no route to an added
        address and kept one to a removed address. Only the difference is
        sent, removals first, as for a move: an address that stayed is not
        touched, and between the two messages no node routes an address that
        has left the port.
        """
        old, new = self._addresses(original), self._addresses(port)
        if old == new:
            return
        left = [f for f in original.get('fixed_ips') or []
                if f.get('ip_address') in old - new]
        added = [f for f in port.get('fixed_ips') or []
                 if f.get('ip_address') in new - old]
        if left:
            self._notify_port(context, dict(original, fixed_ips=left),
                              removed=True)
        if added:
            self._notify_port(context, dict(port, fixed_ips=added))

    def _release_moved_reservations(self, context, port):
        """Delete the bandwidth reservations a port's new host voids.

        Decision D0 of PLAN-RESERVATION-FOLLOWS-INSTANCE: the reserved hops
        lead to the host they were computed for, so a port elsewhere (or
        unbound) ends its reservations; whoever made them asks again. Under
        the admission lock, so a deletion never interleaves with an
        admission reading the books. Never raises into the port update.
        """
        host = port.get(portbindings.HOST_ID) or None
        try:
            with db_api.CONTEXT_WRITER.using(context):
                domain_db.lock_all_domains(context)
                gone = te_db.delete_moved_reservations(context, port['id'],
                                                       host)
        except Exception:
            LOG.exception("SRv6 TE: could not release the reservations of "
                          "port %s after it moved", port['id'])
            return
        for path in gone:
            LOG.warning("SRv6 TE: reservation %(id)s (%(bw)d Mbit/s from "
                        "%(s)s to port %(p)s) deleted: it was reserved for "
                        "%(old)s and the port is now on %(new)s",
                        {'id': path['id'], 'bw': path[te_def.BANDWIDTH],
                         's': path[te_def.SOURCE_HOST], 'p': port['id'],
                         'old': path['reserved_host'], 'new': host})

    def _release_stale_reservations(self, context):
        """Release reservations whose port moved without our noticing.

        The safety net of _release_moved_reservations, run by the TE
        liveness worker: a port update that never reached the callback (a
        server killed between ML2's commit and the callback) leaves a
        reservation for a host the port has left. One read of the
        reservations, one get_ports for their ports. A deleted port needs
        nothing: its paths went with it (ON DELETE CASCADE).
        """
        held = {}
        for path in te_db.get_bandwidth_paths(context):
            port_id = path[te_def.DESTINATION_PORT_ID]
            if port_id:
                held.setdefault(port_id, set()).add(path['reserved_host'])
        if not held:
            return
        for port in self._core_plugin().get_ports(
                context, filters={'id': sorted(held)}):
            if held[port['id']] != {port.get(portbindings.HOST_ID) or None}:
                self._release_moved_reservations(context, port)

    @registry.receives(resources.PORT, [events.AFTER_UPDATE])
    def registry_port_updated(self, resource, event, trigger, payload=None):
        context = payload.context
        port = payload.latest_state
        original = payload.states[0] if payload.states else {}
        if ((original.get(portbindings.HOST_ID) or None) !=
                (port.get(portbindings.HOST_ID) or None)):
            # Before any route is pushed: the new host's route must not be
            # built from hops that lead to the old one.
            self._release_moved_reservations(context.elevated(), port)
        try:
            was_active = original.get('status') == n_const.PORT_STATUS_ACTIVE
            is_active = port.get('status') == n_const.PORT_STATUS_ACTIVE
            if is_active and not was_active:
                self._notify_port(context, port)
            elif was_active and not is_active:
                self._notify_port(context, original, removed=True)
            elif is_active:
                # A live port that moved host: withdraw the old SID, then
                # advertise the new one. Order matters -- the reverse would
                # leave a window where two nodes claim the same address.
                old_host = original.get(portbindings.HOST_ID)
                new_host = port.get(portbindings.HOST_ID)
                if old_host and new_host and old_host != new_host:
                    self._notify_port(context, original, removed=True)
                    self._notify_port(context, port)
                else:
                    self._notify_fixed_ip_change(context, original, port)
        except Exception:
            LOG.exception("SRv6: failed to process port update for %s",
                          port.get('id'))

    @registry.receives(resources.PORT, [events.AFTER_DELETE])
    def registry_port_deleted(self, resource, event, trigger, payload=None):
        context = payload.context
        port = payload.latest_state
        try:
            self._notify_port(context, port, removed=True)
        except Exception:
            LOG.exception("SRv6: failed to process port delete for %s",
                          port.get('id'))

    # ------------------------------------------------------------------
    # agent -> server
    # ------------------------------------------------------------------
    def get_workers(self):
        """The TE liveness monitor and the TE re-optimisation, one each.

        Neutron starts every worker with worker_process_count 0 as a thread
        of one extra process, so a node that dies is noticed and pushed once,
        and a congested link handled once, not once per API worker. Each
        starts only when its interval is not 0; the re-optimisation also
        needs a topology_file.

        The liveness monitor's first pass waits half of agent_down_time,
        like Neutron's own agent status check: right after a server restart
        the agents have not yet reported, and a late heartbeat must not read
        as a death. The re-optimisation's first pass waits one interval.
        """
        workers = []
        interval = self.conf.te_liveness_interval
        if interval:
            monitor = te_liveness.TeLivenessMonitor(self)
            workers.append(neutron_worker.PeriodicWorker(
                monitor.check, interval,
                max(cfg.CONF.agent_down_time // 2, 1),
                desc='srv6-te-liveness'))
        interval = self.conf.te_reopt_interval
        if interval and self.capacity:
            reopt = te_reopt.TeReoptimiser(self)
            workers.append(neutron_worker.PeriodicWorker(
                reopt.check, interval, interval, desc='srv6-te-reopt'))
        return workers

    def start_rpc_listeners(self):
        """Serve sync_state. Called by neutron's RPC worker at startup.

        On TOPIC_SRV6_PLUGIN, never topics.PLUGIN: a consumer there joins
        the round-robin for the core plugin's own RPC (see constants.py).
        """
        self.endpoints = [rpc.Srv6ServerRpcCallback(self)]
        self.conn = n_rpc.Connection()
        self.conn.create_consumer(constants.TOPIC_SRV6_PLUGIN,
                                  self.endpoints, fanout=False)
        LOG.info("SRv6 plugin serving RPC on topic %s",
                 constants.TOPIC_SRV6_PLUGIN)
        return self.conn.consume_in_threads()

    def handle_sync_state(self, context, host, locator, capabilities=None):
        """Register a locator and return every domain's full state.

        Also re-broadcasts each domain when the node's locator is new or
        changed, because that alters what every *other* node must
        encapsulate to. Doing it here rather than making agents poll keeps
        convergence bounded by one RPC round trip instead of one resync
        interval.

        Only then (F13). It used to re-broadcast on every call, and every
        agent calls this at start, on every resync, and on every port of a
        network in no domain: domains x nodes casts per call, each one a
        full re-apply on every agent. A re-broadcast built before a
        concurrent domain delete could also land after the delete and
        bring the domain back (F9). The caller itself always gets the
        payloads as the return value, so it converges whatever is sent.
        """
        admin_context = n_context.get_admin_context()
        self._ensure_pool(admin_context)
        self._check_capabilities(host, capabilities)
        if locator:
            try:
                sid.parse_locator(locator)
                if self.usid:
                    self._check_usid_locator(admin_context, host, locator)
            except sid.InvalidLocator as e:
                LOG.error("SRv6 agent on %(host)s reported an unusable "
                          "locator %(loc)s: %(err)s",
                          {'host': host, 'loc': locator, 'err': e})
                # Not []: the agent would remove every domain it holds.
                raise rpc.Srv6SyncRefused(
                    "unusable locator %s: %s" % (locator, e))
            locator_changed = srv6_db.set_host_locator(
                admin_context, host, locator,
                datetime.datetime.now(datetime.timezone.utc))
        else:
            locator_changed = False
        locators = srv6_db.get_host_locators(admin_context)
        # Once per sync, not once per domain. The node that is syncing may
        # still count as dead for a moment (its first heartbeat can follow
        # this call); the liveness monitor re-pushes when it turns alive.
        dead_hosts = self._dead_hosts(admin_context)
        payloads = []
        for domain in domain_db.get_domains(admin_context):
            # F11: a domain with no network has no state on any node. It
            # used to be left out of the RPCs (create sends none) but put in
            # here, so every node built its VRF at the first sync after the
            # create. Left out, the agent's stale-domain pass and its kernel
            # garbage collector remove what an older build made.
            if not domain.get(srv6_def.NETWORKS):
                continue
            payload = self._build_domain_payload(admin_context, domain,
                                                 locators, dead_hosts)
            if payload is not None:
                payloads.append(payload)
        if locator_changed:
            # Tell everyone else about the new locator.
            LOG.info("SRv6: locator of %(h)s is new or changed; "
                     "re-broadcasting %(n)d domain(s)",
                     {'h': host, 'n': len(payloads)})
            for payload in payloads:
                self.agent_rpc.domain_updated(admin_context, payload)
        return payloads
