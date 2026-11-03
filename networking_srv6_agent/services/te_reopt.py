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

"""Move heavy VM pairs off congested links (PLAN-UNRESERVED-STEERING U-f).

Each pass reads the measured link state (srv6_link_states) and:

1. finds the congested links: the last te_reopt_samples measurements of the
   link all above te_reopt_high of its capacity, and all newer
   than the last move off it (a move must show in the data before the link
   is judged again);
2. releases (U-f, step 7) the automatic paths no longer needed: a pair idle
   for te_auto_release_after, or one whose default routes would now all
   stay at or below te_reopt_low with its rate added (the worst case: ECMP
   may hash it onto any of them); never inside the hold-down;
3. on the hottest link first, moves (U-f, step 6) the largest VM pairs the
   fabric measured on it (srv6_aggregate_loads): a pair of at least
   te_auto_min_share of the link, that no user path covers, gets a path of
   its own (origin auto) or has its auto path moved;
   a link that carries one pair only is left (moving it only moves the
   congestion, Chang & Wang's "more than one flow"). The new path must
   pass the what-if check: every link of it at or below te_auto_fit after
   adding the pair (nobody there is pushed towards congestion);

until its load minus what was moved is at most te_reopt_low (hysteresis),
te_reopt_max_moves moves were made in this pass (budget, shared by moves
and releases), skipping anything moved within te_reopt_hold_down
(damping). A path moves onto a path with room where every link holds its
measured load (L4) minus the mover's own traffic, avoiding every congested
and dead link; in place, make-before-break. No room: it stays.

The memory of moves (hold-down, last move per link, idle since) is this
process's: one worker, like the liveness monitor; a restart forgets it,
which at worst allows one early move or release.
"""

import datetime

from neutron_lib import context as n_context
from oslo_log import log as logging
from oslo_utils import timeutils

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.db import te_db
from networking_srv6_agent.services import pce

LOG = logging.getLogger(__name__)


def _pairs(view):
    """{(source port, destination port): pair} of the resolved entries.

    pair: {'entry': one of its entries (domain, ports, hosts), 'links':
    {link: Mbit/s on it}, 'rate': what reaches the destination host (its
    leaf>host links carry all of it, however ECMP split it before)}.
    """
    pairs = {}
    for e in view:
        if not (e.get('domain_id') and e.get('source_port_id') and
                e.get('destination_port_id') and e.get('source_host') and
                e.get('destination_host')):
            continue
        key = (e['source_port_id'], e['destination_port_id'])
        pair = pairs.setdefault(key, {'entry': e, 'links': {}, 'rate': 0.0})
        link = (e['from_node'], e['to_node'])
        pair['links'][link] = pair['links'].get(link, 0.0) + e['mbps']
        if e['to_node'] == e['destination_host']:
            pair['rate'] += e['mbps']
    for pair in pairs.values():
        if not pair['rate']:             # no leaf>host sample: its largest
            pair['rate'] = max(pair['links'].values())
    return pairs


def _covered(pair, paths):
    """Whether a user path decides this pair's route.

    Any rank: a path of the pair's domain to its destination port or node,
    from its source port, its source node, or any source. An auto path is
    never created over one (A11).
    """
    e = pair['entry']
    for p in paths:
        if p.get(te_def.ORIGIN) == te_def.ORIGIN_AUTO or \
                p['domain_id'] != e['domain_id']:
            continue
        if not (p[te_def.DESTINATION_PORT_ID] == e['destination_port_id'] or
                p[te_def.DESTINATION_HOST] == e['destination_host']):
            continue
        if p.get(te_def.SOURCE_PORT_ID):
            if p[te_def.SOURCE_PORT_ID] == e['source_port_id']:
                return True
        elif p[te_def.SOURCE_HOST] in (None, e['source_host']):
            return True
    return False


