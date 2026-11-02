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

"""Measured load per VM pair and link (PLAN-UNRESERVED-STEERING step 2).

The fabric nodes count, per out-link, the SRv6 traffic of every
(outer SID, inner source, inner destination) they forward (GATE A: nft reads
the inner header past the outer one and past an SRH). This module checks a
posted batch, and turns the stored rows into what U-f needs: for each fresh
key, its rate and peak, the domain, the two ports with their hosts, and
whether the traffic is steered.

Which domain: a decap SID (`sid_for(locator, function_id)`, both modes)
names the destination host and the domain directly. Anything else is
steered traffic. In usid mode a steered SID is a container that still ends
with the destination's decap C-SIDs (fc00:0:a1:b3:3:e300:: ends with 3,
e300), so a suffix match finds the domain. In encap mode the SID is a
waypoint's End SID, which names no domain, so the inner addresses decide:
the one domain whose ports hold both, or none if that is ambiguous
(overlapping tenant addresses).

Pure functions and one small class: the plugin feeds them the database
rows, the domains and the ports, so they test without Neutron.
"""

import collections
import ipaddress
import uuid

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid

# A fabric agent sends its top 16 per link (A8); the server accepts up to
# this many, so a changed constant on the agents does not break ingestion,
# while a runaway or hostile collector cannot fill the table.
MAX_PAIRS_PER_LINK = 64
MAX_MBPS = 1e7          # 10 Tbit/s: no link carries more; also NaN/inf
FIELDS = ('reporter', 'from_node', 'to_node', 'sid', 'source_address',
          'destination_address', 'mbps')
_ID_NAMESPACE = uuid.UUID('6f1c2d0e-8a51-4b7e-9c3a-5d2e4f6a7b80')


class Invalid(ValueError):
    """A batch the server refuses; the message is the API's reason."""


def entry_id(row):
    """A stable, opaque id for one (link, SID, source, destination)."""
    return str(uuid.uuid5(_ID_NAMESPACE, '|'.join(
        row[k] for k in ('from_node', 'to_node', 'sid', 'source_address',
                         'destination_address'))))


def check_batch(items, capacity):
    """(rows to store, unknown links) of one posted batch.

    Every row is normalised (compressed address text). Raises Invalid on
    anything the server must not trust: a reporter that is not the sending
    end (the counters are per out-interface), a SID that is not IPv6,
    inner addresses that are not addresses or not of one family, a rate
    that is not a number from 0 to MAX_MBPS, a key posted twice, more than
    MAX_PAIRS_PER_LINK pairs on one link. An item for a link the topology
    does not have is left out and returned in `unknown`, as for link
    states.
    """
    rows, seen, unknown = [], set(), []
    per_link = collections.Counter()
    for item in items:
        row = {k: item.get(k) for k in FIELDS}
        link = (row['from_node'], row['to_node'])
        if link not in capacity:
            unknown.append(link)
            continue
        name = '%s>%s' % link
        if row['reporter'] != row['from_node']:
            raise Invalid(_("%(r)s cannot report %(l)s: VM-pair loads are "
                            "measured by the sending end") %
                          {'r': row['reporter'], 'l': name})
        try:
            row['sid'] = str(ipaddress.IPv6Address(row['sid']))
        except ValueError:
            raise Invalid(_("sid %(s)r on %(l)s is not an IPv6 address") %
                          {'s': row['sid'], 'l': name})
        try:
            src = ipaddress.ip_address(row['source_address'])
            dst = ipaddress.ip_address(row['destination_address'])
        except ValueError:
            raise Invalid(_("the inner addresses %(s)r, %(d)r on %(l)s are "
                            "not IP addresses") %
                          {'s': row['source_address'],
                           'd': row['destination_address'], 'l': name})
        if src.version != dst.version:
            raise Invalid(_("the inner addresses %(s)s, %(d)s on %(l)s are "
                            "of different families") %
                          {'s': src, 'd': dst, 'l': name})
        row['source_address'], row['destination_address'] = str(src), str(dst)
        v = row['mbps']
        if (isinstance(v, bool) or not isinstance(v, (int, float)) or
                not 0 <= v < MAX_MBPS):
            raise Invalid(_("mbps of %(l)s must be a number from 0 to "
                            "%(t)g") % {'l': name, 't': MAX_MBPS})
        key = (link, row['sid'], row['source_address'],
               row['destination_address'])
        if key in seen:
            raise Invalid(_("%(l)s %(s)s %(a)s>%(b)s posted twice") %
                          {'l': name, 's': row['sid'], 'a': src, 'b': dst})
        seen.add(key)
        per_link[link] += 1
        if per_link[link] > MAX_PAIRS_PER_LINK:
            raise Invalid(_("more than %(n)d VM pairs on %(l)s in one "
                            "batch") % {'n': MAX_PAIRS_PER_LINK, 'l': name})
        rows.append(row)
    return rows, unknown


