# networking-srv6-agent — migration plan

Extracting the SRv6 dataplane from `networking-bgpvpn` into a standalone, **additive** Neutron
service plugin that adds SRv6 connectivity between compute nodes and operator traffic
engineering, with no BGP and no VPN federation.

**Status:** plan only. No code has been ported yet. This repo currently holds a package skeleton
and this document.

---

## 1. Why

### 1.1 The defect this fixes

The SRv6 work currently lives as a *service driver* inside `networking-bgpvpn`. That plugin loads
exactly one driver:

```python
# networking_bgpvpn/neutron/services/plugin.py:63-72
drivers, default_provider = service_base.load_drivers(SERVICE_PROVIDER_TYPE, self)
self.driver = drivers[default_provider]

if len(drivers) > 1:
    LOG.warning("Multiple drivers configured for BGPVPN, although"
                "running multiple drivers in parallel is not yet"
                "supported")
```

There is no per-VPN provider attribute on the `bgpvpn` resource. So configuring
`service_provider = BGPVPN:SRv6:...` does not *add* SRv6 to BGPVPN — it **replaces** BGPVPN's
BGP/MPLS VPN service with it. An operator cannot run bagpipe for federation and SRv6 for intra-DC
traffic at the same time.

This contradicts the project's own framing. `SRV6-IMPLEMENTATION-PLAN.md` §1 describes "a
**second, independent** connectivity service"; `STUDY-networking-sr.md` §4 sells the design as
"**additive** L3VPN *beside* the normal VXLAN/Geneve L2 overlay". As built it is neither. The
existing gaps table records a symptom — `bgpvpn-vni` and `bgpvpn-routes-control` "disappear while
it is the active one" — but the cause is larger than missing attributes: the whole service is
displaced.

There is no cheap fix in place. Per-VPN provider selection does not exist, and adding it means
changing shared upstream code that every other BGPVPN driver runs.

### 1.2 What else it buys

- **Traffic engineering becomes a first-class resource.** In `networking-bgpvpn` a TE path has to
  hang off the `bgpvpn` object as an admin-only child of a tenant-owned resource — an object whose
  attributes are compute hostnames living under a parent the tenant can `GET`. Here it is a peer
  resource in a plugin whose whole subject is SRv6, and the topology-leak problem is structural
  rather than something policy has to paper over.
- **One fewer repository fork.** The `neutron-lib` fork exists only to host `bgpvpn_srv6.py` and
  `bgpvpn_srv6_te.py`. `networking-sr` demonstrates the alternative: it defines
  `EXTENDED_ATTRIBUTES_2_0` inline and subclasses the older
  `neutron_lib.api.extensions.ExtensionDescriptor`, needing no neutron-lib change at all. Dropping
  that fork also removes the layered-venv recipe currently needed because DevStack carries stock
  neutron-lib and three test modules fail to import without the fork on `PYTHONPATH`.
- **No driver abstraction to maintain.** `driver_api.py`'s 456 lines exist so several backends can
  share one API. With a single implementation the plugin *is* the implementation.

### 1.3 What is deliberately given up

- **Federation with external VPNs.** Route targets, route distinguishers, and the border-speaker
  design (`DESIGN-border-speaker.md`) are BGP/BGPVPN capabilities. If intra-DC connectivity is the
  target this is a fair trade, but it is a decision, not a side effect. Anyone needing to attach
  Neutron networks to a VPN that exists outside the cloud should still use `networking-bgpvpn`.
- **Router associations and port associations.** Already rejected or unimplemented today; they do
  not come along.
- The `openstack bgpvpn ...` CLI, the Horizon panel and the Heat resource. None of these ever
  covered the SRv6 attributes anyway.

### 1.4 What this does *not* unblock

Phase 5 (evaluation) and the Phase 7 TE gate are both blocked on a **third node**. This migration
changes neither. It is worth doing on its own merits — coexistence, scope, one less fork — not as
a way to make either gate reachable.

---

## 2. Naming

| | |
|---|---|
| Repo | `costa99/networking-srv6-agent` (private) |
| Package | `networking_srv6_agent` |
| Service plugin alias | `srv6` |
| Grouping resource | **`srv6_domain`** — *not* "VPN" |
| Agent extension | `srv6` (l2 agent extension, as today) |

**Why `-agent` and not plain `networking-srv6`.** That name is already taken by
`costa99/networking-srv6`, a separate line of work from July 2026 exploring an **OVN-native**
dataplane — an ML2 type driver plus patches to OVN itself, with no sidecar agent. It is set aside,
not deleted, and shares no code with this repo. The suffix names the distinguishing choice: this
plugin's dataplane is the **Linux kernel**, driven by an L2 agent extension, layered beside OVS.
The Python package is `networking_srv6_agent` for the same reason — the other project's package is
`networking_srv6`, and identical package names cannot coexist in one environment.

Worth reading before starting, even though that work is paused: its
`docs/phase-7.3.2-design.md` records that **OVS always emits a full SRH, so encapsulation overhead
is 64 bytes rather than 40**, and its evidence tree carries a multi-segment SRH capture and a fix
for an OVS `srv6_segs` parsing bug. The MTU figure in particular is worth checking against
`sid.encap_overhead`'s `48 + 16·n`.

