# P5 gate — two nodes on networking-srv6-agent (2026-09-10)

**Goal.** Show that VMs on different compute nodes, in two different networks with no router
between them, communicate through an SRv6 domain. Run on the testbed after both nodes were
re-stacked on `networking-srv6-agent` (`P5-PLAN.md` §7, §9).

**Raw log:** `implementation/srv6-plugin/evidence/p5-two-node-srv6-agent.txt`, every command and
its output.

## Verdict so far

| Check | Result |
|---|---|
| Plugin, extensions, agents, locators (control plane) | **PASS** |
| Domain create and network attach as a plain tenant; `sid_function` hidden from it | **PASS** |
| Kernel state on both nodes: VRF, seal, decap SID, gateway ports, encap routes | **PASS** |
| Seal (§8.6): cross-domain and underlay lookups from a tenant gateway port | **PASS** — all `No route to host` |
| Delete: VRF, SID, gateway ports gone and table flushed, on both nodes | **PASS** |
| IPv6 underlay, both directions (configured on node 2 by the user, part 2) | **PASS** |
| VM → VM ping across nodes, SRH on the wire — part 2, before the fix | FAIL: the VRF seal dropped the encapsulated packet (§9) |
| **VM → VM ping across nodes, SRH on the wire — part 3, with the SID-rule fix** | **PASS**: 5/5 both ways, 20 SRH packets captured, `Ip6InNoRoutes` +0 on both nodes (§10) |
| SID rules: installed on both nodes, pruned on detach, restored on re-attach | **PASS** (§10.3, §10.7) |
| Seal with the rules in place: no cross-domain reach in either direction | **PASS** (§10.6) |
| Negative control (a): no underlay route to the peer locator → no traffic | **PASS** (§10.8) |
| Negative control (b): `seg6_enabled=0` on the receiver | **Not a valid control at depth 1**: the ping keeps working, as `evidence/seg6-enabled-semantics.txt` already showed (§10.8) |

Everything SRv6 built is in place on both nodes. The one missing piece is outside the plugin:
node 2 has neither the underlay address `fd00:aa::2` nor a route to node 1's locator. Node 1
already encapsulates toward node 2, but its next hop never resolves (§8).

---

## 1. Deployment state

| | node 1 `ale` (192.168.0.160) | node 2 `ale-XPS-15-9570` (192.168.0.107) |
|---|---|---|
| package | `networking-srv6-agent 0.0.1.dev5`, editable from GitHub `main` | same |
| server | `service_plugins = router,srv6`; `[srv6] function_id_ranges = 1:4095`, `vrf_table_base = 10000` | — (`[srv6] vrf_table_base = 10000` only) |
| agent | `[agent] extensions = srv6`, `tunnel_types = vxlan`; locator `fc00:0:1::/48` on `enp2s0f1` | same, locator `fc00:0:2::/48` on `wlp59s0` |
| q-agt log | `SRv6 agent extension initialised on ale (locator fc00:0:1::/48)` | `... on ale-XPS-15-9570 (locator fc00:0:2::/48)` |
| End SID | `fc00:0:1:fff0:: End dev enp2s0f1` | `fc00:0:2:fff0:: End dev wlp59s0` |
| underlay | `fd00:aa::1/64`, `fc00:0:2::/48 via fd00:aa::2` (added by hand; `srv6-underlay` unit **not** installed) | **none** — no `fd00:aa::2`, no route to `fc00:0:1::/48`, no `srv6-underlay` unit |

Control plane, checked from node 1:
- `openstack extension list --network` shows `srv6` and `srv6-locators`.
- Two OVS agents are alive, one per host.
- Both compute services are up.
- The `neutron-rpc-server` log shows `SRv6 sync_state from ...` for both hosts.

```
GET /v2.0/srv6_locators (admin)
{"srv6_locators": [{"id": "ale", "host": "ale", "locator": "fc00:0:1::/48", ...},
                   {"id": "ale-XPS-15-9570", "host": "ale-XPS-15-9570", "locator": "fc00:0:2::/48", ...}]}
```

## 2. Test topology (project `demo`)

