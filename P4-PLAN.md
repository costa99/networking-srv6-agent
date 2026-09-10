# P4 — Service plugin and API

Detailed plan for phase 4 of `MIGRATION-PLAN.md` §5. P1–P3 are complete and verified: the package
builds, the pure layers and the data model are ported, 111 unit tests pass with zero skips, and the
expand migration applies with no drift against the models.

**What P4 delivers.** A working REST API and the service plugin behind it. `openstack`-visible
domain CRUD, network associations, a read-only locator collection, policies, and the server half of
the agent RPC. **Nothing programs a kernel yet** — that is P5.

**Gate.** Domain create → function id allocated → visible over REST; network association accepted
for an ordinary tenant network and refused for an external, routered or other-project one;
`neutron-server` starts with `service_plugins = ...,srv6`.

**Revised 2026-09-10** for `MIGRATION-PLAN.md` §8.5/§8.6 and three decisions taken that day:
network associations require the domain's own project (shared networks allowed, §4.3);
`sid_function` is admin-only to read (§5); P5 re-stacks the testbed with a devstack plugin
(`P5-PLAN.md`). §4.1, §4.2 and §4.4 also pin what P5 and P6 depend on.

---

## 1. Three scope questions §5 leaves open

`MIGRATION-PLAN.md` is ambiguous in three places. Resolving them first, because each changes what
gets written.

### 1.1 `services/rpc.py` moves from P5 to P4