The grouping object still has to exist: something must say "these networks share a VRF". What
disappears is the federation vocabulary around it, not the object. Calling it a domain rather than
a VPN keeps the name honest about what it does.

---

## 3. Port map

**~2,040 lines port with renames. ~1,100 lines are new. ~170 lines are discarded.**

### 3.1 Ports essentially unchanged

The rename is `vpn` → `domain` in names and log messages; the logic is SRv6, not VPN.

| From `networking-bgpvpn` | LOC | To | Change |
|---|---|---|---|
| `neutron/privileged/seg6.py` + `__init__.py` | 240 | `networking_srv6/privileged/` | **none** — pure `ip(8)` wrappers, no Neutron concepts. Update the privsep context module prefix |
| `services/service_drivers/srv6/sid.py` | 108 | `networking_srv6/common/sid.py` | **none** — pure SID arithmetic |
| `agent/srv6/dataplane.py` | 616 | `networking_srv6/agent/dataplane.py` | rename only; `vpn_id`/`function_id`/`vrf_table` are already opaque ids |
| `agent/srv6/agent_extension.py` | 345 | `networking_srv6/agent/agent_extension.py` | rename; RPC topic constants |
| `services/service_drivers/srv6/rpc.py` | 115 | `networking_srv6/services/rpc.py` | rename topics |
| `services/service_drivers/srv6/config.py` | 76 | `networking_srv6/common/config.py` | config group `[srv6]` / `[srv6_agent]` |
| `services/service_drivers/srv6/constants.py` | 90 | `networking_srv6/common/constants.py` | rename |
| `db/srv6_db.py` | 212 | `networking_srv6/db/srv6_db.py` | **one change**: the allocation FK retargets from `bgpvpns.id` to `srv6_domains.id` |
| `db/srv6_te_db.py` | 241 | `networking_srv6/db/te_db.py` | same FK retarget; `bgpvpn_id` column → `domain_id` |

### 3.2 Splits — `driver.py` (441 lines)

Roughly 270 lines are portable logic and 170 are BGPVPN hook scaffolding.

| Keeps | Becomes |
|---|---|
| `_network_info`, `_routes_for_vpn`, `_build_vpn_payload`, `_push_vpn`, `_push_vpn_by_id`, `_vpns_for_network`, `_notify_port`, `registry_port_updated`, `registry_port_deleted`, `start_rpc_listeners`, `handle_sync_state`, `_ensure_pool`, `_vrf_table`, `_core_plugin` | the service plugin's core, in `networking_srv6/services/plugin.py` |
| **Discarded** | |
| `create_bgpvpn_precommit` / `postcommit`, `delete_bgpvpn_precommit` / `postcommit`, `update_bgpvpn_postcommit`, `create_net_assoc_precommit` / `postcommit`, `delete_net_assoc_postcommit`, `create_router_assoc_precommit`, `_validate_net_assoc`'s external-network check | replaced by direct CRUD on the plugin; there is no driver/plugin split to orchestrate across |

The precommit/postcommit *ordering* discipline is still worth keeping even without the
abstraction — write inside the transaction, fan out RPC after it commits. Delta **D5** from the
original build is the reason: a postcommit hook cannot look up anything the commit destroys, which
is how the function-id release was silently a no-op. Preserve that by reading what you need before
the delete, not by reproducing the hook framework.

### 3.3 New code (~1,100 lines)

| Piece | Est. LOC | Notes |
|---|---|---|
| API definitions, inline | 250 | `srv6_domain` + network associations + `srv6_te_path` + read-only `srv6_locators`. Defined in-repo like `networking-sr` does, so **no neutron-lib fork** |
| Extension descriptors | 80 | one per resource; `ExtensionDescriptor` subclass with `get_resources()` |
| Service plugin | 250 | CRUD + lifecycle + the ported driver logic |
| DB models + CRUD for `srv6_domain` and its associations | 250 | model the shape on `bgpvpn_db.py`'s net-assoc methods, without RT/RD parsing or the filter hooks |
| Alembic migration | 80 | fresh initial revision; **not** a port of the two existing ones |
| Policies | 120 | domain = project-scoped, `sid_function` admin-only to read; TE + locators = admin-only |
| `pyproject.toml`, entry points, devstack plugin, docs | 60 | |

### 3.4 Tests

~1,960 lines port; ~700–900 are new.

| From | LOC | Portability |
|---|---|---|
| `tests/unit/agent/srv6/test_dataplane.py` | 663 | **near-verbatim** — kernel invariants, `autospec=True` on the privsep helpers |
| `tests/unit/services/srv6/test_sid.py` | 166 | **verbatim** |
| `tests/unit/privileged/test_seg6.py` | 141 | **verbatim** |
| `tests/unit/agent/srv6/test_agent_extension.py` | 451 | near-verbatim, incl. the deadlock watchdog |
| `tests/unit/db/test_srv6_db.py` | 291 | port; FK retarget |
| `tests/unit/db/test_srv6_te_db.py` | 248 | port; FK retarget |
| `tests/unit/services/srv6/test_driver.py` | 407 | **rewrite** against the plugin |