| | network | subnet | instance | host | port |
|---|---|---|---|---|---|
| A | `srv6-net1` (MTU 1400) | 10.90.1.0/24, gw .1 | `vm-n1` 10.90.1.11 | `ale` | port security off |
| B | `srv6-net2` (MTU 1400) | 10.90.2.0/24, gw .1 | `vm-n2` 10.90.2.21 | `ale-XPS-15-9570` | port security off |

There is no router: if the two talk, the domain did it. The instances use cirros 0.6.3 and
`m1.nano`, with keypair `srv6-p5-key` injected through the config drive. The pings are run over
SSH from node 1's DHCP namespaces, `ip netns exec qdhcp-<net> ssh -i p5-key cirros@...`. The key
is on node 1 under `/opt/stack/srv6-evidence/p5-gate/`.

**Fix needed on the way: nova cell mapping.** The first `vm-n2` boot ended in ERROR:
`400 Host 'ale-XPS-15-9570' is not mapped to any cell`. The post-stack step on node 1 had not
been run yet:

```bash
nova-manage cell_v2 discover_hosts --verbose      # [n1]
# Creating host mapping for compute host 'ale-XPS-15-9570' ...
```

After that `vm-n2` booted ACTIVE on node 2. This belongs in the re-stack checklist
(`node2-srv6-agent-local.conf` already lists it at the bottom).

## 3. Domain and associations — as the tenant

All of it as `openrc demo demo`: user `demo`, roles `member`, `reader`, `anotherrole`, no admin.
The REST calls are the ones in `HOWTO-connect-networks.md`.

```
POST /v2.0/srv6_domains {"srv6_domain": {"name": "red"}}
  → {"id": "f2211361-...", "project_id": "cb93ab36...", "name": "red",
     "srv6_behavior": "End.DT46", "networks": []}                  ← no sid_function
POST .../network_associations {srv6-net1} → project_id cb93ab36...  (demo)
POST .../network_associations {srv6-net2} → project_id cb93ab36...
GET  /v2.0/srv6_domains/f2211361-... as demo   → no sid_function
GET  /v2.0/srv6_domains/f2211361-... as admin  → "sid_function": 2394
```

Function id **2394 = 0x95a**: VRF `sv6vrf-2394`, table `12394`, SIDs `fc00:0:1:95a::` and
`fc00:0:2:95a::`.

## 4. Kernel state

**Node 1**, with both VMs ACTIVE:

```
$ ip route show table 12394
unreachable default metric 4278198272                                  ← the seal (v4)
10.90.1.0/24 dev svp-2394-4 proto kernel scope link src 10.90.1.1
10.90.2.0/24 dev svp-2394-5 proto kernel scope link src 10.90.2.1
10.90.2.21  encap seg6 mode encap segs 1 [ fc00:0:2:95a:: ] dev enp2s0f1 scope link
$ ip -6 route show table 12394 | grep unreachable
unreachable default dev lo metric 4278198272 pref medium               ← the seal (v6)
$ ip -6 route show table main | grep seg6local
fc00:0:1:95a::  encap seg6local action End.DT46 vrftable 12394 dev sv6vrf-2394
fc00:0:1:fff0:: encap seg6local action End dev enp2s0f1
$ sudo ovs-vsctl list-ports br-int | grep svp-2394
svp-2394-4
svp-2394-5
```

**Node 2:**

```
$ ip route show table 12394
unreachable default metric 4278198272
10.90.1.11  encap seg6 mode encap segs 1 [ fc00:0:1:95a:: ] dev wlp59s0 scope link
10.90.2.0/24 dev svp-2394-1 proto kernel scope link src 10.90.2.1
$ ip -6 route show table main | grep seg6local
fc00:0:2:95a::  encap seg6local action End.DT46 vrftable 12394 dev sv6vrf-2394
fc00:0:2:fff0:: encap seg6local action End dev wlp59s0
```

Things this shows:

