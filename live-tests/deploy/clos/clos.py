#!/usr/bin/env python3
"""clos.py <command> [args]: everything derived from clos.topo (PLAN-CLOS-TESTBED.md).

  nodes [role]   name role id vm mgmt, one node per line (CLOS_CLOUD=ovn: the
                 OVN VM of a shared host slot)
  vms            name vm, every VM of every cloud (host slots: ML2 and OVN)
  nets           netname bridge, one link per line
  nics <node>    netname mac, the node's fabric NICs
  boot <node>    the node's /usr/local/sbin/clos-routes.sh (stdout)
  links <node>   the fabric agent's link map of a spine/leaf (stdout):
                 <interface> <this node> <neighbour>, in the plugin's names
  te             the SRv6 plugin's [srv6] topology_file (stdout): every node with
                 its locator (hosts under their Neutron host name = VM name of
                 the current cloud) and every link with its capacity
  shape [off]    mac-server's /usr/local/sbin/clos-shape.sh (stdout): tc tbf at
                 each link's capacity on both of its taps (off: remove them)
  test           self-check of the generator on clos.topo

Addressing (D5): link i = fd01:<i>::/64 (a = ::1, b = ::2), NIC MAC
52:54:00:c1:<i>:<01 a | 02 b>, NIC named after the peer; loopback fd00:cc::<id>
on dummy clos0; locator fc00:0:<id>::/48. IPv4 (PLAN-PATH-COMPUTATION, user
2026-11-01: the VXLAN/Geneve endpoints of legacy, dvr and ovn stay IPv4): link
i = 10.254.<i>.0/30 (a = .1, b = .2), loopback 10.255.0.<id as hex>/32. Routing (D6): static ECMP over every
shortest-path next hop to every other locator, loopback and link prefix -- what
an IGP would compute, with no IGP.
"""
import collections
import os
import sys

# CLOS_TOPO: a variant of the topology (e.g. one spine slowed down, PLAN-MODE-EVALUATION B6)
TOPO = os.environ.get('CLOS_TOPO') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clos.topo')


def load(path=TOPO):
    nodes, links = {}, []
    for line in open(path):
        f = line.split('#')[0].split()
        if not f:
            continue
        if f[0] == 'node':
            nodes[f[1]] = dict(name=f[1], role=f[2], id=f[3], vm=f[4], mgmt=f[5],
                               vms=[f[4]] + f[6:7])
            if os.environ.get('CLOS_CLOUD') == 'ovn' and len(f) == 8:
                nodes[f[1]].update(vm=f[6], mgmt=f[7])
        elif f[0] == 'link':
            assert f[1] in nodes and f[2] in nodes, line
            links.append(dict(i=len(links) + 1, a=f[1], b=f[2], cap=int(f[3])))
    assert len(links) < 256  # one MAC byte per link
    return nodes, links


def v4_lo(node):
    """IPv4 loopback: the id read as hex (hosts 1-8, spines a1/a2 = 161/162, leaves b1-b6 = 177-182)."""
    return '10.255.0.%d' % int(node['id'], 16)


def ends4(link, node):
    """(this node's IPv4 link address, the peer's)."""
    i = link['i']
    me, peer = (1, 2) if node == link['a'] else (2, 1)
    return '10.254.%d.%d' % (i, me), '10.254.%d.%d' % (i, peer)


def ends(link, node):
    """(this node's address, peer, peer's address, MAC of this node's NIC)."""
    i = link['i']
    if node == link['a']:
        return 'fd01:%x::1' % i, link['b'], 'fd01:%x::2' % i, '52:54:00:c1:%02x:01' % i
    return 'fd01:%x::2' % i, link['a'], 'fd01:%x::1' % i, '52:54:00:c1:%02x:02' % i


def netname(link):
    return 'clos-%s%s' % (link['a'], link['b'])


def adjacency(links):
    adj = collections.defaultdict(dict)  # node -> peer -> link
    for l in links:
        adj[l['a']][l['b']] = l
        adj[l['b']][l['a']] = l
    return adj


def distances(adj, targets):
    """Hop count from every node to the nearest node in targets (BFS)."""
    d = {t: 0 for t in targets}
    q = list(targets)
    for u in q:
        for v in adj[u]:
            if v not in d:
                d[v] = d[u] + 1
                q.append(v)
    return d


