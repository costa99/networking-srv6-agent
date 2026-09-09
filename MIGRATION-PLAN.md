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
| Policies | 120 | domain = project-scoped; TE + locators = admin-only |
| `setup.cfg`, entry points, devstack plugin, docs | 60 | |

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
srv6_domain                     project-scoped, the grouping + isolation object
  id, name, project_id
  srv6_behavior      (End.DT46; create-only)
  sid_function       (read-only, server-allocated)
  networks           (via associations)

  /v2.0/srv6/domains/{id}/network_associations      project-scoped

srv6_te_path                    ADMIN ONLY, top level — a fabric fact, not a tenant fact
  id, domain_id, destination_host, via_hosts[], segments[] (read-only)
  /v2.0/srv6/te_paths

srv6_locator                    ADMIN ONLY, read-only
  host, locator, updated_at
  /v2.0/srv6/locators
```

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
- **Policy fails closed.** oslo.policy denies a rule it has never registered
  (`oslo_policy/policy.py:1089`, *"If the rule doesn't exist, fail closed"*). Any attribute
  carrying `enforce_policy: True` must have a registered default in the same commit, or every call
  — admin included — returns 403 with nothing in the logs explaining why.

---

## 5. Sequence

Each phase leaves the tree importable and testable.

**P1 — Skeleton and packaging.** `setup.cfg` with the entry points below, `tox.ini`, `requirements.txt`, licence headers. Verify the package imports and `neutron.policies` resolves.

```
[neutron.service_plugins]        srv6 = networking_srv6.services.plugin:Srv6Plugin
[neutron.agent.l2.extensions]    srv6 = networking_srv6.agent.agent_extension:Srv6AgentExtension
[neutron.db.alembic_migrations]  networking-srv6 = networking_srv6.db.migration:alembic_migrations
[neutron.policies]               networking-srv6 = networking_srv6.policies:list_rules
[oslo.config.opts]               networking-srv6 = networking_srv6.common.config:list_opts
```

> Write `setup.cfg` by hand and check the entry points land in the built dist. The
> `networking-bgpvpn` checkout's `setup.cfg` is truncated to 36 bytes on every branch and has lost
> its `entry_points` section — the installed dist is the only surviving copy. Do not inherit that
> failure mode.

**P2 — Pure layers, no Neutron coupling.** Port `privileged/`, `common/sid.py`,
`common/constants.py`, `common/config.py` and their tests. These should pass immediately; if they
do not, something was BGPVPN-coupled that was not supposed to be.

**P3 — Data model.** `srv6_domain` + associations, the allocation pool and locator registry
retargeted, the TE tables, one fresh initial migration. Port the DB tests. Watch for skips.

**P4 — Service plugin and API.** Extension descriptors, plugin CRUD, policies. Domain create →
function id allocated → visible over REST. Nothing programs a kernel yet.

**P5 — Agent and dataplane.** Port `dataplane.py` and `agent_extension.py` with their tests, and
the RPC. This is the phase that reproduces the working system.

**Gate:** the existing two-node lifecycle — domain create, network association, kernel state, VM
to VM ping across nodes, delete, cleanup — passes exactly as it does today under BGPVPN. Compare
against `implementation/srv6-plugin/evidence/` rather than judging by eye.

**P6 — Traffic engineering.** The TE path resource, segment-list construction, per-route MTU. The
design work is already done in the TE plan; the only change here is that the resource is top level
from the start, which makes the descriptor and plugin methods simpler than that plan describes.

**P7 — Coexistence proof.** The point of the exercise: `networking-bgpvpn` with the **bagpipe**
driver and `networking-srv6` enabled simultaneously, both functional. This is the acceptance test
that the original arrangement could not pass, and it belongs in the evidence tree.

---

## 6. Testbed changes

Node 1 = `ale` / 192.168.0.160 (controller + compute), node 2 = this workstation / 192.168.0.107.

- **`local.conf`**: add `networking-srv6` to `enable_plugin`; `service_plugins` gains `srv6`;
  remove `service_provider = BGPVPN:SRv6:...`. If BGPVPN stays enabled it reverts to its own
  driver, which is the coexistence case.
- **Agent config**: `[agent] extensions = srv6` replaces `bgpvpn_srv6`. Note the recorded trap —
  appending a second `[agent]` section splits an existing one and silently drops the earlier keys.
- **The neutron-lib fork can be dropped** once nothing imports `bgpvpn_srv6*`. Removing it also
  removes the layered-venv recipe for running unit tests.
- **`devstack@q-agt` does not restart cleanly**: `systemctl restart` leaves the unit
  `deactivating` on the old MainPID while the old code keeps running, so any test after it silently
  exercises the previous build. `kill -9` the MainPID, restart, confirm the PID changed. Allow
  ~50 s.
- **`devstack@q-svc` is not currently running** on node 1 — never started since a reboot rather
  than crashed.
- Keep the old BGPVPN-based deployment reachable (a second branch, or a snapshot) until P5's gate
  passes. Do not migrate the testbed and then discover the dataplane regressed.

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

## 8. Open questions

1. **Does `srv6_domain` need router associations?** Rejected today. Without a router, inter-domain
   and north-south traffic has no path. Fine for a thesis; needs stating.
2. **One domain per project, or many?** Many is more flexible and matches the current model. One
   would let the domain be implicit and remove a resource — at the cost of the any-to-any grouping
   that makes this an L3VPN rather than a flat routed network.
3. **Keep `End.DT46` only?** `End.DT4` / `End.DT6` are accepted by the API and rejected by the
   driver today. The new API can simply not offer them.
4. **Does the L2 agent extension remain the right host**, or should this be a standalone agent as
   `networking-sr` chose? The extension keeps OVS and the gateway-port mechanism intact and is
   working; a standalone agent would be a much larger change with no identified benefit.