- **The encap routes are there.** Each node points the other node's VM at that node's decap SID.
  Node 2 held `10.90.1.11 → fc00:0:1:95a::` before `vm-n2` even existed. It arrived through the
  incremental port-event path (`routes_updated`) when `vm-n1`'s port went ACTIVE.
- **Gateway ports appear only where they are needed.** Node 1 hosts a DHCP port on both networks,
  so it has gateway ports for both. Node 2 has one only for `srv6-net2`, where `vm-n2` lives. The
  names differ (`svp-2394-5` on node 1, `svp-2394-1` on node 2) because local VLANs are per-node.
  That is expected.
- **The domain gateway answers.** From inside each VM, the default route points at the domain
  gateway, and the gateway replies:
  ```
  vm-n1: default via 10.90.1.1 dev eth0 ; ping 10.90.1.1 → 2/2 received
  vm-n2: default via 10.90.2.1 dev eth0 ; ping 10.90.2.1 → 2/2 received
  ```

## 5. The seal — `MIGRATION-PLAN.md` §8.6, re-measured

The original leak was a tenant's gateway port resolving *another tenant's* SID. To reproduce that
exactly, a second domain `blue` was created in a different project (`alt_demo`) with one network,
`10.91.1.0/24`. That installed its SID `fc00:0:1:9e8::` (fid 2536) on both nodes. The kernel
lookups below run from red's gateway port on node 1, each paired with the same lookup from
`main`. The `main` result shows the target really is routable outside the VRF.

| # | From red (`iif svp-2394-5`) to | inside the VRF | same lookup from `main` |
|---|---|---|---|
| 1 | blue's SID on this node `fc00:0:1:9e8::` | **No route to host** | `End.DT46 vrftable 12536` |
| 2 | node 2's LAN address `192.168.0.107` | **No route to host** | `dev enp2s0f1` |
| 3 | blue's SID on node 2 `fc00:0:2:9e8::` | **No route to host** | `via fd00:aa::2` |
| 4 | node 1's End SID `fc00:0:1:fff0::` | **No route to host** | `End dev enp2s0f1` |
| control | vm-n1 `10.90.1.11` (same domain) | `dev svp-2394-4 table 12394` | — |

Before P5 (evidence/`vrf-fallthrough.txt`), lookup #1 resolved to the other tenant's
`End.DT46` decap. Now every lookup that leaves the domain stops at the `unreachable default`,
while in-domain routing is unchanged.

**Note on `alt_demo`.** DevStack gives the `alt_demo` user the `admin` role in its own project
(its token roles include `admin`). So `blue`'s create response did include `sid_function`. That
is correct behaviour for an admin, not a policy hole: the non-admin `demo` user never sees the
field (§3). For tenant-view tests, use `demo` or a project without admin.

## 6. Delete — P5-PLAN §4.2

`blue` was deleted by `alt_demo` (HTTP 204). Afterwards, on **both** nodes:

```
VRF sv6vrf-2536:             Device "sv6vrf-2536" does not exist.
ip route show table 12536:   []            ← the unreachable default is gone too: flushed
ip -6 route show table 12536:[]
SID fc00:0:{1,2}:9e8:::      []
svp-2536-* on br-int (n1):   []
q-agt: domain_deleted e1056d2c-... ; domain ... (function id 2536) torn down on this node
```

Red's VRF and SIDs were untouched. Without the flush, the deviceless `unreachable default` would
have outlived the VRF and been inherited by the next domain given function id 2536.
`blue-net1` was deleted afterwards.

## 7. Baseline: VM → VM before the underlay exists

```
[vm-n1] ping -c3 -W2 10.90.2.21     → 3 transmitted, 0 received
[n1] tcpdump -ni enp2s0f1 'ip6 and (ip6 proto 43 or icmp6)'   → 0 packets
[n1] ip -6 neigh show fd00:aa::2    → fd00:aa::2 dev enp2s0f1 FAILED
```

Node 1 has the encap route and hands the packet to the IPv6 stack, but the next hop toward node
2's locator is `fd00:aa::2`, which nobody owns. The neighbour entry fails and nothing leaves the
NIC. This is exactly the "correct encap route, silent loss" failure `DESIGN-border-speaker.md`
§9 warns about, and the only thing between here and the gate.