def routes(node, nodes, links):
    """[(prefix, [(via, dev), ...])] for everything not on this node."""
    adj = adjacency(links)
    dests = []
    for n in nodes.values():
        if n['name'] != node:
            dests.append(('fc00:0:%s::/48' % n['id'], [n['name']]))
            dests.append(('fd00:cc::%s/128' % n['id'], [n['name']]))
    for l in links:
        if node not in (l['a'], l['b']):
            dests.append(('fd01:%x::/64' % l['i'], [l['a'], l['b']]))
    for n in nodes.values():
        if n['name'] != node:
            dests.append((v4_lo(n) + '/32', [n['name']]))
    for l in links:
        if node not in (l['a'], l['b']):
            dests.append(('10.254.%d.0/30' % l['i'], [l['a'], l['b']]))
    out = []
    for prefix, owners in dests:
        d = distances(adj, owners)
        hops = []
        for peer in sorted(adj[node]):
            if d.get(peer, 1 << 30) == d[node] - 1:
                link = adj[node][peer]
                via = ends(link, node)[2] if ':' in prefix else ends4(link, node)[1]
                hops.append((via, peer))
        out.append((prefix, hops))
    return out


def boot_script(node, nodes, links):
    me = nodes[node]
    mine = [l for l in links if node in (l['a'], l['b'])]
    s = ['#!/bin/bash',
         '# clos-routes.sh for %s, generated by live-tests/deploy/clos/clos.py from clos.topo.' % node,
         '# Do not edit here: change clos.topo and push again (clos.sh push).',
         '# Renames each fabric NIC (found by MAC) after its peer, addresses it, sets the',
         '# SRv6 sysctls and installs the static ECMP routes (PLAN-CLOS-TESTBED.md D5, D6).',
         '# Idempotent; run at boot by clos-routes.service, before devstack@q-agt.',
         'set -u',
         'rc=0',
         '# ECMP hash: IPv6 carries only SRv6 here, which has no ports, so policy 0',
         '# (addresses + flow label; the agent derives the label from the inner flow);',
         '# IPv4 carries VXLAN/Geneve, whose entropy is the outer UDP source port: policy 1.',
         'sysctl -qw net.ipv6.conf.all.forwarding=1 net.ipv6.conf.all.seg6_enabled=1 \\',
         '    net.ipv6.conf.default.seg6_enabled=1 net.ipv6.fib_multipath_hash_policy=0 \\',
         '    net.ipv4.fib_multipath_hash_policy=1%s' % (' net.ipv4.ip_forward=1' if me['role'] != 'host' else ''),
         'nic() {  # <mac> <name> <address> <v4 address>: rename, sysctls, MTU, addresses',
         '    local cur',
         '    cur=$(ip -br link | awk -v m="$1" \'$3 == m {print $1; exit}\')',
         '    [ -n "$cur" ] || { echo "no NIC with MAC $1 (for $2)" >&2; rc=1; return; }',
         '    if [ "$cur" != "$2" ]; then ip link set dev "$cur" down; ip link set dev "$cur" name "$2"; fi',
         '    for k in accept_ra=0 accept_dad=0 seg6_enabled=1; do',
         '        echo "${k#*=}" > /proc/sys/net/ipv6/conf/$2/${k%=*}',
         '    done',
         '    echo 2 > /proc/sys/net/ipv4/conf/$2/rp_filter   # loose: ECMP returns on either uplink',
         '    ip link set dev "$2" mtu 9000 up',
         '    ip -6 addr replace "$3/64" dev "$2" nodad',
         '    ip -4 addr replace "$4/30" dev "$2"',
         '}']
    for l in mine:
        addr, peer, _, mac = ends(l, node)
        s.append('nic %s %s %s %s' % (mac, peer, addr, ends4(l, node)[0]))
    s += ['ip link show clos0 >/dev/null 2>&1 || ip link add clos0 type dummy',
          'ip link set dev clos0 up',
          'ip -6 addr replace fd00:cc::%s/128 dev clos0' % me['id'],
          'ip -4 addr replace %s/32 dev clos0' % v4_lo(me),
          'r() { ip -6 route replace "$@" || { echo "route failed: $*" >&2; rc=1; }; }',
          'r4() { ip -4 route replace "$@" || { echo "route failed: $*" >&2; rc=1; }; }']
    for prefix, hops in routes(node, nodes, links):
        s.append('%s %s %s' % ('r' if ':' in prefix else 'r4', prefix,
                               ' '.join('nexthop via %s dev %s' % h for h in hops)))
    s.append('r unreachable fc00::/16')
    if me['role'] in ('spine', 'leaf'):
        # this node's uN on the whole /48, as the agent installs it on a host.
        # dev clos0 (the dummy loopback, always up), not a fabric NIC: a route
        # on a NIC is flushed when that NIC goes down, and the node would stop
        # processing its own SID on every path (PLAN-LINK-STATE-TE step 4,
        # LOG op 12). Not lo: seg6local on lo rejects (underlay autodetect).
        s.append('r fc00:0:%s::/48 encap seg6local action End flavors next-csid '
                 'lblen 32 nflen 16 dev clos0' % me['id'])
    s.append('exit $rc')
    return '\n'.join(s) + '\n'


