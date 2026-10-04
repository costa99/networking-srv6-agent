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

Per gateway port and gateway address, on a node with tunnels (F12: every
node's gateway of a network holds the same address on one L2 segment, so
its ARP and ND must not cross nodes; IPv6 gateways are added `nodad`):
    table=0,priority=100,in_port=<patch-tun>,dl_vlan=<vlan>,arp,
        arp_tpa=<gw4> actions=drop                 # and arp_spa=<gw4>
    table=0,priority=100,in_port=<patch-tun>,dl_vlan=<vlan>,icmp6,
        icmp_type=135,nd_target=<gw6> actions=drop # and icmp_type=136

With the OVS firewall driver, per gateway port (F1, F2: the port is not a
Neutron port, so without these the firewall never sees its traffic):
    table=60,priority=100,in_port=<of> actions=ct_clear,
        set_field:<of>->reg5,set_field:<vlan>->reg6,resubmit(,73)
    table=73,priority=80,reg5=<of> actions=resubmit(,94)

Per first segment the domain encapsulates to -- a remote decap SID, or for a
steered route (P6) a transit End SID, the latter only while the edge filter
covers it:
    ip -6 rule add pref 999 iif sv6vrf-<fid> to <first seg>/128 lookup main

Per node, over every registered locator (P6):
    table inet srv6_edge -- on iifname "svp-*", drop `ip6 daddr @sid_space`
                            and drop `rt type 4` (any tenant-built SRH)

With dataplane_encoding = usid (RFC 9800 NEXT-C-SID, USID-PLAN.md) the node
state changes and the routes are packed:
    ip -6 route replace <locator>/48 encap seg6local action End \
        flavors next-csid lblen 32 nflen 16 dev <underlay>   # the uN, in
                                         # place of `local <locator> dev lo`
    ip -6 route replace unreachable <block>:<node>:e000::/52 table main
    ip -6 route replace <sid>/64 encap seg6local action End.DT46 ...
    ip route replace <ip>/32 encap seg6 mode encap.red \
        segs <C-SID container>[,...] dev <underlay> table <table>