---

## 8. What is left, and how to finish it

> **Update (part 2).** The user did step 1. Step 2 then failed, for a reason inside the plugin:
> see §9.

**1. Configure node 2's underlay.** This needs `sudo` on node 2, which asks for a password. Either:

```bash
sudo ip -6 addr add fd00:aa::2/64 dev wlp59s0
sudo ip -6 route add fc00:0:1::/48 via fd00:aa::1 dev wlp59s0
ping6 -c3 fd00:aa::1
```

or make it survive reboots and Wi-Fi reconnects (recommended; node 1 lacks the unit too):

```bash
sudo bash ~/OpenStackStudy/implementation/srv6-plugin/systemd/install.sh --role node2   # [n2]
sudo bash /path/to/systemd/install.sh --role node1                                      # [n1]
```

**2. Then the remaining steps**, which reuse everything built above. Nothing needs recreating:

- vm-n1 → 10.90.2.21 and vm-n2 → 10.90.1.11, both directions;
- `tcpdump 'ip6 proto 43'` on node 1's `enp2s0f1`, expecting `RT6 (type=4, segleft=0,
  [0]fc00:0:2:95a::)` around `IP 10.90.1.11 > 10.90.2.21`;
- decap counters on node 2: `ip -s link show sv6vrf-2394`;
- the two negative controls from the old HOWTO §8.3: drop the peer-locator route, and set
  `seg6_enabled=0` on the receiver.

## 9. Part 2 — with the underlay: the seal breaks encapsulation

### 9.1 The underlay works

```
[n2] fd00:aa::2/64 on wlp59s0 ; fc00:0:1::/48 via fd00:aa::1 ; ping -6 fd00:aa::1 → 2/2
[n1] ping6 fd00:aa::2 → 2/2 ; neigh fd00:aa::2 lladdr 9c:b6:d0:b8:04:d9 REACHABLE
[n2] seg6_enabled(wlp59s0)=1  all.forwarding=1  vrf.strict_mode=1
```

### 9.2 The pings still fail, and nothing reaches the wire

```
[vm-n1 on n1] ping -c5 10.90.2.21  → 5 transmitted, 0 received
[vm-n2 on n2] ping -c5 10.90.1.11  → 5 transmitted, 0 received
[n1] tcpdump -ni enp2s0f1 'ip6 proto 43'  → 0 packets
```

The tenant packets **do** enter the VRF. `sv6vrf-2394` RX went from 12 to 17 on node 1 (+5) and
from 5 to 10 on node 2 (+5). They die after that.

### 9.3 Cause: the outer IPv6 header is routed inside the VRF, and the seal refuses it

After seg6 encapsulates a *forwarded* packet, the kernel routes the new outer IPv6 header with
the ingress device unchanged. That device is the gateway port, which is enslaved to the VRF. So
the outer lookup for the remote decap SID happens in the **domain's own table**. The old build
never noticed: that lookup missed in the VRF table and fell through to `main` — the same
fall-through §8.6 closed. With the seal, it hits the IPv6 `unreachable default` and the packet
is dropped.

Measured, all read-only:

```
# node 1
$ ip -4 route get 10.90.2.21 from 10.90.1.11 iif svp-2394-4
10.90.2.21 ... encap seg6 mode encap segs 1 [ fc00:0:2:95a:: ] dev enp2s0f1 table 12394   ← inner: OK
$ ip -6 route get fc00:0:2:95a:: from fe80::1 iif svp-2394-4
RTNETLINK answers: No route to host                                                        ← outer, in the VRF
$ ip -6 route get fc00:0:2:95a::
fc00:0:2:95a:: via fd00:aa::2 dev enp2s0f1                                                 ← outer, from main
3 echo requests from vm-n1  →  Ip6InNoRoutes 8 → 11  (delta 3)

