# P5 — Agent and dataplane

Detailed plan for phase 5 of `MIGRATION-PLAN.md` §5. Written 2026-09-10, after §8.5 (no admin VRF,
admin-only per-VM TE) and §8.6 (domain VRFs fall through to `main`). **P4 must be complete first**:
the agent in this phase talks to P4's `services/rpc.py`, and its endpoint signatures are pinned by
`P4-PLAN.md` §4.4.

**What P5 delivers.** The per-node half — the kernel dataplane and the OVS L2 agent extension,
ported from `networking-bgpvpn` — plus a devstack plugin, with both testbed nodes re-stacked on the
new package. This is the phase that reproduces the working system, and the first time this package
programs a kernel.

**Gate.** `MIGRATION-PLAN.md` §5 P5: domain create, network association, kernel state, VM-to-VM
ping across nodes, delete, cleanup — compared against `implementation/srv6-plugin/evidence/`, not
judged by eye. Plus the §8.6 probes in `evidence/vrf-fallthrough.txt` now return `unreachable`.

---

## 1. Scope decisions

### 1.1 The port source is `networking-bgpvpn` branch `srv6-te`

For the SRv6 agent, driver and privileged paths, `srv6-te` and `srv6-l3vpn` are identical —
`git log` between them over those paths is empty in both directions. `srv6-sfc` is *behind*, not
ahead: its two extra commits (`0316ca3`, `194b9ca`) re-apply the reconciliation work, but
`git diff srv6-te srv6-sfc` shows it still lacks `_ensure_node_end_sid` and the TE constants — the
divergence `TE-VS-SFC-SRV6.md` §3 recorded. Port from `srv6-te` HEAD, `a83ea04`.

### 1.2 The segment-list consumer moves from P6 to P5

`MIGRATION-PLAN.md` §5 originally put the agent half of TE in P6. It moves here — about ten lines
in `add_routes` (§4.5) — for one reason: `PAYLOAD_VERSION` is an **equality** check on both sides
(`agent_extension.py:246-253`, `rpc.py:107-112`). If P6 had to bump the payload version to add
`segments`, every agent still on P5 code would log a warning and ignore *all* updates — a silent
stall, not a degraded mode. With the consumer already present and `segments` optional under
version 1 (`P4-PLAN.md` §4.4), P6 is server-only and no bump is ever needed.

### 1.3 Deploy by re-stacking, with a devstack plugin

Decided 2026-09-10 over injecting into the existing stack. Costs: ~40 minutes per `stack.sh`, the
demo objects are lost, and node 1 has ~0–1 GB free during the run. Buys: a single-pass,
reproducible deploy; configuration written by devstack's own `iniset` helpers, which removes the
recorded `[agent]` section-split trap (§6); and the neutron-lib fork and `inject-srv6.sh` leave the
deploy path entirely. The repo has no `devstack/` yet — P5 writes it.

---

## 2. Files

```
networking_srv6_agent/
  agent/
    dataplane.py            port of agent/srv6/dataplane.py + §4.1–4.3, 4.5            (~660)
    agent_extension.py      port of agent/srv6/agent_extension.py + §4.3, 4.4          (~380)
  privileged/seg6.py        +3 unprivileged wrappers over ip_route_cmd (§4.1–4.3)       (+40)
  common/constants.py       +VRF_UNREACHABLE_METRIC                                     (+6)
  tests/unit/
    agent/test_dataplane.py         port + new cases                                   (~760)
    agent/test_agent_extension.py   port + new cases + RPC contract test (§5)          (~540)
    privileged/test_seg6.py         +wrapper tests                                     (+50)
devstack/
  settings, plugin.sh                                                                  (~60)
pyproject.toml              uncomment [project.entry-points."neutron.agent.l2.extensions"]
implementation/srv6-plugin/
  node1-srv6-agent-local.conf, node2-srv6-agent-local.conf     (stock conf + 3 lines each)
```

---

## 3. The port

