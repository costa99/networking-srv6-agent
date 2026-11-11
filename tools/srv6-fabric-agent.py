#!/usr/bin/env python3
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

"""srv6-fabric-agent.py -- link telemetry of one fabric node.

Runs on each spine and leaf of the underlay (a Linux switch, as SONiC is;
on the test bed, Linux VMs). It tells Neutron what the fabric really
carries, so that the server can see congested links and steer heavy VM
pairs off them. It reads
only what the kernel already counts: every period, one read of
/proc/net/dev (bytes, packets and drops of every interface) and of
/sys/class/net/<if>/operstate. It turns them into one report covering
each neighbour link, in both directions, as seen from this node:

  seen=tx  this node -> neighbour: what this node offered to the link
  seen=rx  neighbour -> this node: what the link delivered

with Mbit/s and packets/s averaged over the period, drops counted in the
period, and up/down. Both ends of a link between two fabric nodes report
it, so the server can pair them: the sender's tx minus the receiver's rx
is what the link lost.

Output: with --neutron-url, each report is POSTed to Neutron's
srv6_link_states as one batch, authenticated with a Keystone token from
--credentials; the server stamps it with its own clock. A failed POST is
dropped, never queued: a late measurement is worth nothing. Only changes
of the push state are logged, so a dead API does not flood the journal.
Without --neutron-url, one JSON line per period on stdout (debugging).

With --aggregates the node also counts, per link
it sends on, the SRv6 traffic of every VM pair: an nft table of its own
(installed at start, needs CAP_NET_ADMIN) keys dynamic sets on (out
interface, outer destination SID, inner source, inner destination). nft's
transport-header offset lands on the inner header, past the outer IPv6
header and any SRH. Every period the agent reads the counters, keeps the
TOP_K largest pairs per link at FLOOR_MBPS or more, and POSTs them to
srv6_aggregate_loads, a second batch next to the link states.

Usage: srv6-fabric-agent.py --links FILE [--period 5] [--count N]
                            [--neutron-url URL --credentials FILE]
                            [--aggregates]
       srv6-fabric-agent.py test          (self-check, no system access)

The link map has one line per neighbour: <interface> <this node>
<neighbour>, in the SRv6 plugin's node names (the names of its
topology_file). The credentials file holds openrc-style lines (export
optional): OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME and
optionally OS_USER_DOMAIN_ID, OS_PROJECT_DOMAIN_ID (default "default").
Run it as a service on every fabric node, with the map of that node.
Standard library only.
"""
import argparse
import calendar
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API_PATH = '/v2.0/srv6_link_states'
AGGREGATE_PATH = '/v2.0/srv6_aggregate_loads'
# VM pairs reported per link and period, and the smallest rate reported
# (a development constant, not an option; the server accepts up to 64 per
# link).
TOP_K = 16
FLOOR_MBPS = 1.0
NFT_TABLE = 'srv6_agg'
# The inner-family test and the counter update sit in different rules (a
# jump): nft 1.0.9 cannot print a rule holding both (payload.c assertion),
# which would break `nft list ruleset` on the node. An element
# idle for 60 s leaves its set; the agent reads every period.
NFT_SCRIPT = """table inet %(t)s
delete table inet %(t)s
table inet %(t)s {
    set v4 {
        typeof oifname . ip6 daddr . @th,96,32 . @th,128,32
        flags dynamic, timeout
        timeout 60s
        size 16384
        counter
    }
    set v6 {
        typeof oifname . ip6 daddr . @th,64,128 . @th,192,128
        flags dynamic, timeout
        timeout 60s
        size 16384
        counter
    }
    chain inner4 {
        update @v4 { oifname . ip6 daddr . @th,96,32 . @th,128,32 }
    }
    chain inner6 {
        update @v6 { oifname . ip6 daddr . @th,64,128 . @th,192,128 }
    }
    chain count {
        type filter hook forward priority 0; policy accept;
        meta nfproto ipv6 meta l4proto 4 jump inner4
        meta nfproto ipv6 meta l4proto 41 jump inner6
    }
}
""" % {'t': NFT_TABLE}


