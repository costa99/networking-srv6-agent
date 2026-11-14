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

"""labgen.py <command> [node]: everything lab.sh derives from lab.conf.

Input (environment, set by lab.sh from lab.conf):
  LAB_HOSTS     "name[=address] ..."  the OpenStack nodes, controller first
  LAB_FABRIC    none, or a topology file (see fabric.topo.example)
  LAB_MGMT_NET  "192.168.150"         VM mode: the management /24

Commands:
  nodes            name role id mgmt-address mgmt-mac, one node per line
  nets             libvirt-network bridge, one per fabric link
  nics <node>      libvirt-network mac, the node's fabric NICs
  underlay <node>  the node's /usr/local/sbin/lab-underlay.sh
  topology         the plugin's [srv6] topology_file
  links <node>     the fabric agent's link map of a switch
  test             self-check, no system access

Addressing. Node ids: hosts 1.., switches 0x101... Locator fc00:0:<id>::/48
(one /32 block, as microSID needs). Underlay address fd00:1ab::<id>: on a flat
lab a /64 on the NIC holding the management address, on a fabric a /128 on the
dummy lab0. Fabric link i: fd00:1ab:<i>::/64 (a = ::1, b = ::2), NIC named
after its peer, MTU 9000. Routing: static ECMP over every shortest-path next
hop, what an IGP would compute.
"""

import collections
import os
import re
import sys

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,14}$')   # also a NIC name


class LabError(Exception):
    pass


def load(env=os.environ):
    """({name: node}, [link]) from LAB_HOSTS and LAB_FABRIC."""
    net = env.get('LAB_MGMT_NET', '192.168.150')
    nodes = collections.OrderedDict()
    for i, word in enumerate(env.get('LAB_HOSTS', '').split(), 1):
        name, _, addr = word.partition('=')
        nodes[name] = dict(name=name, role='host', id=i,
                           mgmt=addr or '%s.%d' % (net, 10 + i))
    if not nodes:
        raise LabError('HOSTS is empty')
    links = []
    fabric = env.get('LAB_FABRIC', 'none')
    if fabric != 'none':
        switches = 0
        for n, line in enumerate(open(fabric), 1):
            f = line.split('#')[0].split()
            where = '%s:%d' % (fabric, n)
            if not f:
                continue
            if f[0] == 'switch' and len(f) == 2:
                switches += 1
                nodes[f[1]] = dict(name=f[1], role='switch', id=0x100 + switches,
                                   mgmt='%s.%d' % (net, 100 + switches))
            elif f[0] == 'link' and len(f) == 4:
                a, b = f[1], f[2]
                if a not in nodes or b not in nodes or a == b:
                    raise LabError('%s: link %s-%s: both ends must be HOSTS or '
                                   'switches declared above' % (where, a, b))
                if any({a, b} == {l['a'], l['b']} for l in links):
                    raise LabError('%s: second link %s-%s' % (where, a, b))
                links.append(dict(i=len(links) + 1, a=a, b=b, cap=int(f[3])))
            else:
                raise LabError('%s: expected "switch <name>" or '
                               '"link <a> <b> <Mbit/s>"' % where)
        if len(links) > 255:
            raise LabError('at most 255 links')
        adj = adjacency(links)
        for name in nodes:
            if not adj[name]:
                raise LabError('%s has no fabric link' % name)
        for name in nodes:
            routes(name, nodes, links)      # raises on a partitioned fabric
    for name in nodes:
        if not NAME.match(name):
            raise LabError('%r: use up to 15 letters, digits, - or _' % name)
    return nodes, links


def mgmt_mac(node):
    return '52:54:00:1a:%02x:%02x' % (node['id'] >> 8, node['id'] & 0xff)


def locator(node):
    return 'fc00:0:%x::/48' % node['id']


def lo(node):
    return 'fd00:1ab::%x' % node['id']


def ends(link, me):
    """(my address, peer, peer address, my NIC's MAC) on a fabric link."""
    i = link['i']
    if me == link['a']:
        return 'fd00:1ab:%x::1' % i, link['b'], 'fd00:1ab:%x::2' % i, \
            '52:54:00:1b:%02x:01' % i
    return 'fd00:1ab:%x::2' % i, link['a'], 'fd00:1ab:%x::1' % i, \
        '52:54:00:1b:%02x:02' % i


def adjacency(links):
    adj = collections.defaultdict(dict)       # node -> peer -> link
    for l in links:
        adj[l['a']][l['b']] = l
        adj[l['b']][l['a']] = l
    return adj