def shape_script(links, off=False):
    """tbf on the host side of every link: a tap's egress is what enters its VM,
    so shaping both taps of a link shapes both directions. Taps are found by
    MAC at run time (libvirt names them vnetN in start order; tap MAC = guest
    MAC with fe: in front). burst 32 kB > the 9000-byte MTU (tbf splits GSO
    packets); latency 20 ms bounds the queue (~250 kB at 100 Mbit/s), so a
    congested link shows as delay and drops, not as an endless buffer."""
    s = ['#!/bin/bash',
         '# clos-shape.sh, generated by live-tests/deploy/clos/clos.py %sfrom clos.topo.' % ('shape off ' if off else ''),
         '# Idempotent; run by clos.sh shape and by the libvirt qemu hook on VM start.',
         'tap() { grep -lx "$1" /sys/class/net/vnet*/address 2>/dev/null | cut -d/ -f5; }',
         'rc=0']
    for l in links:
        for end in (1, 2):
            mac = 'fe:54:00:c1:%02x:%02x' % (l['i'], end)
            if off:
                cmd = 'tc qdisc del dev "$t" root 2>/dev/null; true'
            else:
                cmd = ('tc qdisc replace dev "$t" root tbf rate %dmbit burst 32kb latency 20ms || rc=1'
                       % l['cap'])
            s.append('t=$(tap %s); [ -n "$t" ] && { %s; }   # %s-%s end %d' % (mac, cmd, l['a'], l['b'], end))
    s.append('exit $rc')
    return '\n'.join(s) + '\n'


def te_name(nodes, n):
    """A node as the SRv6 plugin names it: hosts by Neutron host (VM) name."""
    return nodes[n]['vm'] if nodes[n]['role'] == 'host' else n


def agent_links(node, nodes, links):
    """The fabric agent's link map (PLAN-LINK-STATE-TE step 1): which NIC of
    this node leads to which neighbour, in the plugin's names, so that what the
    agent reports matches the links of the topology_file."""
    s = ['# srv6-fabric-agent link map for %s, generated by live-tests/deploy/clos/clos.py' % node,
         '# links %s from clos.topo. <interface> <this node> <neighbour>' % node]
    for l in links:
        if node in (l['a'], l['b']):
            peer = ends(l, node)[1]   # the NIC is named after its peer (boot_script)
            s.append('%s %s %s' % (peer, te_name(nodes, node), te_name(nodes, peer)))
    return '\n'.join(s) + '\n'


def te_topology(nodes, links):
    """The plugin's view of the fabric (networking_srv6_agent/services/pce.py)."""
    name = lambda n: te_name(nodes, n)
    s = ['# SRv6 plugin topology_file, generated by live-tests/deploy/clos/clos.py te',
         '# from clos.topo (PLAN-PATH-COMPUTATION). node <name> <locator>; link <a> <b> <Mbit/s>.']
    s += ['node %s fc00:0:%s::/48' % (name(n), nodes[n]['id']) for n in nodes]
    s += ['link %s %s %d' % (name(l['a']), name(l['b']), l['cap']) for l in links]
    return '\n'.join(s) + '\n'