def load_links(path):
    """[(interface, this node, neighbour)] from the link map; # comments."""
    out = []
    for line in open(path):
        f = line.split('#')[0].split()
        if f:
            if len(f) != 3:
                sys.exit('%s: expected <interface> <node> <neighbour>: %r'
                         % (path, line))
            out.append(tuple(f))
    return out


def parse_dev(text):
    """/proc/net/dev -> {interface: 6 counters}.

    The counters: rx bytes, rx packets, rx drops, tx bytes, tx packets, tx
    drops. One read for every interface: cheaper than six /sys files per
    interface, and all counters come from the same instant. Columns after
    the colon: rx bytes packets errs drop fifo frame compressed multicast,
    then tx bytes packets errs drop ...
    """
    out = {}
    for line in text.splitlines()[2:]:          # two header lines
        name, _, f = line.partition(':')
        f = f.split()
        if len(f) >= 12:
            out[name.strip()] = tuple(int(f[i]) for i in (0, 1, 3, 8, 9, 11))
    return out


def operstate(iface):
    """'up', 'down', ... as the kernel says; 'absent' if the NIC is gone."""
    try:
        with open('/sys/class/net/%s/operstate' % iface) as f:
            return f.read().strip()
    except OSError:
        return 'absent'


def report(node, links, prev, cur, states, elapsed):
    """One report: two directed records per neighbour link.

    A counter that went backwards (interface re-created, VM rebooted under
    a running agent) gives no rate for that period (None), never a negative
    or huge one; the next period is normal again. The counters are 64 bit,
    so a real wrap does not happen. 'unknown' counts as up: it is what the
    kernel says for an interface without a carrier concept, and it is never
    what a downed link reports ('down').
    """
    recs = []
    for iface, me, peer in links:
        up = states.get(iface) in ('up', 'unknown')
        p, c = prev.get(iface), cur.get(iface)
        d = [b - a for a, b in zip(p, c)] if p and c else None
        if d and min(d) < 0:
            d = None
        for seen, src, dst, k in (('tx', me, peer, 3), ('rx', peer, me, 0)):
            recs.append({
                'from': src, 'to': dst, 'seen': seen, 'iface': iface,
                'up': up,
                'mbps': round(d[k] * 8 / elapsed / 1e6, 3) if d else None,
                'pps': round(d[k + 1] / elapsed, 1) if d else None,
                'drops': d[k + 2] if d else None})
    return {'node': node, 't': round(time.time(), 3),
            'period': round(elapsed, 3), 'links': recs}


def api_items(rep):
    """The report as srv6_link_states batch items (reporter = this node)."""
    return [{'reporter': rep['node'], 'from_node': x['from'],
             'to_node': x['to'], 'up': x['up'], 'mbps': x['mbps'],
             'drops': x['drops']} for x in rep['links']]


def parse_pair_set(text, version):
    """`nft -j list set` -> {(oif, sid, src, dst): bytes}.

    nft prints a 32-bit inner field as a number and a 128-bit one as a
    "0x..." string; both become the address text.
    """
    make = ipaddress.IPv4Address if version == 4 else ipaddress.IPv6Address
    out = {}
    for obj in json.loads(text).get('nftables', []):
        for e in obj.get('set', {}).get('elem', []):
            e = e.get('elem', {})
            oif, sid, src, dst = e['val']['concat']
            src, dst = (make(int(v, 0) if isinstance(v, str) else v)
                        for v in (src, dst))
            out[(oif, sid, str(src), str(dst))] = e['counter']['bytes']
    return out