The high portability of the first four is the evidence that the original layering was right, and
is worth stating as a result in its own right: the SRv6 logic was kept free of BGPVPN concepts,
so it moves.

**Zero skips remains the pass condition.** Neutron's `SqlTestCase` turns a `DBReferenceError` into
a *skip*, so a broken foreign key hides as a skipped test rather than a failure — and this
migration retargets two foreign keys.

---

## 4. Resource model

```
srv6_domain                     project-scoped, many per project (8.2)
  id, name, project_id
  srv6_behavior      (End.DT46 - the only valid value, 8.3; create-only)
  sid_function       (read-only, server-allocated; ADMIN-only to read)
  networks           (via associations)

  /v2.0/srv6_domains
  /v2.0/srv6_domains/{id}/network_associations      project-scoped; the network must
                                                    belong to the domain's project (8.1)

srv6_te_path                    ADMIN ONLY, top level — a fabric fact, not a tenant fact
  id, domain_id, via_hosts[], segments[] (read-only)
  selector, exactly one of (8.5):
    destination_host      all of the domain's traffic toward that compute node
    destination_port_id   traffic toward one VM — that port's fixed IPs only
  precedence per route: port path > host path > direct
  /v2.0/srv6_te_paths

srv6_locator                    ADMIN ONLY, read-only
  host, locator, updated_at
  /v2.0/srv6_locators
```

URLs follow `P4-PLAN.md` §1.3: no path prefix, and every collection prefixed `srv6_`. Neutron
derives the policy rule name from the collection name, so a bare `domains` would register a global,
cross-service `create_domain` rule.

Three decisions carried over from the TE design work:

- **TE paths are top level, not a child of a domain.** Their attributes are compute hostnames and
  underlay locators — infrastructure vocabulary. A child of a tenant-readable object leaks the host
  inventory and which host a peer workload runs on, which is why Neutron treats `binding:host_id`
  and `provider:*` as admin-only. `domain_id` is a body field.
- **`srv6_locators` must exist.** Today `SRv6HostLocator` is written by agents through `sync_state`
  and read only internally — `get_host_locators` has two callers, both in the driver. Without an
  API an admin cannot discover valid host names, and a typo in `via_hosts` produces an empty
  segment list, which means "not steered". A silent no-op is the worst failure mode a steering
  command can have.
- **Network associations only.** No router associations, and a network that already has a Neutron
  router interface is refused (§8.1). A domain is an island: no north-south, no inter-domain
  routing.
- **Policy fails closed.** oslo.policy denies a rule it has never registered
  (`oslo_policy/policy.py:1089`, *"If the rule doesn't exist, fail closed"*). Any attribute
  carrying `enforce_policy: True` must have a registered default in the same commit, or every call
  — admin included — returns 403 with nothing in the logs explaining why.

---

## 5. Sequence

Each phase leaves the tree importable and testable.

**P1 — Skeleton and packaging.** `pyproject.toml`, `setup.cfg`, `.stestr.conf`, `tox.ini`,
`requirements.txt`, licence headers. Verify the package imports and that every *live* entry point
resolves.

```
[neutron.service_plugins]        srv6 = networking_srv6.services.plugin:Srv6Plugin
[neutron.agent.l2.extensions]    srv6 = networking_srv6.agent.agent_extension:Srv6AgentExtension
[neutron.db.alembic_migrations]  networking-srv6 = networking_srv6.db.migration:alembic_migrations
[neutron.policies]               networking-srv6 = networking_srv6.policies:list_rules
[oslo.config.opts]               networking-srv6 = networking_srv6.common.config:list_opts
```

> **Declare these in `pyproject.toml`, not `setup.cfg`.** An earlier draft of this section read the
> `networking-bgpvpn` checkout's 36-byte `setup.cfg` as truncated — as if it had *lost* its
> `entry_points` section and the installed dist were the only surviving copy. That was a
> misdiagnosis, and it cost a rewrite. Under pbr ≥ 6.1.1 with `build-backend = "pbr.build"`,
> metadata and entry points live in `pyproject.toml` as PEP 621 `[project.entry-points."…"]`
> tables, and `setup.cfg` is *supposed* to be two lines: `[metadata]` and `name`. Both
> `networking-bgpvpn` and `/opt/stack/neutron` are laid out exactly that way, and bgpvpn's
> `pyproject.toml` tables match its installed `entry_points.txt` one for one. Nothing was lost.
>
> The real hazard is the opposite one: `[entry_points]` and `description_file` are pbr's *older*
> `setup.cfg` dialect, and if nothing enables pbr, setuptools ignores both **silently** — a
> package that installs and imports fine while every entry point is missing. So the check still
> stands: build the dist and read `entry_points.txt` out of the wheel. Do not take
> `pip install -e .` succeeding as evidence.