| from (`networking-bgpvpn`, `srv6-te`) | to | change |
|---|---|---|
| `VpnState(vpn_id, …)`, `self.vpns`, `ensure_vpn`, `delete_vpn` | `DomainState(domain_id, …)`, `self.domains`, `ensure_domain`, `delete_domain` | rename |
| `networking_bgpvpn.neutron.privileged.seg6` | `networking_srv6_agent.privileged.seg6` | import (ported in P2) |
| `services.service_drivers.srv6.{config,constants,sid}` | `networking_srv6_agent.common.{config,constants,sid}` | import |
| `[bgpvpn_srv6_agent]`, `[bgpvpn_srv6]` | `[srv6_agent]`, `[srv6]` | config groups (P2 `common/config.py`) |
| `get_topic_name(AGENT, TOPIC_SRV6_BGPVPN, UPDATE)` | `get_topic_name(AGENT, TOPIC_SRV6, UPDATE)` | must match P4's notifier exactly |
| `vpn_updated`, `routes_updated`, `vpn_deleted` | endpoints below | method **and** kwarg names |
| `_apply_vpn` | `_apply_domain` | rename |

The agent endpoints, matching `P4-PLAN.md` §4.4 kwarg for kwarg:

```python
def domain_updated(self, context, domain=None, payload_version=1, **kwargs): ...
def routes_updated(self, context, domain_id=None, add=None, remove=None,
                   payload_version=1, **kwargs): ...
def domain_deleted(self, context, domain_id=None, function_id=None,
                   vrf_table=None, payload_version=1, **kwargs): ...
```

**Unchanged on purpose:** kernel object names (`sv6vrf-<fid>`, `svp-<fid>-<vlan>`,
`common/constants.py:43-49`), so the comparison against `evidence/` stays literal; the sysctl list
and its comments; the enslave-before-address ordering; `_bridge_gateway_ports` reading the bridge
rather than memory; the lock split between `_sync_state` and `_do_sync_state` that fixed the RPC
deadlock.

---

## 4. Five changes beyond the rename

### 4.1 Seal every domain VRF (§8.6)

In `ensure_domain`, right after the VRF is up and **before** the SID route — and so before any
gateway port can be enslaved, since `ensure_gateway_port` requires the state `ensure_domain`
creates:

```python
for family_v6 in (False, True):
    priv_seg6.replace_unreachable_default(vrf_table, family_v6=family_v6)
```

```python
# privileged/seg6.py — unprivileged wrapper, like replace_seg6_route
def replace_unreachable_default(table, family_v6=False):
    args = ['-6'] if family_v6 else []
    args += ['route', 'replace', 'unreachable', 'default',
             'metric', str(constants.VRF_UNREACHABLE_METRIC), 'table', str(table)]
    return ip_route_cmd(args)
```

`VRF_UNREACHABLE_METRIC = 4278198272` is the value the kernel's `Documentation/networking/vrf.rst`
uses: the highest metric anything else in the table would use, so it only ever matches a miss.
`replace` makes it idempotent across resyncs.

It survives reconciliation without special-casing: `list_seg6_routes` keeps only lines containing
`encap seg6 ` (`privileged/seg6.py:188`), so `_remove_unwanted_routes` never sees it. Pin that with
a test — it is the kind of invariant a later "tidy the parser" change breaks silently.

### 4.2 Flush the table on delete

`delete_domain` deletes the VRF device and stops there (`dataplane.py:251-283`). That cannot remove
the unreachable default: it has no device. The encap routes point at the *underlay* device, not the
VRF, so they very likely survive too — no evidence file shows a table after a delete, so this is
unverified, and §9 captures it. Without a flush, a domain later handed the same function id (the
pool is round-robin and wraps at 4,095) inherits a stale table: routes to the old domain's addresses.

```python
for family_v6 in (False, True):
    priv_seg6.flush_table(sid.vrf_table(function_id, self.vrf_table_base),
                          family_v6=family_v6)
```

`flush_table` tolerates an already-empty table, like `delete_route` does.

### 4.3 Garbage-collect from the kernel, not from memory

`_do_sync_state` removes domains in `set(self.vpns) - set(wanted)` (`agent_extension.py:147-153`).
`self.vpns` starts empty on every start, so **after a restart that set is always empty**: a domain
deleted while the agent was down is never collected — contradicting the comment right above it.
The existing test (`test_agent_extension.py:173`) pre-seeds `self.vpns`, which is why it passes.

