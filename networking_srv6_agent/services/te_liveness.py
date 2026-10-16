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

"""Re-push the domains whose TE paths cross a node that died or came back.

The plugin already routes a path around a dead waypoint whenever it builds a
payload (Srv6Plugin._dead_hosts): the dead node is judged from Neutron's
agent table, which every API worker can read. What nothing does by itself is
BUILD a payload when a node dies -- Neutron publishes an event for every
heartbeat but none for the heartbeats that stop. This monitor is that timer.
It keeps no truth of its own: it compares the set of dead waypoints with the
one it saw last time and, on a difference, pushes the affected domains in
full. A push is idempotent and the agent swaps its routes atomically, the
same mechanism as an admin's re-steer (E9), so a repeated or late push is
harmless.

It runs in one process (neutron's PeriodicWorker), so a node's death is one
push, not one per API worker.
"""

from neutron_lib import context as n_context
from oslo_log import log as logging

from networking_srv6_agent.db import te_db

LOG = logging.getLogger(__name__)


class TeLivenessMonitor:
    """check() is the periodic function; self._dead is the set last pushed."""

    def __init__(self, plugin):
        self._plugin = plugin
        # None until the first check: the first one has nothing to compare
        # with, and acts on every dead waypoint it finds.
        self._dead = None

    @staticmethod
    def _waypoints(paths):
        """{host: [(domain_id, path_id)]} for every via host of every path."""
        waypoints = {}
        for path in paths:
            for host in path['via_hosts']:
                waypoints.setdefault(host, []).append(
                    (path['domain_id'], path['id']))
        return waypoints

    def check(self):
        """One pass. Never raises: a periodic function that raises is gone."""
        try:
            self._check()
        except Exception:
            LOG.exception("SRv6 TE liveness check failed; trying again at "
                          "the next interval")

    def _check(self):
        ctx = n_context.get_admin_context()
        waypoints = self._waypoints(te_db.get_all_te_paths(ctx))
        # Only a waypoint matters: the death of a node no path crosses
        # changes no route, so it must not cause a push.
        dead = frozenset(self._plugin._dead_hosts(ctx)) & set(waypoints)
        if dead == self._dead:
            return
        # First pass: act on what is dead now. After a server restart the
        # agents may hold routes pushed before the node died, and nothing
        # else would correct them before their next full resync.
        changed = dead if self._dead is None else dead ^ self._dead
        previous = self._dead or frozenset()
        affected = {}
        for host in changed:
            for domain_id, path_id in waypoints.get(host, ()):
                affected.setdefault(domain_id, set()).add(path_id)
        for host in sorted(changed & (dead - previous)):
            LOG.warning("SRv6: node %(h)s looks dead (its agent missed "
                        "its heartbeats); TE paths through it go direct: "
                        "%(p)s",
                        {'h': host,
                         'p': ', '.join(sorted(
                             p for _d, p in waypoints[host]))})
        for host in sorted((previous - dead) & set(waypoints)):
            LOG.info("SRv6: node %s is alive again; its TE paths are "
                     "steered through it again", host)
        for domain_id in sorted(affected):
            self._plugin._push_domain_by_id(ctx, domain_id)
        # Only after every push went out: a push that raised leaves the old
        # set, so the next pass sees the same difference and tries again.
        self._dead = dead