§5 lists the RPC under P5 ("Port `dataplane.py` and `agent_extension.py` with their tests, and the
RPC"). But §3.2 keeps `start_rpc_listeners` and `handle_sync_state` in P4's plugin core, and
`_push_vpn` calls `agent_rpc.vpn_updated`. **P4's plugin does not import without `rpc.py`.**

Port `services/service_drivers/srv6/rpc.py` → `services/rpc.py` in P4. It is the *server* half in
both directions (`Srv6AgentNotifyAPI` sends, `Srv6ServerRpcCallback` receives `sync_state`); P5
ports only the agent-side consumer. Stubbing it in P4 would mean writing throwaway code.

### 1.2 `srv6_locators` lands in P4; `srv6_te_path` waits for P6

§3.3 lumps all four API definitions together, but §5 puts the TE path resource in P6. Split them:

- **P4** — `srv6_domain`, its `network_associations` sub-resource, and `srv6_locators`.
- **P6** — `srv6_te_path`.

Locators belong in P4 because the model and its CRUD already exist from P3 (`srv6_db.SRv6HostLocator`,
`get_host_locators`), the resource is read-only with no plugin logic of its own, and enabling
`neutron.service_plugins` at the end of P4 exposes whatever descriptors are present — adding it later
means a second round of policy registration and alias changes. It also proves the admin-only policy
path before P6 depends on it.

**State plainly in the docs:** until P5 exists, `GET /v2.0/srv6_locators` always returns `[]`, because
the rows are written only by an agent's `sync_state`. That is correct, not broken.

### 1.3 URLs: `/v2.0/srv6_domains`, not §4's `/v2.0/srv6/domains/…`

§4 sketches `/v2.0/srv6/domains/{id}/network_associations`. That cannot be built as written. Neutron
derives the oslo.policy rule names from the resource *member* name in the attribute map, and the URL
segment from the *collection* name — they are the same string. A collection named `domains` produces
policy rules called `create_domain`, `get_domain`, `update_domain` in oslo.policy's **global,
cross-service** namespace. That is a collision waiting to happen and unreadable in `policy.yaml`.

| | collection | URL | policy rule |
|---|---|---|---|
| §4 as written | `domains` | `/v2.0/srv6/domains` | `create_domain` ✗ too generic |
| bgpvpn's shape | `srv6_domains` + prefix | `/v2.0/srv6/srv6_domains` | `create_srv6_domain` — but stutters |
| **chosen** | `srv6_domains`, no prefix | `/v2.0/srv6_domains` | `create_srv6_domain` |

Drop `path_prefix`. The `srv6_` prefix on each collection already groups the three resources, and
without a prefix the sub-resource `ResourceExtension` needs no `path_prefix` kwarg either — less code.
This is also what `networking-sr` does. §4 of MIGRATION-PLAN.md needs updating to match.

Final URL surface:

```
/v2.0/srv6_domains                                        project-scoped
/v2.0/srv6_domains/{id}
/v2.0/srv6_domains/{domain_id}/network_associations       project-scoped
/v2.0/srv6_domains/{domain_id}/network_associations/{id}
/v2.0/srv6_locators                                       ADMIN, read-only
/v2.0/srv6_te_paths                                       ADMIN            (P6)
```

---

## 2. Files

```
networking_srv6_agent/
  extensions/
    srv6.py                 API definition + descriptor + PluginBase ABC + exceptions   (~230)
    srv6_locators.py        API definition + descriptor + ABC                            (~70)
  services/
    rpc.py                  ported from driver-side rpc.py, renamed                     (~115)
    plugin.py               the service plugin                                          (~330)
  policies/
    __init__.py             list_rules()                                                 (~25)
    srv6_domain.py                                                                       (~90)
    network_association.py                                                               (~70)
    srv6_locator.py                                                                      (~35)
  tests/unit/
    extensions/__init__.py, test_srv6.py, test_srv6_locators.py                         (~180)
    services/test_plugin.py, test_rpc.py                                                (~520)
    policies/__init__.py, test_policies.py                                              (~110)
```

Roughly **970 lines of source and 810 of tests**, against §3.3's estimate of 250 + 80 + 250 + 120 =
700 for the P4 slice. The overrun is the RPC moved in from P5 (115) and the policy-coverage test,
which §3.3 did not budget for.

---

## 3. API definitions and extension descriptors

Defined in-repo, as `networking-sr` does — **no neutron-lib fork**. This is the change that lets the
layered-venv recipe die.

### 3.1 `extensions/srv6.py`

Two maps in one module, because the associations are a sub-resource of the domain and neutron builds
them together:

```python
ALIAS = 'srv6'
RESOURCE_NAME    = 'srv6_domain'
COLLECTION_NAME  = 'srv6_domains'
NET_ASSOCS       = 'network_associations'

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id':            {allow_post: False, allow_put: False, 'type:uuid',
                          is_visible: True, primary_key: True},
        'project_id':    {allow_post: True,  allow_put: False, required_by_policy: True,
                          'type:string': PROJECT_ID_FIELD_SIZE,
                          is_filter: True, is_sort_key: True, is_visible: True},
        'name':          {allow_post: True,  allow_put: True, default: '',
                          'type:string': NAME_FIELD_SIZE,
                          is_filter: True, is_sort_key: True, is_visible: True},
        'srv6_behavior': {allow_post: True,  allow_put: False,
                          default: constants.DEFAULT_BEHAVIOR,
                          'type:values': constants.VALID_BEHAVIORS,
                          is_visible: True, enforce_policy: True},
        'sid_function':  {allow_post: False, allow_put: False,
                          is_visible: True, enforce_policy: True},
        'networks':      {allow_post: False, allow_put: False, is_visible: True},
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {
    NET_ASSOCS: {
        'parent': {'collection_name': COLLECTION_NAME, 'member_name': RESOURCE_NAME},
        'parameters': { 'id', 'project_id', 'network_id' },   # network_id: type:uuid, enforce_policy
    },
}
```

Notes that matter:

- **`srv6_behavior` validates against `constants.VALID_BEHAVIORS`**, which P2 already set to
  `['End.DT46']`. This is §8.3 landing: the API now advertises exactly the choice the dataplane has.
  It also **deletes `driver.py:103-114` `_validate_behavior` outright** — the old code accepted three
  values at the API and rejected two in the driver.
- **`allow_put: False` on `srv6_behavior`** — create-only, matching the DB layer, which already
  ignores it in `update_domain`.
- `project_id`, not `tenant_id`. bgpvpn still carries `tenant_id` in its sub-resource map; there is
  no reason to inherit that.
- No `route_targets`, `route_distinguishers`, `type`, or `routers`. With them go
  `driver.py:223-232`'s two rejections — an API that never offers the field needs no check.

The descriptor follows bgpvpn's `Bgpvpn.get_resources()` (`neutron/extensions/bgpvpn.py:96-137`):
`resource_helper.build_plural_mappings` + `build_resource_info` for the top-level resource, then a
loop over `SUB_RESOURCE_ATTRIBUTE_MAP` calling `base.create_resource(..., parent=parent)` and
wrapping each in a `ResourceExtension`. Pass `register_quota=True` — the function-id pool is finite
(4,095) and shared across projects, so an unquota'd create is a cross-tenant exhaustion vector.

### 3.2 The class-naming trap

Neutron discovers extensions by **scanning a directory and deriving the class name from the
filename**: `neutron/api/extensions.py:456` does `ext_name = mod_name.capitalize()`. So

| file | required class name |
|---|---|
| `extensions/srv6.py` | `Srv6` |
| `extensions/srv6_locators.py` | `Srv6_locators` |

`str.capitalize()` lowercases everything after the first character, so `Srv6Locators` would **not** be
found and the extension would be silently absent — a 404 with nothing in the log. Both descriptor
modules must also be reachable, which needs, once, at import of each descriptor module:

```python
extensions.append_api_extensions_path(networking_srv6_agent.extensions.__path__)
```

### 3.3 The plugin interface ABC

`Srv6PluginBase(libbase.ServicePluginBase)` with `get_plugin_type()` returning `ALIAS`,
`supported_extension_aliases = [ALIAS]`, and abstract methods matching neutron's derived names:

```
create_srv6_domain / get_srv6_domain / get_srv6_domains / update_srv6_domain / delete_srv6_domain
create_srv6_domain_network_association
get_srv6_domain_network_association / get_srv6_domain_network_associations
delete_srv6_domain_network_association
```

**No router-association methods at all** (§8.1) — not abstract, not implemented, not in the attribute
map. `driver.py:307-310`'s `create_router_assoc_precommit` raise is deleted rather than ported: a
method that only ever raises is worse than an endpoint that does not exist, because it 500s where a
404 is the honest answer.

`update_srv6_domain_network_association` is also omitted. bgpvpn carries one with a `TODO(amotoki):
PUT operation is not defined in the API ref. Drop it?` against its policy. There is nothing on an
association to update.

Exceptions, in `extensions/srv6.py` alongside the definition:
`Srv6DomainNotFound`, `Srv6DomainNetAssocNotFound`, `Srv6DomainNetAssocAlreadyExists`,
`Srv6ExternalNetworkNotAllowed`, `Srv6RouteredNetworkNotAllowed`, `Srv6NetworkNotOwned`
(a `NotAuthorized`, so 403 — §4.3), `Srv6NoFunctionIdAvailable`.

---

## 4. The service plugin

`services/plugin.py`, `@registry.has_registry_receivers class Srv6Plugin(srv6.Srv6PluginBase)`.

### 4.1 Port map from `driver.py` (441 lines)

| `driver.py` | → | change |
|---|---|---|
| `_ensure_pool` :80-92 | keep | none |
| `_core_plugin` :97, `_vrf_table` :100 | keep | none |
| `_network_info` :119-138 | keep | none |
| `_routes_for_vpn` :140-173 | `_routes_for_domain` | rename; every route keeps `{prefix, host, port_id}` — P6's per-VM precedence needs `port_id` (§4.4) |
| `_port_is_relevant` :175-182 | keep | none |
| `_build_vpn_payload` :184-204 | `_build_domain_payload` | behaviour read from the domain dict, not `bgpvpn_srv6_def` |
| `_push_vpn` :206-209 | `_push_domain` | `agent_rpc.domain_updated` |
| `_push_vpn_by_id` :211-218 | `_push_domain_by_id` | `domain_db.get_domain` returns `None`; drop the bare `except Exception` |
| `_vpns_for_network` :315-317 | `_domains_for_network` | **use `domain_db.get_domain_ids_for_network`** — P3 already wrote it, and it is one indexed query instead of a filtered list of full dicts |
| `_notify_port` :319-357 | keep | renames |
| `registry_port_updated` :359-382, `registry_port_deleted` :384-392 | keep | none |
| `start_rpc_listeners` :397-409 | keep | now called by neutron directly; the delegation shim in `BGPVPNPlugin.start_rpc_listeners` disappears |
| `handle_sync_state` :411-441 | keep | iterates `domain_db.get_domains` |
| `_validate_behavior` :103-114 | **delete** | the API definition enforces it (§8.3) |
| `create/delete/update_bgpvpn_*commit` :223-286 | **fold into CRUD** | §4.2 |
| `create/delete_net_assoc_*commit` :298-305 | **fold into CRUD** | §4.2 |
| `create_router_assoc_precommit` :307-310 | **delete** | §8.1 |
| `_validate_net_assoc` :291-296 | **extend** | §4.3 |

### 4.2 Transaction discipline without the hook framework

§3.2: *"write inside the transaction, fan out RPC after it commits"*. Delta **D5** is why — a
postcommit hook cannot look up what the commit destroyed, which is how the function-id release was
silently a no-op. Preserve it explicitly:

```python
def create_srv6_domain(self, context, srv6_domain):
    d = srv6_domain['srv6_domain']
    self._ensure_pool(context)
    with db_api.CONTEXT_WRITER.using(context):        # both, or neither
        domain = domain_db.create_domain(context, d)
        domain['sid_function'] = srv6_db.allocate_function_id(
            context, domain['id'])
    return domain                                     # no RPC: a domain with
                                                      # no networks has no
                                                      # presence on any node

def delete_srv6_domain(self, context, id):
    function_id = srv6_db.get_function_id(context, id)   # BEFORE the delete
    with db_api.CONTEXT_WRITER.using(context):
        if domain_db.delete_domain(context, id) is None:
            raise srv6.Srv6DomainNotFound(id=id)
        srv6_db.release_function_id(context, id)
    if function_id is None:
        LOG.warning(...)                                 # nothing to name
        return
    self.agent_rpc.domain_deleted(context, id, function_id,
                                  self._vrf_table(function_id))
```

Three things this gets right that are easy to lose:

1. **The explicit `CONTEXT_WRITER.using` around create.** `domain_db.create_domain` and
   `srv6_db.allocate_function_id` each carry their own `@db_api.CONTEXT_WRITER`, which *joins* an
   outer writer rather than opening a second one. Without the `with`, they are two transactions and a
   pool-exhaustion failure leaves a committed domain with no function id.
2. **`get_function_id` before `delete_domain`.** The allocation FK is `ondelete='SET NULL'`, so the
   row is cleared by the database as part of the delete. This is D5 exactly, and
   `test_srv6_db.test_release_after_the_foreign_key_nulled_the_row_returns_none` is the regression
   that already guards the DB half.
3. **Never decorate the plugin CRUD methods themselves with `CONTEXT_WRITER`.** If the whole method
   runs inside a transaction, the fan-out happens pre-commit and D5 comes back in a new shape —
   agents told about state that may still roll back.

`update_srv6_domain` → `domain_db.update_domain` then `_push_domain`. Association create/delete →
validate + write, then `_push_domain_by_id(context, domain_id)` after the block.

**A tenant's delete removes admin configuration.** `srv6_te_paths.domain_id` is
`ON DELETE CASCADE` (P3, `db/te_db.py`), so deleting a domain silently deletes every TE path an
admin put on it. That is the right lifetime — a path with no domain is meaningless — but a tenant
action erasing admin state should leave a trace. In `delete_srv6_domain`, read
`te_db.get_te_paths(context, id)` **before** the delete (the D5 rule again) and `LOG.warning` the
count and ids removed. The table exists from P3, so this belongs in P4 even though the TE API is P6.

### 4.3 §8.1: the validation that was specified and never written

`driver.py:291-296` rejects only `router:external`. Add the second half:

```python
def _validate_net_assoc(self, context, domain, network_id):
    plugin = self._core_plugin()
    network = plugin.get_network(context, network_id)
    if network['project_id'] != domain['project_id']:      # ownership, below
        raise srv6.Srv6NetworkNotOwned(network_id=network_id,
                                       domain_id=domain['id'])
    if network.get('router:external'):
        raise srv6.Srv6ExternalNetworkNotAllowed(network_id=network_id)
    router_ports = plugin.get_ports(context, filters={
        'network_id': [network_id],
        'device_owner': list(n_const.ROUTER_INTERFACE_OWNERS)})
    if router_ports:
        raise srv6.Srv6RouteredNetworkNotAllowed(
            network_id=network_id, router_id=router_ports[0]['device_id'])
```

Shape borrowed from `networking_bgpvpn/neutron/services/plugin.py:128-143`
(`_validate_network_has_router_assoc`) but **simpler**: bgpvpn only refuses when the router is
*already in another BGPVPN*; here any router interface is refused, because two things routing for one
subnet — the SRv6 VRF gateway port `svp-<fid>-<vlan>` and the Neutron router port — is the condition
the design rules out.

Use `n_const.ROUTER_INTERFACE_OWNERS`, not the bare `DEVICE_OWNER_ROUTER_INTF` bgpvpn uses. The tuple
is `(router_interface, ha_router_replicated_interface, router_interface_distributed)`, so the bare
constant misses the port entirely under **L3HA and under DVR** — both ordinary deployments — and the
check would pass on a network that is in fact routed. (`DEVICE_OWNER_ROUTER_HA_INTF` is deliberately
excluded from that tuple upstream; it is the VRRP sync port on the HA network, not an interface on a
tenant subnet, so its absence is correct here too.)

**This makes the island semantics true rather than merely intended**, and the README must say so:
inside a domain, VM-to-VM works; north-south (floating IPs, SNAT) and inter-domain routing do not.

**Ownership — the check this section originally left out.** Policy alone does not cover it: the
association's policy (§5) authorises the caller against the association's own `project_id`; it
says nothing about who owns the *network*.
Without the `project_id` comparison above, a tenant could attach any network it can see —
including one owned by another project — to its own VRF. bgpvpn makes the same comparison
(`networking_bgpvpn/neutron/services/plugin.py:207-213`). Decided 2026-09-10:

- **The network must belong to the domain's project**, whoever calls. The association's own
  `project_id` is **forced to the domain's**, never taken from the caller, so an admin can associate
  on a tenant's behalf and the result is still the tenant's.
- **Shared networks are allowed** if the domain's project owns them. State the consequence in the
  docs: other projects' instances on that shared network join the domain, and on every node the
  gateway port `svp-<fid>-<vlan>` answers for that network's gateway. Sharing a network and then
  attaching it to a domain is the owner's decision.

There is a matching race: nothing stops a router interface being added to an already-associated
network afterwards. bgpvpn closes it with a `registry.receives(ROUTER_INTERFACE, [BEFORE_CREATE])`
handler (`plugin.py:93-120`). Doing the same is ~15 lines and belongs in P4 — add it, raising rather
than warning, or the check above is trivially bypassed by reordering two commands.

### 4.4 `services/rpc.py`

Straight port with the P2 constant renames already in place:

| from | to |
|---|---|
| `Srv6AgentNotifyAPI.vpn_updated` | `domain_updated` → `constants.DOMAIN_UPDATED` |
| `vpn_deleted(context, vpn_id, …)` | `domain_deleted(context, domain_id, …)` → `constants.DOMAIN_DELETED` |
| `routes_updated(context, vpn_id, …)` | `routes_updated(context, domain_id, …)` |
| `topics.get_topic_name(topic, constants.TOPIC_SRV6_BGPVPN, UPDATE)` | `constants.TOPIC_SRV6` |
| `Srv6ServerRpcCallback(driver)` | `Srv6ServerRpcCallback(plugin)` |

**Rename the kwargs too, not only the methods.** oslo.messaging dispatches by method name and passes
the kwargs through. The agent's handlers end in `**kwargs`, so if the server kept sending
`vpn=` to a handler declared `domain_updated(self, context, domain=None, …, **kwargs)`, `vpn`
would land in `**kwargs`, `domain` would stay `None`, and the handler would return **without a
word**. The contract, fixed here and consumed by P5:

```
domain_updated(context, domain=<payload>)
routes_updated(context, domain_id=, add=[...], remove=[...])
domain_deleted(context, domain_id=, function_id=, vrf_table=)
```

`P5-PLAN.md` §5 adds a test that binds every cast to the agent's named parameters.

**Payload shape, and why the version stays 1.** Each route is `{prefix, host, port_id}`, and may
carry an optional `segments` list (transit SIDs), which P6 fills in. `PAYLOAD_VERSION` is an
**equality** check on both sides, so bumping it later would make every older agent log a warning
and ignore *all* updates — a silent stall. Adding an optional key that the P5 agent already reads
(`P5-PLAN.md` §4.5) avoids the bump entirely. No version change is planned through P6.

Keep the `payload_version` check (`rpc.py:107-112`) and the comment on
`TOPIC_SRV6_PLUGIN` explaining why it must not be `topics.PLUGIN` — that one is a recorded
observation, not a theory.

---

## 5. Policies — the part that fails closed

§4: oslo.policy **denies a rule it has never registered** (`oslo_policy/policy.py:1089`). Any
attribute with `enforce_policy: True` needs a registered default in the same commit, or every call —
admin included — returns 403 with nothing in the log saying why.

Use the **current** personas, not bgpvpn's. `neutron_lib.policy`'s `RULE_ADMIN_OR_OWNER` /
`RULE_ADMIN_ONLY` sit under a `# Deprecated rules:` comment in neutron-lib 5.0.1. The live ones are in
`neutron_lib.policy.rules`, which is what neutron's own `conf/policies/network.py` uses:

| resource | create / update / delete | get |
|---|---|---|
| `srv6_domain` | `lib_rules.ADMIN_OR_PROJECT_MEMBER` | `lib_rules.ADMIN_OR_PROJECT_READER` |
| `srv6_domain:srv6_behavior` | member / — | reader |
| `srv6_domain:sid_function` | — (read-only) | **`lib_rules.ADMIN`** |
| network association | `lib_rules.ADMIN_OR_PROJECT_MEMBER` (was PARENT_OWNER — see below) | `ADMIN_OR_PROJECT_READER` |
| `srv6_locator` | — (read-only) | `lib_rules.ADMIN` |

All with `scope_types=['project']`.

**Correction (as built): not `ADMIN_OR_PARENT_OWNER_*`.** This table first proposed the parent-owner
personas for the association. They cannot work here. `rule:ext_parent_owner` is
`project_id:%(ext_parent:project_id)s`, and neutron fills `ext_parent_*` into the target only for
the parents in neutron-lib's `EXT_PARENT_RESOURCE_MAPPING` — floatingip, router, local_ip and qos
policy (`neutron/api/v2/base.py` `_set_parent_id_into_ext_resources_request`,
`neutron/policy.py` `OwnerCheck._extract`). For `srv6_domain` the rule could never pass for a
tenant. So the association carries its own `project_id` and the personas are
`ADMIN_OR_PROJECT_*`, as bgpvpn does. What policy cannot see, the plugin checks: that the caller owns
the *domain* (`Srv6Plugin._get_domain_checked`, 404 otherwise, so another project's domain id
reveals nothing), and that the domain's project owns the *network* (§4.3). The association's
`project_id` is then forced to the domain's.

**`sid_function` is admin-only to read** (decided 2026-09-10). It is half of an underlay SID —
infrastructure vocabulary, like the locators and TE paths already admin-only (`MIGRATION-PLAN.md`
§4). Tenants never need it, and because ids are handed out round-robin, seeing your own makes your
neighbours' guessable — the input the §8.6 fall-through needed. Neutron's attribute-level GET policy
drops the field from a non-admin's response rather than refusing the request, so tenants still
`GET` their domain; admins see the field for debugging.

`policies/__init__.py` mirrors bgpvpn's: `itertools.chain` over the three modules. It is currently an
empty file and **both** `neutron.policies` and `oslo.policy.policies` entry points will point at its
`list_rules`.

**Write the coverage test in the same commit** (`tests/unit/policies/test_policies.py`): walk
`RESOURCE_ATTRIBUTE_MAP` and `SUB_RESOURCE_ATTRIBUTE_MAP` of **every** descriptor module under
`extensions/` — not just `srv6.py`, so P6's `srv6_te_paths` is covered the day it lands, with no
edit to this test — collect every attribute carrying
`enforce_policy: True`, and assert that for each operation that can touch it there is a rule of that
name in `list_rules()`. This is the only cheap defence against a 403 whose cause is invisible.

---

## 6. Tests

| file | from | shape |
|---|---|---|
| `services/test_plugin.py` | rewrite of `tests/unit/services/srv6/test_driver.py` (407) | keep every test whose subject survives; drop `TestVpnCreate.test_l2_is_refused`, `test_route_distinguishers_are_refused`, `test_unimplemented_behavior_is_refused`, `TestAssociations.test_router_association_is_refused` — all four assert on API surface that no longer exists |
| `services/test_rpc.py` | `test_driver.py:388-407` `TestServerRpcCallback` | near-verbatim; `driver` → `plugin` |
| `extensions/test_srv6.py` | new | attribute-map invariants: `srv6_behavior` accepts only `End.DT46`, `allow_put` is False, `sid_function` is read-only, the sub-resource parent is wired to `srv6_domains` |
| `policies/test_policies.py` | new | the `enforce_policy` coverage walk (§5) |

Add to `test_plugin.py`, none of which exist today because the driver could not express them:

- `create_srv6_domain` allocates and returns `sid_function`
- pool exhaustion rolls back the domain row (proves §4.2's single writer)
- `delete_srv6_domain` reads the function id before the delete and still notifies (the plugin-side
  half of D5, which `test_srv6_db.py` only covers at the DB layer)
- external network refused; **routered network refused** (§8.1, new); `ha_router_replicated_interface`
  also refused
- adding a router interface to an associated network is refused
- another project's network is refused with 403 — **also when an admin calls**
- the domain owner's **shared** network is accepted
- an association created by an admin gets the domain's `project_id`, not the admin's
- a non-admin `GET` omits `sid_function`; an admin `GET` includes it
- deleting a domain that has TE paths logs their count (§4.2)

`test_plugin.py` becomes a `SqlTestCase` rather than the current `base.BaseTestCase` with a mocked
driver, because the plugin now owns real DB calls. **Zero skips stays the pass condition** — the
same `DBReferenceError`-becomes-skip trap applies.

---

## 7. Entry points

At the end of P4, uncomment in **`pyproject.toml`** (not `setup.cfg` — see the corrected §5 note):

```toml
[project.entry-points."neutron.service_plugins"]
"srv6" = "networking_srv6_agent.services.plugin:Srv6Plugin"

[project.entry-points."neutron.policies"]
"networking-srv6-agent" = "networking_srv6_agent.policies:list_rules"

[project.entry-points."oslo.policy.policies"]
"networking-srv6-agent" = "networking_srv6_agent.policies:list_rules"
```

Then confirm they land, exactly as P1 did — `pip wheel` and read `entry_points.txt` out of the wheel.
`pip install -e .` succeeding proves nothing.

---

## 8. Verification

**Unit, on this workstation** (layered venv from the P1 work):

```bash
VENV=<scratchpad>/venv
"$VENV"/bin/pip install --no-deps -e .
"$VENV"/bin/stestr run          # expect ~200 tests, 0 skipped, 0 failed
"$VENV"/bin/python -m flake8    # exit 0
"$VENV"/bin/python -c "from importlib import metadata; \
  print([ (e.group,e.name) for e in metadata.distribution('networking-srv6-agent').entry_points ])"
```

**REST, on node 1** (`stack@192.168.0.160`, passwordless ssh, NOPASSWD sudo). `service_plugins` gains
`srv6`; leave BGPVPN enabled — that is already half of P7's coexistence case.

```bash
openstack extension list --network | grep srv6          # the alias is advertised
# as demo:
openstack --os-cloud demo ... POST /v2.0/srv6_domains {"srv6_domain": {"name": "red"}}
#   → 201, srv6_behavior End.DT46, and NO sid_function field (admin-only read)
GET  /v2.0/srv6_domains/{id}  as admin                                           → sid_function non-null
POST /v2.0/srv6_domains/{id}/network_associations {"network_id": "<demo net>"}   → 201
POST .../network_associations {"network_id": "<public>"}                         → 400 external
openstack router add subnet <r> <s2>; POST .../network_associations {net2}       → 400 routered
GET  /v2.0/srv6_locators  as demo                                                → 200, [] (per-item policy filter)
GET  /v2.0/srv6_locators  as admin                                               → 200, []
DELETE /v2.0/srv6_domains/{id}                                                   → 204
```

Then check the function id returned to the pool:
`select function_id, domain_id from srv6_function_allocations where domain_id is not null;`

**Association ownership (§4.3).** Under BGPVPN, an admin creating an association against a
demo-owned network got 403, because bgpvpn compares the association's `tenant_id` (the caller's)
with the VPN's. Here the association inherits the domain's project, so the checks are:

```
as demo:  POST .../srv6_domains/{admin-owned domain}/network_associations  {demo net}   → 404 (not the domain's owner)
as admin: POST .../srv6_domains/{admin-owned domain}/network_associations  {demo net}   → 403 (ownership)
as admin: POST .../srv6_domains/{demo-owned domain}/network_associations   {demo net}   → 201, project_id = demo
```

Source `openrc demo demo` for the tenant calls, as recorded for BGPVPN.

`devstack@q-svc` is not currently running on node 1 (never started since a reboot). It has to be up
for any of this. `q-agt` does not need restarting in P4 — nothing agent-side changes yet — which is
one bring-up hazard this phase avoids.

---

## 9. Corrections this phase makes to `MIGRATION-PLAN.md`

**Applied 2026-09-10**, together with the §8.5/§8.6 decisions. The list stays here as the record
of why each was made.

1. **§4** — the URL sketch `/v2.0/srv6/domains/…` becomes `/v2.0/srv6_domains/…`, with the
   policy-namespace reason recorded (§1.3 above).
2. **§5 P5** — "and the RPC" moves to P4 (§1.1).
3. **§5 P4** — say explicitly that `srv6_locators` ships here and returns `[]` until P5.
4. **§3.3** — the P4 line-count estimate is ~700; the real figure is ~970 source + ~810 test, the
   difference being the RPC and the policy-coverage test.

---

## 10. What will bite

- **A missing policy rule is a silent 403.** Nothing in the log names the rule. If an admin gets 403
  on a brand-new endpoint, look for an `enforce_policy: True` attribute with no registered default
  before looking anywhere else. The §5 coverage test exists for this.
- **`ext_name = mod_name.capitalize()`.** A descriptor class named `Srv6Locators` instead of
  `Srv6_locators` yields a 404 and no error.
- **`register_quota=True` needs a default.** Without one the quota is unlimited, which is the current
  behaviour, so this is safe — but set `quota_srv6_domain` deliberately rather than by omission.
- **`_notify_port` fires on every port update in the cloud**, including for networks in no domain.
  `_domains_for_network` runs first and returns early; keep it that way, and keep it on the indexed
  `get_domain_ids_for_network` rather than a filtered `get_domains`.
- **Do not migrate the testbed off BGPVPN yet.** §6 is explicit: keep the BGPVPN-based deployment
  reachable until P5's dataplane gate passes. P4 adds a plugin beside it; it does not replace anything.
- **A renamed RPC kwarg fails silently** (§4.4). A handler that receives `vpn=` where it expects
  `domain=` returns without a word. `P5-PLAN.md` §5 tests it; until then, read the kwargs.
- **Ownership is not policy.** The association policy passes an admin, and passes a tenant
  attaching *someone else's* network to its own domain. Only §4.3's `project_id` comparison stops
  the second case.

---

## 11. As built (2026-09-10)

Implemented as planned, with these deviations — each forced by something found while building:

| Where | Plan said | Built | Why |
|---|---|---|---|
| §5 association policy | `ADMIN_OR_PARENT_OWNER_*` | `ADMIN_OR_PROJECT_*` + plugin checks | `ext_parent_owner` resolves only for four hard-coded parents (§5 correction) |
| §3.1 API definitions | inline in `extensions/srv6.py` | `api/definitions/srv6.py`, `srv6_locators.py` | `APIExtensionDescriptor` wants a module-like `api_definition`; a separate module mirrors neutron-lib's layout, still with no fork |
| §3.1 sub-resource | `allow_bulk=True` | `allow_bulk=False` | emulated bulk runs the plugin method in a loop inside one transaction — RPC fan-out before commit |
| §8 locators as a tenant | 403 | 200 `[]`, single item 404 | neutron filters a collection by per-item policy rather than refusing it |
| §8 foreign domain | 403 | 404 | the plugin hides another project's domain |
| payload | caller's context | `context.elevated()` | shared networks are allowed, and their other-project ports are invisible to the owner's context — their routes would silently go missing |
| `srv6_db` | — | `get_host_locator_details()` | the locator API needs `updated_at`; `get_host_locators` returns only `{host: locator}` |
| locators API | — | alias `srv6-locators`, `id` = host | the host name is the primary key and what an admin types into a TE path |

The `network_associations` collection name is shared with networking-bgpvpn. When both extensions
load, neutron merges the two parameter maps in `attributes.RESOURCES`. That was checked and is
harmless. For a sub-resource that entry holds `parent`/`parameters`, not attribute names, so
`_build_match_rule` builds no attribute rules on create. The GET-side attribute checks use
`might_not_exist=True`, so unregistered rules pass.

**Verification.** 348 unit tests, 0 skipped, including REST tests through neutron's own harness
(`NeutronDbPluginV2TestCase`, `TestNoL3NatPlugin` for external-net, `TestL3NatServicePlugin` for the
router cases). They cover:
- `sid_function` hidden from tenants and visible to admins;
- the external, routered, other-project and shared-network cases;
- the router-interface race (409);
- rollback on pool exhaustion;
- the TE-path cascade log;
- the entry points, read from the installed metadata.

Two environment notes for the unit venv:
- neutron's REST test base imports `webtest`, which the layered venv lacked;
- a stale in-repo `networking_srv6_agent.egg-info` (git-ignored) shadowed the fresh dist-info
  whenever tests ran from the repo root, so the new entry points looked absent. Delete it after
  changing entry points.

Not done here: the REST checks against node 1 (§8). They need the running testbed, which P5
re-stacks.
