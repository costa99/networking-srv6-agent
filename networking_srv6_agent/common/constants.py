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

# RPC topic namespace. Kept distinct from every BGPVPN driver's so this
# plugin and networking-bgpvpn can run in one deployment without their
# fanouts colliding -- which is the whole point of this package existing.
TOPIC_SRV6 = 'srv6'

# The agent->server topic. This MUST NOT be neutron's topics.PLUGIN: a
# consumer registered there joins the round-robin for neutron's own core
# plugin RPC and starts receiving calls such as get_network_details, which
# it answers with UnsupportedVersion -- breaking the OVS agent for whichever
# requests happen to land on it. Observed, not theoretical.
TOPIC_SRV6_PLUGIN = 'srv6-plugin'

# Server -> agent fanout methods.
DOMAIN_UPDATED = 'domain_updated'
DOMAIN_DELETED = 'domain_deleted'
ROUTES_UPDATED = 'routes_updated'

# Agent -> server call.
SYNC_STATE = 'sync_state'

# Every RPC payload carries this. An agent that receives a version it does
# not understand logs and ignores rather than half-applying a message whose
# shape it cannot rely on.
PAYLOAD_VERSION = 1

# Naming. Both are bounded by IFNAMSIZ (15 chars + NUL), which is why the
# function id rather than the domain UUID appears in them.
#
# These strings are deliberately UNCHANGED from the networking-bgpvpn build.
# The migration's acceptance gate is that the two-node lifecycle produces the
# same kernel objects as before, compared against the recorded evidence -- and
# that comparison is only literal if the names match. Nothing collides:
# networking-bgpvpn's own drivers name their objects differently.
VRF_PREFIX = 'sv6vrf-'          # sv6vrf-<function_id>
GW_PORT_PREFIX = 'svp-'         # svp-<function_id>-<local_vlan>

# The function id field is 16 bits wide, immediately after the locator.
FUNCTION_BITS = 16

# Per-domain function ids are the microSID Local ID Block under both
# encodings, so switching encoding never strands a domain. They lie below the
# topology SIDs (End, End.X), which are reserved in the high part so a domain
# can never be allocated an id that collides with a node's own transit SID.
# parse_function_id_ranges refuses any range outside MIN..MAX.
MIN_DOMAIN_FUNCTION_ID = 0xe000
MAX_DOMAIN_FUNCTION_ID = 0xefff
MIN_TOPOLOGY_FUNCTION_ID = 0xf000
MAX_TOPOLOGY_FUNCTION_ID = 0xffff

# This node's own plain transit SID, used for traffic engineering: another
# node prepends <our locator><this id> to a segment list to route a packet
# through us on its way somewhere else.
#
# A CONSTANT, not an allocation, for three reasons. The locator already
# makes the address unique -- it is per-node and the agent refuses to start
# without one -- so allocation would buy uniqueness that already exists. It
# is what makes the SID *computable by the encapsulating node*: with an
# allocated id, node1 would have to learn node2's End id over the wire,
# which is exactly the symbolic-to-numeric resolution the payload design
# avoids. And it has no lifecycle: nothing observes "node removed from the
# cloud", so an allocated id would never be released.
#
# It sits at the TOP of the reserved range, leaving 0xf000-0xffef free for a
# future allocated End.X pool (link-level adjacency SIDs, which genuinely do
# need one id per adjacency).
NODE_END_FUNCTION_ID = 0xfff0

# The transit behavior: decrement segments_left, copy the next segment into
# the outer destination address, and forward by an ordinary route lookup.
#
# Deliberately NOT a member of VALID_BEHAVIORS below: that list is the set of
# *decapsulation* behaviors a domain may choose, and a domain whose
# srv6_behavior was 'End' would be nonsense. End is node state, installed
# once per node by the agent, never selected through the API.
BEHAVIOR_END = 'End'

# The decapsulation behaviors a domain may choose. Exactly one today: the
# API offers no choice it cannot honour. The attribute stays in the API
# anyway, so adding End.DT4 / End.DT6 later is a new value here rather than
# a new attribute.
BEHAVIOR_END_DT46 = 'End.DT46'
VALID_BEHAVIORS = [BEHAVIOR_END_DT46]
DEFAULT_BEHAVIOR = BEHAVIOR_END_DT46

# Cap on the transit nodes in one operator-defined TE path. A chosen bound,
# not a derived one: it keeps the encap overhead with full SIDs at
# 48 + 16*(6 transits + 1 terminal) = 160 bytes, which leaves 1340 tenant
# bytes on a 1500-byte underlay. The agent only warns when a route's tenant
# MTU plus its overhead exceeds the underlay MTU (dataplane.check_mtu); with
# the --mtu 1400 guidance on a 1500-byte underlay that happens above 2
# transits.
MAX_TE_VIA_HOSTS = 6
# (In usid mode the cap no longer binds on overhead -- six vias plus the
# decap pair is two containers, 64 bytes -- but `encap` still exists and the
# API stays the same in both.)

# The metric of the `unreachable default` route that seals every domain VRF.
# Without it a lookup that misses in the VRF table
# falls through the l3mdev rule to `main`, where every domain's End.DT46 SID
# lives -- measured on node 1, evidence/vrf-fallthrough.txt: a tenant's
# gateway port resolved another tenant's SID and would have decapsulated
# into that tenant's VRF. The value is the one the kernel's
# Documentation/networking/vrf.rst uses: high enough that anything else in
# the table wins, so it only ever answers a miss.
VRF_UNREACHABLE_METRIC = 4278198272