**Entry points are enabled per phase, not up front.** Only groups whose targets exist are live;
the rest sit commented in `pyproject.toml` under the phase that will create them. An entry point naming
a missing module does not fail quietly — `service_plugins = srv6` with no plugin class stops
neutron-server from starting, and the traceback names an `ImportError` rather than saying the
feature is half-built. Each phase below ends by uncommenting its own group and confirming it
resolves.

Note `neutron.db.alembic_migrations` is **not** satisfied by an empty package — but not for the
reason it looks like. Neutron never imports that entry point: `neutron/db/migration/cli.py`
`_get_root_versions_dir` **string-splits** `module:attr` into a directory path. So the `module:attr`
form is a path convention, and what has to exist on disk is `env.py`, `script.py.mako` and the
`EXPAND_HEAD` / `CONTRACT_HEAD` files beside a `versions/` directory.

**P2 — Pure layers, no Neutron coupling.** Port `privileged/`, `common/sid.py`,
`common/constants.py`, `common/config.py` and their tests. These should pass immediately; if they
do not, something was BGPVPN-coupled that was not supposed to be.

**P3 — Data model.** `srv6_domain` + associations, the allocation pool and locator registry
retargeted, the TE tables, one fresh initial migration. Port the DB tests. Watch for skips.
Ends by enabling `neutron.db.alembic_migrations`.

**P4 — Service plugin and API.** Extension descriptors, plugin CRUD, policies. Domain create →
function id allocated → visible over REST. Nothing programs a kernel yet. Ends by enabling
`neutron.service_plugins`, `neutron.policies` and `oslo.policy.policies`.

Carries three items from §8: `srv6_behavior` offers **only** `End.DT46` (§8.3); router
associations are not implemented at all rather than implemented-and-rejected (§8.1); and network
association validation rejects both `router:external` networks **and networks that already have a
Neutron router interface** — the check the original design specified and never got (§8.1).
It also requires the network to belong to the domain's project (shared networks allowed, §8.1),
makes `sid_function` admin-only to read, and ports the RPC server half (`P4-PLAN.md` §1.1).

**P5 — Agent and dataplane.** Port `dataplane.py` and `agent_extension.py` with their tests. The RPC
server half moved to P4, since P4's plugin cannot import without it (`P4-PLAN.md` §1.1). This is the
phase that reproduces the working system. Ends by enabling `neutron.agent.l2.extensions` — the last
one, at which point the package becomes installable and functional. Detailed in `P5-PLAN.md`,
which adds four changes beyond the port and the seal below:

- **Flush the VRF table on delete.** Deleting the VRF device cannot remove the deviceless
  unreachable default, and probably not the encap routes either.
- **Garbage-collect from the kernel.** The ported GC diffs in-memory state, which is empty after
  every restart, so a domain deleted while the agent was down is never collected.
- **Consume `segments`.** The agent-side half of TE moves here from P6, so P6 is server-only and the
  payload version never has to change.
- **Deploy by re-stacking.** A devstack plugin, and both nodes re-stacked on it (§6).

Add the `driver_type` check in `initialize()` (§8.4): it is already a parameter and is never read,
so a non-OVS agent that loads the extension — macvtap or SR-IOV — fails obscurely instead of being
refused with a clear message.

Seal every domain VRF (§8.6): after the VRF exists, `ensure_vpn` installs
`unreachable default metric 4278198272` in its table for **both** families, through the existing
`privileged/seg6.py` `ip_route_cmd` entrypoint — no new privileged code. It must survive
`_remove_unwanted_routes`: confirm `list_seg6_routes` ignores non-encap routes, and test it.

**Gate:** the existing two-node lifecycle — domain create, network association, kernel state, VM
to VM ping across nodes, delete, cleanup — passes exactly as it does today under BGPVPN. Compare
against `implementation/srv6-plugin/evidence/` rather than judging by eye; the one intended
difference is the two unreachable defaults per VRF table. Rerun the probes in
`evidence/vrf-fallthrough.txt`: the three cross-domain lookups must now return `unreachable`, and
the in-domain control lookup must be unchanged.

**P6 — Traffic engineering.** Detailed in `P6-PLAN.md` (2026-09-10): API, model, plugin, edge
filter, End-SID rules, tests, deploy steps and a seven-step two-node gate. **Status 2026-09-11:**
implemented in code with unit tests (506 tests, 0 skips; `P6-PLAN.md` §10). The two-node gate has not
been run yet. The TE path resource,
segment-list construction, per-route MTU. The
design work is already done in the TE plan; the only change here is that the resource is top level
from the start, which makes the descriptor and plugin methods simpler than that plan describes.

Plus the per-VM selector (§8.5):

- **Model.** `SRv6TePath.destination_host` becomes nullable; add `destination_port_id`, FK →
  `ports.id` `ondelete='CASCADE'` (a path for a deleted VM is meaningless); a check constraint that
  exactly one selector is set; a second unique constraint `(domain_id, destination_port_id)`. A **new
  expand revision**, not an edit to the initial one. `get_te_paths_for_vpn`'s `{host: vias}` becomes
  a structure holding both maps.
