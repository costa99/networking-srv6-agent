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

"""Privileged helpers for the operations neutron's ip_lib cannot express.

This module lives inside the privileged package rather than beside the
dataplane that calls it because oslo.privsep requires an entrypoint to be
below its context's module prefix -- placing it beside the dataplane raises
"entrypoints must be below networking_srv6_agent.privileged" at import
time.

Everything else the dataplane needs -- VRF creation, address assignment,
plain routes -- goes through neutron.agent.linux.ip_lib. Only these two need
their own entry points, and they run under this package's own privsep
context (see networking_srv6_agent.privileged) because oslo.privsep
refuses to place an entrypoint outside its context's module prefix:

  set_link_master   enslave an interface to a VRF. ip_lib has no wrapper.

  replace_seg6_route / replace_seg6local_route
                    seg6 and seg6local encapsulations. pyroute2 can encode
                    them, but whether the *installed* pyroute2 encodes
                    `End.DT46 vrftable` correctly is version-dependent
                    (risk R1 in the plan), so these shell out to ip(8),
                    which is the same interface the manual dataplane proof
                    used and therefore known to produce the exact state
                    that was verified on the wire.
"""

from oslo_concurrency import processutils
from oslo_log import log as logging
import pyroute2

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import constants
from networking_srv6_agent import privileged


LOG = logging.getLogger(__name__)


@privileged.default.entrypoint
def set_link_master(device, master):
    """Enslave `device` to VRF `master`.

    Ordering note for callers: enslave BEFORE assigning an address. An
    address added first lands in the main routing table and does NOT move
    when the interface is later enslaved, which produces a VRF that looks
    correct in `ip link` but has no connected route in its own table.
    """
    with pyroute2.IPRoute() as ipr:
        dev_idx = ipr.link_lookup(ifname=device)
        master_idx = ipr.link_lookup(ifname=master)
        if not dev_idx:
            raise RuntimeError(_("interface %s not found") % device)
        if not master_idx:
            raise RuntimeError(_("VRF %s not found") % master)
        ipr.link('set', index=dev_idx[0], master=master_idx[0])


@privileged.default.entrypoint
def ip_route_cmd(args):
    """Run `ip` with the given argument list and return stdout.

    Kept deliberately narrow: the caller supplies an argument *list*, never
    a shell string, so nothing here can be turned into shell injection by a
    hostile network name or address.
    """
    cmd = ['ip'] + list(args)
    out, err = processutils.execute(*cmd, check_exit_code=True)
    if err:
        LOG.debug("ip %s: %s", ' '.join(args), err.strip())
    return out


@privileged.default.entrypoint
def modprobe_vrf():
    """Load vrf.ko.

    Needed before net.vrf.strict_mode can be written at all: the sysctl
    does not exist until the module is loaded, and strict mode must be on
    before the first VRF is created.
    """
    processutils.execute('modprobe', 'vrf', check_exit_code=False)


def replace_seg6_route(prefix, segments, dev, table, family_v6=False):
    """Install an encapsulation route for a remote prefix.

    Produces, for a one-element segment list:
        ip route replace <prefix> encap seg6 mode encap segs <sid> \
            dev <dev> table <table>
    """
    args = ['-6'] if family_v6 else []
    args += ['route', 'replace', prefix,
             'encap', 'seg6', 'mode', 'encap',
             'segs', ','.join(segments),
             'dev', dev, 'table', str(table)]
    return ip_route_cmd(args)


def delete_route(prefix, table, family_v6=False):
    """Remove a route from a VRF table, tolerating its absence."""
    args = ['-6'] if family_v6 else []
    args += ['route', 'del', prefix, 'table', str(table)]
    try:
        return ip_route_cmd(args)
    except processutils.ProcessExecutionError as e:
        if 'No such process' in str(e) or 'Cannot find' in str(e):
            return None
        raise