The same flaw sits in `delete_vpn` itself: it removes gateway ports from its in-memory map
(`dataplane.py:267-268`), so a delete issued after a restart leaves every `svp-<fid>-*` port on
`br-int`.

Fix both with the rule `_bridge_gateway_ports` already follows — read the kernel:

```python
# agent_extension._do_sync_state, after a SUCCESSFUL sync_state only
wanted_fids = {p['function_id'] for p in wanted.values()}
for fid in self.dataplane.present_function_ids() - wanted_fids:
    LOG.info("SRv6: function id %s has kernel state but no domain; removing", fid)
    self.dataplane.delete_domain(None, function_id=fid)
```

- `present_function_ids()` parses `sv6vrf-<n>` names from a third wrapper, `list_vrf_names()`
  (`ip -o link show type vrf` through `ip_route_cmd`). The prefix is this package's alone —
  networking-bgpvpn's other drivers name their objects differently (`constants.py:46-47`).
- `delete_domain` deletes `_bridge_gateway_ports(function_id)`, not the in-memory map.
- An exception from `sync_state` already returns before any GC (`agent_extension.py:140-143`). A
  successful *empty* answer GCs everything, which is correct: the database is the truth.

This is also what makes the re-stack safe: if node 1 is not rebooted, the old build's
`sv6vrf-3579` and `sv6vrf-3783` are still in the kernel, and the first sync removes them. Record it.

### 4.4 Refuse to run on anything but the OVS agent (§8.4)

First thing in `initialize()`, before any sysctl or kernel write:

```python
from neutron.plugins.ml2.drivers.openvswitch.agent.common import constants as ovs_const

if driver_type != ovs_const.EXTENSION_DRIVER_TYPE:
    raise SystemExit("The srv6 L2 agent extension needs the Open vSwitch agent "
                     "(br-int and the local VLAN map); it was loaded by the %r "
                     "agent." % driver_type)
```

That constant is exactly what the OVS agent passes (`ovs_neutron_agent.py:338-339`). Compare
against it, not a literal. Linuxbridge no longer exists in this neutron; the agents that *can* load
this extension by mistake are macvtap (`'macvtap'`, `macvtap_neutron_agent.py:40`) and SR-IOV.
`SystemExit` matches `_validate_config`'s existing refusals.

### 4.5 Consume `segments` (moved from P6, §1.2)

In `add_routes`, where the encap route is built (`dataplane.py:516-520`):

```python
own_end = ipaddress.IPv6Address(
    sid.format_sid(self.locator, constants.NODE_END_FUNCTION_ID))
transit = list(route.get('segments') or [])
while transit and ipaddress.IPv6Address(transit[0]) == own_end:
    transit.pop(0)          # the payload fans out to every node; the server
                            # cannot know which one is the ingress
segs = transit + [remote_sid]
```

Compared as addresses, not strings, so `fc00:0:1:fff0::` and a zero-padded form agree. Until P6 no
payload carries `segments`, so behaviour is byte-identical to today — which is what the gate
compares. The incremental `routes_updated` path goes through `add_routes` too, so it is covered.
MTU stays per network (`check_mtu` in `_apply_domain`) in P5; the per-route check lands in P6 once
routes carry `network_id`.

---

### 4.6 SID rules — the seal must let the domain's own traffic out (found at the gate)

§4.1 alone broke every cross-node packet, and the two-node gate caught it (`REPORT-P5-two-node-
2026-09-10.md` §9). After seg6 encapsulates a packet that arrived on a gateway port, the kernel
routes the new **outer** IPv6 header with the ingress device unchanged, which means in the VRF.
The lookup for the remote decap SID hit the IPv6 `unreachable default`, so `Ip6InNoRoutes` went
up once per echo request on both nodes and nothing reached the wire. The old build had only ever
worked through the fall-through the seal closes. Mocked unit tests cannot see where that lookup
happens.

The fix is one policy rule per (domain, remote node), placed ahead of the l3mdev rule:

```
ip -6 rule add pref 999 iif sv6vrf-<fid> to <remote decap SID>/128 lookup main
```

- **`iif <VRF master>`** matches traffic arriving on that VRF's enslaved ports (the pre-4.8
  `vrf.rst` pattern), so another domain's VRF gains nothing.
