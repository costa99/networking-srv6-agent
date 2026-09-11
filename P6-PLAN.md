# P6 — Traffic engineering

Detailed plan for phase 6 of `MIGRATION-PLAN.md` §5. Written 2026-09-10, after the P5 two-node gate
passed (`REPORT-P5-two-node-2026-09-10.md` §10).

**Status (2026-09-11): §1–§5 implemented, in code and unit tests.** Unit suite: 506 tests, 0 skips.
§6 (deploy) and §7 (the two-node gate) have not been run yet. Where the code differs from this plan,
§10 records it.

**What P6 delivers.** An **admin-only** way to steer a domain's traffic, toward a whole compute host
or a single VM, through an explicit list of waypoints. That is what MIGRATION-PLAN §1.2 and §8.5
promised.

**Gate.** Two nodes, §7:
- a steered VM gets a depth-2 SRH while a neighbour stays at depth 1;
- a forged SRH from a tenant is dropped by the edge filter;
- the P5 behaviour does not regress.

---

## 0. Decisions this plan builds on

| Decision | Source |
|---|---|
| Admin only; no admin VRF; per-VM selector; "avoid a node" = explicit via-list | MIGRATION-PLAN §8.5 |
| TE path is a top-level resource; `domain_id` in the body | §4, P4-PLAN §1.3 |
| Edge filter on the gateway ports before any End-SID rule | §8.7 |
| The filter's SID space = registered locators (no new config) | decided 2026-09-10 |
| A waypoint that stops resolving → **fall back to the direct route**, path reports **DEGRADED** | decided 2026-09-10 |
| Extras: **status field only**. No CLI (openstacksdk/OSC), no source-host selector, no underlay hygiene in P6 | decided 2026-09-10 |
| `via == destination` allowed: the only depth-2 SRH two nodes can produce | P5-PLAN §4.5 |
| `PAYLOAD_VERSION` stays 1; `segments` is an optional route key; the agent consumer exists since P5 | P4-PLAN §4.4, P5-PLAN §4.5 |

**Already in place:**
- **DB layer:** `db/te_db.py` (`SRv6TePath`, `SRv6TePathHop`, CRUD, `get_te_paths_for_vpn`) and its
  tables in the initial migration.
- **Agent consumer:** `agent/dataplane.py`: `_transit_segments`, `_install_routes` (End-SID rule
  gated off), `_ensure_sid_rules` / `_remove_unwanted_sid_rules`.
- **Wrappers and constants:** `privileged/seg6.py` `replace_seg6_route` (any depth),
  `add_sid_rule` / `list_sid_rules`; `common/constants.py` `NODE_END_FUNCTION_ID = 0xfff0`,
  `MAX_TE_VIA_HOSTS = 6`, `SID_RULE_PRIORITY`.
- **Locator API:** `srv6_locators` (P4), the admin's authoring aid for `via_hosts`.

---

## 1. API — `srv6-te`

- **New files:**
  - `api/definitions/srv6_te.py`;
  - `extensions/srv6_te.py` with a class named **`Srv6_te`** (neutron derives the class name from
    the file name with `str.capitalize()`, P4-PLAN §3.2);
  - `policies/srv6_te_path.py`.
- **Plugin:** `Srv6Plugin.supported_extension_aliases` gains `'srv6-te'`.
- **Surface:** collection `srv6_te_paths`, URL `/v2.0/srv6_te_paths`, rules
  `create|get|update|delete_srv6_te_path`.

| Attribute | POST | PUT | Notes |
|---|---|---|---|
| `id` | – | – | uuid, primary key |
| `project_id` | yes | – | `required_by_policy`. **Forced to the domain's project** by the plugin, as associations are. A read-only definition would break neutron's `populate_project_id`, so this is the working form of TE-VS-SFC §4.3.5's advice |
| `domain_id` | yes | – | create-only, uuid |
| `destination_host` | yes | – | create-only, default None |
| `destination_port_id` | yes | – | create-only, uuid-or-none, default None |
| `via_hosts` | yes | yes | `convert_to_list`, `type:list_of_unique_strings`, default `[]`. PUT **replaces**: re-steering without a delete/create window. `[]` = explicitly direct |
| `segments` | – | – | read-only, server-resolved: `[<via End SIDs>..., <destination decap SID>]`, or `[]` when not in effect |
| `status` | – | – | read-only, `ACTIVE` or `DEGRADED` (§3). What the **server** resolved, not an agent acknowledgement |

