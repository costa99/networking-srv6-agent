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

"""The SRv6 domains drawn on Horizon's Network Topology (FEATURE D24).

srv6_domains() is a pure function of the domain list and the networks the
topology shows, so what gets linked is unit-tested without a browser; the
page's script only draws the result.

A domain is drawn only when it joins at least two networks the topology
shows: a domain with one network links nothing, and a network the page
does not draw has nowhere to attach a line.
"""

import itertools


def srv6_domains(domains, network_ids, detail_url):
    """The JSON-ready list the page draws, one entry per linked domain.

    network_ids are the topology's networks, in its order (the flat view's
    columns, left to right). detail_url(domain_id) is the domain's page:
    each drawn link leads there.

    Each entry has the domain's networks in that order, and links: every
    pair of them, since a domain routes between all of its networks.
    """
    order = {nid: i for i, nid in enumerate(network_ids)}
    out = []
    for d in sorted(domains, key=lambda d: d.name_or_id):
        nets = sorted((n for n in d.networks if n in order), key=order.get)
        if len(nets) < 2:
            continue
        out.append({
            'id': d.id,
            'name': d.name_or_id,
            'url': detail_url(d.id),
            'networks': nets,
            'links': [list(p) for p in itertools.combinations(nets, 2)],
        })
    return out