- **`to <own SID>/128`**: the only new reach is the domain's own decap SIDs, which inject only
  into itself.
- **`lookup main`**: the underlay picks the next hop, however many hops away.

The rules are derived from the routes the agent already installs, since a route's first segment
is its outer destination. They are reconciled from the kernel like the routes:
- `add_routes` adds;
- `sync_routes` adds and prunes;
- `delete_domain` removes them before the VRF;
- `present_function_ids` counts rules whose VRF is gone (`[detached]`), so the orphan GC collects
  them.

The privileged wrappers are `add_sid_rule`, `delete_sid_rule` and `list_sid_rules`; the priority
is `SID_RULE_PRIORITY = 999`.

**It fails closed for TE.** When a route's first segment is a transit End SID (P6), no rule is
added and the agent logs why. A VRF allowed to reach an End SID lets a tenant hand-craft an SRH
through it toward another domain's SID. P6 needs SRH filtering at the gateway port, or HMAC, before
it lifts this. Decided 2026-09-10: edge filtering. See `MIGRATION-PLAN.md` §8.7 for the threat
walk-through, the nftables ruleset and the P6 gate test.

## 5. RPC contract test

`P4-PLAN.md` §4.4 explains the failure: a kwarg the agent does not name lands in `**kwargs`, the
named one defaults to `None`, and the handler returns without a word. Test it once, from the
server's side:

```python
def test_every_cast_binds_to_named_parameters(self):
    notifier = rpc.Srv6AgentNotifyAPI()
    with mock.patch.object(notifier.client, 'prepare') as prepare:
        notifier.domain_updated(self.ctx, {'id': 'd'})
        notifier.routes_updated(self.ctx, 'd', add=[], remove=[])
        notifier.domain_deleted(self.ctx, 'd', 7, 10007)
    for call in prepare.return_value.cast.call_args_list:
        ctx, method = call.args
        sig = inspect.signature(getattr(agent_extension.Srv6AgentExtension, method))
        bound = sig.bind(None, ctx, **call.kwargs)          # self, context
        self.assertEqual({}, bound.arguments.get('kwargs', {}), method)
```

---

## 6. Devstack plugin

Modelled on `networking-bgpvpn/devstack/plugin.sh`, which already stacks on this testbed.

`devstack/settings`:

```bash
NETWORKING_SRV6_AGENT_DIR=${NETWORKING_SRV6_AGENT_DIR:-$DEST/networking-srv6-agent}
SRV6_LOCATOR=${SRV6_LOCATOR:-}                   # required on every agent node
SRV6_UNDERLAY_INTERFACE=${SRV6_UNDERLAY_INTERFACE:-}
SRV6_FUNCTION_ID_RANGES=${SRV6_FUNCTION_ID_RANGES:-1:4095}
SRV6_VRF_TABLE_BASE=${SRV6_VRF_TABLE_BASE:-10000}
SRV6_RESYNC_INTERVAL=${SRV6_RESYNC_INTERVAL:-300}
```

`devstack/plugin.sh`:

```bash
if [[ "$1" == "stack" && "$2" == "install" ]]; then
    setup_develop $NETWORKING_SRV6_AGENT_DIR
elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
    # EVERY node: the agent reads the *server* group's vrf_table_base
    # (agent_extension.py:59-61), so a compute-only node would otherwise
    # silently use the default and disagree with the server.
    iniset $NEUTRON_CONF srv6 vrf_table_base $SRV6_VRF_TABLE_BASE
    if is_service_enabled neutron-api || is_service_enabled q-svc; then
        neutron_service_plugin_class_add srv6
        iniset $NEUTRON_CONF srv6 function_id_ranges $SRV6_FUNCTION_ID_RANGES
    fi
    if is_service_enabled neutron-agent || is_service_enabled q-agt; then
        [[ -n "$SRV6_LOCATOR" ]] || die $LINENO "SRV6_LOCATOR must be set on every agent node"
        source $NEUTRON_DIR/devstack/lib/l2_agent
        plugin_agent_add_l2_agent_extension srv6
        configure_l2_agent
        iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent locator $SRV6_LOCATOR
        iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent underlay_interface $SRV6_UNDERLAY_INTERFACE
        iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent resync_interval $SRV6_RESYNC_INTERVAL
    fi
fi
```