Four things differ from the old build, each a defect it had or one found at
the gate: the table is sealed, the seal lets the domain's own outer headers
out through the SID rules, the table is flushed on delete, and a delete finds
its gateway ports on the bridge rather than in memory.
"""

import collections
import ipaddress
import time

from neutron.agent.linux import ip_lib
from neutron.privileged.agent.linux import ip_lib as priv_ip_lib
from neutron_lib.agent.common import constants as agent_consts
from neutron_lib.plugins.ml2 import ovs_constants as ovs_const
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
#   tcp_/udp_l3mdev_accept
#                 0, the kernel default, asserted (F7): at 1 a socket bound
#                 in the default VRF answers connections that arrive through
#                 any VRF, so every service of the host would be open to
#                 every tenant. With the local rule after l3mdev a VRF's
#                 packet is not delivered locally anyway; this is the second
#                 lock on the same door.
GLOBAL_SYSCTLS = [
    ('net.ipv6.conf.all.forwarding', '1'),
    ('net.ipv4.ip_forward', '1'),
    ('net.ipv6.conf.all.seg6_enabled', '1'),
    ('net.ipv6.conf.default.seg6_enabled', '1'),
    ('net.ipv6.conf.lo.seg6_enabled', '1'),
    ('net.vrf.strict_mode', '1'),
    ('net.ipv4.conf.all.rp_filter', '0'),
    ('net.ipv4.conf.default.rp_filter', '0'),
    ('net.ipv4.tcp_l3mdev_accept', '0'),
    ('net.ipv4.udp_l3mdev_accept', '0'),
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
    """The whole edge-filter table as one nft script.

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
                 bridge=None, apply_sysctls=True, ovs_firewall=False,
                 tunnel_patch_port=None):
        self.locator = locator
        self.underlay_interface = underlay_interface
        self.vrf_table_base = vrf_table_base
        self.bridge = bridge
        self.apply_sysctls = apply_sysctls
        # Whether the OVS agent filters ports with the openvswitch firewall
        # driver; only then do gateway ports need firewall flows.
        self.ovs_firewall = ovs_firewall
        # gateway port name -> ofport its firewall flows were installed for
        self.gateway_flows = {}
        # br-int's patch port to br-tun, or None without tunnels; and
        # gateway port name -> the ND/ARP flow matches installed for it (F12)
        self.tunnel_patch_port = tunnel_patch_port
        self.gateway_nd_flows = {}
        # domain_id -> DomainState, and domain_id -> {network_id: port name}
        self.domains = {}
        self.gateway_ports = collections.defaultdict(dict)
        # True only after a successful apply; the networks it covers then.
        self.edge_filter_active = False
        self._edge_filtered = ()
        # (prefix, depth) already warned about, so a steered route that does
        # not fit says so once rather than on every resync.
        self._mtu_warned = set()
        # How segment lists are encoded, as the server last said
        # (ensure_encoding). initialize_node reads what the kernel already
        # holds, so a restart in a usid deployment does not flap the uN.
        self.encoding = constants.ENCODING_ENCAP
        # What the probe found this node can do; None until it has run.
        self.capabilities = None

    @property
    def own_end_sid(self):
        return sid.format_sid(self.locator, constants.NODE_END_FUNCTION_ID)

    @property
    def usid(self):
        return self.encoding == constants.ENCODING_USID

    @property
    def own_transit_sid(self):
        """The SID other nodes name this one by as a TE waypoint.

        The End SID <locator>:fff0:: with full SIDs; the uN, the locator's
        own address, in usid mode.
        """
        if self.usid:
            return sid.node_sid(self.locator)
        return self.own_end_sid

    @property
    def decap_prefixlen(self):
        """/128 with full SIDs; RFC 9800 s5.3's block+node+function /64."""
        if self.usid:
            return constants.USID_FUNCTION_PREFIXLEN
        return 128

    # ------------------------------------------------------------------
    # node-level setup
    # ------------------------------------------------------------------
    def initialize_node(self):
        """Sysctls, the vrf module, and this node's locator route."""
        self._ensure_module_vrf()
        self._apply_global_sysctls()
        self._ensure_local_rule_after_l3mdev()
        if self.underlay_interface:
            self._set_sysctl(
                'net.ipv6.conf.%s.seg6_enabled' % self.underlay_interface, '1')
            self._set_sysctl(
                'net.ipv4.conf.%s.rp_filter' % self.underlay_interface, '0')
        self._probe_capabilities()
        # Whatever encoding the kernel shows from before a restart is kept
        # until the server says otherwise: in a usid deployment, putting
        # the `local` route back here would cut every TE path through this
        # node for the seconds until the first payload restored the uN.
        self.encoding = self._kernel_encoding()
        self._ensure_locator_route()
        self._ensure_node_end_sid()
        # Own locator only for now; the first sync_state widens it to every
        # registered node before any route is programmed.
        self.ensure_edge_filter([self.locator])
        LOG.info("SRv6 dataplane initialised: locator=%(loc)s "
                 "underlay=%(ul)s encoding=%(enc)s capabilities=%(cap)s",
                 {'loc': self.locator, 'ul': self.underlay_interface,
                  'enc': self.encoding,
                  'cap': ', '.join(self.capabilities or []) or 'none'})

    def _probe_capabilities(self):
        try:
            self.capabilities = sorted(priv_seg6.probe_capabilities())
        except Exception as e:
            LOG.warning("SRv6: could not probe for encap.red / next-csid "
                        "support: %s", e)
            self.capabilities = []

    def _locator_routes(self):
        """(has the `local` route, has a uN) on this node's locator."""
        lines = priv_seg6.show_main_route(self.locator)
        has_local = any(line.split()[0] == 'local' for line in lines)
        has_un = any('seg6local' in line.split() for line in lines)
        return has_local, has_un

    def _kernel_encoding(self):
        try:
            _has_local, has_un = self._locator_routes()
        except Exception as e:
            LOG.debug("SRv6: could not read the locator route: %s", e)
            return constants.ENCODING_ENCAP
        return constants.ENCODING_USID if has_un else constants.ENCODING_ENCAP

    def ensure_encoding(self, encoding):
        """Switch node state to an encoding. Returns whether it changed.

        Node state, like the End SID: what the locator /48 is, and whether
        the Local ID Block is sealed. Per-domain state follows on the next
        ensure_domain / sync_routes, which is why a change makes the agent
        resync every domain.

        A usid request this node cannot honour -- a locator that is not
        F3216, or a kernel without the flavor -- is refused here, loudly,
        and the node stays on full SIDs. That is the black hole of
        USID-PLAN.md D3 made visible; the probe told the server too.
        """
        if encoding not in constants.VALID_ENCODINGS:
            LOG.error("SRv6: the server asked for unknown dataplane "
                      "encoding %r; keeping %s", encoding, self.encoding)
            return False
        if encoding == self.encoding:
            return False
        if encoding == constants.ENCODING_USID:
            try:
                sid.check_usid_locator(self.locator)
            except sid.InvalidLocator as e:
                LOG.error("SRv6: dataplane_encoding is usid but this node "
                          "cannot take part: %s. TE paths through it and "
                          "traffic to it will fail.", e)
                return False
            missing = [c for c in constants.ENCODING_REQUIRES[encoding]
                       if c not in (self.capabilities or [])]
            if self.capabilities is not None and missing:
                LOG.error("SRv6: dataplane_encoding is usid but this "
                          "node's kernel or iproute2 lacks %s; trying "
                          "anyway, expect the routes to fail",
                          ', '.join(missing))
        previous = self.encoding
        self.encoding = encoding
        self._ensure_locator_route()
        # A domain's decap SID moves between /128 and /64; the old length
        # would otherwise linger in main until the domain is deleted.
        old_len = 128 if self.decap_prefixlen != 128 else \
            constants.USID_FUNCTION_PREFIXLEN
        for state in self.domains.values():
            self._delete_decap_route(state.function_id, old_len)
        self._mtu_warned.clear()
        LOG.info("SRv6: dataplane encoding %(old)s -> %(new)s",
                 {'old': previous, 'new': encoding})
        return True

    def _ensure_locator_route(self):
        """The /48 is the `local` route with full SIDs, the uN with usid.

        Both sit on the same prefix in the same table, so one replaces the
        other (USID-PLAN.md D6). The old one is deleted first, by what the
        kernel shows rather than what this process remembers, so the switch
        works in both directions and after a restart.

        In usid mode the Local ID Block is sealed with an unreachable /52:
        a LIB function that has no /64 decap route is dropped there instead
        of the uN reading it as the next node. With full SIDs the seal is
        removed, since there is no uN for it to protect against.
        """
        try:
            has_local, has_un = self._locator_routes()
        except Exception as e:
            LOG.debug("SRv6: could not read the locator route: %s", e)
            has_local = has_un = False
        if self.usid:
            if has_local:
                priv_seg6.delete_main_route(self.locator, route_type='local')
            self._ensure_node_usid()
            try:
                priv_seg6.replace_unreachable_route(
                    sid.lib_prefix(self.locator))
            except Exception as e:
                LOG.error("SRv6: could not seal the Local ID Block %(p)s: "
                          "%(e)s. A packet for an unallocated function is "
                          "forwarded as if the function were a node.",
                          {'p': sid.lib_prefix(self.locator), 'e': e})
            return
        if has_un:
            priv_seg6.delete_main_route(self.locator)
        priv_seg6.replace_locator_route(self.locator)
        try:
            lib = sid.lib_prefix(self.locator)
        except sid.InvalidLocator:
            return
        try:
            priv_seg6.delete_main_route(lib, route_type='unreachable')
        except Exception as e:
            LOG.debug("SRv6: removing the LIB seal %(p)s: %(e)s",
                      {'p': lib, 'e': e})

    def _ensure_node_usid(self):
        """This node's uN: End with the NEXT-C-SID flavor on the whole /48.

        A packet whose DA is <block>:<this node>:<more C-SIDs> is shifted
        left one C-SID and forwarded by an ordinary lookup, with no SRH; a
        DA whose remaining C-SIDs are zero falls back to classic End on the
        SRH, which is how a path longer than one container continues
        (RFC 9800 s4.1.1). Same placement rules as the End SID: main, and
        `dev` is syntax, not the forwarding decision.
        """
        dev = self.underlay_interface or 'lo'
        try:
            priv_seg6.replace_seg6local_route(
                sid.node_sid(self.locator), constants.BEHAVIOR_END, dev,
                prefixlen=constants.USID_LOCATOR_PREFIXLEN,
                next_csid=(constants.USID_BLOCK_BITS,
                           constants.USID_CSID_BITS))
        except Exception as e:
            LOG.error("SRv6: could not install this node's uN %(s)s: %(e)s. "
                      "Nothing addressed to this node's locator is "
                      "delivered, and TE paths through it fail.",
                      {'s': self.locator, 'e': e})
            return
        LOG.info("SRv6: node uN %(s)s (End, next-csid lblen %(b)d nflen "
                 "%(c)d) via %(d)s",
                 {'s': self.locator, 'b': constants.USID_BLOCK_BITS,
                  'c': constants.USID_CSID_BITS, 'd': dev})

    def _delete_decap_route(self, function_id, prefixlen):
        local_sid = sid.format_sid(self.locator, function_id)
        try:
            priv_seg6.delete_route('%s/%d' % (local_sid, prefixlen), 'main',
                                   family_v6=True)
        except Exception as e:
            LOG.debug("SRv6: removing SID %(s)s/%(l)d: %(e)s",
                      {'s': local_sid, 'l': prefixlen, 'e': e})

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

    def _ensure_local_rule_after_l3mdev(self):
        """Move the `local` FIB rule from 0 to after l3mdev (1000), F7.

        At 0 a lookup from inside a VRF consults the host's local table
        before the l3mdev rule sends it to the VRF's table, so every
        address of the host -- management, underlay, any other -- answered
        a tenant: ICMP, and anything a raw socket takes. The new rule goes
        in before the old one is deleted, so the host is never without a
        local rule. Every agent start re-asserts it: a reboot restores the
        kernel default.

        A failure is logged, not raised: refusing to start would take every
        domain on this node down to close a hole that only exposes the
        host itself, and the next start tries again.
        """
        for family_v6 in (False, True):
            name = 'IPv6' if family_v6 else 'IPv4'
            try:
                prefs = priv_seg6.local_rule_prefs(family_v6=family_v6)
                if constants.LOCAL_RULE_PRIORITY not in prefs:
                    priv_seg6.add_local_rule(constants.LOCAL_RULE_PRIORITY,
                                             family_v6=family_v6)
                for pref in prefs:
                    if pref < constants.LOCAL_RULE_PRIORITY:
                        priv_seg6.delete_local_rule(pref, family_v6=family_v6)
                        LOG.info("SRv6: %(f)s local rule moved from pref "
                                 "%(old)d to %(new)d, after l3mdev",
                                 {'f': name, 'old': pref,
                                  'new': constants.LOCAL_RULE_PRIORITY})
            except Exception as e:
                LOG.error("SRv6: could not move the %(f)s local rule after "
                          "l3mdev: %(e)s. Tenants can reach this host's own "
                          "addresses from inside their VRF.",
                          {'f': name, 'e': e})

    def _apply_global_sysctls(self):
        for key, value in GLOBAL_SYSCTLS:
            self._set_sysctl(key, value)

    # ------------------------------------------------------------------
    # edge filter (P6)
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
        # A plain End.DT46 also serves as the last C-SID of a compressed
        # path (RFC 9800 s4.1): after the final shift the DA is exactly
        # <block>:<node>:<function>::, which this route matches.
        # Extra arguments only in usid mode: with full SIDs the call stays
        # exactly what every earlier build made.
        extra = {'prefixlen': self.decap_prefixlen} if self.usid else {}
        priv_seg6.replace_seg6local_route(local_sid, behavior, vrf_name,
                                          vrf_table=vrf_table, **extra)
        LOG.info("SRv6: domain %(d)s local SID %(sid)s/%(l)d %(b)s -> "
                 "table %(t)s",
                 {'d': domain_id, 'sid': local_sid, 'b': behavior,
                  'l': self.decap_prefixlen, 't': vrf_table})

        self.domains[domain_id] = DomainState(domain_id, function_id,
                                              vrf_table, vrf_name)
        return self.domains[domain_id]

    @staticmethod
    def _seal_table(domain_id, vrf_table):
        """Fails CLOSED.

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

        # Both lengths whatever the encoding: one left from before a mode
        # switch would otherwise outlive the domain.
        for prefixlen in (128, constants.USID_FUNCTION_PREFIXLEN):
            self._delete_decap_route(function_id, prefixlen)
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
            self._ensure_gateway_flows(port_name, local_vlan)
            # Before the address: no other node may hear it claimed.
            self._ensure_gateway_nd_flows(port_name, local_vlan, subnets)

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
            existing = {a['cidr']: a for a in device.addr.list()}
        except Exception:
            existing = {}
        for subnet in subnets:
            gateway_ip = subnet.get('gateway_ip')
            if not gateway_ip:
                continue
            prefixlen = subnet['cidr'].split('/')[1]
            cidr = '%s/%s' % (gateway_ip, prefixlen)
            present = existing.get(cidr)
            if present is not None and not present.get('dadfailed'):
                continue
            try:
                if ':' not in gateway_ip:
                    device.addr.add(cidr)
                    continue
                if present is not None:
                    # Added by an older build, with DAD, and it lost to
                    # another node's gateway: the kernel neither uses nor
                    # answers for it, and never retries (F12).
                    LOG.info("SRv6: %(c)s on %(p)s failed duplicate address "
                             "detection; re-adding it without DAD",
                             {'c': cidr, 'p': port_name})
                    device.addr.delete(cidr)
                priv_seg6.add_address_nodad(port_name, cidr)
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

    def local_network_vlans(self):
        """network_id -> {local VLAN, ...} of every live port on br-int.

        Read from OVSDB, not from the OVS agent's in-memory LocalVlanManager.
        port_bound writes Port.other_config:net_uuid and Port.tag before
        any extension hears of the port, and the agent rebuilds its own VLAN
        hints from those two columns after a restart
        (_restore_local_vlan_map) -- so they are right even while the
        in-memory map is still empty, which it is during every restart.
        That emptiness is what used to force reconcile_gateway_ports to
        give up, and giving up kept leftover gateways forever (F6, F8).

        A port in the dead VLAN (4095: admin down, unbound) is not live, and
        a gateway port is not a network's port. Returns None if the bridge
        cannot be read: the callers then delete nothing.
        """
        if self.bridge is None:
            return None
        try:
            rows = self.bridge.get_ports_attributes(
                'Port', columns=['name', 'tag', 'other_config'],
                if_exists=True)
        except Exception as e:
            LOG.warning("SRv6: could not read br-int ports from OVSDB: %s", e)
            return None
        present = collections.defaultdict(set)
        for row in rows or []:
            if str(row.get('name', '')).startswith(constants.GW_PORT_PREFIX):
                continue
            net_id = (row.get('other_config') or {}).get('net_uuid')
            tag = row.get('tag')
            # an optional column: ovsdbapp gives an int when set and [] when
            # not; a one-element list is accepted too, to be safe
            if isinstance(tag, list) and len(tag) == 1:
                tag = tag[0]
            if not net_id or not isinstance(tag, int):
                continue
            if tag == ovs_const.DEAD_VLAN_TAG:
                continue
            present[net_id].add(tag)
        return dict(present)

    def reconcile_gateway_ports(self, domain_id, local_vlans):
        """Delete gateway ports for networks that are no longer local.

        local_vlans is the set of VLANs on which the domain's networks have
        a live port on this node (local_network_vlans), so an empty set
        means exactly that: nothing of this domain is here any more, and
        every svp-<fid>-* of it goes. A port is stale when its network left
        the domain (a detach, F6) or when the network's last port left the
        node (a VM deleted or migrated, F8). The second case is the
        dangerous one: the OVS agent reclaims the VLAN and hands it to the
        next network that arrives, and a leftover gateway then routes that
        network -- any project's -- into this domain. t9 measured it on
        2026-10-03 (REPORT-ISOLATION.md 4.7): r1 +3, r2 +3.

        There used to be an early return here for "the domain has networks
        but none resolves to a VLAN", meant for the empty VLAN map every
        restart passes through. It could not tell that apart from "no
        network is local any more", and kept every leftover. The VLANs now
        come from OVSDB, which is not empty at a restart, so the branch is
        gone.
        """
        state = self.domains.get(domain_id)
        if state is None:
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

    # ------------------------------------------------------------------
    # firewall flows of gateway ports (F1, F2)
    # ------------------------------------------------------------------
    def _ensure_gateway_flows(self, port_name, local_vlan):
        """Show a gateway port to the OVS firewall as an unfiltered port.

        The firewall only programs Neutron ports. An svp-* port is not one,
        so table 60 sent its frames straight to NORMAL: they skipped the
        destination's ingress pipeline, so the security group never applied
        to traffic routed in (F2), and its conntrack zone never saw the
        reply's other half, so the instance's reply was +inv and dropped
        (F1). These are the two flows the firewall installs for a port with
        port security disabled (_initialize_egress_no_port_security), so
        frames from the gateway now enter the destination's ingress pipeline
        like frames from any other port.

        One difference, ct_clear, measured by t11 (LOG-2026-10-03-portsec.md
        s2): the frames leave the host's IP stack carrying its own conntrack
        entry whenever the host tracks connections at all (devstack's NAT
        rules are enough), OVS takes it as +trk, and table 81 then skips the
        guest-zone lookup and drops the reply as not established. qr-*
        ports never show this because crossing a namespace clears it; svp-*
        ports live in the root namespace.

        Re-asserted on every call, with this process's cookie: the OVS agent
        deletes flows of any other cookie at its first iteration
        (cleanup_stale_flows), so a restart removes them, and the agent
        installs them again when handle_port and the resync reach the port.
        """
        if not self.ovs_firewall:
            return
        ofport = self.bridge.get_port_ofport(port_name)
        if not isinstance(ofport, int) or ofport <= 0:
            LOG.warning("SRv6: gateway port %(p)s has no ofport yet "
                        "(%(o)s); its firewall flows wait for the next "
                        "resync", {'p': port_name, 'o': ofport})
            return
        if self.gateway_flows.get(port_name) not in (None, ofport):
            # Same name, new ofport: the old flows would match whatever
            # port OVS gives that number next.
            self._delete_gateway_flows(port_name)
        try:
            self.bridge.add_flow(
                table=ovs_const.TRANSIENT_TABLE,
                priority=100,
                in_port=ofport,
                actions='ct_clear,'
                        'set_field:%d->reg%d,'
                        'set_field:%d->reg%d,'
                        'resubmit(,%d)' % (
                            ofport, agent_consts.REG_PORT,
                            local_vlan, agent_consts.REG_NET,
                            ovs_const.ACCEPT_OR_INGRESS_TABLE))
            self.bridge.add_flow(
                table=ovs_const.ACCEPT_OR_INGRESS_TABLE,
                priority=80,
                **{'reg%d' % agent_consts.REG_PORT: ofport,
                   'actions': 'resubmit(,%d)' % (
                       ovs_const.ACCEPTED_EGRESS_TRAFFIC_NORMAL_TABLE)})
        except Exception as e:
            LOG.error("SRv6: could not install the firewall flows of "
                      "%(p)s: %(e)s. Instances behind it with port security "
                      "on cannot answer routed traffic until the next "
                      "resync.", {'p': port_name, 'e': e})
            return
        self.gateway_flows[port_name] = ofport

    def _delete_gateway_flows(self, port_name):
        """Remove the flows _ensure_gateway_flows installed for a port.

        Only this process's cookie is matched (the bridge adds it), so a
        firewall flow that took over the same match is left alone. Flows of
        an earlier process are not known here; cleanup_stale_flows removes
        them.
        """
        ofport = self.gateway_flows.pop(port_name, None)
        if ofport is None or self.bridge is None:
            return
        try:
            self.bridge.delete_flows(table=ovs_const.TRANSIENT_TABLE,
                                     in_port=ofport)
            self.bridge.delete_flows(
                table=ovs_const.ACCEPT_OR_INGRESS_TABLE,
                **{'reg%d' % agent_consts.REG_PORT: ofport})
        except Exception as e:
            LOG.warning("SRv6: could not remove the firewall flows of "
                        "%(p)s (ofport %(o)s): %(e)s",
                        {'p': port_name, 'o': ofport, 'e': e})

    # ------------------------------------------------------------------
    # neighbour discovery of gateway ports (F12)
    # ------------------------------------------------------------------
    @staticmethod
    def _gateway_nd_matches(subnets):
        """The ARP/ND fields that name a gateway address, per subnet.

        Who-has and is-at for an IPv4 gateway (a request names it as target,
        a reply or a gratuitous ARP as sender); Neighbor Solicitation (135)
        and Advertisement (136) for an IPv6 one -- DAD's probe is an NS too.
        """
        matches = set()
        for subnet in subnets or []:
            gateway_ip = subnet.get('gateway_ip')
            if not gateway_ip:
                continue
            if ':' in gateway_ip:
                for icmp_type in (135, 136):
                    matches.add((('proto', 'icmp6'),
                                 ('icmp_type', icmp_type),
                                 ('nd_target', gateway_ip)))
            else:
                for field in ('arp_tpa', 'arp_spa'):
                    matches.add((('proto', 'arp'), (field, gateway_ip)))
        return matches

    def _ensure_gateway_nd_flows(self, port_name, local_vlan, subnets):
        """Keep the gateway's ARP and ND on this node (F12).

        Every node with a port of the network has a gateway port on it,
        each holding the subnet's gateway address, and neutron joins the
        nodes' local VLANs of the network into one L2 segment over the
        tunnels. So a guest's who-has or Neighbor Solicitation for its
        gateway reached every node's gateway, and any of them could answer:
        for IPv4 a race the local one usually won, for IPv6 worse, because
        duplicate address detection disabled the address on every node but
        the first ("dadfailed"), whose gateway then served the network on
        every node, over the tunnel. When that gateway went, IPv6 routing
        on the others stopped for good: DAD is not retried (spike,
        LOG-2026-10-04-vrf-fixes.md).

        Dropping, on the way in from the tunnel, every ARP or ND frame that
        names a gateway address of this network makes each node's gateway
        the only one its guests can hear, and hides it from the other
        nodes' DAD. Only these frames: the guests' own ARP and ND, and all
        unicast traffic, cross the tunnel as before. Matched on the
        network's local VLAN, since another network may use the same
        gateway address.

        Re-asserted on every call, with this process's cookie, like the
        firewall flows, and for the same reason: the OVS agent sweeps them
        at its first iteration after a restart. Only br-tun is covered: a
        network carried by a physical bridge (VLAN or flat) has no tunnel
        port to filter, and keeps the old behaviour.
        """
        if self.bridge is None or not self.tunnel_patch_port:
            return
        ofport = self.bridge.get_port_ofport(self.tunnel_patch_port)
        if not isinstance(ofport, int) or ofport <= 0:
            LOG.warning("SRv6: no ofport for %(t)s; the gateway ARP/ND "
                        "flows of %(p)s wait for the next resync",
                        {'t': self.tunnel_patch_port, 'p': port_name})
            return
        wanted = {(ofport, local_vlan, match)
                  for match in self._gateway_nd_matches(subnets)}
        stale = self.gateway_nd_flows.get(port_name, set()) - wanted
        if stale:
            self._delete_nd_flows(port_name, stale)
        installed = set()
        for flow in sorted(wanted):
            of, vlan, match = flow
            try:
                self.bridge.add_flow(
                    table=ovs_const.LOCAL_SWITCHING,
                    priority=constants.GATEWAY_ND_FLOW_PRIORITY,
                    in_port=of, dl_vlan=vlan, actions='drop',
                    **dict(match))
            except Exception as e:
                LOG.error("SRv6: could not install a gateway ARP/ND flow of "
                          "%(p)s (%(m)s): %(e)s",
                          {'p': port_name, 'm': dict(match), 'e': e})
                continue
            installed.add(flow)
        self.gateway_nd_flows[port_name] = installed

    def _delete_nd_flows(self, port_name, flows):
        for of, vlan, match in sorted(flows):
            try:
                self.bridge.delete_flows(table=ovs_const.LOCAL_SWITCHING,
                                         in_port=of, dl_vlan=vlan,
                                         **dict(match))
            except Exception as e:
                LOG.warning("SRv6: could not remove a gateway ARP/ND flow "
                            "of %(p)s (%(m)s): %(e)s",
                            {'p': port_name, 'm': dict(match), 'e': e})

    def _delete_gateway_nd_flows(self, port_name):
        flows = self.gateway_nd_flows.pop(port_name, None)
        if flows and self.bridge is not None:
            self._delete_nd_flows(port_name, flows)

    def _delete_gateway_port(self, port_name):
        # Flows first: once the port is gone OVS can hand its ofport to the
        # next port, and the flows would apply to that one; and the VLAN
        # the ND flows match can go to another network.
        self._delete_gateway_flows(port_name)
        self._delete_gateway_nd_flows(port_name)
        if self.bridge is not None:
            try:
                self.bridge.delete_port(port_name)
            except Exception as e:
                LOG.debug("SRv6: deleting OVS port %(p)s: %(e)s",
                          {'p': port_name, 'e': e})

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    @staticmethod
    def _segments_for(route, local_host):
        """The transit SIDs this node, as the ingress, pushes for a route.

        One payload fans out to every node, so a route carries the transit
        list of each ingress that a source-specific TE path names
        (`segments_by_source`, keyed by host) next to the list that holds
        at every other ingress (`segments`). This node takes its own entry
        when there is one, and `segments` otherwise. An entry that is an
        empty list is an explicit "direct from this node": it must win over
        `segments`, which is why membership is tested, not truth. All of
        the precedence between paths is the server's (_attach_segments);
        this is only the lookup of the answer it computed for this host.
        """
        by_source = route.get('segments_by_source') or {}
        if local_host in by_source:
            return by_source[local_host]
        return route.get('segments')

    def _transit_segments(self, segments):
        """A route's transit SIDs, minus any leading transit SID of ours.

        The payload fans out to every node, and the server does not know
        which one will encapsulate a given packet: any-source segments are
        pushed by every node, a source entry by the node it names, which
        may also be its first waypoint. A path whose first waypoint is this
        node would otherwise send each packet into our own End behaviour and
        back out -- correct, but a pointless trip through the stack. Only
        LEADING occurrences are dropped: this node appearing later in the
        list is a real waypoint. Compared as addresses, not strings. "Ours"
        is the End SID, or the uN in usid mode -- whichever the server
        resolved the via hosts to.
        """
        own = ipaddress.IPv6Address(self.own_transit_sid)
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
        SID, and `segments_by_source`, the lists that hold only at the
        ingress nodes it names (_segments_for). A route without either is
        exactly the depth-1 route of the old build. `mtus` maps network_id
        -> MTU, for the per-route check of a steered route.

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
        the gateway ports. So the rule is added only
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
            transit = self._transit_segments(
                self._segments_for(route, local_host))
            segments, mode = self._encode(transit + [remote_sid], prefix)
            # The outer DA is the first PUSHED element: the decap SID, a
            # transit End SID, or in usid mode the first C-SID container --
            # which starts with the first hop's block and node, so it lies
            # inside that node's locator and the edge filter covers it.
            first = segments[0]
            if not transit:
                rule_sids.add(_normalise_sid(first))
            elif self._edge_filter_covers(first):
                rule_sids.add(_normalise_sid(first))
            else:
                LOG.warning("SRv6: domain %(d)s route %(p)s starts at "
                            "transit SID %(s)s, but the edge filter is not "
                            "active for it; the VRF may not reach an End SID "
                            "without it, so this "
                            "path is sealed",
                            {'d': domain_id, 'p': prefix, 's': first})
            self._check_route_mtu(prefix,
                                  (mtus or {}).get(route.get('network_id')),
                                  len(segments))
            extra = {'mode': mode} if self.usid else {}
            try:
                priv_seg6.replace_seg6_route(
                    prefix, segments, self.underlay_interface,
                    state.vrf_table, family_v6=':' in prefix.split('/')[0],
                    **extra)
            except Exception as e:
                LOG.error("SRv6: could not install %(p)s -> %(s)s: %(e)s",
                          {'p': prefix, 's': segments, 'e': e})
                continue
            LOG.debug("SRv6: domain %(d)s %(p)s -> segs %(s)s table %(t)s",
                      {'d': domain_id, 'p': prefix, 's': segments,
                       't': state.vrf_table})
        return wanted, rule_sids

    def _encode(self, segments, prefix):
        """(what to push, seg6 mode) for a logical segment list.

        Full SIDs with H.Encaps, or in usid mode the list packed into
        NEXT-C-SID containers and pushed with H.Encaps.Red, so a path that
        fits one container carries no SRH. A list that cannot be packed --
        locators from different blocks -- is pushed as full SIDs, still
        reduced: bigger, but it works, since every SID in it is a valid
        SID on its own.
        """
        if not self.usid:
            return segments, constants.SEG6_MODE_ENCAP
        try:
            return (sid.pack_next_csid(segments),
                    constants.SEG6_MODE_ENCAP_RED)
        except sid.InvalidLocator as e:
            LOG.warning("SRv6: route %(p)s: cannot compress %(s)s (%(e)s); "
                        "pushing full SIDs", {'p': prefix, 's': segments,
                                              'e': e})
            return segments, constants.SEG6_MODE_ENCAP_RED

    # ------------------------------------------------------------------
    # SID rules
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
        Depth counts what is pushed: in usid mode a steered route that fits
        one container is depth 1 and costs no more than a direct one.
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
        overhead = sid.encap_overhead(segment_count, reduced=self.usid)
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