# node 2
$ ip -6 route get fc00:0:1:95a:: from fe80::1 iif svp-2394-1   → No route to host
$ ip -6 route get fc00:0:1:95a::                               → via fd00:aa::1 dev wlp59s0
Ip6InNoRoutes = 5  — exactly the 5 echo requests vm-n2 sent
```

One drop per echo request, on both nodes, counted as "no route" by the IPv6 stack. The
encapsulation happens and the outer packet is then unroutable.

**What this means.** The §8.6 seal is correct about what it blocks, but it also blocks the
domain's own traffic. The unit tests could not catch it: they mock the kernel, and nothing in
them models where the outer lookup happens. It is exactly the kind of fact P5's gate exists to
find. §5's probe #3 already showed the symptom (red's port could not reach a SID on node 2); it
was read as the seal working, without checking red's *own* remote SID.

### 9.4 Fix options

| | Change | Keeps the seal? | Verdict |
|---|---|---|---|
| **A** | Per domain, one policy rule per remote node, placed ahead of l3mdev: `ip -6 rule add pref 999 iif sv6vrf-<fid> to <that node's SID for this domain>/128 lookup main` | yes — only this domain's VRF, and only toward this domain's own SIDs | **recommended** |
| B | The same /128s as routes in the VRF table, `via <underlay next hop>` | yes | needs the next hop, which is the underlay's business; breaks when the path is more than one hop |
| C | Drop the IPv6 seal, keep IPv4 | no — a tenant can address another domain's SID again (the original leak was IPv6) | reject |

**A in practice.**
- **Scope:** a packet from red's VRF may leave the VRF only toward red's own decap SIDs on other
  nodes. Addressing them directly can only inject into red itself, which that tenant can already
  reach. Every other SID and underlay address stays behind the seal.
- **Next hop:** `lookup main` needs none; the underlay routing decides, however many hops.
- **Cost:** `domains × (nodes − 1)` rules per node. The agent already knows exactly which remote
  SIDs a domain encapsulates to (the last segment of each route), so it can derive and reconcile
  them the same way it reconciles routes.
- **Rule creation and GC:** delete must remove the rules; the kernel GC must also collect rules
  whose VRF is gone.

**Caveat for P6.** With TE, the outer destination is the *first* segment: a transit node's End
SID, not a decap SID. Letting a VRF reach End SIDs reopens a path. A tenant could hand-craft an
SRH through that End toward another domain's SID. So P6 needs either SRH filtering at the gateway
port or an HMAC (RFC 8754 §5.1 / `seg6 hmac`). Record it in P6 before building TE on top of A.

> **Decided (2026-09-10): edge filtering.** An agent-managed `inet srv6_edge` nftables table on
> the gateway ports drops tenant-originated IPv6 aimed at the SID space, and any tenant-originated
> SRH, before routing. The agent's own encapsulation never passes that point. TE may add End-SID
> rules only while the filter is active. `MIGRATION-PLAN.md` §8.7 has the threat walk-through,
> the ruleset (syntax checked with `nft -c` on node 1) and the P6 gate test.

**Not yet tested.** The one-line rule on node 1 is a live policy-routing change on the testbed,
and it was stopped at the permission prompt. It has **not** been applied. Node 2's counterpart
needs `sudo` on node 2. Confirming A by hand needs:

```bash
# [n1]
sudo ip -6 rule add pref 999 iif sv6vrf-2394 to fc00:0:2:95a::/128 lookup main
# [n2]
sudo ip -6 rule add pref 999 iif sv6vrf-2394 to fc00:0:1:95a::/128 lookup main
# check, on n1: the outer lookup now resolves, the seal still refuses the rest
ip -6 route get fc00:0:2:95a:: from fe80::1 iif svp-2394-4     # → via fd00:aa::2
ip -6 route get fc00:0:1:fff0:: from fe80::1 iif svp-2394-4    # → No route to host
# undo
sudo ip -6 rule del pref 999 iif sv6vrf-2394 to fc00:0:2:95a::/128 lookup main   # [n1]
sudo ip -6 rule del pref 999 iif sv6vrf-2394 to fc00:0:1:95a::/128 lookup main   # [n2]
```

Then repeat 9.2 with the capture: the gate expects `RT6 (... [0]fc00:0:2:95a::)` around
`IP 10.90.1.11 > 10.90.2.21`, and replies in both directions.