- **`[srv6]` goes in `neutron.conf`, never a side file.** `networking_bgpvpn.conf` taught that
  lesson: a special-cased file that silently ignores other option groups under uwsgi (INSTALL §6.1).
- **`configure_l2_agent` is an `iniset`** (`neutron/devstack/lib/l2_agent`), so the old trap —
  appending a second `[agent]` section splits the first and silently drops `tunnel_types` — cannot
  happen. Still confirm the file is the one the agent reads:
  `systemctl cat devstack@q-agt | grep ExecStart`.
- **The database needs nothing.** `init_neutron` runs `neutron-db-manage upgrade head`
  (devstack `lib/neutron:501`), which finds this package through its
  `neutron.db.alembic_migrations` entry point — the way networking-bgpvpn's migrations land today.
- Uncomment the `neutron.agent.l2.extensions` group in `pyproject.toml`. As in P1 and P4: build
  the wheel and read `entry_points.txt` out of it; `pip install -e .` succeeding proves nothing.

---

## 7. Re-stacking the testbed

**Prerequisites.** P4 merged. P5's unit suite green. The repo pushed to
`costa99/networking-srv6-agent` (`MIGRATION-PLAN.md` §7 — the empty repo is created in the browser).
On **both** nodes `sudo -iu stack ssh -T git@github.com` answers `Hi costa99!` (the node confs
already document the `known_hosts` hang).

**Configs.** `node{1,2}-srv6-agent-local.conf` are the stock confs plus three lines. The stock confs
already pin the ML2/OVS types (the `tunnel_types = gre` failure) and carry no forks; they keep
upstream networking-bgpvpn with the Dummy driver, which is half of P7's coexistence case for free.

```
# node 1                                           # node 2
enable_plugin networking-srv6-agent \              enable_plugin networking-srv6-agent \
  git@github.com:costa99/networking-srv6-agent.git main   git@github.com:costa99/networking-srv6-agent.git main
SRV6_LOCATOR=fc00:0:1::/48                         SRV6_LOCATOR=fc00:0:2::/48
SRV6_UNDERLAY_INTERFACE=enp2s0f1                   SRV6_UNDERLAY_INTERFACE=wlp59s0
```

**Order.**

1. `./unstack.sh` on node 2, then on node 1.
2. Node 1: copy its conf to `/opt/stack/devstack/local.conf`, `./stack.sh`. Then:
   - `openstack extension list --network | grep srv6` — the alias is advertised;
   - `systemctl cat devstack@q-agt` still shows the `srv6-underlay` drop-in
     (`implementation/srv6-plugin/systemd/`) — it lives in `/etc`, but confirm;
   - q-agt's log shows `SRv6 agent extension initialised`, and the old `sv6vrf-*` are gone (§4.3).
3. Node 2: the same, after node 1 is fully up.
4. `sudo /usr/local/sbin/srv6-underlay status` on both.
5. Recreate the demo as the `demo` user, following the HOWTO: `srv6-net1` 10.90.1.0/24,
   `srv6-net2` 10.90.2.0/24, a domain, two associations, `vm-n1` on node 1, `vm-n2` on node 2.

Any q-agt restart: `kill -9` the MainPID, restart, confirm the PID changed, allow ~50 s.

**Rollback.** Stock conf + `./stack.sh` + `inject-srv6.sh` — the documented two-pass BGPVPN
workflow — then recreate the VPN.

---

## 8. Tests

**Zero skips remains the pass condition.** Run in the layered venv from the P1 work (stock
neutron-lib, no `PYTHONPATH`).

Ported near-verbatim: `test_dataplane.py` (663) and `test_agent_extension.py` (451), the deadlock
watchdog included. New:

