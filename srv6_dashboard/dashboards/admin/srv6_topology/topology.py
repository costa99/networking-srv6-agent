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

"""What the topology page draws, assembled from five API answers.

build() is a pure function of the lists the API returned, so the whole
picture is unit-tested without a browser (FEATURE D8): the JavaScript only
lays out and draws what this returns, it decides nothing.

    hosts     every host with a registered locator, plus, flagged with
              locator None, any host without one that a domain's VM runs
              on or a TE path names. Such a host can be neither waypoint
              nor destination: it is why a path is DEGRADED, so the page
              must show it, not leave the path undrawn.
    domains   each domain with the hosts its VMs run on (VM count per
              host): where the domain's traffic enters and leaves.
    te_paths  each path as its hop list: the waypoints in order, then the
              destination host (for a port path, the host the port is
              bound to, if any). A path limited to one ingress (source)
              starts its hops at that host, minus leading waypoints equal
              to it: the node drops its own SID, so the packet never
              visits it twice (FEATURE-TE-SOURCE-HOST).
    links     each directed underlay link with its measured state
              (srv6_link_states, PLAN-LINK-STATE-TE step 3) and a level
              the page colours it by. A node on a link without a locator
              is a fabric node (spine/leaf): drawn as such, not flagged.
              Every node gets a tier, its hop distance from the nearest
              host with a locator: hosts 0, leaves 1, spines 2 on a Clos.
"""

import collections

# Shares of the link's capacity: the 90 % is te_reopt_high's default, the
# load the re-optimisation will call congested (PLAN-LINK-STATE-TE L5).
BUSY, CONGESTED = 0.5, 0.9


def _level(link):
    """down, unknown (stale or no rate), ok, busy or congested."""
    if link.get('up') is False:
        return 'down'
    if link.get('stale') or link.get('mbps') is None:
        return 'unknown'
    load = link.get('load')
    if load is None:
        return 'unknown'
    return ('congested' if load >= CONGESTED else
            'busy' if load >= BUSY else 'ok')


def _tiers(hosts, links):
    """{node: hop distance to the nearest host with a locator} (BFS).

    A node no host reaches goes one tier above the highest.
    """
    adj = collections.defaultdict(set)
    for link in links:
        adj[link['from']].add(link['to'])
        adj[link['to']].add(link['from'])
    tier = {h: 0 for h, v in hosts.items() if v['locator']}
    queue = list(tier)
    for node in queue:
        for peer in sorted(adj[node]):
            if peer not in tier:
                tier[peer] = tier[node] + 1
                queue.append(peer)
    top = max(tier.values(), default=-1) + 1
    return {h: tier.get(h, 0 if hosts[h]['locator'] or not adj[h] else top)
            for h in hosts}


def _port_host(port):
    # The SDK's attribute name, and the API's for wrappers built from raw
    # API dicts (Horizon's own test data uses those).
    return port.get('binding_host_id') or port.get('binding:host_id') or None


def build(locators, domains, ports, te_paths, path_url, links=()):
    """The page's data as a JSON-ready dict.

    path_url(path_id) gives a TE path's detail URL: the page links each
    drawn path to it. links: srv6_link_states entries (may be empty).
    """
    hosts = {loc.host: {'host': loc.host, 'locator': loc.locator,
                        'updated_at': loc.updated_at}
             for loc in locators}
    out_links = []
    for ls in links:
        cap, mbps = ls.get('capacity_mbps'), ls.get('mbps')
        link = {k: ls.get(k) for k in (
            'up', 'mbps', 'delivered_mbps', 'peak_mbps', 'capacity_mbps',
            'age', 'stale')}
        link.update({'from': ls.get('from_node'), 'to': ls.get('to_node'),
                     'load': (round(mbps / cap, 4)
                              if cap and mbps is not None else None)})
        link['level'] = _level(link)
        out_links.append(link)
        for node in (link['from'], link['to']):
            # shortcut: a host of the topology file whose agent never
            # registered is drawn as fabric; the API does not say roles
            hosts.setdefault(node, {'host': node, 'locator': None,
                                    'updated_at': None, 'fabric': True})

    net_domain = {nid: d.id for d in domains for nid in d.networks}
    presence = collections.defaultdict(collections.Counter)
    port_host = {}
    for port in ports:
        host = _port_host(port)
        port_host[port.id] = host
        domain_id = net_domain.get(port.network_id)
        if (host is None or domain_id is None or
                not (port.device_owner or '').startswith('compute:')):
            continue
        presence[domain_id][host] += 1
        hosts.setdefault(host, {'host': host, 'locator': None,
                                'updated_at': None})

    out_domains = [{
        'id': d.id,
        'name': d.name_or_id,
        'project_id': d.project_id,
        'networks': len(d.networks),
        'hosts': dict(sorted(presence[d.id].items())),
    } for d in sorted(domains, key=lambda d: d.name_or_id)]

    out_paths = []
    for p in te_paths:
        if p.destination_host:
            dest = p.destination_host
            label = 'host %s' % dest
        else:
            dest = port_host.get(p.destination_port_id)
            label = 'port %s%s' % (p.destination_port_id[:8],
                                   ' on %s' % dest if dest else '')
        via = list(p.via_hosts or [])
        source = getattr(p, 'source_host', None)
        start = []
        if source:
            start = [source]
            while via and via[0] == source:
                via.pop(0)
        hops = start + via + ([dest] if dest else [])
        for host in hops:
            hosts.setdefault(host, {'host': host, 'locator': None,
                                    'updated_at': None})
        out_paths.append({
            'id': p.id,
            'domain_id': p.domain_id,
            'status': p.status,
            'via': list(p.via_hosts or []),
            'source': source,
            'destination': label,
            'hops': hops,
            'segments': list(p.segments or []),
            'url': path_url(p.id),
        })

    tiers = _tiers(hosts, out_links)
    for name, host in hosts.items():
        host.setdefault('fabric', False)
        host['tier'] = tiers[name]
    return {
        'hosts': [hosts[h] for h in sorted(hosts)],
        'domains': out_domains,
        'te_paths': out_paths,
        'links': sorted(out_links, key=lambda x: (x['from'], x['to'])),
    }