### 9.5 Fix A implemented in the agent

Implemented as `P5-PLAN.md` §4.6: `agent/dataplane.py`, plus three privileged wrappers in
`privileged/seg6.py` and `SID_RULE_PRIORITY = 999` in `common/constants.py`. For every domain,
the agent keeps exactly one rule per remote node it encapsulates to:

```
ip -6 rule add pref 999 iif sv6vrf-<fid> to <remote decap SID>/128 lookup main
```

The rules are derived from the installed routes and reconciled from the kernel:
- added on `add_routes`;
- added and pruned on `sync_routes`;
- removed by `delete_domain` before the VRF;
- counted by the orphan GC even when their VRF is gone (`[detached]`).

**P6 guard:** no rule is added toward a transit End SID; the route stays sealed and the agent logs
why (§9.4 caveat).

**Unit-verified:** 367 tests, 0 skipped, flake8 clean. There are 19 new tests:
- one rule per remote node, not per route;
- no rule for local routes;
- kernel spelling vs `format_sid` spelling;
- no rule for a transit first segment;
- no pruning on the incremental path, pruning on a full sync of this VRF's rules only;
- an unreadable rule list prunes nothing;
- delete removes the rules before the device;
- detached rules still count as orphans;
- plus the argv and parser of the wrappers.

**Not yet on the testbed.** Both nodes run the code from GitHub `main`, cloned in
`/opt/stack/networking-srv6-agent`. Deploying means getting the change onto both nodes, restarting
both q-agt, and checking that the rules appear:

```bash
# get the code there: commit + push, then on each node, as stack:
cd /opt/stack/networking-srv6-agent && git pull
# restart q-agt (node 1: kill -9 the MainPID first, confirm the PID changed)
sudo systemctl restart devstack@q-agt
# the first resync (or the domain push) installs:
ip -6 rule show | grep '^999:'
#   999: from all to fc00:0:2:95a:: iif sv6vrf-2394 lookup main     (node 1)
#   999: from all to fc00:0:1:95a:: iif sv6vrf-2394 lookup main     (node 2)
```

Then rerun §9.2 with the capture, and the negative controls.

## 10. Part 3 — restacked with the SID-rule fix: the gate passes

### 10.1 Getting the fix onto the nodes

The user pushed the fix (`4e9485b Implement SID rules ...`) and restacked. **Restacking did not
deploy it.** Devstack reuses an existing clone, and both `/opt/stack/networking-srv6-agent`
checkouts were still at `f908800`. The deployed module lacked `add_sid_rule`: checked by importing
it from outside the repo, since importing from inside the repo picks up the workstation checkout
and gives a false yes. The clone is a detached HEAD with no tracking branch, so `git pull` fails
too. What works:

```bash
cd /opt/stack/networking-srv6-agent
git fetch origin main && git merge --ff-only FETCH_HEAD      # → 4e9485b
# restart q-agt; on node 1 kill -9 the MainPID first, and confirm the PID changed
```

- **node 1** (by me, as `stack`): at `4e9485b`, MainPID 2925219 → 2928280, and the running module
  has `add_sid_rule`.
- **node 2** (by the user): the same, q-agt restarted at 20:07:22.

