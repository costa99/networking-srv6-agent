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
        # The microSID Local ID Block, the only ids allowed under either
        # encoding, so switching dataplane_encoding needs no change here.
        default=['0xe000:0xefff'],
        help=_("Comma-separated list of <start>:<end> tuples enumerating the "
               "SID function ids available for allocation to SRv6 "
               "domains. Plays the role vni_ranges plays for VXLAN. Ids "
               "are pre-populated into a pool table and handed out at "
               "random. Values may be written in hex. Every range must lie "
               "inside the microSID Local ID Block 0xe000:0xefff (4096 "
               "ids), the default, with either dataplane_encoding.")),
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
               "block.")),
        ],
        help=_("How every node encodes a domain's segment lists. One value "
               "for the whole deployment, sent to the agents in each domain "
               "payload. Change it on a deployment with no domains: restart "
               "the server, then the agents.")),
    cfg.IntOpt(
        'vrf_table_base',
        default=0,
        help=_("The VRF routing table for a domain is vrf_table_base plus its "
               "function id. Derived, never allocated. With the default 0 "
               "the table is the function id itself (0xe000-0xefff, far "
               "from main=254 and local=255); raise it only if another "
               "agent on the node uses tables in that range.")),
    cfg.IntOpt(
        'te_liveness_interval',
        default=15,
        min=0,
        help=_("Seconds between checks for a TE waypoint whose node has "
               "died. A path through a node whose Open vSwitch agent is "
               "down is reported DEGRADED and its routes go direct until "
               "the agent is alive again. A node is judged dead by "
               "Neutron's own agent_down_time, so the failover time is "
               "about agent_down_time plus this interval; lower "
               "agent_down_time to fail over faster. 0 turns the check "
               "off: a dead waypoint then blackholes its paths until an "
               "admin re-steers them.")),
    cfg.StrOpt(
        'topology_file',
        help=_("Optional underlay topology, read once at server start. "
               "Lines 'node <name> <locator>' (fabric nodes and hosts; a "
               "host's name is its Neutron host name) and 'link <a> <b> "
               "<Mbit/s>' (capacity per direction). With it, fabric nodes "
               "may be named in via_hosts, fabric agents may post link "
               "state, and the TE re-optimisation steers heavy VM pairs "
               "off congested links.")),
    cfg.IntOpt(
        'link_state_stale_after',
        default=15,
        min=1,
        help=_("Seconds after which a link measurement posted to "
               "srv6_link_states is stale: a link with nothing newer is "
               "shown with stale=true, and is no longer used as a "
               "measurement. Keep it at about three periods of the fabric "
               "agents.")),
    cfg.IntOpt(
        'link_state_window',
        default=30,
        min=1,
        help=_("Seconds over which peak_mbps of a link is the highest "
               "measurement. Each batch a fabric node posts deletes that "
               "node's measurements older than twice this window. Keep it "
               "at least as long as several fabric agent periods.")),
    cfg.IntOpt(
        'te_reopt_interval',
        default=10,
        min=0,
        help=_("Seconds between passes of the TE re-optimisation, which "
               "steers heavy VM pairs off congested links, as the fabric "
               "nodes measure them (srv6_link_states). 0 turns it off. "
               "Needs topology_file and te_auto_steering.")),
    cfg.FloatOpt(
        'te_reopt_high',
        default=constants.TE_REOPT_HIGH_DEFAULT,
        help=_("A link whose measured load is above this share of its "
               "capacity for te_reopt_samples measurements in a row is "
               "congested. Two TCP elephants sharing a link rarely hold "
               "it above 0.9 for that long, so the default is lower.")),
    cfg.FloatOpt(
        'te_reopt_low',
        default=0.56,
        help=_("Moves off a congested link stop once its load, minus the "
               "VM pairs moved, is at most this share of its capacity. "
               "Below te_reopt_high (hysteresis).")),
    cfg.IntOpt(
        'te_reopt_samples',
        default=3,
        min=1,
        help=_("Measurements in a row above te_reopt_high that make a link "
               "congested. They must all be newer than the last move off "
               "the link, so a move is seen in the data before the next.")),
    cfg.IntOpt(
        'te_reopt_hold_down',
        default=60,
        min=0,
        help=_("Seconds during which a VM pair that was moved or released "
               "is not moved again by the re-optimisation.")),
    cfg.IntOpt(
        'te_reopt_max_moves',
        default=2,
        min=1,
        help=_("Automatic paths created, moved or released per "
               "re-optimisation pass at most.")),
    # --- U-f: steering heavy VM pairs (PLAN-UNRESERVED-STEERING)
    cfg.BoolOpt(
        'te_auto_steering',
        default=True,
        help=_("Let the TE re-optimisation steer heavy traffic: on a "
               "congested link, the largest VM pair the fabric nodes "
               "measured (srv6_aggregate_loads) gets a path of its own "
               "(origin auto) onto links with room; the path is released "
               "when the pair goes idle or "
               "the default routes have room again. Acts only with a "
               "topology_file and fabric agents that report VM pairs "
               "(--aggregates).")),
    cfg.FloatOpt(
        'te_auto_min_share',
        default=0.1,
        help=_("A VM pair is steered only if it carries at least this share "
               "of the congested link's capacity (the elephant "
               "threshold); a steered pair below it on its path is idle.")),
    cfg.FloatOpt(
        'te_auto_fit',
        default=0.7,
        min=0,
        max=1,
        help=_("What-if check for a steered VM pair: every link of its new "
               "path must stay at or below this share of its capacity with "
               "the pair's measured rate added. 1 means only 'fits' (the "
               "path computation's own check): "
               "a TCP pair's measured rate is its share of the congested "
               "link, not its demand, so a margin mostly refuses moves.")),
    cfg.IntOpt(
        'te_auto_release_after',
        default=120,
        min=0,
        help=_("Seconds an automatic path stays idle (its pair below "
               "te_auto_min_share) before it is released and its traffic "
               "goes back to the default routes.")),
    cfg.IntOpt(
        'te_auto_max_paths',
        default=64,
        min=0,
        help=_("Automatic paths at most, in the whole cloud.")),
    cfg.IntOpt(
        'te_auto_max_rules_per_host',
        default=16,
        min=0,
        help=_("Source VMs with an automatic path at most per node: each "
               "has a policy rule that every packet the node routes walks.")),
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
        help=_("Optional override of the interface the encapsulated "
               "packets leave by: the nexthop device of the seg6 encap "
               "routes and the interface on which seg6_enabled is "
               "asserted. Unset, the device is found per node from the "
               "route to its locator.")),
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


# Neutron's own group, where it looks up quota_<resource> for every
# registered quota resource; without this option srv6_domain fell back to
# default_quota, unlimited (audit 2026-11-02b finding 1).
QUOTAS_GROUP = 'QUOTAS'

quota_opts = [
    cfg.IntOpt(
        'quota_srv6_domain',
        default=10,
        help=_("Number of SRv6 domains allowed per project. Every domain "
               "takes a SID function id from a pool all projects share "
               "(function_id_ranges, 4096 ids at most), so without a limit "
               "one project could take them all. A negative value means "
               "unlimited.")),
]


def register_server_opts(conf=cfg.CONF):
    conf.register_opts(srv6_server_opts, SRV6_SERVER_GROUP)
    conf.register_opts(quota_opts, QUOTAS_GROUP)


def register_agent_opts(conf=cfg.CONF):
    conf.register_opts(srv6_agent_opts, SRV6_AGENT_GROUP)


def list_opts():
    """Entry point for oslo-config-generator."""
    return [(SRV6_SERVER_GROUP, srv6_server_opts),
            (SRV6_AGENT_GROUP, srv6_agent_opts),
            (QUOTAS_GROUP.lower(), quota_opts)]