def routes(me, nodes, links):
    """[(prefix, [(via, dev)])] for every remote prefix: all next hops on a
    shortest path (BFS from the prefix's owners)."""
    adj = adjacency(links)
    dests = [(p(n), [n['name']]) for n in nodes.values() if n['name'] != me
             for p in (locator, lambda n: lo(n) + '/128')]
    dests += [('fd00:1ab:%x::/64' % l['i'], [l['a'], l['b']])
              for l in links if me not in (l['a'], l['b'])]
    out = []
    for prefix, owners in dests:
        dist = {o: 0 for o in owners}
        queue = list(owners)
        for u in queue:
            # a host is never transit, even when dual-homed
            if nodes[u]['role'] == 'host' and u not in owners:
                continue
            for v in adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    queue.append(v)
        if me not in dist:
            raise LabError('%s has no path to %s' % (me, prefix))
        hops = [(ends(adj[me][p], me)[2], p) for p in sorted(adj[me])
                if dist.get(p, 1 << 30) == dist[me] - 1 and
                (p in owners or nodes[p]['role'] != 'host')]
        out.append((prefix, hops))
    return out


HEAD = '''#!/bin/bash
# lab-underlay.sh for %(name)s, generated by lab/labgen.py. Do not edit here:
# change lab.conf (or the topology file) and run `lab.sh underlay` again.
# Idempotent; run at boot by lab-underlay.service, before the SRv6 agent.
set -u
rc=0
r() { ip -6 route replace "$@" || { echo "route failed: $*" >&2; rc=1; }; }
seg6() { echo 1 > "/proc/sys/net/ipv6/conf/$1/seg6_enabled"; }
'''


def underlay_script(me, nodes, links):
    node = nodes[me]
    s = [HEAD % node]
    if not links:
        # flat: one shared segment, the NIC that holds the management address
        s.append('dev=$(ip -o -4 addr show | awk -v a=%s \'{split($4, p, "/")} '
                 'p[1] == a {print $2; exit}\')' % node['mgmt'])
        s.append('[ -n "$dev" ] || { echo "no NIC holds %s" >&2; exit 1; }'
                 % node['mgmt'])
        s.append('seg6 all; seg6 "$dev"')
        s.append('ip -6 addr replace %s/64 dev "$dev" nodad' % lo(node))
        for peer in nodes.values():
            if peer['name'] != me:
                s.append('r %s via %s dev "$dev"' % (locator(peer), lo(peer)))
    else:
        s.append('seg6 all')
        if node['role'] == 'switch':
            s.append('sysctl -qw net.ipv6.conf.all.forwarding=1')
        s.append('''nic() {  # <mac> <name> <address>: find by MAC, rename, address
    local cur
    cur=$(ip -br link | awk -v m="$1" '$3 == m {print $1; exit}')
    [ -n "$cur" ] || { echo "no NIC with MAC $1 (link to $2)" >&2; rc=1; return; }
    if [ "$cur" != "$2" ]; then ip link set dev "$cur" down; ip link set dev "$cur" name "$2"; fi
    echo 0 > "/proc/sys/net/ipv6/conf/$2/accept_ra"; seg6 "$2"
    ip link set dev "$2" mtu 9000 up
    ip -6 addr replace "$3/64" dev "$2" nodad
}''')
        for l in links:
            if me in (l['a'], l['b']):
                addr, peer, _, mac = ends(l, me)
                s.append('nic %s %s %s' % (mac, peer, addr))
        s.append('ip link show lab0 >/dev/null 2>&1 || ip link add lab0 type dummy')
        s.append('ip link set dev lab0 up')
        s.append('ip -6 addr replace %s/128 dev lab0' % lo(node))
        for prefix, hops in routes(me, nodes, links):
            s.append('r %s %s' % (prefix, ' '.join(
                'nexthop via %s dev %s' % h for h in hops)))
        if node['role'] == 'switch':
            # a switch is a TE waypoint: its uN (usid) and End SID (encap),
            # on lab0, which never goes down (a route on a NIC would vanish
            # with it; seg6local on lo rejects)
            loc = locator(node)[:-len('::/48')]
            s.append('r %s encap seg6local action End flavors next-csid '
                     'lblen 32 nflen 16 dev lab0' % locator(node))
            s.append('r %s:fff0::/128 encap seg6local action End dev lab0'
                     % loc)
    # SID space this lab does not own never leaks to the default route
    s.append('r unreachable fc00::/16')
    s.append('exit $rc')
    return '\n'.join(s) + '\n'


def topology(nodes, links):
    s = ['# SRv6 plugin topology_file, generated by lab/labgen.py.',
         '# node <name> <locator>; link <a> <b> <Mbit/s per direction>.']
    s += ['node %s %s' % (n['name'], locator(n)) for n in nodes.values()]
    s += ['link %s %s %d' % (l['a'], l['b'], l['cap']) for l in links]
    return '\n'.join(s) + '\n'


def agent_links(me, links):
    s = ['# srv6-fabric-agent link map of %s, generated by lab/labgen.py.' % me,
         '# <interface> <this node> <neighbour>']
    for l in links:
        if me in (l['a'], l['b']):
            peer = ends(l, me)[1]
            s.append('%s %s %s' % (peer, me, peer))    # NIC named after peer
    return '\n'.join(s) + '\n'