- **Validation — a 400, never a silent `segments: []`.** Every `via_host` must be in
  `get_host_locators`; `len(via_hosts) <= MAX_TE_VIA_HOSTS` (defined since the TE commit, never
  enforced); the port must be on a network associated with the domain. Do **not** reject
  `via == destination`: it is a harmless detour, and on two nodes it is the only way to put a
  depth-2 SRH on the wire (`TE-DEPTH2-2026-09-01.md`).
- **Payload.** `_routes_for_domain` attaches `segments` — transit End SIDs,
  `format_sid(locator, NODE_END_FUNCTION_ID)` — per route, port path over host path over none. VM
  migration needs no new trigger: `registry_port_updated` already re-pushes the domain when
  `binding:host_id` changes, and resolution runs at build time.
- **Agent.** Only the per-route MTU check, `check_mtu(mtu, segment_count=len(segs))`, once routes
  carry `network_id`. The segment consumer — `segments + [remote_sid]`, dropping leading segments
  equal to this node's own End SID — already landed in P5 (`P5-PLAN.md` §1.2, §4.5), so P6 is
  otherwise server-only.
- **Policy.** Every verb and every `enforce_policy` attribute — `destination_host`,
  `destination_port_id`, `via_hosts`, `segments` — is `rules.ADMIN`, in the same commit as the
  descriptor.

**Gate (two nodes):** a per-VM path with `via=[node2]` toward a VM on node 2 puts a depth-2 SRH on
the underlay for that VM's address only, while a second VM on node 2 stays depth-1; migrate the
steered VM and the route follows. Real waypoint avoidance stays blocked on a third node.

**Before any End-SID rule: the edge filter (§8.7).** A VRF allowed to reach a transit End SID lets
a tenant forge an SRH into another domain. The `inet srv6_edge` filter on the gateway ports comes
first. The gate adds the attack test from §8.7 next to the positive one.

**P7 — Coexistence proof.** The point of the exercise: `networking-bgpvpn` with the **bagpipe**
driver and `networking-srv6` enabled simultaneously, both functional. This is the acceptance test
that the original arrangement could not pass, and it belongs in the evidence tree.

---

## 6. Testbed changes

Node 1 = `ale` / 192.168.0.160 (controller + compute), node 2 = this workstation / 192.168.0.107.

- **Deploy by re-stacking** (decided 2026-09-10; `P5-PLAN.md` §6–7). The repo ships a devstack
  plugin, and `node{1,2}-srv6-agent-local.conf` are the stock confs plus `enable_plugin
  networking-srv6-agent …`, `SRV6_LOCATOR` and `SRV6_UNDERLAY_INTERFACE`. The stock confs keep
  upstream BGPVPN with its Dummy driver, which is half of the coexistence case.
- **Agent config** is written by devstack's `configure_l2_agent`, an `iniset`. That removes the
  recorded trap — appending a second `[agent]` section splits the existing one and silently drops
  its earlier keys.
- **The neutron-lib fork can be dropped** once nothing imports `bgpvpn_srv6*`. Removing it also
  removes the layered-venv recipe for running unit tests.
- **`devstack@q-agt` does not restart cleanly**: `systemctl restart` leaves the unit
  `deactivating` on the old MainPID while the old code keeps running, so any test after it silently
  exercises the previous build. `kill -9` the MainPID, restart, confirm the PID changed. Allow
  ~50 s.
- **`devstack@q-svc` is not currently running** on node 1 — never started since a reboot rather
  than crashed.
- Keep the old BGPVPN-based deployment reachable until P5's gate passes. Do not migrate the testbed
  and then discover the dataplane regressed. The way back is the documented two-pass workflow —
  stock conf, `./stack.sh`, `inject-srv6.sh` — then recreate the VPN.

---

## 7. Publishing

The repo is private and follows the convention in `implementation/srv6-plugin/push-fork.sh`:
GitHub user `costa99`, SSH remote. Unlike the two forks it has no upstream, so `origin` points
straight at the private repo.

`gh` is not installed on this workstation and there is no API token, so the empty repo has to be
created in the browser — the same manual step `push-fork.sh` already documents for the other two.

```bash
# once, in the browser: https://github.com/new
#   owner costa99 · name networking-srv6-agent · PRIVATE
#   no README, no .gitignore, no licence  (this repo already has all three)
git -C ~/OpenStackStudy/networking-srv6-agent push -u origin main
```

The remote is already configured and the first commit is made; only the empty GitHub repo is
missing. **Do not push to `costa99/networking-srv6`** — that is the OVN project, and this repo's
history is unrelated to it.

---

## 8. Decisions

Resolved 2026-09-09. Each was a genuine fork; the reasoning is recorded so it does not get
re-litigated.

### 8.1 Router associations stay rejected — and the missing check gets written

A domain accepts **network** associations only. `create_router_assoc_precommit` continues to raise,
as it does today.

