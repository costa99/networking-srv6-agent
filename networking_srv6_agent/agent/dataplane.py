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

"""Srv6LinuxDataplane -- the kernel state one node holds for one domain.

Ported from networking-bgpvpn (branch srv6-te) with `vpn` renamed `domain`.
The kernel objects and their names are unchanged on purpose: for a given
domain, what this class writes and what the manual proof wrote are the same
objects with the same names, and the migration's gate compares against the
recorded evidence literally.

Per domain:
    ip link add sv6vrf-<fid> type vrf table <base+fid>
    ip [-6] route replace unreachable default metric 4278198272 \
        table <table>                                    # the seal, 8.6
    ip -6 route replace <sid>/128 encap seg6local action End.DT46 \
        vrftable <table> dev sv6vrf-<fid>

Per attached network present on this node:
    ovs-vsctl add-port br-int svp-<fid>-<vlan> tag=<vlan> \
        -- set interface svp-<fid>-<vlan> type=internal
    ip link set svp-<fid>-<vlan> master sv6vrf-<fid>
    ip addr replace <gateway_ip>/<len> dev svp-<fid>-<vlan>

Per remote port:
    ip route replace <ip>/32 encap seg6 mode encap \
        segs [<transit End SID>,...]<remote sid> dev <underlay> table <table>

Per first segment the domain encapsulates to -- a remote decap SID, or for a
steered route (P6) a transit End SID, the latter only while the edge filter
covers it:
    ip -6 rule add pref 999 iif sv6vrf-<fid> to <first seg>/128 lookup main

Per node, over every registered locator (P6, MIGRATION-PLAN.md 8.7):
    table inet srv6_edge -- on iifname "svp-*", drop `ip6 daddr @sid_space`
                            and drop `rt type 4` (any tenant-built SRH)

Four things differ from the old build, each a defect it had or one found at
the gate (P5-PLAN.md 4): the table is sealed (4.1), the seal lets the
domain's own outer headers out through the SID rules (4.6), the table is
flushed on delete (4.2), and a delete finds its gateway ports on the bridge
rather than in memory (4.3).
"""

import collections
import ipaddress
import time

from neutron.agent.linux import ip_lib
from neutron.privileged.agent.linux import ip_lib as priv_ip_lib
from oslo_log import log as logging

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid
from networking_srv6_agent.privileged import nft as priv_nft
from networking_srv6_agent.privileged import seg6 as priv_seg6


LOG = logging.getLogger(__name__)

# Sysctls SRv6 needs. Two of the three fail SILENTLY when wrong -- measured
# in rung 0.D and again in the VM demo -- which is why the agent asserts
# them rather than assuming a correctly-provisioned host.
#
#   seg6_enabled  the kernel DROPS a packet carrying an SRH on an interface
#                 where this is 0. The value that counts is the one on the
#                 interface the packet ARRIVES on, and `all` does not apply
#                 retroactively to interfaces that already exist.
#   strict_mode   End.DT46 is refused outright without it ("Strict mode for
#                 VRF is disabled"). It must be 1 BEFORE the VRF is created,
#                 because the table<->VRF mapping that `vrftable N` resolves
#                 against is registered at VRF creation time.
#   rp_filter     at the default, decapsulated packets vanish with no
#                 counter moving anywhere.
GLOBAL_SYSCTLS = [
    ('net.ipv6.conf.all.forwarding', '1'),
    ('net.ipv4.ip_forward', '1'),
    ('net.ipv6.conf.all.seg6_enabled', '1'),
    ('net.ipv6.conf.default.seg6_enabled', '1'),
    ('net.ipv6.conf.lo.seg6_enabled', '1'),
    ('net.vrf.strict_mode', '1'),
    ('net.ipv4.conf.all.rp_filter', '0'),
    ('net.ipv4.conf.default.rp_filter', '0'),
]


# How long to wait for ovs-vswitchd to materialise an internal port's
# kernel netdev after ovs-vsctl returns.
DEVICE_WAIT_SECONDS = 10


class Srv6DataplaneError(Exception):
    pass


DomainState = collections.namedtuple(
    'DomainState', ['domain_id', 'function_id', 'vrf_table', 'vrf_name'])


def _normalise_sid(address):
    """One spelling per SID, whatever `ip rule show` or format_sid wrote."""
    try:
        return str(ipaddress.IPv6Address(address))
    except ValueError:
        return address


def _normalise_prefix(prefix):
    """Compare what the payload says with what `ip route show` prints.

    They disagree on host routes: the server sends 10.90.2.21/32 and the
    kernel prints 10.90.2.21. Comparing the strings marks every host route
    as unwanted and deletes the whole table.
    """
    try:
        return str(ipaddress.ip_network(prefix, strict=False))
    except ValueError:
        LOG.debug("SRv6: cannot parse prefix %s", prefix)
        return prefix