**Policies:** every verb and every `enforce_policy` attribute is `neutron_lib.policy.rules.ADMIN`,
`scope_types=['project']`. The read side is the exposure: host names and underlay SIDs (TE-VS-SFC
§4.3.1). The P4 coverage test (`tests/unit/policies/test_policies.py`) walks the new descriptor
automatically.

**Exceptions** (in `extensions/srv6_te.py`):
- `Srv6TePathNotFound` (404);
- `Srv6TePathInvalid` (400, with a reason);
- `Srv6TePathExists` (409, the same selector already has a path in this domain).

## 2. Model and migration

**`db/te_db.py`:**
- `destination_host` becomes nullable.
- New column `destination_port_id`: String(36), FK `ports.id` `ondelete=CASCADE` (a path for a
  deleted VM is meaningless), nullable.
- A second unique constraint, `uniq_srv6_te_paths0domain_id0destination_port_id`.
- **No CHECK constraint.** "Exactly one selector" is enforced in the plugin; CHECK support is
  uneven across MySQL and MariaDB versions.

**New functions:**
- `get_all_te_paths(context, filters, fields)`: the top-level list, with filters `id`,
  `domain_id`, `destination_host`, `destination_port_id`, `project_id`.
- `get_te_paths_for_domain(context, domain_id)` → `{'by_port': {port_id: [via...]}, 'by_host':
  {host: [via...]}}`. It replaces `get_te_paths_for_vpn`, and the leftover `vpn` name goes with it.
- `_make_te_path_dict` adds `destination_port_id` and `status`.

**Migration:** a new expand revision after `2f8a1b3c9d40` (update `EXPAND_HEAD`):
`alter_column(destination_host, nullable=True)`, `add_column` + FK + unique constraint. Neutron's
expand check forbids only drop operations (`neutron/tests/functional/db/test_migrations.py`,
`DROP_OPERATIONS`), and relaxing nullability is not one. Re-run P3's models-vs-migrations
comparison afterwards.

## 3. Plugin (`services/plugin.py`)

**CRUD:**
- **Create:**
  1. Look the domain up (elevated); if it's missing, 404 on the domain.
  2. Enforce **exactly one** selector (400).
  3. `destination_host` must be in `srv6_db.get_host_locators` (400).
  4. `destination_port_id`: the port must exist, and its `network_id` must be one of the domain's
     networks (400).
  5. `via_hosts`: at most `MAX_TE_VIA_HOSTS`, each one registered (400). `via == destination` is
     accepted.
  6. Force `project_id` to the domain's, then `te_db.create_te_path`.
  7. `_push_domain_by_id(domain)`.
- **Update:** `via_hosts` only, same validation, then push.
- **Delete:** push. Domain delete already cascades the paths and logs them (P4-PLAN §4.2).
- **Get / list:** attach `segments` and `status` through `_resolve_te_path`.

**`_resolve_te_path(context, path, locators, function_id)`:**
- The destination host is `destination_host`, or the port's `binding:host_id`.
- If the destination and every via resolve: `segments = [format_sid(loc[v], NODE_END_FUNCTION_ID)
  for v in via] + [format_sid(loc[dest], function_id)]`, and `status = ACTIVE`.
- If any host lacks a locator, or the port is unbound: `segments = []`, `status = DEGRADED`, and a
  warning is logged.

**Payload (`_routes_for_domain`):**
- Each route gains **`network_id`**, for the agent's per-route MTU check.
- It gains **`segments`** (transit End SIDs only; the agent appends the decap SID) when a path
  applies. Precedence: **port path > host path > direct**.
- A DEGRADED path contributes no `segments`, so the route **falls back to direct** (§0).

**Incremental path:** `_notify_port` must attach the same `segments`, through a shared helper.
Otherwise a steered VM's route arrives at depth 1 via `routes_updated`, and stays so until the next
full sync.

**Triggers:**
- TE CRUD → push the domain.
- A port binding change is covered by the existing receivers, since resolution happens at build
  time.