# The priority of the policy rules that let a domain's VRF reach its OWN
# remote decap SIDs through `main` -- just ahead of the l3mdev rule (1000).
# Needed because the seal above also caught the domain's own traffic:
# after seg6 encapsulates a packet that arrived on a gateway port, the
# kernel routes the new OUTER IPv6 header in the same VRF, hit the IPv6
# unreachable default, and dropped every packet (at the two-node
# gate, Ip6InNoRoutes one per echo request). One rule per (domain, remote
# node):
#     ip -6 rule add pref 999 iif sv6vrf-<fid> to <remote sid>/128 lookup main
# `iif <vrf master>` matches traffic arriving on the VRF's enslaved ports,
# so another domain's VRF gains nothing, and `lookup main` leaves the next
# hop to the underlay however many hops away it is.
SID_RULE_PRIORITY = 999

# Where the `local` FIB rule goes (F7). The kernel puts it at 0, ahead of
# the l3mdev rule at 1000, so a lookup from inside a VRF found every host
# address first: t9 saw a tenant ping the management address and deliver a
# forged ICMPv6 to the underlay address. After l3mdev, a VRF's lookup ends
# at its own table (sealed, so it never falls through), and everything else
# still finds `local` before `main` -- the order Documentation/networking/
# vrf.rst recommends:
#     ip [-6] rule add pref 32765 lookup local; ip [-6] rule del pref 0
LOCAL_RULE_PRIORITY = 32765

# br-int table-0 priority of the flows that keep a gateway's ARP and ND on
# its own node (F12): above the OVS agent's own table-0 flows for patch
# ports (single digits, a few more with DVR), below its dead-VLAN drop
# (65535).
GATEWAY_ND_FLOW_PRIORITY = 100

# The nftables table of the SRv6 edge filter: on the
# gateway ports, drop tenant packets addressed into the SID space or carrying
# any SRH at all. Its own table in the inet family, so neutron's iptables-nft
# tables (ip/ip6 filter|nat|mangle) are never touched, and replacing it
# wholesale on every resync cannot disturb them.
EDGE_FILTER_TABLE = 'srv6_edge'

# seg6 encapsulation overhead: outer IPv6 (40) + SRH fixed part (8) +
# 16 bytes per segment. With a single-segment list that is 64 bytes.
SRH_FIXED_OVERHEAD = 48
SRH_PER_SEGMENT = 16
# The two parts separately, for reduced encapsulation (RFC 8986 s5.2): the
# first SID travels only in the outer destination address, and a list of
# one pushes no SRH at all -- 40 bytes.
IPV6_HEADER = 40
SRH_HEADER = 8

# How the dataplane encodes a segment list ([srv6] dataplane_encoding).
# One deployment-wide value, carried to every agent in the domain payload
# (USID-PLAN.md D3).
#
#   encap  full 128-bit SIDs, H.Encaps: outer IPv6 + SRH. What every build
#          before this one did, so it stays the default.
#   usid   RFC 9800 NEXT-C-SID ("microSID") in the F3216 format, pushed
#          with H.Encaps.Red: the whole path is packed into C-SID
#          containers, and a path that fits in one carries no SRH.
ENCODING_ENCAP = 'encap'
ENCODING_USID = 'usid'
VALID_ENCODINGS = [ENCODING_ENCAP, ENCODING_USID]
DEFAULT_ENCODING = ENCODING_ENCAP

# The `ip route ... encap seg6 mode <x>` each encoding pushes with.
SEG6_MODE_ENCAP = 'encap'
SEG6_MODE_ENCAP_RED = 'encap.red'

# F3216 (RFC 9800 s4.1, the one format an implementation MUST support): a
# 32-bit Locator-Block followed by 16-bit C-SIDs, six to a container. The
# testbed's /48 locators already have this shape -- block fc00:0::/32 plus
# a 16-bit node C-SID -- and a domain SID <locator><function> is already
# block:node:function, which is what a uDT46 is.
USID_BLOCK_BITS = 32
USID_CSID_BITS = 16
USID_LOCATOR_PREFIXLEN = USID_BLOCK_BITS + USID_CSID_BITS   # /48
# RFC 9800 s5.3: a function SID is installed at block+node+function, /64.
USID_FUNCTION_PREFIXLEN = USID_LOCATOR_PREFIXLEN + USID_CSID_BITS

# The C-SID space under the block is split (RFC 9800 s5, s9.2) into a
# Global ID Block of node ids and a Local ID Block of node-local functions.
# The split is Cisco's for F3216, the widely deployed reference. It has to be
# disjoint: under a uN, a function id that is also a node id is read as "the
# next node", so a packet for a deleted domain's SID would be shifted and
# forwarded to that node instead of dropped (USID-PLAN.md D2). 0 is reserved
# (end of container).
USID_GIB_MIN = 0x0001
USID_GIB_MAX = 0xdfff
# The domain function ids. 4096 of them and a single /52 under each node --
# so one unreachable route drops every LIB function that has no decap route.
USID_LIB_MIN = MIN_DOMAIN_FUNCTION_ID
USID_LIB_MAX = MAX_DOMAIN_FUNCTION_ID
USID_LIB_PREFIXLEN = USID_LOCATOR_PREFIXLEN + 4                 # /52

# What an agent reports in sync_state that its kernel and iproute2 can do,
# so the server can name a node that cannot honour dataplane_encoding.
CAPABILITY_ENCAP_RED = 'encap-red'
CAPABILITY_NEXT_CSID = 'next-csid'
ENCODING_REQUIRES = {
    ENCODING_ENCAP: (),
    ENCODING_USID: (CAPABILITY_ENCAP_RED, CAPABILITY_NEXT_CSID),
}

# The routing table the capability probe writes its throwaway routes into.
# No rule ever points at it, so nothing there can carry traffic, and it is
# far above every VRF table (vrf_table_base + a 16-bit function id).
PROBE_TABLE = 4000000000
