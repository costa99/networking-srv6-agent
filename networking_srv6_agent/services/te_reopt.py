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

"""Move bandwidth reservations off congested links (PLAN-LINK-STATE-TE U-d).

Each pass reads the measured link state (srv6_link_states) and:

1. finds the congested links: the last te_reopt_samples measurements of the
   link all above te_reopt_high of its reservable capacity, and all newer
   than the last move off it (a move must show in the data before the link
   is judged again);
2. on the hottest link first, moves the largest reservations crossing it,
   so the fewest moves clear it, until its load minus what was moved is at
   most te_reopt_low (hysteresis), te_reopt_max_moves moves were made in
   this pass (budget), skipping a reservation moved within
   te_reopt_hold_down (damping: a reservation cannot flap faster);
3. moves one through Srv6Plugin._reroute_reservation: under the admission
   lock, onto a path with room where every link holds max(reserved,
   measured) (L4), avoiding every congested and dead link, in place (the
   head-end swaps its route: make-before-break). No room: it stays.

Traffic without a reservation is never moved (U-f is out of scope): a link
hot only from such traffic is logged and left. The memory of moves (hold-
down, last move per link) is this process's: one worker, like the liveness
monitor; a restart forgets it, which at worst allows one early move.
"""

import datetime

from neutron_lib import context as n_context
from oslo_log import log as logging
from oslo_utils import timeutils

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.db import te_db
from networking_srv6_agent.services import pce

LOG = logging.getLogger(__name__)


class TeReoptimiser:
    """check() is the periodic function."""

    def __init__(self, plugin):
        self._plugin = plugin
        self._moved = {}          # reservation id -> time of its last move
        self._link_moved = {}     # (a, b) -> time of the last move off it

    def check(self):
        """One pass. Never raises: a periodic function that raises is gone."""
        try:
            self._check(n_context.get_admin_context())
        except Exception:
            LOG.exception("SRv6 TE re-optimisation failed; trying again at "
                          "the next interval")

    def _congested(self, ctx):
        """{(a, b): latest Mbit/s} of the links congested now."""
        plugin, conf = self._plugin, self._plugin.conf
        out = {}
        for link, series in plugin._load_series(ctx).items():
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
        if not plugin.capacity:
            return
        silent, outage = plugin._silent_fabric(ctx)
        if outage:
            return                      # the liveness monitor logs it
        congested = self._congested(ctx)
        if not congested:
            return
        dead = plugin._dead_links(ctx, silent)
        cap = {link: mbps for link, mbps in plugin.capacity.items()
               if link not in dead and link not in congested}
        measured = plugin._measured_load(ctx)
        now = timeutils.utcnow()
        hold = now - datetime.timedelta(seconds=conf.te_reopt_hold_down)
        # an entry at or before `hold` no longer holds anything down: drop
        # it, or every reservation ever moved stays here (audit 2026-11-02 6)
        self._moved = {k: t for k, t in self._moved.items() if t > hold}
        moves = 0
        for link in sorted(congested, key=lambda k: (
                -congested[k] / plugin.capacity[k], k)):    # hottest first
            load, limit = congested[link], conf.te_reopt_low * \
                plugin.capacity[link]
            crossing = sorted(
                (p for p in te_db.get_bandwidth_paths(ctx)
                 if link in pce.links_of(te_db.reservation_hops(p))),
                key=lambda p: (-p[te_def.BANDWIDTH], p['id']))
            if not crossing:
                LOG.info("SRv6 TE re-optimisation: %(l)s carries %(m).1f "
                         "Mbit/s with no reservation on it: nothing to move",
                         {'l': '%s>%s' % link, 'm': load})
            for p in crossing:
                if moves >= conf.te_reopt_max_moves or load <= limit:
                    break
                if self._moved.get(p['id'], hold) > hold:
                    continue                # hold-down
                old, path = plugin._reroute_reservation(
                    ctx, p['id'], cap, measured=measured)
                if old is None or path is None:
                    LOG.info("SRv6 TE re-optimisation: no path with room "
                             "for reservation %(id)s (%(bw)d Mbit/s) off "
                             "%(l)s; it stays",
                             {'id': p['id'], 'bw': p[te_def.BANDWIDTH],
                              'l': '%s>%s' % link})
                    continue
                moves += 1
                load -= p[te_def.BANDWIDTH]
                self._moved[p['id']] = now
                for hop in pce.links_of(te_db.reservation_hops(old)):
                    self._link_moved[hop] = now
                LOG.warning("SRv6 TE re-optimisation: %(l)s congested "
                            "(%(m).1f Mbit/s > %(h)s of %(c)d): reservation "
                            "%(id)s (%(bw)d Mbit/s) moved from %(old)s to "
                            "%(new)s",
                            {'l': '%s>%s' % link, 'm': congested[link],
                             'h': conf.te_reopt_high,
                             'c': plugin.capacity[link], 'id': p['id'],
                             'bw': p[te_def.BANDWIDTH],
                             'old': '-'.join(te_db.reservation_hops(old)),
                             'new': '-'.join(path)})
            if moves >= conf.te_reopt_max_moves:
                break