- A locator change is covered by `handle_sync_state`'s rebroadcast.

## 4. Agent

### 4.1 Edge filter (MIGRATION-PLAN §8.7)

**Privileged:** a new module `privileged/nft.py` under the package's own privsep context. It holds
one entrypoint, `apply_ruleset(text)`, which runs `nft -f -` with the ruleset on stdin
(`ip_route_cmd` runs only `ip`).

**The ruleset** is rendered by the dataplane, deterministically, with the locators sorted, and
replaced atomically in one transaction. Syntax checked with `nft -c` against nftables 1.0.9 on
node 1:

```
add table inet srv6_edge
delete table inet srv6_edge
table inet srv6_edge {
  set sid_space { type ipv6_addr; flags interval; auto-merge; elements = { <locators> } }
  chain prerouting {
    type filter hook prerouting priority raw;
    iifname "svp-*" ip6 daddr @sid_space counter drop
    iifname "svp-*" rt type 4 counter drop
  }
}
```

It lives in its own table, so neutron's iptables-nft tables (`ip`/`ip6 filter|nat|mangle`) are
never touched.

**When it is applied:**
- **At `initialize_node`,** with the node's own locator.
- **On every locator-set change:** `_apply_domain` merges `payload['locators']` and calls
  `dataplane.ensure_edge_filter(self.locators)`.
- **On every resync,** because a full replace is idempotent.

`dataplane.edge_filter_active` is True only after a successful apply. Missing `nft` or a failed
apply sets it False and logs an error.

**Why the agent's own traffic passes:** a tenant packet enters PREROUTING through its gateway port
(`iif svp-*`) before it is routed. The agent's encapsulation happens after that routing decision
(seg6 lwtunnel, input path), so the outer header never enters PREROUTING. The unit tests cannot
prove this; the gate must (G1, G2).

### 4.2 End-SID rules

In `_install_routes`, when a route has transit segments:
- **Filter active:** add `transit[0]`, normalised, to `rule_sids`. The existing
  `_ensure_sid_rules` and `_remove_unwanted_sid_rules` do the rest, including pruning when a path
  is removed or emptied.
- **Filter not active:** keep the P5 fail-closed warning, and add no rule.

### 4.3 Per-route MTU

A route now carries `network_id`. `check_mtu(mtu, segment_count=len(segments))` runs with that
network's MTU, taken from the payload, or from the cached domain payload for `routes_updated`. It
warns once per (prefix, depth), not on every resync.

## 5. Tests (unit, zero skips)

**Definitions:**
- selectors are create-only;
- `via_hosts` is PUT-able;
- `segments` and `status` are read-only;
- no attribute is visible to non-admins.

**Policy:** the coverage walk (automatic).

**REST** (the P4 harness, `NeutronDbPluginV2TestCase`):
- admin CRUD;
- as a tenant: list `[]`, show 404, create 403;
- rejected with 400: unknown via, more than 6 vias, both or neither selector, a port outside the
  domain, a missing destination host;
- a duplicate selector → 409;
- `via == destination` accepted;
- `project_id` forced to the domain's;
- update replaces the vias and pushes; delete pushes;
- domain delete cascades the paths.

**Plugin unit:**
- precedence port > host > direct;
- a DEGRADED path (lost via, unbound port) gives the direct route and `status` DEGRADED;
- `_notify_port` carries `segments`;
- `segments` shape and order;
- `network_id` on every route.

**DB:**
- selector columns;
- one path per port per domain;
- a port delete cascades its path;
- `get_all_te_paths` filters;
- `get_te_paths_for_domain` shape.

**Agent:**
- ruleset rendering (sorted locators, idempotent text);
- applied on init and on locator change;
- a failed apply → inactive → no End-SID rule;
- active → End-SID rule for `transit[0]` only;
- the rule pruned when the path is removed;
- the MTU warning at depth > 1, once.

**`privileged/nft`:** argv and stdin.

## 6. Deploy (lessons from P5)

1. **Code:** restacking does not update an existing clone, and `git pull` fails on the detached
   HEAD. On both nodes, as `stack`:
   ```bash
   cd /opt/stack/networking-srv6-agent
   git fetch origin main && git merge --ff-only FETCH_HEAD
   ```
