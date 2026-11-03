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

"""Constrained path computation for the server's automatic paths (U-f).

One request at a time (source, destination, demand): drop every directed
link whose residual capacity is below the demand, then take the path with
the fewest hops and, among those, the widest bottleneck -- CSPF, Tier 1 of
the research notes (PLAN-PATH-COMPUTATION). No path: nothing is moved.

The underlay comes from a static topology file, read once:

    node <name> <locator>      fabric nodes and hosts (host = Neutron host)
    link <a> <b> <Mbit/s>      full duplex: this capacity in each direction

Pure standard library: the graphs are a few dozen nodes, and the work is a
Dijkstra per create.
"""

import collections
import heapq

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import sid


class TopologyError(ValueError):
    pass


def load(path):
    """({node: locator}, {(a, b): Mbit/s}) from a topology file.

    Every link appears in both directions. A malformed line is an error at
    start, not a path computed on half a topology.
    """
    locators, cap = {}, {}
    with open(path) as f:
        for n, line in enumerate(f, 1):
            words = line.split('#')[0].split()
            if not words:
                continue
            where = '%s:%d' % (path, n)
            if words[0] == 'node' and len(words) == 3:
                try:
                    sid.parse_locator(words[2])
                except sid.InvalidLocator as e:
                    raise TopologyError('%s: %s' % (where, e))
                locators[words[1]] = words[2]
            elif words[0] == 'link' and len(words) == 4:
                a, b = words[1], words[2]
                if a not in locators or b not in locators or a == b:
                    raise TopologyError(
                        _('%s: link %s-%s needs two declared nodes')
                        % (where, a, b))
                try:
                    mbps = int(words[3])
                except ValueError:
                    mbps = 0
                if mbps <= 0:
                    raise TopologyError(_('%s: capacity must be a positive '
                                          'integer (Mbit/s)') % where)
                cap[(a, b)] = cap[(b, a)] = mbps
            else:
                raise TopologyError(_('%s: expected "node <name> <locator>" '
                                      'or "link <a> <b> <Mbit/s>"') % where)
    return locators, cap


def links_of(path):
    """The directed links of a node path."""
    return list(zip(path, path[1:]))


def compute(cap, used, src, dst, bw):
    """The widest of the shortest paths with room for bw, or None.

    cap: {(a, b): Mbit/s}; used: {(a, b): Mbit/s already in use}.
    Returns the full node path [src, ..., dst]: every hop is pinned, so the
    links the traffic takes are known exactly.

    Dijkstra on the label (hops, -bottleneck): both only get worse along a
    path, so the first time dst is popped its label is optimal. Ties go to
    the lexicographically smallest path, so the answer is deterministic.
    """
    adj = collections.defaultdict(list)
    for (a, b), c in cap.items():
        residual = c - used.get((a, b), 0)
        if residual >= bw:
            adj[a].append((b, residual))
    best = {src: (0, float('-inf'))}
    heap = [(0, float('-inf'), [src])]
    while heap:
        hops, neg_bn, path = heapq.heappop(heap)
        node = path[-1]
        if node == dst:
            return path
        if (hops, neg_bn) > best.get(node, (hops, neg_bn)):
            continue                      # a better label was found since
        for nxt, residual in adj[node]:
            if nxt in path:
                continue
            label = (hops + 1, max(neg_bn, -residual))
            # <=, not <: an equally good path is kept too, and the heap then
            # pops the lexicographically smallest of them first
            if label <= best.get(nxt, (float('inf'), 0)):
                best[nxt] = label
                heapq.heappush(heap, label + (path + [nxt],))
    return None


def shortest_links(cap, src, dst):
    """Every directed link of every shortest (fewest-hop) path src -> dst.

    What default routing may use: the underlay's ECMP spreads traffic over
    all of them, and which one a pair takes can change (a flow label, the
    head-end's route cache: TECH-DOC-NOTES N7). The U-f release judges the
    room on all of them (worst case). Empty if dst is unreachable.
    """
    adj, pred = collections.defaultdict(list), collections.defaultdict(list)
    for a, b in cap:
        adj[a].append(b)
        pred[b].append(a)
    dist = {src: 0}
    queue = collections.deque([src])
    while queue:
        node = queue.popleft()
        for nxt in adj[node]:
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                queue.append(nxt)
    if dst not in dist or src == dst:
        return set()
    # back from dst, along links that lose exactly one hop each
    links, frontier = set(), {dst}
    while frontier:
        step = set()
        for b in frontier:
            for a in pred[b]:
                if dist.get(a) == dist[b] - 1:
                    links.add((a, b))
                    step.add(a)
        frontier = step - {src}
    return links