def _locator_networks(locators):
    """Sorted, de-duplicated locator networks; an unparseable one is skipped.

    Sorted so the same set always renders the same ruleset text.
    """
    networks = set()
    for locator in locators:
        if not locator:
            continue
        try:
            networks.add(ipaddress.IPv6Network(locator, strict=False))
        except ValueError:
            LOG.warning("SRv6: leaving unparseable locator %s out of the "
                        "edge filter", locator)
    return sorted(networks)


def render_edge_ruleset(networks):
    """The whole edge-filter table as one nft script (MIGRATION-PLAN.md 8.7).

    `add` then `delete` then the full table: `delete` alone fails on a table
    that does not exist yet, and nft runs the script as one transaction, so
    the replacement is atomic. The SID space is every registered locator --
    no new configuration (decided 2026-09-10). `auto-merge` because interval
    sets refuse overlapping elements; locators are disjoint anyway.

    The drop rules match only the gateway ports: a tenant packet passes
    PREROUTING with iif svp-* before it is routed, while the agent's own
    encapsulation happens after that routing decision and the outer header
    never enters PREROUTING. VRF traffic crosses PREROUTING a second time
    with the VRF device as iif; matching svp-* catches the first pass.
    """
    table = 'inet %s' % constants.EDGE_FILTER_TABLE
    iifname = '%s*' % constants.GW_PORT_PREFIX
    lines = [
        'add table %s' % table,
        'delete table %s' % table,
        'table %s {' % table,
        '  set sid_space {',
        '    type ipv6_addr',
        '    flags interval',
        '    auto-merge',
    ]
    if networks:
        lines.append('    elements = { %s }'
                     % ', '.join(str(n) for n in networks))
    lines += [
        '  }',
        '  chain prerouting {',
        '    type filter hook prerouting priority raw; policy accept;',
        '    iifname "%s" ip6 daddr @sid_space counter drop' % iifname,
        '    iifname "%s" rt type 4 counter drop' % iifname,
        '  }',
        '}',
    ]
    return '\n'.join(lines) + '\n'