The restack also dropped nova's host mapping for node 2 again, so `nova-manage cell_v2
discover_hosts` was re-run before any boot.

### 10.2 Fresh fixtures (project `demo`, as in §2)

- `srv6-net1` 10.90.1.0/24 and `srv6-net2` 10.90.2.0/24, MTU 1400, no router.
- `vm-n1` 10.90.1.11 on `ale`, `vm-n2` 10.90.2.21 on `ale-XPS-15-9570`, port security off.
- Domain `red` `411225e7-…` was created and attached **as the tenant `demo`**. Function id
  **56 = 0x38**: table `10056`, SIDs `fc00:0:1:38::` and `fc00:0:2:38::`.

### 10.3 The rules, one per node, exactly as designed

```
[n1] 999:  from all to fc00:0:2:38:: iif sv6vrf-56 lookup main
[n2] 999:  from all to fc00:0:1:38:: iif sv6vrf-56 lookup main
[n2 q-agt] SRv6: domain 411225e7-... may reach fc00:0:1:38:: via main
```

The seal is still in both tables (`unreachable default` for IPv4 and IPv6), next to the connected
subnets and the encap route toward the other node's VM.

### 10.4 The outer lookup now leaves the VRF, and only toward the domain's own SID

```
[n2] ip -6 route get fc00:0:1:38:: from fe80::1 iif svp-56-1    → via fd00:aa::1 dev wlp59s0
[n1] ip -6 route get fc00:0:2:38:: from fe80::1 iif sv6vrf-56   → via fd00:aa::2 dev enp2s0f1
     everything else from red: fc00:0:{1,2}:fff0::, the other node's LAN address → No route to host
```

**A tool caveat on node 1's kernel (6.8).** There, `ip route get ... iif svp-56-4` (the enslaved
gateway port) still answered `No route to host` with the rule present. With `iif sv6vrf-56` (the
VRF master) it resolves. So `route get` on 6.8 does not apply the port → VRF-master mapping when
it matches `iif` rules. Node 2 (kernel 7.0) resolves in both forms. Real packets on node 1 do match
the rule (§10.5). When checking these rules on 6.8, use `iif <vrf>`.

### 10.5 The gate: VM → VM across nodes, SRH on the wire

```
[vm-n1 on n1] ping -c5 10.90.2.21   → 5 transmitted, 5 received, 0% packet loss
[vm-n2 on n2] ping -c5 10.90.1.11   → 5 transmitted, 5 received, 0% packet loss
[n1] tcpdump -ni enp2s0f1 'ip6 proto 43'  → 20 packets (/opt/stack/srv6-evidence/p5-gate/srh-part3.pcap)

IP6 (hlim 63, next-header Routing (43) payload length: 108) fdbd:…:6916 > fc00:0:2:38::: RT6 (len=2, type=4,
    segleft=0, last-entry=0, [0]fc00:0:2:38::) IP 10.90.1.11 > 10.90.2.21: ICMP echo request, seq 1
IP6 (hlim 63, next-header Routing (43) payload length: 108) fd00:aa::2 > fc00:0:1:38::: RT6 (len=2, type=4,
    segleft=0, last-entry=0, [0]fc00:0:1:38::) IP 10.90.2.21 > 10.90.1.11: ICMP echo reply, seq 1

Ip6InNoRoutes delta: n1 +0 (11 → 11), n2 +0 (5 → 5)      ← part 2: +1 per echo request
sv6vrf-56 RX:        n1 3 → 23, n2 3 → 23
```

All four properties the old HOWTO §8.1 asks of the capture hold:
- the `Routing (43)` header is present;
- the segment list has one entry, the peer's SID for this domain;
- the outer destination is inside the peer's locator, not its LAN address;
- the inner packet goes between two subnets with no router between them.

Each node is seen both encapsulating and decapsulating.

One detail: node 1's outer source is `fdbd:…:6916`, a SLAAC address from the home LAN router,
not `fd00:aa::1`. seg6 encapsulation picks the source by ordinary address selection. It works
because the source only needs to be routable back, and node 2 reaches it on-link. For a
predictable source, set `ip sr tunsrc set fd00:aa::1` on each node; that belongs to the underlay
setup, not the plugin.

### 10.6 The rules open nothing across domains

A second domain `blue` (project `alt_demo`, function id 1831 = 0x727) was given one network, which
installed its SIDs. With both domains present on node 1:

| Lookup | Result |
|---|---|
| red (`iif sv6vrf-56`) → red's remote SID `fc00:0:2:38::` | `via fd00:aa::2` |
| red → blue SID on node 1 `fc00:0:1:727::` (also with `iif svp-56-4`) | **No route to host** |
| red → blue SID on node 2 `fc00:0:2:727::` | **No route to host** |
| blue (`iif sv6vrf-1831`) → red's remote SID `fc00:0:2:38::` | **No route to host** — red's rule is scoped to red's VRF |
| blue → red's SID on node 1 `fc00:0:1:38::` | **No route to host** |

Blue has no instances on another node, so it correctly got **no** rule. Deleting blue (HTTP 204,
as `alt_demo`) removed its VRF and SID and flushed table `11831` in both families, on **both**
nodes. Red's rules were untouched. `blue-net1` was deleted afterwards.

### 10.7 The rules follow the routes (reconciliation, live)

```
as demo: DELETE the srv6-net2 association       → HTTP 204
  [n1] rules: []   route to 10.90.2.21: []   gateway ports: svp-56-4 only
  [n1 q-agt] SRv6: domain 411225e7-... no longer encapsulates to fc00:0:2:38::; removing its rule