def pair_rates(prev, cur, elapsed):
    """{key: Mbit/s} over one period, from two reads of the pair counters.

    A key absent before is new since the last read: its whole counter is
    this period's (an element is created by its first packet). A counter
    that went backwards was dropped (60 s idle) and re-created: what it
    holds now is this period's. prev None is the first read: a baseline
    only, its counters hold history of unknown length.
    """
    if prev is None:
        return {}
    out = {}
    for key, b in cur.items():
        a = prev.get(key)
        d = b - a if a is not None and b >= a else b
        out[key] = d * 8 / elapsed / 1e6
    return out


def aggregate_items(node, links, rates, top_k=TOP_K, floor=FLOOR_MBPS):
    """srv6_aggregate_loads batch items: the top_k pairs per link >= floor.

    Interfaces that are not in the link map (management) are ignored.
    """
    peer = {iface: nb for iface, _me, nb in links}
    by_link = {}
    for (oif, sid, src, dst), mbps in rates.items():
        if oif in peer and mbps >= floor:
            by_link.setdefault(oif, []).append((mbps, sid, src, dst))
    items = []
    for oif in sorted(by_link):
        for mbps, sid, src, dst in sorted(by_link[oif], reverse=True)[:top_k]:
            items.append({'reporter': node, 'from_node': node,
                          'to_node': peer[oif], 'sid': sid,
                          'source_address': src, 'destination_address': dst,
                          'mbps': round(mbps, 3)})
    return items


def nft_install():
    """Replace this agent's nft table, atomically (one nft transaction)."""
    subprocess.run(['nft', '-f', '-'], input=NFT_SCRIPT, text=True,
                   check=True, capture_output=True, timeout=10)


def nft_read():
    """Both pair sets' counters, as one dict."""
    out = {}
    for name, version in (('v4', 4), ('v6', 6)):
        r = subprocess.run(['nft', '-j', 'list', 'set', 'inet', NFT_TABLE,
                            name], check=True, capture_output=True,
                           text=True, timeout=10)
        out.update(parse_pair_set(r.stdout, version))
    return out


def load_credentials(path):
    """{OS_*: value} from an openrc-style file: KEY=VALUE, export, quotes."""
    env = {}
    for line in open(path):
        line = line.strip()
        if line.startswith('export '):
            line = line[7:]
        key, eq, value = line.partition('=')
        if eq and key.startswith('OS_'):
            env[key.strip()] = value.strip().strip('"\'')
    missing = [k for k in ('OS_AUTH_URL', 'OS_USERNAME', 'OS_PASSWORD',
                           'OS_PROJECT_NAME') if not env.get(k)]
    if missing:
        sys.exit('%s: missing %s' % (path, ', '.join(missing)))
    return env