class TeReoptimiser:
    """check() is the periodic function."""

    def __init__(self, plugin):
        self._plugin = plugin
        self._moved = {}          # pair -> its last move
        self._link_moved = {}     # (a, b) -> time of the last move off it
        self._idle_since = {}     # auto path id -> first idle pass
        # pair -> (when, {link: Mbit/s}) it was just placed on: not in the
        # measurements yet, so counted by every later what-if until the
        # peak window has seen it (eval 2026-11-02: three pairs piled onto
        # one spine because each check read the fabric before the others)
        self._pending = {}

    def check(self):
        """One pass. Never raises: a periodic function that raises is gone."""
        try:
            self._check(n_context.get_admin_context())
        except Exception:
            LOG.exception("SRv6 TE re-optimisation failed; trying again at "
                          "the next interval")

    def _congested(self, ctx, hist):
        """{(a, b): latest Mbit/s} of the links congested now."""
        plugin, conf = self._plugin, self._plugin.conf
        out = {}
        for link, series in plugin._load_series(ctx, hist).items():
            last = series[-conf.te_reopt_samples:]
            cap = plugin.capacity.get(link)
            since = self._link_moved.get(link)
            if (cap and len(last) == conf.te_reopt_samples and
                    all(m is not None and m > conf.te_reopt_high * cap
                        for _t, m in last) and
                    (since is None or last[0][0] > since)):
                out[link] = last[-1][1]
        return out

    def _check(self, ctx):
        plugin, conf = self._plugin, self._plugin.conf
        if not plugin.capacity or not conf.te_auto_steering:
            return
        silent, outage = plugin._silent_fabric(ctx)
        if outage:
            LOG.error("SRv6 TE: no fabric node has posted link state for "
                      "%ss: treated as an outage of the collection, not of "
                      "the fabric; nothing is steered",
                      conf.link_state_stale_after)
            return
        # the link-state history once per pass, for every caller below: it
        # was read 3 times idle and once more per candidate move, most of
        # the pass's CPU (PLAN-C4-OPTIMISATION O1)
        hist = plugin._history(ctx)
        view = plugin._link_state_view(ctx, hist)
        congested = self._congested(ctx, hist)
        now = timeutils.utcnow()
        hold = now - datetime.timedelta(seconds=conf.te_reopt_hold_down)
        # an entry at or before `hold` no longer holds anything down: drop
        # it, or every pair ever moved stays here (audit 2026-11-02 6)
        self._moved = {k: t for k, t in self._moved.items() if t > hold}
        seen = now - datetime.timedelta(seconds=conf.link_state_window)
        self._pending = {k: v for k, v in self._pending.items()
                         if v[0] > seen}
        dead = plugin._dead_links(ctx, silent, view)
        measured = plugin._measured_load(ctx, view)
        pairs = _pairs(plugin._aggregate_view(ctx))
        moves = self._release(ctx, pairs, congested, dead, measured, now,
                              hold)
        if not congested:
            return
        cap = {link: mbps for link, mbps in plugin.capacity.items()
               if link not in dead and link not in congested}
        paths = te_db.get_all_te_paths(ctx)
        moved = {}      # pair -> the pair, moved earlier in this pass
        for link in sorted(congested, key=lambda k: (
                -congested[k] / plugin.capacity[k], k)):    # hottest first
            if moves >= conf.te_reopt_max_moves:
                break
            # a pair moved off an earlier link has left this one too: two
            # elephants sharing two links were both moved, one per link
            # (PLAN-C4-OPTIMISATION X3)
            load = congested[link] - sum(pr['links'].get(link, 0.0)
                                         for pr in moved.values())
            limit = conf.te_reopt_low * plugin.capacity[link]
            if load <= limit:
                continue
            steered, load = self._steer_pairs(
                ctx, link, load, limit, pairs, paths, cap, measured, now,
                hold, conf.te_reopt_max_moves - moves, moved)
            moves += steered
            if not steered:
                LOG.info("SRv6 TE re-optimisation: %(l)s carries %(m).1f "
                         "Mbit/s with no VM pair to steer",
                         {'l': '%s>%s' % link, 'm': load})

    # ------------------------------------------------------------------
    # U-f: VM pairs (PLAN-UNRESERVED-STEERING steps 6-7)
    # ------------------------------------------------------------------
    def _used(self, measured, own, key=None):
        """{link: Mbit/s held}: the measured load, own traffic off.

        measured: the pass's _measured_load. own: {link: Mbit/s} of the
        pair being placed, as the fabric measured it (L10). Plus what the
        other pairs placed in
        the last link_state_window add to their new links (_pending): the
        fabric has not shown it yet. Counting it twice for a while once it
        shows only makes the check stricter, never over-commits a link.
        """
        used = {link: max(0, mbps - own.get(link, 0))
                for link, mbps in measured.items()}
        for other, (_when, links) in self._pending.items():
            if other != key:
                for link, mbps in links.items():
                    used[link] = used.get(link, 0) + mbps
        return used

    def _steer_pairs(self, ctx, link, load, limit, pairs, paths, cap,
                     measured, now, hold, budget, moved):
        """Move the largest pairs off one link. (moves, load).

        moved: {pair key: pair} of this pass, updated with every move.
        """
        plugin, conf = self._plugin, self._plugin.conf
        on_link = [pr for pr in pairs.values() if link in pr['links']]
        if len(on_link) <= 1:
            if on_link:
                LOG.info("SRv6 TE re-optimisation: %(l)s carries one VM pair "
                         "only; moving it would move the congestion",
                         {'l': '%s>%s' % link})
            return 0, load
        floor = conf.te_auto_min_share * plugin.capacity[link]
        autos = {(p[te_def.SOURCE_PORT_ID], p[te_def.DESTINATION_PORT_ID]): p
                 for p in paths if p.get(te_def.ORIGIN) == te_def.ORIGIN_AUTO}
        host_of = {pr['entry']['source_port_id']: pr['entry']['source_host']
                   for pr in pairs.values()}
        candidates = sorted(
            (pr for pr in on_link if pr['links'][link] >= floor and
             not _covered(pr, paths)),
            key=lambda pr: (-pr['rate'], pr['entry']['source_port_id'],
                            pr['entry']['destination_port_id']))
        moves = 0
        for pr in candidates:
            if moves >= budget or load <= limit:
                break
            e = pr['entry']
            key = (e['source_port_id'], e['destination_port_id'])
            if self._moved.get(key, hold) > hold:
                continue                                    # hold-down
            existing = autos.get(key)
            if existing is None and not self._room_for_another(
                    e, autos, host_of):
                continue
            used = self._used(measured, pr['links'], key)
            path = pce.compute(cap, used, e['source_host'],
                               e['destination_host'], pr['rate'])
            if path is None or not all(
                    used.get(hop, 0) + pr['rate'] <=
                    conf.te_auto_fit * plugin.capacity[hop]
                    for hop in pce.links_of(path)):
                LOG.info("SRv6 TE re-optimisation: no path with room for VM "
                         "pair %(s)s>%(d)s (%(r).1f Mbit/s) off %(l)s; it "
                         "stays", {'s': e['source_port_id'],
                                   'd': e['destination_port_id'],
                                   'r': pr['rate'], 'l': '%s>%s' % link})
                continue
            placed = plugin._place_auto_path(ctx, e, path, existing)
            if placed is None:
                continue
            autos[key] = placed
            moved[key] = pr
            self._pending[key] = (now, {hop: pr['rate']
                                        for hop in pce.links_of(path)})
            moves += 1
            load -= pr['links'][link]
            self._moved[key] = now
            self._idle_since.pop(placed['id'], None)
            for hop in pr['links']:
                self._link_moved[hop] = now
            LOG.warning("SRv6 TE re-optimisation: %(l)s congested (%(m).1f "
                        "Mbit/s): VM pair %(s)s>%(d)s (%(r).1f Mbit/s, "
                        "%(sh)s>%(dh)s) %(how)s %(new)s",
                        {'l': '%s>%s' % link, 'm': load + pr['links'][link],
                         's': e['source_port_id'],
                         'd': e['destination_port_id'], 'r': pr['rate'],
                         'sh': e['source_host'], 'dh': e['destination_host'],
                         'how': 'moved to' if existing else
                         'steered onto an automatic path',
                         'new': '-'.join(path)})
        return moves, load

    def _room_for_another(self, e, autos, host_of):
        """The caps (A9): auto paths in all, steered VMs per node."""
        conf = self._plugin.conf
        if len(autos) >= conf.te_auto_max_paths:
            LOG.info("SRv6 TE re-optimisation: te_auto_max_paths (%d) "
                     "reached; not steering %s", conf.te_auto_max_paths,
                     e['source_port_id'])
            return False
        steered = {sp for sp, _dp in autos
                   if host_of.get(sp) == e['source_host']}
        if e['source_port_id'] not in steered and \
                len(steered) >= conf.te_auto_max_rules_per_host:
            LOG.info("SRv6 TE re-optimisation: %(h)s already steers %(n)d "
                     "VMs (te_auto_max_rules_per_host); not steering %(p)s",
                     {'h': e['source_host'], 'n': len(steered),
                      'p': e['source_port_id']})
            return False
        return True

    def _release(self, ctx, pairs, congested, dead, measured, now, hold):
        """Release the auto paths no longer needed (A5). Returns releases."""
        plugin, conf = self._plugin, self._plugin.conf
        autos = te_db.get_all_te_paths(ctx, {'origin': [te_def.ORIGIN_AUTO]})
        alive = {p['id'] for p in autos}
        self._idle_since = {k: t for k, t in self._idle_since.items()
                            if k in alive}
        live = {link: mbps for link, mbps in plugin.capacity.items()
                if link not in dead}
        released = 0
        for p in autos:
            if released >= conf.te_reopt_max_moves:
                break
            key = (p[te_def.SOURCE_PORT_ID], p[te_def.DESTINATION_PORT_ID])
            pr = pairs.get(key)
            rate = pr['rate'] if pr else 0.0
            src = pr['entry']['source_host'] if pr else \
                plugin._destination_of(ctx, {te_def.DESTINATION_PORT_ID:
                                             key[0]})
            dst = pr['entry']['destination_host'] if pr else \
                plugin._destination_of(ctx, {te_def.DESTINATION_PORT_ID:
                                             key[1]})
            hops = pce.links_of([src] + p[te_def.VIA_HOSTS] + [dst])
            # the links the topology knows: a VM that is gone or unbound
            # leaves an end of the path unknown, which must not make the
            # floor 0 and the path never idle
            known = [plugin.capacity[hop] for hop in hops
                     if hop in plugin.capacity]
            floor = conf.te_auto_min_share * min(known) if known else 0.0
            why = None
            if not rate or rate < floor:
                since = self._idle_since.setdefault(p['id'], now)
                if (now - since).total_seconds() >= \
                        conf.te_auto_release_after:
                    why = 'idle for %ds' % (now - since).total_seconds()
            else:
                self._idle_since.pop(p['id'], None)
                default = pce.shortest_links(live, src, dst) if src and \
                    dst else set()
                own = pr['links'] if pr else {}
                if default and not (default & set(congested)) and all(
                        max(0, measured.get(hop, 0) - own.get(hop, 0)) +
                        rate <= conf.te_reopt_low * plugin.capacity[hop]
                        for hop in default):
                    why = 'the default routes have room again'
            if why is None or self._moved.get(key, hold) > hold:
                continue
            if plugin._release_auto_path(ctx, p) is None:
                continue
            released += 1
            self._moved[key] = now       # no re-steering inside the hold-down
            self._idle_since.pop(p['id'], None)
            for hop in pce.shortest_links(live, src, dst) if src and dst \
                    else ():
                self._link_moved[hop] = now
            LOG.warning("SRv6 TE re-optimisation: automatic path %(id)s for "
                        "VM pair %(s)s>%(d)s (%(r).1f Mbit/s) released, "
                        "%(why)s; its traffic is back on the default routes",
                        {'id': p['id'], 's': key[0], 'd': key[1], 'r': rate,
                         'why': why})
        return released
