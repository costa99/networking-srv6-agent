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

# Per-domain function ids come from the low part of the field; topology SIDs
# (End, End.X) are reserved in the high part so a domain can never be
# allocated an id that collides with a node's own transit SID.
MIN_DOMAIN_FUNCTION_ID = 1
MAX_DOMAIN_FUNCTION_ID = 0x0fff
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

# The decapsulation behaviors a domain may choose. Exactly one today (see
# MIGRATION-PLAN.md 8.3): the API offers no choice it cannot honour. The
# attribute stays in the API anyway, so adding End.DT4 / End.DT6 later is a
# new value here rather than a new attribute.
BEHAVIOR_END_DT46 = 'End.DT46'
VALID_BEHAVIORS = [BEHAVIOR_END_DT46]
DEFAULT_BEHAVIOR = BEHAVIOR_END_DT46

# Cap on the transit nodes in one operator-defined TE path. Arithmetic, not
# taste: encap_overhead(6 transits + 1 terminal) = 48 + 16*7 = 160 bytes,
# which leaves 1340 tenant bytes on a 1500-byte underlay -- the point where
# this project's own "--mtu 1400" guidance stops being satisfiable.
MAX_TE_VIA_HOSTS = 6

# The metric of the `unreachable default` route that seals every domain VRF
# (MIGRATION-PLAN.md 8.6). Without it a lookup that misses in the VRF table
# falls through the l3mdev rule to `main`, where every domain's End.DT46 SID
# lives -- measured on node 1, evidence/vrf-fallthrough.txt: a tenant's
# gateway port resolved another tenant's SID and would have decapsulated
# into that tenant's VRF. The value is the one the kernel's
# Documentation/networking/vrf.rst uses: high enough that anything else in
# the table wins, so it only ever answers a miss.
VRF_UNREACHABLE_METRIC = 4278198272

# seg6 encapsulation overhead: outer IPv6 (40) + SRH fixed part (8) +
# 16 bytes per segment. With a single-segment list that is 64 bytes.
SRH_FIXED_OVERHEAD = 48
SRH_PER_SEGMENT = 16