def selftest():
    nodes, links = load()
    assert len(links) == 24 and len(nodes) == 14
    r = dict(routes('OVS1', nodes, links))
    assert [d for _, d in r['fc00:0:3::/48']] == ['l1', 'l2'], r['fc00:0:3::/48']
    assert [d for _, d in r['fc00:0:2::/48']] == ['l1', 'l2']          # intra-rack via both leaves
    r = dict(routes('s1', nodes, links))
    assert [d for _, d in r['fc00:0:3::/48']] == ['l3', 'l4']
    r = dict(routes('l1', nodes, links))
    assert [d for _, d in r['fc00:0:3::/48']] == ['s1', 's2']
    assert [d for _, d in r['fd00:cc::1/128']] == ['OVS1']
    assert [d for _, d in r['fd01:d::/64']] == ['s1', 's2']             # link l3-OVS3
    # every node reaches every prefix that is not its own
    for n in nodes:
        for p, hops in routes(n, nodes, links):
            assert hops, (n, p)
    # 8 equal-cost shortest paths between the racks: 2 first leaves x 2 spines x 2 last leaves
    adj = adjacency(links)
    def count(u, dst, d):
        return 1 if u == dst else sum(count(v, dst, d) for v in adj[u] if d[v] == d[u] - 1)
    assert count('OVS1', 'OVS3', distances(adj, ['OVS3'])) == 8
    assert count('OVS2', 'OVS4', distances(adj, ['OVS4'])) == 8
    assert count('OVS1', 'OVS5', distances(adj, ['OVS5'])) == 8           # rack A -> rack C
    assert count('OVS5', 'OVS6', distances(adj, ['OVS6'])) == 2           # intra-rack C
    assert nodes['OVS4']['vms'] == ['ubuntu-ovs4', 'ubuntu-ovn4'] and nodes['s1']['vms'] == ['ale-s1']
    r = dict(routes('OVS1', nodes, links))
    assert [d for _, d in r['10.255.0.3/32']] == ['l1', 'l2'], r['10.255.0.3/32']
    assert [v for v, _ in r['10.255.0.3/32']] == ['10.254.9.1', '10.254.10.1']
    assert [d for _, d in dict(routes('s1', nodes, links))['10.255.0.4/32']] == ['l3', 'l4']
    assert v4_lo(nodes['s1']) == '10.255.0.161' and v4_lo(nodes['l4']) == '10.255.0.180'
    s = boot_script('l1', nodes, links)
    assert 'nic 52:54:00:c1:01:02 s1 fd01:1::2 10.254.1.2' in s and 'action End flavors next-csid' in s
    assert 'nflen 16 dev clos0' in s           # the SID survives any fabric NIC going down
    assert 'r4 10.255.0.1/32 nexthop via 10.254.9.2 dev OVS1' in s and 'ip_forward=1' in s
    assert 'ip_forward' not in boot_script('OVS1', nodes, links)
    sh = shape_script(links)
    assert 'fe:54:00:c1:01:01' in sh and 'rate 100mbit' in sh and 'rate 1000mbit' in sh
    assert sh.count('tbf rate') == 2 * len(links) and 'tbf' not in shape_script(links, off=True)
    te = te_topology(nodes, links)
    assert 'node ubuntu-ovs1 fc00:0:1::/48' in te and 'node s1 fc00:0:a1::/48' in te
    assert 'link s1 l1 100' in te and 'link l1 ubuntu-ovs1 1000' in te
    assert 'seg6local' not in boot_script('OVS1', nodes, links)
    al = agent_links('l1', nodes, links).splitlines()[2:]
    assert al == ['s1 l1 s1', 's2 l1 s2', 'OVS1 l1 ubuntu-ovs1', 'OVS2 l1 ubuntu-ovs2'], al
    assert len(agent_links('s1', nodes, links).splitlines()) == 2 + 6
    print('clos.py self-test ok')


def main(argv):
    nodes, links = load()
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'nodes':
        for n in nodes.values():
            if len(argv) < 3 or n['role'] in argv[2:]:
                print(n['name'], n['role'], n['id'], n['vm'], n['mgmt'])
    elif cmd == 'vms':
        for n in nodes.values():
            for vm in n['vms']:
                print(n['name'], vm)
    elif cmd == 'nets':
        for l in links:
            print(netname(l), 'br-%s%s' % (l['a'], l['b']))
    elif cmd == 'nics':
        for l in links:
            if argv[2] in (l['a'], l['b']):
                print(netname(l), ends(l, argv[2])[3])
    elif cmd == 'boot':
        sys.stdout.write(boot_script(argv[2], nodes, links))
    elif cmd == 'te':
        sys.stdout.write(te_topology(nodes, links))
    elif cmd == 'links':
        sys.stdout.write(agent_links(argv[2], nodes, links))
    elif cmd == 'shape':
        sys.stdout.write(shape_script(links, off=argv[2:3] == ['off']))
    elif cmd == 'test':
        selftest()
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
