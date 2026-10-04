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

import datetime
import ipaddress

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

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def
from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.common import config
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.db import te_db
from networking_srv6_agent.extensions import srv6 as srv6_ext
from networking_srv6_agent.extensions import srv6_te as te_ext
from networking_srv6_agent.services import rpc


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
                                   te_def.ALIAS]

    def __init__(self):
        super().__init__()
        config.register_server_opts()
        self.conf = cfg.CONF[config.SRV6_SERVER_GROUP]
        self.agent_rpc = rpc.Srv6AgentNotifyAPI()
        self._pool_synced = False
        self.encoding = self.conf.dataplane_encoding
        self._validate_encoding_config()
        LOG.info("SRv6 service plugin initialised (vrf_table_base=%(base)s, "
                 "function_id_ranges=%(ranges)s, dataplane_encoding=%(enc)s)",
                 {'base': self.conf.vrf_table_base,
                  'ranges': self.conf.function_id_ranges,
                  'enc': self.encoding})

    @property
    def usid(self):
        return self.encoding == constants.ENCODING_USID

    def _validate_encoding_config(self):
        """Refuse to start in usid mode with function ids outside the LIB.

        Under a uN a function id is read as the next node when it has no
        decap route of its own, so ids that are also node ids would turn a
        deleted domain's traffic into traffic for another node
        (USID-PLAN.md D2). Ids already allocated outside the range are
        kept and warned about by sync_allocation_pool, as for any range
        change.
        """
        if not self.usid:
            return
        try:
            ranges = srv6_db.parse_function_id_ranges(
                self.conf.function_id_ranges)
        except ValueError as e:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] function_id_ranges', opt_value=str(e))
        for start, end in ranges:
            if not (sid.is_lib_function_id(start) and
                    sid.is_lib_function_id(end)):
                raise n_exc.InvalidConfigurationOption(
                    opt_name='[srv6] function_id_ranges',
                    opt_value=_("%(s)#x:%(e)#x -- with dataplane_encoding = "
                                "usid every range must lie inside the "
                                "microSID Local ID Block %(lo)#x:%(hi)#x")
                    % {'s': start, 'e': end,
                       'lo': constants.USID_LIB_MIN,
                       'hi': constants.USID_LIB_MAX})

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

    def _build_domain_payload(self, context, domain, locators=None):
        """Full desired state of one domain, as agents consume it.

        Built with an ELEVATED context whoever triggered it. A shared network
        is allowed in a domain, and the instances on it may
        belong to other projects: under the owner's own context their ports
        are invisible, so their routes would silently be missing.
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
                locators),
            'locators': locators,
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
        # No RPC: a domain with no network has no presence on any node.
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
    def _transit_sids(self, via_hosts, locators):
        """The transit SIDs of via_hosts in order, or None if one is unknown.

        Each host's End SID, <locator>:fff0::, or in usid mode its uN, the
        locator's own address -- the agent packs those into containers.

        None, not a shorter list: a path missing a waypoint is not the path
        the admin asked for, and quietly dropping a hop is the silent
        partial steering the locator API exists to prevent. The caller
        falls back to the direct route instead, and the path reports
        DEGRADED.
        """
        if any(not locators.get(host) for host in via_hosts):
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

    def _attach_segments(self, admin_context, domain_id, routes, locators):
        """Give each steered route its transit SIDs. Mutates `routes`.

        Precedence per route: port path > host path > direct. A port path
        with no vias is an explicit "direct", and still wins over a host
        path. A DEGRADED path contributes nothing, so its routes fall back
        to the DIRECT route -- not to the next path down, which the admin
        did not ask for either.

        Transit SIDs only: the agent appends the decap SID, from the locator
        of the route's host, as it always has. A route with no path carries
        no `segments` key at all, which keeps it byte-for-byte the P5 route.
        """
        paths = te_db.get_te_paths_for_domain(admin_context, domain_id)
        by_port, by_host = paths['by_port'], paths['by_host']
        if not by_port and not by_host:
            return routes
        for route in routes:
            via_hosts = by_port.get(route.get('port_id'))
            if via_hosts is None:
                via_hosts = by_host.get(route.get('host'))
            if not via_hosts:
                continue
            transit = self._transit_sids(via_hosts, locators)
            if transit is None:
                LOG.warning("SRv6 domain %(d)s: the TE path toward %(p)s "
                            "names a via host with no registered locator "
                            "(%(v)s); routing it directly",
                            {'d': domain_id, 'p': route['prefix'],
                             'v': ', '.join(via_hosts)})
                continue
            route['segments'] = transit
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

    def _resolve_te_path(self, context, path, locators, function_id):
        """Attach `segments` and `status` -- what the server resolved.

        ACTIVE:   [<via End SIDs>..., <destination decap SID>]
        DEGRADED: [], because a via host or the destination has no locator,
                  or the port is unbound; the routes go direct meanwhile.

        `encoded_segments` is what the ingress will actually push: the
        same list with full SIDs, the C-SID containers in usid mode --
        computed by the same sid.pack_next_csid the agent calls, so the
        two cannot disagree.

        Server resolution, not an agent acknowledgement: ACTIVE says the
        payload carries this list, not that any kernel holds it.
        """
        path = dict(path)
        destination = self._destination_of(context.elevated(), path)
        transit = self._transit_sids(path[te_def.VIA_HOSTS], locators)
        if (destination and locators.get(destination) and
                transit is not None and function_id is not None):
            segments = transit + [
                sid.format_sid(locators[destination], function_id)]
            path[te_def.SEGMENTS] = segments
            path[te_def.ENCODED_SEGMENTS] = self._encoded(segments)
            path[te_def.STATUS] = te_def.STATUS_ACTIVE
            return path
        LOG.warning("SRv6 TE path %(id)s is DEGRADED: destination %(dst)s, "
                    "via %(via)s; not every host on it has a registered "
                    "locator (or the port is unbound), so its routes go "
                    "direct", {'id': path['id'], 'dst': destination,
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
            srv6_db.get_function_id(admin_context, path['domain_id']))

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
        if port_id:
            self._validate_destination_port(admin_context, domain, port_id)
        # via == destination is accepted on purpose: a harmless detour, and
        # on two nodes the only depth-2 SRH there is (TE-DEPTH2).
        via_hosts = body.get(te_def.VIA_HOSTS) or []
        self._validate_via_hosts(via_hosts, locators)

        # Forced, never the caller's, as for associations: the path belongs
        # with the domain it steers.
        body = dict(body, project_id=domain['project_id'],
                    via_hosts=via_hosts)
        selector = ('port %s' % port_id) if port_id else ('host %s' % host)
        try:
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
        function_ids = {}
        res = []
        for path in paths:
            domain_id = path['domain_id']
            if domain_id not in function_ids:
                function_ids[domain_id] = srv6_db.get_function_id(
                    admin_context, domain_id)
            res.append(_only(self._resolve_te_path(
                context, path, locators, function_ids[domain_id]), fields))
        return res

    def update_srv6_te_path(self, context, id, srv6_te_path):
        body = srv6_te_path[te_def.RESOURCE_NAME]
        locators = srv6_db.get_host_locators(context.elevated())
        if te_def.VIA_HOSTS in body:
            # PUT replaces the list: re-steering with no delete/create
            # window. [] un-steers.
            body = dict(body, via_hosts=body[te_def.VIA_HOSTS] or [])
            self._validate_via_hosts(body[te_def.VIA_HOSTS], locators)
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

    @registry.receives(resources.PORT, [events.AFTER_UPDATE])
    def registry_port_updated(self, resource, event, trigger, payload=None):
        context = payload.context
        port = payload.latest_state
        original = payload.states[0] if payload.states else {}
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

        Also re-broadcasts each domain, because a node whose locator is new
        (or changed) alters what every *other* node must encapsulate to.
        Doing it here rather than making agents poll keeps convergence
        bounded by one RPC round trip instead of one resync interval.
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
                return []
            srv6_db.set_host_locator(admin_context, host, locator,
                                     datetime.datetime.now(
                                         datetime.timezone.utc))
        locators = srv6_db.get_host_locators(admin_context)
        payloads = []
        for domain in domain_db.get_domains(admin_context):
            payload = self._build_domain_payload(admin_context, domain,
                                                 locators)
            if payload is not None:
                payloads.append(payload)
        # Tell everyone else about the (possibly new) locator.
        for payload in payloads:
            self.agent_rpc.domain_updated(admin_context, payload)
        return payloads