class Srv6LinuxDataplane:

    def __init__(self, locator, underlay_interface, vrf_table_base,
                 bridge=None, apply_sysctls=True):
        self.locator = locator
        self.underlay_interface = underlay_interface
        self.vrf_table_base = vrf_table_base
        self.bridge = bridge
        self.apply_sysctls = apply_sysctls
        # domain_id -> DomainState, and domain_id -> {network_id: port name}
        self.domains = {}
        self.gateway_ports = collections.defaultdict(dict)
        # True only after a successful apply; the networks it covers then.
        self.edge_filter_active = False
        self._edge_filtered = ()
        # (prefix, depth) already warned about, so a steered route that does
        # not fit says so once rather than on every resync.
        self._mtu_warned = set()

    @property
    def own_end_sid(self):
        return sid.format_sid(self.locator, constants.NODE_END_FUNCTION_ID)

    # ------------------------------------------------------------------
    # node-level setup
    # ------------------------------------------------------------------
    def initialize_node(self):
        """Sysctls, the vrf module, and this node's locator route."""
        self._ensure_module_vrf()
        self._apply_global_sysctls()
        if self.underlay_interface:
            self._set_sysctl(
                'net.ipv6.conf.%s.seg6_enabled' % self.underlay_interface, '1')
            self._set_sysctl(
                'net.ipv4.conf.%s.rp_filter' % self.underlay_interface, '0')
        priv_seg6.replace_locator_route(self.locator)
        self._ensure_node_end_sid()
        # Own locator only for now; the first sync_state widens it to every
        # registered node before any route is programmed.
        self.ensure_edge_filter([self.locator])
        LOG.info("SRv6 dataplane initialised: locator=%(loc)s "
                 "underlay=%(ul)s", {'loc': self.locator,
                                     'ul': self.underlay_interface})

    def _ensure_node_end_sid(self):
        """This node's plain transit SID, so others can route *through* us.

        NODE state, not domain state. Another node may source-route a packet
        through this one while it holds no domain at all -- that is
        precisely what a transit node is -- so this is installed before any
        domain exists, and delete_domain never removes it.

        Ordered after replace_locator_route deliberately: that installs
        `local <locator> dev lo table main`, and this /128 has to win over
        it by longest prefix WITHIN main. Were it in the local table
        instead, ip rule would consult that at priority 0, ahead of main at
        32766, and shadow this route wholesale.
        """
        end_sid = self.own_end_sid
        # `dev` is required syntax for a seg6local route, not a forwarding
        # decision: End rewrites the destination to the next segment and
        # re-runs the FIB lookup, which is what actually picks the output
        # interface. lo as a fallback so an unset underlay_interface --
        # already only a warning at _validate_config -- cannot stop this
        # node being usable as a transit.
        dev = self.underlay_interface or 'lo'
        try:
            priv_seg6.replace_seg6local_route(end_sid, constants.BEHAVIOR_END,
                                              dev)
        except Exception as e:
            LOG.error("SRv6: could not install this node's End SID %(s)s: "
                      "%(e)s. Other nodes cannot source-route through this "
                      "one; traffic engineering via this node will silently "
                      "fall back to the underlay shortest path.",
                      {'s': end_sid, 'e': e})
            return
        LOG.info("SRv6: node transit SID %(s)s (End) via %(d)s",
                 {'s': end_sid, 'd': dev})

    def _ensure_module_vrf(self):
        """`net.vrf.strict_mode` does not exist until vrf.ko is loaded.

        Loading it implicitly by creating a VRF would be too late: strict
        mode has to be on before the first VRF is created.
        """
        try:
            priv_seg6.modprobe_vrf()
        except Exception as e:
            LOG.debug("SRv6: modprobe vrf: %s", e)

    def _set_sysctl(self, key, value):
        path = '/proc/sys/' + key.replace('.', '/')
        try:
            with open(path) as f:
                current = f.read().strip()
        except OSError:
            LOG.warning("SRv6: sysctl %s does not exist on this kernel", key)
            return
        if current == value:
            return
        if not self.apply_sysctls:
            LOG.error("SRv6: sysctl %(k)s is %(cur)s but must be %(want)s; "
                      "apply_sysctls is False so the agent will not change "
                      "it. SRv6 traffic WILL be dropped until it is fixed.",
                      {'k': key, 'cur': current, 'want': value})
            return
        # neutron's ip_lib.sysctl takes the argv AFTER "sysctl", so the
        # value is passed as a single -w key=value token.
        if ip_lib.sysctl(['-w', '%s=%s' % (key, value)]) != 0:
            LOG.error("SRv6: could not set sysctl %(k)s=%(v)s. SRv6 traffic "
                      "on this node will be affected.",
                      {'k': key, 'v': value})
            return
        LOG.info("SRv6: sysctl %(k)s %(cur)s -> %(v)s",
                 {'k': key, 'cur': current, 'v': value})

    def _apply_global_sysctls(self):
        for key, value in GLOBAL_SYSCTLS:
            self._set_sysctl(key, value)

    # ------------------------------------------------------------------
    # edge filter (P6, MIGRATION-PLAN.md 8.7)
    # ------------------------------------------------------------------
    def ensure_edge_filter(self, locators):
        """Filter the gateway ports over every known locator, this one too.

        A full replace every time: idempotent, and it restores a table
        somebody deleted by hand. Returns whether the filter is active.
        Any failure, a missing nft included, leaves it INACTIVE -- and
        _install_routes then lets no VRF reach an End SID. There is no knob
        to switch it off.
        """
        networks = _locator_networks(list(locators) + [self.locator])
        try:
            priv_nft.apply_ruleset(render_edge_ruleset(networks))
        except Exception as e:
            self.edge_filter_active = False
            self._edge_filtered = ()
            LOG.error("SRv6: could not apply the edge filter (table inet "
                      "%(t)s): %(e)s. Steered routes stay sealed on this "
                      "node until it applies.",
                      {'t': constants.EDGE_FILTER_TABLE, 'e': e})
            return False
        if not self.edge_filter_active or \
                tuple(networks) != self._edge_filtered:
            LOG.info("SRv6: edge filter active on %(p)s* over %(n)s",
                     {'p': constants.GW_PORT_PREFIX,
                      'n': ', '.join(str(n) for n in networks)})
        self.edge_filter_active = True
        self._edge_filtered = tuple(networks)
        return True

    def _edge_filter_covers(self, sid_address):
        """Whether a tenant is kept from addressing this SID directly.

        Active is not enough: a rule toward a node whose locator the filter
        has not learned yet -- a routes_updated racing the domain_updated
        that carries the new locator -- would reopen 8.7 for that node.
        """
        if not self.edge_filter_active:
            return False
        try:
            address = ipaddress.IPv6Address(sid_address)
        except ValueError:
            return False
        return any(address in network for network in self._edge_filtered)

    # ------------------------------------------------------------------
    # per-domain
    # ------------------------------------------------------------------
    def ensure_domain(self, domain_id, function_id, behavior):
        """Create the VRF, seal it, and install this node's decap SID.

        Idempotent. The seal comes before the SID and before the domain is
        recorded, so no gateway port can ever be enslaved to an unsealed
        VRF: ensure_gateway_port refuses a domain this method has not
        recorded.
        """
        vrf_table = sid.vrf_table(function_id, self.vrf_table_base)
        vrf_name = sid.vrf_name(function_id)

        # strict_mode BEFORE the VRF exists -- see GLOBAL_SYSCTLS.
        self._set_sysctl('net.vrf.strict_mode', '1')

        if not ip_lib.device_exists(vrf_name):
            LOG.info("SRv6: creating VRF %(vrf)s table %(t)s for domain "
                     "%(d)s", {'vrf': vrf_name, 't': vrf_table,
                               'd': domain_id})
            try:
                # create_interface passes kwargs straight through to
                # pyroute2, so vrf_table becomes IFLA_VRF_TABLE. No neutron
                # patch is needed for this.
                priv_ip_lib.create_interface(vrf_name, None, 'vrf',
                                             vrf_table=vrf_table)
            except priv_ip_lib.InterfaceAlreadyExists:
                pass
        ip_lib.IPDevice(vrf_name).link.set_up()
        self._set_sysctl('net.ipv4.conf.%s.rp_filter' % vrf_name, '0')

        self._seal_table(domain_id, vrf_table)

        local_sid = sid.format_sid(self.locator, function_id)
        priv_seg6.replace_seg6local_route(local_sid, behavior, vrf_name,
                                          vrf_table=vrf_table)
        LOG.info("SRv6: domain %(d)s local SID %(sid)s %(b)s -> table %(t)s",
                 {'d': domain_id, 'sid': local_sid, 'b': behavior,
                  't': vrf_table})

        self.domains[domain_id] = DomainState(domain_id, function_id,
                                              vrf_table, vrf_name)
        return self.domains[domain_id]

    @staticmethod
    def _seal_table(domain_id, vrf_table):
        """MIGRATION-PLAN.md 8.6. Fails CLOSED.

        A VRF that cannot be sealed is a VRF a tenant can escape from, so
        the domain is not programmed at all; the next resync retries.
        """
        for family_v6 in (False, True):
            try:
                priv_seg6.replace_unreachable_default(vrf_table,
                                                      family_v6=family_v6)
            except Exception as e:
                raise Srv6DataplaneError(
                    _("could not seal table %(t)s (IPv%(f)s) of domain "
                      "%(d)s, so it is not being programmed: a lookup that "
                      "misses would fall through to main, where every "
                      "domain's SID lives. %(e)s") %
                    {'t': vrf_table, 'f': 6 if family_v6 else 4,
                     'd': domain_id, 'e': e})

    def delete_domain(self, domain_id, function_id=None, vrf_table=None):
        """Remove everything this node holds for a domain.

        domain_id may be None: the kernel garbage collector (see
        present_function_ids) knows only the function id of what it found.
        vrf_table is part of the RPC payload -- it lets an agent that
        restarted since the domain was created still name the kernel
        objects -- but it is derived here from the function id, the same
        way it was derived when the table was created.
        """
        state = self.domains.pop(domain_id, None) if domain_id else None
        if state is not None:
            function_id = state.function_id
        if function_id is None:
            LOG.debug("SRv6: nothing to delete for unknown domain %s",
                      domain_id)
            return
        vrf_name = sid.vrf_name(function_id)
        table = sid.vrf_table(function_id, self.vrf_table_base)

        # The bridge, not only the in-memory map: after a restart the map is
        # empty while every svp-<fid>-* port is still on br-int.
        ports = set(self.gateway_ports.pop(domain_id, {}).values())
        ports.update(self._bridge_gateway_ports(function_id))
        for port_name in sorted(ports):
            self._delete_gateway_port(port_name)

        # Read from the kernel, like the ports, and while the VRF still
        # exists: the rules name it as their iif.
        for sid_address in sorted(self._sid_rules_for(vrf_name) or ()):
            try:
                priv_seg6.delete_sid_rule(vrf_name, sid_address)
            except Exception as e:
                LOG.debug("SRv6: removing rule %(v)s -> %(s)s: %(e)s",
                          {'v': vrf_name, 's': sid_address, 'e': e})

        local_sid = sid.format_sid(self.locator, function_id)
        try:
            priv_seg6.delete_route('%s/128' % local_sid, 'main',
                                   family_v6=True)
        except Exception as e:
            LOG.debug("SRv6: removing SID %(s)s: %(e)s",
                      {'s': local_sid, 'e': e})
        if ip_lib.device_exists(vrf_name):
            try:
                priv_ip_lib.delete_interface(vrf_name, None)
            except Exception as e:
                LOG.warning("SRv6: could not delete VRF %(v)s: %(e)s",
                            {'v': vrf_name, 'e': e})
        # After the device: deleting it does not empty the table.
        for family_v6 in (False, True):
            priv_seg6.flush_table(table, family_v6=family_v6)
        LOG.info("SRv6: domain %(d)s (function id %(f)s) torn down on this "
                 "node", {'d': domain_id, 'f': function_id})

    def present_function_ids(self):
        """Function ids that have a VRF in the kernel, or None.

        None means "could not tell", which the garbage collector must treat
        as "delete nothing" -- never as "there are none".
        """
        try:
            names = priv_seg6.list_vrf_names()
        except Exception as e:
            LOG.warning("SRv6: could not list VRF devices: %s", e)
            return None
        # A SID rule can outlive its VRF device (shown `[detached]`), so the
        # rules count as presence too. An unreadable rule list only means
        # those orphans wait for the next pass.
        try:
            names = list(names) + [vrf for vrf, _ in
                                   priv_seg6.list_sid_rules()]
        except Exception as e:
            LOG.debug("SRv6: could not list SID rules: %s", e)
        present = set()
        for name in names:
            if not name.startswith(constants.VRF_PREFIX):
                continue
            try:
                present.add(int(name[len(constants.VRF_PREFIX):]))
            except ValueError:
                continue
        return present

    # ------------------------------------------------------------------
    # gateway ports
    # ------------------------------------------------------------------
    def ensure_gateway_port(self, domain_id, network_id, local_vlan, subnets,
                            mtu=None):
        """An OVS internal port owning the subnet gateway inside the VRF.

        This is what pulls tenant traffic into the domain: the instances'
        default route points at an address Neutron reserves but never
        assigns, and this port claims it. Because the port is enslaved to
        the VRF, its connected route lands in the domain's table rather than
        in main.
        """
        state = self.domains.get(domain_id)
        if state is None:
            LOG.debug("SRv6: gateway port requested for unknown domain %s",
                      domain_id)
            return None
        if local_vlan is None:
            LOG.debug("SRv6: network %s has no local VLAN on this node yet",
                      network_id)
            return None

        port_name = sid.gateway_port_name(state.function_id, local_vlan)
        if self.bridge is not None:
            # 'type' is an Interface column and goes through add_port; 'tag'
            # is a *Port* column and must be set separately. Passing tag to
            # add_port puts it in a db_set against Interface, which aborts
            # the whole transaction -- so the port is never created at all
            # and the only symptom is a later "interface not found".
            self.bridge.add_port(port_name, ('type', 'internal'))
            # Re-asserted every time: the OVS agent reallocates local VLANs
            # when ports rebind, and a stale tag silently puts the gateway
            # in the wrong broadcast domain.
            self.bridge.set_db_attribute('Port', port_name, 'tag',
                                         local_vlan)
            # ovs-vsctl returns as soon as the row is in the database, but
            # the kernel netdev for an internal port is created afterwards
            # by ovs-vswitchd. Enslaving immediately therefore fails with
            # "interface not found" -- observed, not hypothetical -- so wait
            # for the device before touching it.
            if not self._wait_for_device(port_name):
                LOG.error("SRv6: OVS internal port %(p)s did not appear "
                          "within %(t)ss; gateway for network %(n)s not "
                          "programmed. It will be retried on the next "
                          "resync.",
                          {'p': port_name, 't': DEVICE_WAIT_SECONDS,
                           'n': network_id})
                return None

        # Enslave BEFORE addressing. An address added first lands in the
        # main table and does not move when the interface is later enslaved.
        try:
            priv_seg6.set_link_master(port_name, state.vrf_name)
        except Exception as e:
            LOG.error("SRv6: could not enslave %(p)s to %(v)s: %(e)s",
                      {'p': port_name, 'v': state.vrf_name, 'e': e})
            return None

        device = ip_lib.IPDevice(port_name)
        if mtu:
            try:
                device.link.set_mtu(mtu)
            except Exception as e:
                LOG.debug("SRv6: could not set mtu %(m)s on %(p)s: %(e)s",
                          {'m': mtu, 'p': port_name, 'e': e})
        device.link.set_up()
        self._set_sysctl('net.ipv4.conf.%s.rp_filter' % port_name, '0')

        # Ask what is already there rather than adding and interpreting the
        # failure: resync runs this repeatedly, and matching on an exception
        # class or message to detect "already present" is brittle enough that
        # it produced a false warning on every single resync.
        try:
            existing = {a['cidr'] for a in device.addr.list()}
        except Exception:
            existing = set()
        for subnet in subnets:
            gateway_ip = subnet.get('gateway_ip')
            if not gateway_ip:
                continue
            prefixlen = subnet['cidr'].split('/')[1]
            cidr = '%s/%s' % (gateway_ip, prefixlen)
            if cidr in existing:
                continue
            try:
                device.addr.add(cidr)
            except Exception as e:
                LOG.warning("SRv6: could not add %(c)s to %(p)s: %(e)s",
                            {'c': cidr, 'p': port_name, 'e': e})

        # The port NAME embeds the local VLAN, so a network whose VLAN the
        # OVS agent reallocated is programmed under a new name. Without
        # this the old port would be orphaned: overwriting the entry below
        # loses the only reference to it, and nothing would ever delete it.
        previous = self.gateway_ports[domain_id].get(network_id)
        if previous and previous != port_name:
            LOG.info("SRv6: domain %(d)s network %(n)s moved to local VLAN "
                     "%(t)s; removing its previous gateway port %(old)s",
                     {'d': domain_id, 'n': network_id, 't': local_vlan,
                      'old': previous})
            self._delete_gateway_port(previous)

        self.gateway_ports[domain_id][network_id] = port_name
        LOG.info("SRv6: domain %(d)s network %(n)s gateway port %(p)s "
                 "tag=%(t)s -> %(vrf)s",
                 {'d': domain_id, 'n': network_id, 'p': port_name,
                  't': local_vlan, 'vrf': state.vrf_name})
        return port_name

    def _bridge_gateway_ports(self, function_id):
        """Every gateway port this domain has on the bridge, from the bridge.

        Deliberately NOT self.gateway_ports: that map is process-local and
        starts empty, while the OVS ports outlive the agent. Reconciling
        against it therefore fixed nothing after a restart -- measured, not
        assumed. The names are deterministic (svp-<fid>-<vlan>), so the
        bridge can be asked directly, and it is the only account of what is
        really there.
        """
        if self.bridge is None:
            return []
        prefix = '%s%d-' % (constants.GW_PORT_PREFIX, function_id)
        try:
            return [p for p in self.bridge.get_port_name_list()
                    if p.startswith(prefix)]
        except Exception as e:
            LOG.warning("SRv6: could not list ports on the bridge: %s", e)
            return []

    def reconcile_gateway_ports(self, domain_id, local_vlans, has_networks):
        """Delete gateway ports for networks that have left the domain.

        Without this a network association delete leaks its port forever:
        ensure_gateway_port only ever added, and delete_domain only fires
        when the node leaves the domain *entirely*.

        local_vlans is the set of local VLANs the domain's networks resolve
        to on this node; has_networks says whether the domain has any
        network at all. The two are separate because they fail differently:

        - has_networks and no local_vlans means the OVS agent has not built
          its VLAN mapping yet, which is the state every agent restart
          passes through. Deleting on that would tear every gateway down
          and rebuild it seconds later on the first port event.
        - not has_networks is unambiguous: no network is in the domain, so
          every port of its is stale.
        """
        state = self.domains.get(domain_id)
        if state is None:
            return
        if has_networks and not local_vlans:
            LOG.debug("SRv6: domain %s has networks but none resolve to a "
                      "local VLAN yet; not reconciling its gateway ports",
                      domain_id)
            return
        wanted = {sid.gateway_port_name(state.function_id, vlan)
                  for vlan in local_vlans}
        for port_name in self._bridge_gateway_ports(state.function_id):
            if port_name in wanted:
                continue
            LOG.info("SRv6: domain %(d)s no longer has a network on local "
                     "VLAN for %(p)s; removing the leftover gateway port",
                     {'d': domain_id, 'p': port_name})
            self._delete_gateway_port(port_name)
            for net_id, name in list(self.gateway_ports[domain_id].items()):
                if name == port_name:
                    del self.gateway_ports[domain_id][net_id]

    @staticmethod
    def _wait_for_device(device, timeout=DEVICE_WAIT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ip_lib.device_exists(device):
                return True
            time.sleep(0.2)
        return ip_lib.device_exists(device)

    def _delete_gateway_port(self, port_name):
        if self.bridge is not None:
            try:
                self.bridge.delete_port(port_name)
            except Exception as e:
                LOG.debug("SRv6: deleting OVS port %(p)s: %(e)s",
                          {'p': port_name, 'e': e})

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def _transit_segments(self, segments):
        """A route's transit SIDs, minus any leading End SID of this node.

        The payload fans out to every node, and the server cannot know
        which one is the ingress. A path whose first waypoint is this node
        would otherwise send each packet into our own End behaviour and
        back out -- correct, but a pointless trip through the stack. Only
        LEADING occurrences are dropped: this node appearing later in the
        list is a real waypoint. Compared as addresses, not strings.
        """
        own = ipaddress.IPv6Address(self.own_end_sid)
        transit = list(segments or [])
        while transit:
            try:
                if ipaddress.IPv6Address(transit[0]) != own:
                    break
            except ValueError:
                break
            transit.pop(0)
        return transit

    def add_routes(self, domain_id, routes, locators, local_host, mtus=None):
        """Install one encap route per remote workload address.

        A route whose host is this node is skipped: the address is already
        reachable through the gateway port's connected route, and installing
        an encap route for it would send local traffic out to the underlay
        and back.

        A route may carry `segments` -- transit End SIDs the server resolved
        from an operator's TE path (P6) -- which go before the remote decap
        SID. A route without them is exactly the depth-1 route of the old
        build. `mtus` maps network_id -> MTU, for the per-route check of a
        steered route (P6-PLAN.md 4.3).

        Also makes sure the domain's VRF may reach the first segment of
        every route (_ensure_sid_rules); only adds, like the routes.
        Pruning is sync_routes' job.

        Returns the normalised prefixes this node should hold an encap
        route for, which is what sync_routes reconciles the table against.
        A prefix is counted as wanted as soon as it is decided to be, not
        when the write succeeds: a route that failed to install is absent
        from the table anyway, and treating it as unwanted would make a
        transient failure delete whatever is already there.
        """
        state = self.domains.get(domain_id)
        if state is None:
            return set()
        wanted, rule_sids = self._install_routes(state, routes, locators,
                                                 local_host, mtus)
        self._ensure_sid_rules(state, rule_sids)
        return wanted

    def _install_routes(self, state, routes, locators, local_host,
                        mtus=None):
        """The route loop. Returns (wanted prefixes, SIDs needing a rule).

        A route's outer destination is its FIRST segment. When that is the
        remote decap SID -- no transit -- the domain's VRF needs a rule to
        reach it (P5). When it is a transit End SID (P6), the VRF needs a
        rule toward that End SID, and such a rule lets a tenant hand-craft
        an SRH through it toward another domain's SID -- unless the edge
        filter stops tenant packets into the SID space and tenant SRHs at
        the gateway ports (MIGRATION-PLAN.md 8.7). So the rule is added only
        while the filter is active and covers that SID. Otherwise the route
        is installed but its outer header stays behind the seal: it fails
        closed, and says so.

        Only transit[0] ever gets a rule. The later segments are reached
        from main by the End behaviour on each waypoint, not from this VRF.
        """
        domain_id = state.domain_id
        wanted = set()
        rule_sids = set()
        for route in routes:
            host = route.get('host')
            if host == local_host:
                continue
            locator = locators.get(host)
            if not locator:
                LOG.debug("SRv6: no locator for host %s; skipping route",
                          host)
                continue
            prefix = route['prefix']
            wanted.add(_normalise_prefix(prefix))
            remote_sid = sid.format_sid(locator, state.function_id)
            transit = self._transit_segments(route.get('segments'))
            segments = transit + [remote_sid]
            if not transit:
                rule_sids.add(_normalise_sid(remote_sid))
            elif self._edge_filter_covers(transit[0]):
                rule_sids.add(_normalise_sid(transit[0]))
            else:
                LOG.warning("SRv6: domain %(d)s route %(p)s starts at "
                            "transit SID %(s)s, but the edge filter is not "
                            "active for it; the VRF may not reach an End SID "
                            "without it (MIGRATION-PLAN.md 8.7), so this "
                            "path is sealed",
                            {'d': domain_id, 'p': prefix, 's': transit[0]})
            self._check_route_mtu(prefix,
                                  (mtus or {}).get(route.get('network_id')),
                                  len(segments))
            try:
                priv_seg6.replace_seg6_route(
                    prefix, segments, self.underlay_interface,
                    state.vrf_table, family_v6=':' in prefix.split('/')[0])
            except Exception as e:
                LOG.error("SRv6: could not install %(p)s -> %(s)s: %(e)s",
                          {'p': prefix, 's': segments, 'e': e})
                continue
            LOG.debug("SRv6: domain %(d)s %(p)s -> segs %(s)s table %(t)s",
                      {'d': domain_id, 'p': prefix, 's': segments,
                       't': state.vrf_table})
        return wanted, rule_sids

    # ------------------------------------------------------------------
    # SID rules (P5-PLAN.md 4.6)
    # ------------------------------------------------------------------
    @staticmethod
    def _sid_rules_for(vrf_name):
        """This VRF's SID rules in the kernel, or None if unreadable.

        The kernel, not memory, for the same reason as the gateway ports
        and the routes: rules outlive the agent process.
        """
        try:
            rules = priv_seg6.list_sid_rules()
        except Exception as e:
            LOG.warning("SRv6: could not list SID rules: %s", e)
            return None
        return {_normalise_sid(s) for vrf, s in rules if vrf == vrf_name}

    def _ensure_sid_rules(self, state, sids):
        present = self._sid_rules_for(state.vrf_name) or set()
        for sid_address in sorted(set(sids) - present):
            try:
                priv_seg6.add_sid_rule(state.vrf_name, sid_address)
            except Exception as e:
                LOG.error("SRv6: could not let %(v)s reach %(s)s: %(e)s. "
                          "Traffic from this domain to that node is dropped "
                          "at the seal until the next resync.",
                          {'v': state.vrf_name, 's': sid_address, 'e': e})
                continue
            LOG.info("SRv6: domain %(d)s may reach %(s)s via main",
                     {'d': state.domain_id, 's': sid_address})

    def _remove_unwanted_sid_rules(self, state, wanted):
        """Drop rules toward SIDs no route of the domain uses any more.

        Only this VRF's rules are ever considered. An unreadable list
        deletes nothing.
        """
        present = self._sid_rules_for(state.vrf_name)
        if present is None:
            return
        for sid_address in sorted(present - set(wanted)):
            LOG.info("SRv6: domain %(d)s no longer encapsulates to %(s)s; "
                     "removing its rule", {'d': state.domain_id,
                                           's': sid_address})
            try:
                priv_seg6.delete_sid_rule(state.vrf_name, sid_address)
            except Exception as e:
                LOG.debug("SRv6: removing rule %(v)s -> %(s)s: %(e)s",
                          {'v': state.vrf_name, 's': sid_address, 'e': e})

    def sync_routes(self, domain_id, routes, locators, local_host,
                    mtus=None):
        """Make the domain's table hold exactly the routes the payload names.

        add_routes only ever added, exactly as the gateway ports only ever
        accumulated, so an address that left the domain kept its encap
        route: measured on node 1 on 2026-09-03.

        Also the withdraw half of a migration: a workload that moved to
        THIS node is skipped by add_routes rather than removed, so without
        this its old encap route would keep sending traffic out to the
        underlay and back.

        Only _apply_domain calls this. routes_updated stays on add_routes
        and remove_routes -- it carries a delta, not the whole domain, so
        reconciling against it would delete every route it did not mention.
        The SID rules follow the same split: a rule left behind by an
        incremental remove only lets the VRF reach its own domain's SID, and
        the next full sync prunes it. An End-SID rule whose path was removed
        or emptied is pruned the same way.
        """
        state = self.domains.get(domain_id)
        if state is None:
            return
        wanted, rule_sids = self._install_routes(state, routes, locators,
                                                 local_host, mtus)
        self._ensure_sid_rules(state, rule_sids)
        self._remove_unwanted_routes(domain_id, wanted)
        self._remove_unwanted_sid_rules(state, rule_sids)

    def _remove_unwanted_routes(self, domain_id, wanted):
        state = self.domains.get(domain_id)
        if state is None:
            return
        for family_v6 in (False, True):
            try:
                present = priv_seg6.list_seg6_routes(state.vrf_table,
                                                     family_v6=family_v6)
            except Exception as e:
                LOG.warning("SRv6: could not read table %(t)s: %(e)s",
                            {'t': state.vrf_table, 'e': e})
                continue
            for prefix in present:
                if _normalise_prefix(prefix) in wanted:
                    continue
                LOG.info("SRv6: domain %(d)s no longer reaches %(p)s; "
                         "removing its encap route from table %(t)s",
                         {'d': domain_id, 'p': prefix, 't': state.vrf_table})
                try:
                    priv_seg6.delete_route(prefix, state.vrf_table,
                                           family_v6=family_v6)
                except Exception as e:
                    LOG.debug("SRv6: removing %(p)s: %(e)s",
                              {'p': prefix, 'e': e})

    def remove_routes(self, domain_id, routes):
        state = self.domains.get(domain_id)
        if state is None:
            return
        for route in routes:
            prefix = route['prefix']
            try:
                priv_seg6.delete_route(
                    prefix, state.vrf_table,
                    family_v6=':' in prefix.split('/')[0])
            except Exception as e:
                LOG.debug("SRv6: removing %(p)s: %(e)s",
                          {'p': prefix, 'e': e})

    def _check_route_mtu(self, prefix, network_mtu, depth):
        """The MTU of one steered route, warned once per (prefix, depth).

        Depth 1 is the per-network check _apply_domain already makes. Each
        extra segment costs 16 bytes, so a network that fits at depth 1 may
        not fit steered; saying so on every resync would bury the log.
        """
        if depth < 2 or not network_mtu:
            return
        key = (_normalise_prefix(prefix), depth)
        if key in self._mtu_warned:
            return
        if not self.check_mtu(network_mtu, segment_count=depth,
                              prefix=prefix):
            self._mtu_warned.add(key)

    def check_mtu(self, network_mtu, segment_count=1, prefix=None):
        """Warn when the tenant MTU plus encapsulation exceeds the underlay.

        Not fatal: small packets still pass, so refusing to program the
        domain would be a worse outcome than a warning plus working
        connectivity for everything under the limit.
        """
        if not network_mtu or not self.underlay_interface:
            return True
        overhead = sid.encap_overhead(segment_count)
        try:
            underlay_mtu = ip_lib.IPDevice(
                self.underlay_interface).link.mtu
        except Exception:
            return True
        if network_mtu + overhead > underlay_mtu:
            LOG.warning(
                "SRv6: tenant MTU %(n)s plus %(o)s bytes of encapsulation"
                "%(r)s exceeds underlay MTU %(u)s on %(i)s. Full-size tenant "
                "frames will be dropped. Lower the network MTU to %(max)s "
                "or raise the underlay MTU.",
                {'n': network_mtu, 'o': overhead, 'u': underlay_mtu,
                 'i': self.underlay_interface,
                 'max': underlay_mtu - overhead,
                 'r': (' (route %s, %d segments)' % (prefix, segment_count)
                       if prefix else '')})
            return False
        return True