as demo: re-attach srv6-net2
  [n1] rules: 999: from all to fc00:0:2:38:: iif sv6vrf-56 lookup main
       route: 10.90.2.21 encap seg6 ... segs 1 [ fc00:0:2:38:: ]   gateway ports: svp-56-4 svp-56-5
  [n1 q-agt] SRv6: domain 411225e7-... may reach fc00:0:2:38:: via main
  [vm-n1] ping 10.90.2.21 → 3/3
```

### 10.8 Negative controls

**(a) Remove node 1's underlay route to node 2's locator — PASS.** The encap route stays in the
VRF and the ping fails 0/3. After restoring the route it succeeds 3/3.

A side finding: with the specific route gone, the outer lookup did **not** fail locally. It fell to
`main`'s default route learned from the LAN router's advertisements (`via fe80::ce2d:… proto ra`),
so SRv6 packets for `fc00:0:2::/48` were sent to the home router. The underlay should carry a
catch-all for the SID space, e.g. `unreachable fc00::/16` in `main` on every node. With it, a
missing peer route blackholes locally instead of leaking to the default router. This is for
`systemd/srv6-underlay`, not the agent.

**(b) `seg6_enabled=0` on the receiving interface — not a valid control at depth 1.** With
`net.ipv6.conf.enp2s0f1.seg6_enabled=0` on node 1, `vm-n2 → vm-n1` still got 3/3 replies, with
the SRH seen arriving. `evidence/seg6-enabled-semantics.txt` (2026-09-02, isolated two-namespace
reproduction on the same kernel) already explains why: `seg6_enabled` gates only native SRH
processing of a routing header with Segments Left > 0 (`ipv6_srh_rcv`). It does not gate the
`seg6local` path. `End.DT46` is a route handled at lookup time, and at depth 1 every packet is
decapsulated there. So the old HOWTO §8.3(b) expectation was wrong for this datapath. The sysctl
becomes load-bearing only for transit through a node without an explicit `seg6local` route for the
traversed SID — the P6 case. The value was restored to 1.

### 10.9 Verdict and state

**The P5 gate passes.** Two VMs on different compute nodes, in two different networks with no
router, communicate through an SRv6 domain created by a plain tenant. The SRH is on the wire in
both directions. The VRFs stay sealed against every other domain and the underlay, and the SID
rules are installed, pruned and restored with the routes.

**State left on the testbed:**
- domain `red` (`411225e7-…`) with `srv6-net1` and `srv6-net2`;
- `vm-n1` and `vm-n2`;
- security group `srv6-sg` and keypair `srv6-p5-key`;
- the captures in `/opt/stack/srv6-evidence/p5-gate/` on node 1.

Removed: domain `blue` and `blue-net1`. Changed outside Neutron:
- the nova host mapping for node 2 (`discover_hosts`);
- node 1's `/opt/stack/networking-srv6-agent`, fast-forwarded to `4e9485b`.

## State left on the testbed (after part 2; superseded by §10.9)

Kept for step 8:
- domain `red` (`f2211361-...`) with `srv6-net1` and `srv6-net2`;
- `vm-n1` and `vm-n2`;
- security group `srv6-sg` and keypair `srv6-p5-key`.

Removed: domain `blue` and `blue-net1`.

Changed outside Neutron: the nova host mapping for `ale-XPS-15-9570` (`discover_hosts`).
