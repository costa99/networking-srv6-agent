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

from oslo_config import cfg

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import constants


SRV6_SERVER_GROUP = 'srv6'
SRV6_AGENT_GROUP = 'srv6_agent'

srv6_server_opts = [
    cfg.ListOpt(
        'function_id_ranges',
        # The microSID Local ID Block, valid under both encodings: one
        # default, so switching dataplane_encoding needs no change here.
        default=['0xe000:0xefff'],
        help=_("Comma-separated list of <start>:<end> tuples enumerating the "
               "SID function ids available for allocation to SRv6 "
               "domains. Plays the role vni_ranges plays for VXLAN. Ids "
               "are pre-populated "
               "into a pool table at startup and handed out round-robin. "
               "Must not overlap 0xf000-0xffff, which is reserved for "
               "per-node topology SIDs. Values may be written in hex. The "
               "default, 0xe000:0xefff, is the microSID Local ID Block "
               "(4096 ids) and works with either dataplane_encoding. With "
               "usid every range must lie inside it; with encap any range "
               "from 1 to 0xefff may be used.")),
    cfg.StrOpt(
        'dataplane_encoding',
        default=constants.DEFAULT_ENCODING,
        choices=[
            (constants.ENCODING_ENCAP,
             _("Full 128-bit SIDs, H.Encaps: outer IPv6 header plus an SRH "
               "holding every SID. 64 bytes for a direct path, 16 more per "
               "TE waypoint.")),
            (constants.ENCODING_USID,
             _("RFC 9800 NEXT-C-SID (microSID), F3216, pushed with "
               "H.Encaps.Red: the path is packed into C-SID containers, "
               "and one that fits in a single container (up to four TE "
               "waypoints) costs 40 bytes with no SRH. Needs kernel >= 6.1 "
               "and iproute2 >= 6.1 on every node, /48 locators in one /32 "
               "block, and function_id_ranges inside 0xe000-0xefff.")),
        ],
        help=_("How every node encodes a domain's segment lists. One value "
               "for the whole deployment, sent to the agents in each domain "
               "payload. Change it on a deployment with no domains: restart "
               "the server, then the agents.")),
    cfg.IntOpt(
        'vrf_table_base',
        default=10000,
        help=_("The VRF routing table for a domain is vrf_table_base plus its "
               "function id. Derived, never allocated. Must be high enough "
               "to avoid the well-known tables (main=254, local=255) and any "
               "table another agent on the node uses.")),
]

srv6_agent_opts = [
    cfg.StrOpt(
        'locator',
        help=_("This node's SRv6 locator, as an IPv6 prefix, e.g. "
               "fc00:0:1::/48. MUST be unique per node and reachable from "
               "every other node over the IPv6 underlay. The agent refuses "
               "to start without it.")),
    cfg.StrOpt(
        'underlay_interface',
        help=_("Interface the encapsulated packets leave by. Used as the "
               "nexthop device for seg6 encap routes and as the interface "
               "on which seg6_enabled is asserted.")),
    cfg.IntOpt(
        'resync_interval',
        default=300,
        help=_("Seconds between full resynchronisations with the server. A "
               "resync is idempotent and also garbage-collects VRFs for "
               "domains this node no longer participates in.")),
    cfg.BoolOpt(
        'apply_sysctls',
        default=True,
        help=_("Whether the agent sets the kernel sysctls SRv6 requires "
               "(seg6_enabled, forwarding, vrf strict_mode, rp_filter). Set "
               "False if the node's sysctls are managed by configuration "
               "management, in which case the agent only verifies them and "
               "logs an error when one is wrong.")),
]


def register_server_opts(conf=cfg.CONF):
    conf.register_opts(srv6_server_opts, SRV6_SERVER_GROUP)


def register_agent_opts(conf=cfg.CONF):
    conf.register_opts(srv6_agent_opts, SRV6_AGENT_GROUP)


def list_opts():
    """Entry point for oslo-config-generator."""
    return [(SRV6_SERVER_GROUP, srv6_server_opts),
            (SRV6_AGENT_GROUP, srv6_agent_opts)]