def selftest():
    import tempfile
    flat = dict(LAB_HOSTS='c1 c2=10.0.0.7 c3')
    nodes, links = load(flat)
    assert [n['mgmt'] for n in nodes.values()] == \
        ['192.168.150.11', '10.0.0.7', '192.168.150.13']
    s = underlay_script('c2', nodes, links)
    assert 'awk -v a=10.0.0.7' in s and 'fd00:1ab::2/64' in s
    assert 'r fc00:0:3::/48 via fd00:1ab::3 dev "$dev"' in s
    assert 'fc00:0:2::/48' not in s and 'seg6local' not in s
    with tempfile.NamedTemporaryFile('w', suffix='.topo') as f:
        f.write('switch s1\nswitch s2\nswitch l1\nswitch l2\n'
                'link s1 l1 1000\nlink s1 l2 1000\nlink s2 l1 1000\n'
                'link s2 l2 1000  # comment\n'
                'link c1 l1 1000\nlink c2 l1 1000\nlink c3 l2 1000\n')
        f.flush()
        nodes, links = load(dict(LAB_HOSTS='c1 c2 c3', LAB_FABRIC=f.name))
    assert nodes['s1']['id'] == 0x101 and nodes['l2']['mgmt'] == '192.168.150.104'
    assert mgmt_mac(nodes['l2']) == '52:54:00:1a:01:04'
    r = dict(routes('c1', nodes, links))
    assert r['fc00:0:3::/48'] == [('fd00:1ab:5::2', 'l1')]
    r = dict(routes('l1', nodes, links))
    assert [d for _, d in r['fc00:0:3::/48']] == ['s1', 's2']    # ECMP
    assert [d for _, d in r['fd00:1ab::1/128']] == ['c1']
    assert [d for _, d in r['fd00:1ab:7::/64']] == ['s1', 's2']  # link c3-l2
    for n in nodes:
        assert all(h for _, h in routes(n, nodes, links)), n
    # dual-homed c1 (l1 + l2): l1 -> l2 goes over the spines, never via c1
    dual = links + [dict(i=len(links) + 1, a='c1', b='l2', cap=1000)]
    r = dict(routes('l1', nodes, dual))
    assert [d for _, d in r['fc00:0:104::/48']] == ['s1', 's2']
    assert [d for _, d in r['fc00:0:1::/48']] == ['c1']
    s = underlay_script('s1', nodes, links)
    assert 'nic 52:54:00:1b:01:01 l1 fd00:1ab:1::1' in s
    assert 'r fc00:0:101::/48 encap seg6local action End flavors' in s
    assert 'r fc00:0:101:fff0::/128 encap seg6local action End dev lab0' in s
    assert 'forwarding=1' in s
    s = underlay_script('c1', nodes, links)
    assert 'seg6local' not in s and 'forwarding' not in s
    assert 'nic 52:54:00:1b:05:01 l1 fd00:1ab:5::1' in s
    t = topology(nodes, links)
    assert 'node c1 fc00:0:1::/48' in t and 'node l2 fc00:0:104::/48' in t
    assert 'link c3 l2 1000' in t
    assert agent_links('l1', links).splitlines()[2:] == \
        ['s1 l1 s1', 's2 l1 s2', 'c1 l1 c1', 'c2 l1 c2']
    for bad in ('link c1 c9 10\n', 'switch s1\nlink c1 s1 1\nlink s1 c1 1\n',
                'switch s1\n', 'node x\n'):
        with tempfile.NamedTemporaryFile('w') as f:
            f.write(bad)
            f.flush()
            try:
                load(dict(LAB_HOSTS='c1', LAB_FABRIC=f.name))
            except LabError:
                continue
            raise AssertionError('accepted: %r' % bad)
    print('labgen.py self-test ok')


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'test':
        return selftest()
    try:
        nodes, links = load()
    except (LabError, OSError, ValueError) as e:
        sys.exit('lab: %s' % e)
    arg = argv[2] if len(argv) > 2 else None
    if arg is not None and arg not in nodes:
        sys.exit('lab: no node %s' % arg)
    if cmd == 'nodes':
        for n in nodes.values():
            print(n['name'], n['role'], n['id'], n['mgmt'], mgmt_mac(n))
    elif cmd == 'nets':
        for l in links:
            print('lab-link%d labbr%d' % (l['i'], l['i']))
    elif cmd == 'nics' and arg:
        for l in links:
            if arg in (l['a'], l['b']):
                print('lab-link%d' % l['i'], ends(l, arg)[3])
    elif cmd == 'underlay' and arg:
        sys.stdout.write(underlay_script(arg, nodes, links))
    elif cmd == 'topology':
        sys.stdout.write(topology(nodes, links))
    elif cmd == 'links' and arg:
        sys.stdout.write(agent_links(arg, links))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