Alongside that, write the validation that was specified and never implemented.
`_validate_net_assoc` currently rejects only `router:external` networks; the design also called for
rejecting **a network that already has a Neutron router interface**, and that check does not exist.
Without it a routered network can be attached today, leaving two things routing for the same
subnet — the SRv6 VRF gateway port `svp-<fid>-<vlan>` and the Neutron router port — with nothing
validating the outcome. Rejecting it makes the island semantics true instead of merely intended.

**Consequence to state plainly in the docs:** a domain is an island. Traffic between VMs inside it
works; north-south (floating IPs, SNAT) and inter-domain routing do not. Instances keep their
ordinary Neutron connectivity on their other interfaces, so this is a restriction on what the
domain carries, not on what a VM can do.

**Ownership (decided 2026-09-10).** A network can join a domain only if the domain's project owns
it — the comparison bgpvpn makes at `networking_bgpvpn/neutron/services/plugin.py:207`, and which
this plan originally left out. Policy does not replace it: the association's policy authorises the
caller against the association's own `project_id` (the parent-owner personas cannot be used —
neutron resolves them only for four hard-coded parent types, `P4-PLAN.md` §5), and says nothing
about who owns the network. The association takes the domain's `project_id`, so an
admin can act on a tenant's behalf. **Shared networks are allowed** if the domain's project owns
them. Consequence: other projects' instances on that shared network join the domain, and the gateway
port answers for its gateway on every node — the owner chose that by sharing it. Details in
`P4-PLAN.md` §4.3.

#### Implementing router associations later — how, and what it costs

The mechanism is known, because bagpipe already does it for MPLS. A `RouterAssociation` supplies
the subnet's real **gateway MAC** (`_get_gateway_mac_by_subnet`), and the agent installs an OVS
flow redirecting frames addressed to that MAC into the VPN dataplane
(`networking_bagpipe/agent/bgpvpn/agent_extension.py:570` `_redirect_br_tun_to_mpls`), plus an ARP
responder so the VM gets an answer for a MAC no interface owns. bagpipe also keeps a synthetic
default (`GATEWAY_MAC = "00:00:5e:00:43:64"`) for the no-router case. The SRv6 version would be the
same shape, redirecting into the VRF rather than into MPLS.

**Pros**

- **North-south becomes possible.** Floating IPs and SNAT could traverse the domain instead of
  bypassing it — today the domain cannot reach the outside world at all.
- **Inter-domain routing becomes possible**, and with it the more interesting TE cases, since a
  steered path that can also reach a gateway is far more useful than one that cannot.
- **Removes the biggest practical restriction.** Most real tenant networks have a router. As it
  stands, adopting an SRv6 domain means giving that up, which rules out most existing workloads.
- **Attach-a-router is better UX** than attaching N networks one at a time.
- It composes with the border-speaker design, which needs an egress point to exist at all.

**Cons**

- **It breaks the design's cleanest property.** `DESIGN.html` currently claims "the virtual switch
  is never asked to understand any of it" — the gateway is a real OVS internal port owning a real
  address inside the VRF, and traffic arrives by ordinary L3 forwarding. Redirect flows put
  SRv6-specific state into `br-int` and that claim is gone.
- **Two gateways, one subnet.** The Neutron router port and the VRF gateway port would both answer
  for the subnet gateway. Which one owns ARP has to be decided and enforced, not left to timing.
- **DVR and L3HA multiply it.** With DVR the router is distributed, so the redirect must be
  installed on every node; with L3HA there is a VRRP master that moves. Both are ordinary Neutron
  deployments and both complicate the flow lifecycle.
- **Security groups get worse before better.** Risk R2 is already open — the gateway port bypasses
  the OVS firewall by construction. Adding a second ingress path into the VRF widens that.
- **Testing burden.** Every case above needs a scenario, and the testbed is two nodes.

**Verdict:** correct as future work, wrong as thesis scope. Record it as a design chapter with the
mechanism named, exactly as R5 already does, and do not build it.

### 8.2 A project may own many domains

Unchanged from the current model: a domain is a resource, a project can own several, each gets its
own function id and VRF.

The alternative — one implicit domain per project, derived from `project_id` — would delete the
resource and its association API, a large share of §3.3's new code. It was rejected because it
removes the grouping that is the feature: every network in a project would be permanently mutually
reachable with no way to keep two sets apart, and TE would lose the ability to steer one workload
group differently from another inside one tenant. Pool capacity does not decide it either way
(4,095 function ids).

### 8.3 `End.DT46` only, but keep the attribute

The API offers `srv6_behavior` with exactly one valid value.

Today the attribute accepts `End.DT4` and `End.DT6` and the driver rejects them, so the API
advertises a choice the dataplane does not have. A fresh API should not inherit that. The attribute
itself stays rather than being dropped: it documents the behaviour explicitly, and adding
single-family support later becomes a new enum value instead of a new attribute. There is no
identified use case — `End.DT46` serves an IPv4-only domain perfectly well.

### 8.4 The L2 agent extension stays — and the restriction gets documented