def replace_seg6local_route(sid_address, action, dev, vrf_table=None):
    """Install a local SRv6 behaviour on one of this node's SIDs.

        End.DT46:  ip -6 route replace <sid>/128 encap seg6local \
                       action End.DT46 vrftable <table> dev <vrf>
        End:       ip -6 route replace <sid>/128 encap seg6local \
                       action End dev <dev>

    vrf_table is omitted rather than defaulted for behaviours that take
    none: iproute2 rejects `action End vrftable <n>` outright, so a caller
    that wrongly supplies one gets a hard failure instead of a subtly wrong
    route. `dev` comes first because every behaviour needs it.

    The SID is never also configured as an *address*: seg6 encapsulation
    picks its outer source by ordinary route lookup, so a SID need only be
    receivable -- which the locator's `local <loc> ... table main` route
    already achieves. Adding it with `ip addr add` would place it in the
    local table, which is consulted at rule priority 0, ahead of main, and
    would shadow this very route so the behaviour never runs.
    """
    args = ['-6', 'route', 'replace', '%s/128' % sid_address,
            'encap', 'seg6local', 'action', action]
    if vrf_table is not None:
        args += ['vrftable', str(vrf_table)]
    args += ['dev', dev]
    return ip_route_cmd(args)


def replace_locator_route(locator):
    """Make every address inside the locator locally deliverable.

        ip -6 route replace local <locator> dev lo table main

    `table main` is load-bearing. Without it the route defaults into the
    *local* table, which ip rule consults at priority 0, ahead of main at
    32766 -- so a local-table entry covering the locator shadows every
    per-SID route wholesale and no seg6local behaviour ever runs. Rule
    priority beats prefix length across tables.
    """
    return ip_route_cmd(['-6', 'route', 'replace', 'local', locator,
                         'dev', 'lo', 'table', 'main'])


def list_seg6_routes(table, family_v6=False):
    """The encap routes a VPN's table currently holds, as prefixes.

    Reads the kernel rather than the agent's own bookkeeping, for the same
    reason the gateway ports are read off the bridge: the routes outlive
    the agent process, so anything that survived a restart is invisible to
    in-memory state and can never be collected.

    seg6local routes are excluded by the `seg6local` test, not by the
    table: those are this node's own SIDs and live in main, but the same
    substring appears in both encapsulations and the naive test matches
    each of them.
    """
    args = ['-6'] if family_v6 else []
    args += ['route', 'show', 'table', str(table)]
    try:
        out = ip_route_cmd(args)
    except processutils.ProcessExecutionError:
        return []
    prefixes = []
    for line in out.splitlines():
        if 'encap seg6 ' not in line or 'seg6local' in line:
            continue
        fields = line.split()
        if fields:
            prefixes.append(fields[0])
    return prefixes


def replace_unreachable_default(table, family_v6=False):
    """Seal a VRF table: a lookup that misses ends here, not in `main`.

        ip [-6] route replace unreachable default metric 4278198272 \
            table <table>

    Without it the l3mdev rule (pref 1000) lets a miss continue to `main`
    (pref 32766), which holds every domain's End.DT46 SID: a tenant's VM
    could address another domain's SID and be decapsulated into that VRF
    (MIGRATION-PLAN.md 8.6). It carries no `encap seg6`, so
    list_seg6_routes never returns it and route reconciliation never
    deletes it.
    """
    args = ['-6'] if family_v6 else []
    args += ['route', 'replace', 'unreachable', 'default',
             'metric', str(constants.VRF_UNREACHABLE_METRIC),
             'table', str(table)]
    return ip_route_cmd(args)


def flush_table(table, family_v6=False):
    """Empty a VRF's table, tolerating one that is already empty.

    Deleting the VRF device does not do this. The unreachable default has
    no device at all, and the encap routes point at the underlay rather
    than the VRF, so both outlive the device -- and a domain later handed
    the same function id would inherit them.
    """
    args = ['-6'] if family_v6 else []
    args += ['route', 'flush', 'table', str(table)]
    try:
        return ip_route_cmd(args)
    except processutils.ProcessExecutionError as e:
        LOG.debug("flushing table %(t)s: %(e)s", {'t': table, 'e': e})
        return None


def list_vrf_names():
    """The name of every VRF device on the node.

    Parses `ip -o link show type vrf`, whose lines look like
        56: sv6vrf-3579: <NOARP,MASTER,UP,LOWER_UP> mtu 65575 ...
    Raises on failure: the caller garbage-collects from this list, and an
    unreadable list must never read as "no VRFs".
    """
    out = ip_route_cmd(['-o', 'link', 'show', 'type', 'vrf'])
    names = []
    for line in out.splitlines():
        fields = line.split(':')
        if len(fields) > 1 and fields[1].strip():
            names.append(fields[1].strip().split('@')[0])
    return names


def route_exists(prefix, table, family_v6=False):
    args = ['-6'] if family_v6 else []
    args += ['route', 'show', prefix, 'table', str(table)]
    try:
        return bool(ip_route_cmd(args).strip())
    except processutils.ProcessExecutionError:
        return False