class Resolver:
    """(SID, inner source, inner destination) -> domain, ports, hosts.

    domains: [(domain dict, function id)]; locators: {host: locator};
    ports_of(domain) -> {normalised address: (port id, host)}, called at
    most once per domain.
    """

    def __init__(self, domains, locators, usid, ports_of):
        self._domains = [d for d, _fid in domains]
        self._ports_of = ports_of
        self._ports = {}
        self._decap = {}
        self._tails = []            # usid: (decap C-SIDs, host, domain)
        for domain, function_id in domains:
            for host, locator in sorted(locators.items()):
                try:
                    address = sid.sid_for(locator, function_id)
                except sid.InvalidLocator:
                    continue
                self._decap[address] = (host, domain)
                if usid:
                    tail = _csids(address)
                    if tail:
                        self._tails.append((tail, host, domain))

    def _addresses(self, domain):
        if domain['id'] not in self._ports:
            self._ports[domain['id']] = self._ports_of(domain)
        return self._ports[domain['id']]

    def __call__(self, sid_text, source, destination):
        out = {'domain_id': None, 'steered': None, 'source_port_id': None,
               'source_host': None, 'destination_port_id': None,
               'destination_host': None}
        address = ipaddress.IPv6Address(sid_text)
        hit = self._decap.get(address)
        out['steered'] = hit is None
        if hit is None and self._tails:
            csids = _csids(address)
            for tail, host, domain in self._tails:
                if len(csids) > len(tail) and csids[-len(tail):] == tail:
                    hit = (host, domain)
                    break
        if hit:
            out['destination_host'], domain = hit
        else:
            # an End SID names no domain: the addresses must, unambiguously
            both = [d for d in self._domains
                    if source in self._addresses(d) and
                    destination in self._addresses(d)]
            if len(both) != 1:
                return out
            domain = both[0]
        ports = self._addresses(domain)
        out['domain_id'] = domain['id']
        if source in ports:
            out['source_port_id'], out['source_host'] = ports[source]
        if destination in ports:
            out['destination_port_id'] = ports[destination][0]
            out['destination_host'] = (out['destination_host'] or
                                       ports[destination][1])
        return out


def _csids(address):
    """The C-SIDs of an address after the usid block; () if it has none."""
    try:
        return tuple(sid._csids_of(address, constants.USID_BLOCK_BITS,
                                   constants.USID_CSID_BITS))
    except sid.InvalidLocator:
        return ()


def view(rows, now, fresh, window_start, resolve, current=None):
    """[entry] of every key still on its link.

    A key is listed when its newest row is at or after fresh AND, if its
    reporter has a heartbeat in `current` ({reporter: cut-off}), at or after
    that cut-off. The agent reports only the pairs above its floor, so a
    pair that left a link (ECMP moved it, it stopped) is simply missing
    from the next batch: without the heartbeat its last row would stay
    listed, at its old rate, until it went stale (GATE B).

    rows: the stored measurements, oldest first. mbps is the newest row's,
    peak_mbps the highest after window_start (as link states, L9).
    """
    current = current or {}
    latest, peak = {}, {}
    for r in rows:
        key = tuple(r[k] for k in FIELDS[1:6])
        latest[key] = r
        if r['measured_at'] > window_start:
            peak[key] = max(peak.get(key, 0.0), r['mbps'])
    out = []
    for key, r in latest.items():
        if r['measured_at'] < max(fresh, current.get(r['reporter'], fresh)):
            continue
        entry = {k: r[k] for k in FIELDS}
        entry.update(resolve(r['sid'], r['source_address'],
                             r['destination_address']))
        entry.update(id=entry_id(r), peak_mbps=peak.get(key),
                     measured_at=r['measured_at'].isoformat(),
                     age=int((now - r['measured_at']).total_seconds()))
        out.append(entry)
    return out