2. **This time the server changes too.** On node 1:
   - run `neutron-db-manage upgrade heads` (the new expand revision);
   - restart `devstack@neutron-api` and `devstack@neutron-rpc-server`.
3. **Both nodes:** restart `devstack@q-agt`. `kill -9` the old MainPID if needed, and confirm the
   PID changed.
4. **Check before testing:**
   - `sudo nft list table inet srv6_edge` on both nodes;
   - the running module, imported from *outside* the repo (from inside, the workstation checkout
     shadows it);
   - `nova-manage cell_v2 discover_hosts` after any restack.

## 7. Gate (two nodes)

Write the results to a new `REPORT-P6-...md`, with the raw log in
`implementation/srv6-plugin/evidence/`.

- **G1, regression plus the filter's first claim:**
  - `srv6_edge` present on both nodes, with both locators in `sid_space`;
  - the P5 pings still 5/5 both ways;
  - the filter's drop counters stay **0**: the agent's own encapsulation isn't filtered;
  - the P5 seal probes and delete/flush still pass.
- **G2, positive TE:**
  - as admin, `POST /v2.0/srv6_te_paths {domain_id: red, destination_port_id: <vm-n2 port>,
    via_hosts: [ale-XPS-15-9570]}`;
  - **API:** `segments = [fc00:0:2:fff0::, fc00:0:2:<fid>::]`, `status ACTIVE`;
  - **node 1:** the route becomes `segs 2 [fc00:0:2:fff0:: fc00:0:2:<fid>::]`, plus a new SID rule
    toward `fc00:0:2:fff0::`;
  - **pings:** 5/5;
  - **capture:** `RT6 len=4 segleft=1`;
  - **control:** a second VM on node 2 (`vm-n2b`) stays at depth 1.
- **G3, precedence:** add a host path `{destination_host: ale-XPS-15-9570, via_hosts: []}`. The port
  path still wins for `vm-n2`; `vm-n2b` follows the host path.
- **G4, un-steer:** `PUT via_hosts: []` → depth 1 again, and the End-SID rule is pruned.
- **G5, tenant view:** as `demo`, list → `[]`, show → 404, create → 403.
- **G6, lost waypoint (optional; it touches the DB):** delete the via host's `srv6_host_locators`
  row and trigger a push. The route falls back to direct and `status` goes DEGRADED. The agent's
  next `sync_state` re-registers the node and the path comes back ACTIVE.
- **G7, attack — the edge filter's reason to exist:**
  - **Setup:**
    - add an IPv6 subnet `fd90:1::/64` to `srv6-net1` (the gateway port gets `fd90:1::1`);
    - give a second domain `blue` (project `alt_demo`) a network and a VM on node 2 (`vm-b2`,
      10.91.1.21);
    - attach an attacker namespace to `srv6-net1` on node 1, neutron-debug style: a Neutron port
      with `--device-owner compute:probe --host ale` and port security off, and an OVS internal
      port with `iface-id` and `attached-mac`. Give it `fd90:1::66/64` and a default route via
      `fd90:1::1`.
  - **The attack:**
    `ip route add 10.91.1.21/32 encap seg6 mode encap segs fc00:0:2:fff0::,fc00:0:2:<blue hex>:: dev <tap>`,
    then `ping 10.91.1.21`. G2's path created the rule toward `fc00:0:2:fff0::`, so without the
    filter the packet would leave red's VRF.
  - **Filter on:** node 1's `ip6 daddr @sid_space` counter rises, and blue's VRF RX counter on node
    2 is unchanged.
  - **Control, filter off** (`nft delete table inet srv6_edge` on node 1, restored by the next
    resync or an agent restart): blue's RX counter rises, which proves the threat was real.
  - **Clean up:** the probe port, the namespace, `vm-b2`, blue.

**Honest limits to state in the report:**
- Two nodes give a depth-2 SRH, and the per-VM selector steering one VM but not another. They do not
  give real waypoint avoidance, which needs a third node (MIGRATION-PLAN §1.4).
- `status` is server resolution, not an agent acknowledgement.

## 8. What will bite

- **Policy fails closed.** Every `enforce_policy` attribute needs its rule in the same commit (the
  coverage test catches it).