| test | guards |
|---|---|
| unreachable default installed for v4 and v6, before the SID route | §4.1 |
| `list_seg6_routes` ignores an `unreachable default` line; `sync_routes` leaves it alone | §4.1 |
| `delete_domain` flushes both families of its table | §4.2 |
| empty in-memory state + a `sv6vrf-N` in the kernel + N not wanted → deleted | §4.3 |
| `delete_domain` after a restart removes the `svp-N-*` ports read from the bridge | §4.3 |
| a failed `sync_state` GCs nothing; a successful empty one GCs everything | §4.3 |
| `driver_type='macvtap'` → `SystemExit` before any sysctl is touched | §4.4 |
| `segments` prepended; leading own End SID stripped; absent `segments` ⇒ today's route | §4.5 |
| every server cast binds to named agent parameters | §5 |

---

## 9. Gate evidence to record

New files under `implementation/srv6-plugin/evidence/`, raw output, as the existing ones are:

- **Bring-up state** on the new package — compare with `two-node-bringup-state.txt`; the only
  intended difference is two `unreachable default` routes per VRF table.
- **`vrf-fallthrough.txt`, "after"** — the three cross-domain probes return `unreachable`; the
  in-domain control lookup is unchanged.
- **Delete** — `ip route show table <T>` and `ip -6 route show table <T>` both empty after the domain
  is deleted; no `svp-<fid>-*` left on `br-int`.
- **Restart GC** — stop q-agt, delete a domain, start q-agt: its VRF, SID and gateway ports disappear
  on the first sync.
- **Lifecycle** — VM-to-VM ping across nodes and an SRH capture, compared with `lifecycle.txt` and
  `two-node-srh-capture.txt`.

---

## 10. What will bite

- **A mismatched RPC kwarg is silent.** §5's test is the only cheap defence.
- **q-agt does not restart cleanly.** Trust the PID and the log, not `systemctl is-active`.
- **`vrf_table_base` on compute nodes.** If node 2 lacks `[srv6] vrf_table_base`, it uses the
  default. That only happens to work while the server also uses the default.
- **The drop-in after a re-stack.** Without `q-agt-wait-for-network.conf`, node 2's agent loses the
  Wi-Fi boot race and hangs while reporting `active`.
- **An unpushed repo** fails `stack.sh` at clone time, after minutes of other work.
- **Node 1's memory.** ~0–1 GB free; a `stack.sh` that dies mid-way looks like a Neutron timeout.
- **Do not trust the ported GC comment.** `agent_extension.py:147-153` claims a guarantee the code
  did not give (§4.3). Read what the test proves, not what the comment says.

---

## 11. Status (2026-09-10)

**Update, same day, after the two-node gate:** §4.6 (SID rules) added. Suite now 367 tests,
0 skipped, flake8 clean. The fix is not yet deployed to the testbed.

**Implemented and unit-verified** (348 tests, 0 skipped, flake8 clean):

- **The port:** `agent/dataplane.py` and `agent/agent_extension.py`, with the §3 renames.
  - The kernel names are unchanged.
  - The endpoints bind the P4 kwargs, which `TestRpcContract` proves.
  - A second test shows the check catches the old `vpn=`.
- **§4.1 Seal.** It fails **closed**: a VRF whose table cannot be sealed gets no SID and no
  recorded state, and therefore can never get a gateway port.
- **§4.2 Flush on delete.** Both families are flushed after the device is removed, and also when
  the device is already gone.
- **§4.3 Kernel GC.**
  - The GC runs only after a *successful* `sync_state`.
  - A `None` from an unreadable VRF list deletes nothing.
  - One failed delete does not stop the others or the apply.
  - `delete_domain` finds its gateway ports on the bridge.
- **§4.4 `driver_type`.** Refused before any config registration or kernel write.
- **§4.5 Segments.**
  - Only leading occurrences of this node's End SID are stripped, compared as addresses.
  - An unparseable segment is passed through for `ip` to refuse.
  - via-destination is kept.
- **§6:** `devstack/settings` and `devstack/plugin.sh`, plus the `neutron.agent.l2.extensions`
  entry point, verified from the installed metadata.
- **§7:** `implementation/srv6-plugin/node{1,2}-srv6-agent-local.conf`.

**Not done: §7's re-stack and §9's gate evidence.** Both are destructive to the running testbed:
`unstack` on both nodes loses the current VMs, networks and VPNs. They also need the private repo
to exist and be pushed first, which is a browser step (MIGRATION-PLAN §7). Neither was run.