Per-node code continues to run as an extension of the OVS L2 agent, which supplies
`agent_api.request_int_br()` for the integration bridge and `LocalVlanManager` for the
network→local-VLAN mapping. A standalone agent would mean rebuilding both; `networking-sr` took
that route and had to drop OVS entirely, writing its own agent, firewall driver and interface
driver.

Two things to add, because the restriction is currently invisible:

- **Check `driver_type` in `initialize()`.** It is already a parameter and is never read. Refuse
  anything other than `ovs_const.EXTENSION_DRIVER_TYPE`, the value the OVS agent passes
  (`ovs_neutron_agent.py:338-339`), and say why. Correction (2026-09-10): the neutron on this
  testbed has **no linuxbridge driver** at all — `plugins/ml2/drivers/` holds macvtap, mech_sriov,
  openvswitch and ovn. So the agents that can load this extension by mistake are macvtap (which
  passes `'macvtap'`) and SR-IOV, not linuxbridge.
- **State the ML2/OVS-only restriction in the README and the docs.** Under **ML2/OVN — the default
  in modern OpenStack — there is no Neutron L2 agent at all**, so this plugin has no host and
  cannot run. That is the honest boundary of the claim, and it is very likely why the sibling
  project `costa99/networking-srv6` pursued an OVN-native design instead.

### 8.5 No admin VRF — steering is an admin-only API over tenant VRFs

Resolved 2026-09-10. The proposal: a second class of VRF, owned by the admin and present on every
node, beside the per-domain tenant VRFs — tenants reach only their own machines, the admin has full
control. The clarified intent: the admin, and only the admin, chooses which traffic to steer, down
to a single destination VM; forces it through named nodes or keeps it off a node; and never reaches
into a tenant VM.

**Standards are not the obstacle.** RFC 8986 lets a node hold any number of `End.DT46` SIDs, one per
VRF, and RFC 9252 defines a global-table service beside VPNs. RFC 8402 defines the SR domain as
under one administrator — which it is here — and the per-node admin state the architecture calls
for already exists: each node's `End` SID `<locator>:fff0::`.

**Why it is still not built: control is not reach.** What was asked for is *control*. Steering a
domain's traffic happens inside that domain's own VRF, by changing the segment list on its encap
routes; `srv6_te_path` does exactly that and is admin-only. An admin VRF adds nothing to control. It
adds only *reach* — a data-plane path for the admin's own packets — and reach into tenant VMs was
ruled out. It would not hold together anyway: Neutron allows overlapping tenant addresses, so one
table cannot route to two tenants' `10.0.0.5`; replies need a route back to the admin in every
tenant VRF, which makes the admin VRF reachable from every tenant; and one-way reach then needs a
stateful firewall. It would also end the island semantics of §8.1.

What the requirement becomes:

- **A per-VM selector.** `srv6_te_path` gains `destination_port_id` beside `destination_host`;
  precedence port path > host path > direct (§4, P6).
- **"Avoid a node" is an explicit waypoint list.** In this flat underlay the default path is direct,
  and a compute node lies on a path only if a TE path names it — so avoiding it means not listing
  it. Constraint-based paths ("exclude X, compute the rest") need a topology model and path
  computation, and are not built.

**Out of scope:** steering the hosts' own traffic in the main table. That is the one reading that
genuinely needs a global-table service, and a bad path there can cut a node off from the controller
— after which the agent can no longer receive the correction.

### 8.6 Domain VRFs must be sealed — today a miss falls through to `main`

Found 2026-09-10 on the running BGPVPN-based testbed; the ported dataplane inherits it. RFC 8754
§5.1 requires packets addressed to SIDs from outside the trusted domain to be dropped, and tenant VMs
are outside it. Evidence: `implementation/srv6-plugin/evidence/vrf-fallthrough.txt`.

The mechanism: the VRF's table is consulted through the `l3mdev` rule at pref 1000, and a miss there
does not stop the lookup — rule processing continues to `main` at 32766. `ensure_vpn` installs no
default route in the VRF table, so every miss falls through, and `main` holds every domain's
`End.DT46` SID. Kernel lookups on node 1, as if ingressing tenant 3783's gateway port:

```
ip -6 route get fc00:0:1:dfb:: from fe80::1 iif svp-3783-5
  → encap seg6local action End.DT46 vrftable 13579        # into ANOTHER tenant's VRF
ip -4 route get 192.168.0.107 from 10.90.2.21 iif svp-3783-5
  → dev enp2s0f1                                           # out to the underlay
ip -6 route get fc00:0:2:dfb:: from fe80::1 iif svp-3783-5
  → via fd00:aa::2 dev enp2s0f1                            # to node 2's copy of that SID
```

So a VM in one domain can address another domain's SID, on its own node or across the underlay, and
be decapsulated into that domain's VRF. The default security group allows all egress and port
security checks only the source, so nothing in OVS stops it. These are FIB decisions; no packet has
been injected to confirm the path end to end.

**Fix (P5):** an `unreachable default metric 4278198272` in every VRF table, both families — the
idiom in the kernel's `Documentation/networking/vrf.rst`. Every steering guarantee in §8.5 depends
on it: a path that can be bypassed by addressing a SID directly is not a guarantee. Until P5 lands,
the same two commands can be applied by hand per VRF on the testbed; `networking-bgpvpn` itself is
not changed.