class Neutron:
    """POSTs batches to srv6_link_states with a Keystone v3 token.

    The token is kept until 5 minutes before it expires, and fetched again
    at once on a 401 (revoked, or Keystone restarted).
    """

    def __init__(self, url, creds):
        self.base = url.rstrip('/')
        self.url = self.base + API_PATH
        self.creds = creds
        self.token, self.expires = None, 0.0

    def _auth(self):
        c = self.creds
        auth_url = c['OS_AUTH_URL'].rstrip('/')
        if not auth_url.endswith('/v3'):
            auth_url += '/v3'
        user = {'name': c['OS_USERNAME'], 'password': c['OS_PASSWORD'],
                'domain': {'id': c.get('OS_USER_DOMAIN_ID', 'default')}}
        project = {'name': c['OS_PROJECT_NAME'],
                   'domain': {'id': c.get('OS_PROJECT_DOMAIN_ID', 'default')}}
        body = {'auth': {
            'identity': {'methods': ['password'],
                         'password': {'user': user}},
            'scope': {'project': project}}}
        req = urllib.request.Request(
            auth_url + '/auth/tokens', json.dumps(body).encode(),
            {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.token = r.headers['X-Subject-Token']
            # e.g. 2026-11-01T23:00:00.000000Z, always UTC
            exp = json.load(r)['token']['expires_at']
        self.expires = calendar.timegm(
            time.strptime(exp[:19], '%Y-%m-%dT%H:%M:%S'))

    def post(self, items, path=API_PATH, key='srv6_link_states'):
        """POST one batch; raises on failure (the caller logs, drops it)."""
        for attempt in (1, 2):
            if not self.token or time.time() > self.expires - 300:
                self._auth()
            req = urllib.request.Request(
                self.base + path, json.dumps({key: items}).encode(),
                {'Content-Type': 'application/json',
                 'X-Auth-Token': self.token})
            try:
                with urllib.request.urlopen(req, timeout=5):
                    return
            except urllib.error.HTTPError as e:
                if e.code != 401 or attempt == 2:
                    raise RuntimeError('HTTP %d: %s' % (
                        e.code, e.read()[:300].decode('utf-8', 'replace')))
                self.token = None               # fetch a new one, once


def read_dev():
    with open('/proc/net/dev') as f:
        return parse_dev(f.read())


class Pusher:
    """The log of one kind of push: only its changes of state.

    'pushing to', then 'push failed ...' once, so a dead API does not
    flood the journal.
    """

    def __init__(self, what):
        self.what, self.failing = what, None

    def ok(self, url):
        if self.failing is not False:
            print('pushing %s to %s' % (self.what, url), file=sys.stderr,
                  flush=True)
        self.failing = False

    def failed(self, e):
        if self.failing is not True:
            print('%s push failed, dropping reports until it works: %s'
                  % (self.what, e), file=sys.stderr, flush=True)
        self.failing = True


def run(links, period, count, neutron=None, aggregates=False):
    node = links[0][1]
    links_push, pairs_push = Pusher('link states'), Pusher('VM-pair loads')
    pairs_prev = None
    if aggregates:
        nft_install()                   # fails the start: systemd restarts
        pairs_prev = nft_read()         # a baseline only
    prev, t_prev = read_dev(), time.monotonic()
    deadline = t_prev + period
    n = 0
    while not count or n < count:
        time.sleep(max(0.0, deadline - time.monotonic()))
        # a fixed grid, so the reports do not drift; a late wake-up (a
        # suspended VM) skips the missed slots instead of firing them all
        late = int((time.monotonic() - deadline) // period)
        deadline += period * max(1, late + 1)
        cur, now = read_dev(), time.monotonic()
        states = {iface: operstate(iface) for iface, _, _ in links}
        rep = report(node, links, prev, cur, states, now - t_prev)
        pairs = []
        if aggregates:
            try:
                pairs_cur = nft_read()
                pairs = aggregate_items(node, links, pair_rates(
                    pairs_prev, pairs_cur, now - t_prev))
                pairs_prev = pairs_cur
            except Exception as e:      # the table gone, nft failing
                pairs_push.failed(e)
                pairs_prev = None       # next good read is a new baseline
        if neutron is None:
            if aggregates:
                rep['pairs'] = pairs
            sys.stdout.write(json.dumps(rep, separators=(',', ':')) + '\n')
            sys.stdout.flush()
        else:
            try:
                neutron.post(api_items(rep))
                links_push.ok(neutron.url)
            except Exception as e:      # any failure: log once, drop, go on
                links_push.failed(e)
            if pairs:                   # nothing above the floor: no POST
                try:
                    neutron.post(pairs, AGGREGATE_PATH,
                                 'srv6_aggregate_loads')
                    pairs_push.ok(neutron.base + AGGREGATE_PATH)
                except Exception as e:
                    pairs_push.failed(e)
        prev, t_prev = cur, now
        n += 1


def _dev_row(iface, rb, rp, rd, tb, tp, td):
    """One /proc/net/dev line, for the self-test."""
    return ('%6s: %d %d 0 %d 0 0 0 0 %d %d 0 %d 0 0 0 0\n'
            % (iface, rb, rp, rd, tb, tp, td))


def selftest():
    head = ('Inter-|   Receive  |  Transmit\n'
            ' face |bytes packets errs drop ...\n')
    a = parse_dev(head + _dev_row('s1', 1000, 10, 0, 2000, 20, 0) +
                  _dev_row('ml1', 5, 1, 0, 5, 1, 0))
    assert a == {'s1': (1000, 10, 0, 2000, 20, 0),
                 'ml1': (5, 1, 0, 5, 1, 0)}, a
    # 5 s later: s1 received 62.5 MB (100 Mbit/s) and sent 31.25 MB
    # (50 Mbit/s), 3 rx drops; ml1's counters went backwards (reset); s2
    # is not in /proc/net/dev at all
    b = parse_dev(head + _dev_row('s1', 1000 + 62500000, 10 + 5000, 3,
                                  2000 + 31250000, 20 + 2500, 0) +
                  _dev_row('ml1', 1, 1, 0, 1, 1, 0))
    links = [('s1', 'l1', 's1'), ('ml1', 'l1', 'ale-ml1'), ('s2', 'l1', 's2')]
    r = report('l1', links, a, b,
               {'s1': 'up', 'ml1': 'unknown', 's2': 'absent'}, 5.0)
    by = {(x['from'], x['to']): x for x in r['links']}
    assert r['node'] == 'l1' and len(r['links']) == 6
    assert by['l1', 's1'] == {
        'from': 'l1', 'to': 's1', 'seen': 'tx', 'iface': 's1', 'up': True,
        'mbps': 50.0, 'pps': 500.0, 'drops': 0}, by['l1', 's1']
    assert by['s1', 'l1']['mbps'] == 100.0 and by['s1', 'l1']['drops'] == 3
    assert by['s1', 'l1']['seen'] == 'rx' and by['s1', 'l1']['pps'] == 1000.0
    # a reset gives no rate; 'unknown' counts as up; a missing NIC is down
    assert by['l1', 'ale-ml1']['mbps'] is None and by['l1', 'ale-ml1']['up']
    assert by['l1', 's2']['up'] is False and by['s2', 'l1']['mbps'] is None
    assert operstate('no-such-interface-xyz') == 'absent'
    items = api_items(r)
    assert items[0] == {'reporter': 'l1', 'from_node': 'l1', 'to_node': 's1',
                        'up': True, 'mbps': 50.0, 'drops': 0}, items[0]
    assert items[1]['reporter'] == 'l1' and items[1]['from_node'] == 's1'
    with tempfile.NamedTemporaryFile('w', delete=False) as f:
        f.write('export OS_AUTH_URL=http://ctl/identity\nOS_USERNAME="agent"\n'
                "export OS_PASSWORD='p=w'\nOS_PROJECT_NAME=admin\n# comment\n")
    try:
        c = load_credentials(f.name)
    finally:
        os.unlink(f.name)
    assert c == {'OS_AUTH_URL': 'http://ctl/identity', 'OS_USERNAME': 'agent',
                 'OS_PASSWORD': 'p=w', 'OS_PROJECT_NAME': 'admin'}, c
    assert (Neutron('http://ctl/networking/', c).url ==
            'http://ctl/networking/v2.0/srv6_link_states')
    # --- VM-pair loads --------------------------------------------------
    # the JSON of nft 1.0.9: 32-bit fields as numbers, 128-bit
    # ones as hex strings
    v4 = json.dumps({'nftables': [{'metainfo': {}}, {'set': {'elem': [
        {'elem': {'val': {'concat': ['s2', 'fc00:0:4:e300::', 173736203,
                                     173736462]},
                  'counter': {'packets': 9615, 'bytes': 13153320}}}]}}]})
    v6 = json.dumps({'nftables': [{'set': {'elem': [
        {'elem': {'val': {'concat': [
            'OVS3', 'fc00:0:3:e300::', '0x20010db8006600000000000000000001',
            '0x20010db8007700000000000000000001']},
            'counter': {'packets': 5, 'bytes': 920}}}]}}]})
    a4, a6 = parse_pair_set(v4, 4), parse_pair_set(v6, 6)
    k4 = ('s2', 'fc00:0:4:e300::', '10.91.1.11', '10.91.2.14')
    k6 = ('OVS3', 'fc00:0:3:e300::', '2001:db8:66::1', '2001:db8:77::1')
    assert a4 == {k4: 13153320} and a6 == {k6: 920}, (a4, a6)
    assert parse_pair_set('{"nftables": [{"set": {}}]}', 4) == {}
    # rates: the first read is a baseline; growth; a reset; a new key
    assert pair_rates(None, a4, 5.0) == {}
    k_new = ('s1', 'fc00:0:3:e300::', '10.91.1.11', '10.91.2.13')
    r = pair_rates({k4: 1000, k6: 5000}, {k4: 1000 + 12500000, k6: 2500,
                                         k_new: 625000}, 5.0)
    assert r == {k4: 20.0, k6: 0.004, k_new: 1.0}, r
    # items: top K per link above the floor; interfaces off the map ignored
    links2 = [('s1', 'l1', 's1'), ('s2', 'l1', 's2'),
              ('OVS1', 'l1', 'ubuntu-ovs1')]
    rates = {('s2', 'sid', '10.0.0.%d' % i, '10.0.1.1'): float(i)
             for i in range(1, 21)}
    rates.update({('s1', 'sid', '10.0.0.1', '10.0.1.1'): 0.5,
                  ('enp1s0', 'sid', '10.0.0.1', '10.0.1.1'): 99.0})
    items = aggregate_items('l1', links2, rates)
    assert len(items) == TOP_K and {x['to_node'] for x in items} == {'s2'}
    assert items[0] == {'reporter': 'l1', 'from_node': 'l1', 'to_node': 's2',
                        'sid': 'sid', 'source_address': '10.0.0.20',
                        'destination_address': '10.0.1.1', 'mbps': 20.0}
    assert min(x['mbps'] for x in items) == 5.0
    assert aggregate_items('l1', links2, rates, top_k=2, floor=19.5) == \
        items[:1]
    # the table: jump chains, never an @th update next to l4proto
    for line in NFT_SCRIPT.splitlines():
        assert not ('l4proto' in line and '@th' in line), line
    assert NFT_SCRIPT.startswith('table inet %s\ndelete table' % NFT_TABLE)
    assert (Neutron('http://ctl/networking', c).base + AGGREGATE_PATH ==
            'http://ctl/networking/v2.0/srv6_aggregate_loads')
    print('srv6-fabric-agent self-test ok')


def main():
    if sys.argv[1:] == ['test']:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--links', required=True,
                    help='link map: <interface> <node> <neighbour>')
    ap.add_argument('--period', type=float, default=5.0,
                    help='seconds per report (default 5)')
    ap.add_argument('--count', type=int, default=0,
                    help='stop after N reports (0 = never)')
    ap.add_argument('--neutron-url',
                    help='Neutron endpoint, e.g. http://ctl/networking')
    ap.add_argument('--credentials',
                    help='openrc-style file with the OS_* credentials')
    ap.add_argument('--aggregates', action='store_true',
                    help='also count and post per-VM-pair loads (nft; '
                         'needs CAP_NET_ADMIN)')
    a = ap.parse_args()
    if bool(a.neutron_url) != bool(a.credentials):
        sys.exit('--neutron-url and --credentials go together')
    links = load_links(a.links)
    if not links or len({me for _, me, _ in links}) != 1 or a.period <= 0:
        sys.exit('the link map must name one node and at least one link; '
                 'period > 0')
    run(links, a.period, a.count,
        Neutron(a.neutron_url, load_credentials(a.credentials))
        if a.neutron_url else None, a.aggregates)


if __name__ == '__main__':
    main()