- **Descriptor naming.** It must be `Srv6_te`, or the API 404s silently.
- **The incremental path.** `_notify_port` without `segments` gives a depth-1 route until the next
  resync.
- **nft interval sets reject overlapping elements.** `auto-merge` covers it, and locators are
  disjoint anyway.
- **VRF traffic crosses PREROUTING twice** (once with `iif svp-*`, once with the VRF device). The
  match on `svp-*` catches the first pass.
- **Kernel 6.8 `ip route get … iif svp-*` does not reflect `iif` rules** (REPORT-P5 §10.4). Verify
  with `iif sv6vrf-<fid>` or with real traffic.
- **`seg6_enabled`** starts to matter at depth ≥ 2 on transit nodes without a matching `seg6local`
  route (`evidence/seg6-enabled-semantics.txt`). The agent already sets it on the underlay.
  Re-check it in G2.
- **Out of scope, still open** (not decided for P6): an `unreachable fc00::/16` catch-all and
  `ip sr tunsrc` in `systemd/srv6-underlay` (REPORT-P5 §10.5, §10.8); CLI support; a source-host
  selector.

## 9. Files

- **New:**
  - `api/definitions/srv6_te.py`;
  - `extensions/srv6_te.py`;
  - `policies/srv6_te_path.py`;
  - `privileged/nft.py`;
  - `db/migration/.../expand/<rev>_te_port_selector.py`;
  - tests `extensions/test_srv6_te.py`, `services/test_te.py`, `privileged/test_nft.py`.
- **Changed:**
  - `db/te_db.py`;
  - `services/plugin.py`;
  - `policies/__init__.py`;
  - `agent/dataplane.py`;
  - `agent/agent_extension.py` (locator-change hook);
  - `tests/unit/db/test_te_db.py`;
  - `tests/unit/agent/test_dataplane.py`;
  - `tests/unit/services/test_plugin.py`.
- **Docs, once P6 lands:** `MIGRATION-PLAN.md` §5 P6, and a TE section in
  `HOWTO-connect-networks.md`.

## 10. Implementation notes (2026-09-11)

Where the code goes beyond the plan, or had to pick between two readings of it:

- **An End-SID rule needs the filter to *cover* the SID, not only to be active.** Suppose a
  `routes_updated` message arrives before the `domain_updated` that carries a new node's locator.
  The filter is then active but does not yet include that node's SID space. `_install_routes`
  therefore adds the rule toward `transit[0]` only if `transit[0]` falls inside a locator of the
  applied filter (`Srv6LinuxDataplane._edge_filter_covers`). Otherwise the route gets the P5
  fail-closed warning.
- **When the filter is applied:**
  - at `initialize_node`, with the node's own locator;
  - on **every** successful `sync_state`, over every locator the payloads carry, before any domain
    is applied (this is the idempotent replace that restores a table deleted by hand, as in G7);
  - on a fanout `domain_updated` only if the payload **changes** the locator set.

  A failed apply makes the filter inactive. The next full sync then prunes any End-SID rule.
- **A DEGRADED port path falls back to direct, not to the host path.** This is the §0 decision read
  literally. A port path with `via_hosts: []` also wins over a host path: it is an explicit
  "direct".
- **Payload:** a route with no path carries no `segments` key, so an unsteered route is identical to
  the P5 one. `network_id` is on every route.
- **`status` is not `enforce_policy`.** The four topology attributes are: `destination_host`,
  `destination_port_id`, `via_hosts` and `segments`. Every verb rule is `ADMIN` in any case. That
  makes 12 rules, pinned in `test_policies`.
- **The per-route MTU check** runs only at depth ≥ 2. Depth 1 stays with the per-network check in
  `_apply_domain`.
- **Migration** `6b1e4d2a7c53` (the expand head): the new FK is unnamed, like every FK in the initial
  revision. Offline, the DDL renders cleanly for MySQL and PostgreSQL. It has not yet run against a
  real MySQL; the first real run is §6 step 2 (`neutron-db-manage upgrade heads` on node 1).
- **nft ruleset:** checked with `nft -c` inside `unshare -rn` on the workstation (nftables 1.0.9).
  It was also applied twice in a row there: the add/delete/table idiom replaces the table without
  error. The kernel lists the two adjacent /48s merged into one range (`auto-merge`).