**Amended 2026-09-10, after the two-node gate: the seal alone breaks encapsulation.** After seg6
encapsulates a packet that arrived on a gateway port, the kernel routes the outer IPv6 header in
the same VRF. The seal therefore dropped every cross-node packet: `Ip6InNoRoutes` +1 per echo
request on both nodes (`REPORT-P5-two-node-2026-09-10.md` §9). The old build only worked through
the very fall-through this section closes. So **do not** seal the old BGPVPN build by hand as the
paragraph above suggests; it breaks the same way.

The complete fix is the seal plus one rule per (domain, remote node), placed ahead of l3mdev:
`ip -6 rule add pref 999 iif sv6vrf-<fid> to <own remote decap SID>/128 lookup main`
(`P5-PLAN.md` §4.6). A domain may leave its VRF only toward its own decap SIDs. Every other SID
and the underlay stay sealed.

For P6 it fails closed: no rule is added toward transit End SIDs. Letting a VRF reach them needs
SRH filtering or HMAC first — decided in §8.7.

### 8.7 P6 guard: edge filtering at the gateway ports

Decided 2026-09-10: edge filtering, not HMAC. TE may not add a SID rule toward a transit End SID
until this filter exists.

**The threat, concretely.** Take three nodes: A (`fc00:0:1::/48`), T (`fc00:0:3::/48`) and B
(`fc00:0:2::/48`). An admin steers red's traffic A → T → B. The encap route on A is then
`segs [fc00:0:3:fff0:: (T's End), fc00:0:2:95a:: (red's decap on B)]`. The outer destination is
the first segment, and the outer lookup happens in red's VRF (§8.6 amendment), so red needs a rule
toward T's End SID.

A rule matches by destination. It cannot tell the agent's encapsulation from a packet a tenant
built by hand. A red VM can send:

```
outer: dst fc00:0:3:fff0::                       ← allowed out by the rule
SRH:   [fc00:0:3:fff0::, fc00:0:2:9e8::]         ← last segment = BLUE's decap SID on B
inner: IPv4 → a blue VM
```

T's `End` does not know who wrote the SRH: it advances and forwards. B's blue `End.DT46`
decapsulates, and red has injected into blue — the §8.6 leak, reopened one hop later. The last
segment could equally be an underlay address. All it takes is a routable IPv6 source: any
dual-stack tenant has one, and a port without port security can spoof one.

P5's rules toward a domain's **own** decap SIDs do not have this problem. A packet sent there can
only be decapsulated into the same domain, and RFC 8986 has `End.DT46` discard packets whose
Segments Left is not zero. An End SID, in contrast, forwards to whatever the tenant listed next.

**The decision.** RFC 8754 §5.1 asks an SR domain to filter at its edge, and tenant VMs are
outside the domain. Their packets enter through the gateway ports, so the filter goes there, in
its own nftables table. Neutron's iptables-nft tables are never touched. The syntax was checked in
`nft -c` mode against nftables 1.0.9 on node 1:

```
table inet srv6_edge {
  set sid_space { type ipv6_addr; flags interval; elements = { <every registered locator> } }
  chain prerouting {
    type filter hook prerouting priority raw;
    iifname "svp-*" ip6 daddr @sid_space counter drop    # the essential rule
    iifname "svp-*" rt type 4 counter drop               # any tenant-originated SRH
  }
}
```

**Why it spares the agent's own traffic.** A tenant packet passes PREROUTING with `iif svp-*`
before it is routed. The agent's encapsulation happens after that routing decision (seg6
lwtunnel, input path), so the outer header never passes PREROUTING. This is the one claim the unit
tests cannot make. The P6 gate proves it:
- **Positive:** a steered path works and the drop counters stay at 0.
- **Attack:** an attacker namespace on a red network encapsulates toward
  `segs <T End>,<blue decap SID>`. With the filter the counter rises and blue sees nothing; without
  it, blue receives the packet.

**In the agent (P6):**
- a privsep entrypoint running `nft -f -`, replacing the table atomically;
- the dataplane renders the ruleset from the known locators, applies it at `initialize_node` and
  on every locator change or resync, and records whether it is active;
- `_install_routes` adds a SID rule toward `transit[0]` **only if the filter is active**; otherwise
  it keeps the P5 fail-closed warning;
- missing `nft` or a failed apply is an error, and End-SID rules stay disabled;
- there is no knob to switch the filter off.

**Why not HMAC.** An SRH HMAC (`seg6 hmac`, `seg6_require_hmac` on transit) would also stop a
forged SRH, and it would additionally protect against an untrusted underlay. The costs are a
shared key distributed to every node, 40 bytes per packet and per-packet CPU, and every
encapsulation on every node would have to carry it. The threat here is the tenant, and the edge
filter answers it where it enters. HMAC stays available if the underlay ever leaves the lab
(`DESIGN-border-speaker.md` §9 already says so for external peering).
