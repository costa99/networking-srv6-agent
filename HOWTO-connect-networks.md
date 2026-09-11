# HOWTO — make two networks communicate over SRv6

Two Neutron networks, possibly with their instances on different compute nodes, become mutually
reachable when both are attached to the same **SRv6 domain**. A domain is one VRF on every node:
its gateway port takes over each attached subnet's gateway address, and traffic between nodes
is carried by SRv6 encapsulation to the destination node's decap SID (`End.DT46`).

It is the same two-step shape as the BGPVPN build: create one grouping object, then attach each
network to it. The differences are listed at the end.

> **Status.** Written 2026-09-10, against the P4/P5 code in this repository. The testbed still
> runs the BGPVPN build, so these commands only work once both nodes have been re-stacked on
> `networking-srv6-agent` (`P5-PLAN.md` §7). Nothing below has been run against a live cloud yet.

---

## 1. Prerequisites

- **Both nodes deployed** on `implementation/srv6-plugin/node{1,2}-srv6-agent-local.conf`.
- **Two tenant networks that meet all of these:**
  - they belong to **your** project;
  - their subnets **do not overlap** (the two share one VRF);
  - they have **no router interface**;
  - they are **not external**.

  For example `net-a` with `10.90.1.0/24` and `net-b` with `10.90.2.0/24`, and one instance on
  each.
- **`jq`** on the machine you run this from. Without it, pipe the output through
  `python3 -m json.tool` and copy the ids by hand.

There is no `openstack srv6 ...` CLI: the migration dropped the `openstack bgpvpn` commands, and
the SRv6 equivalents have not been written (that needs openstacksdk and python-openstackclient
changes). The API is called directly, with a token.

## 2. Session setup

```bash
source ~/devstack/openrc demo demo      # a tenant does all of this itself; no admin step
TOKEN=$(openstack token issue -f value -c id)
NEUTRON=$(openstack endpoint list --service network --interface public -f value -c URL)
api() { curl -s -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' "$@"; }
```

Tokens expire (one hour by default in DevStack). If calls start answering 401, re-run the `TOKEN=`
line.

## 3. Create the domain

```bash
DOMAIN=$(api -X POST $NEUTRON/v2.0/srv6_domains \
           -d '{"srv6_domain": {"name": "red"}}' | jq -r .srv6_domain.id)
echo $DOMAIN
```

The server allocates the domain's SID function id. There is no route target, no route
distinguisher and no `type`. `srv6_behavior` defaults to `End.DT46`, which is also the only value
accepted.

## 4. Attach the networks

```bash
for net in net-a net-b; do
  api -X POST $NEUTRON/v2.0/srv6_domains/$DOMAIN/network_associations \
      -d "{\"network_association\": {\"network_id\": \"$(openstack network show $net -f value -c id)\"}}" | jq
done
```

Each attach pushes the domain to every agent. The instances on `net-a` and `net-b` can now reach
each other through their normal default gateway, whichever node they run on.

## 5. Check it

```bash
# the associations
api $NEUTRON/v2.0/srv6_domains/$DOMAIN/network_associations | jq

# the function id is admin-only, so ask as admin
source ~/devstack/openrc admin admin
ADMIN_TOKEN=$(openstack token issue -f value -c id)
FID=$(curl -s -H "X-Auth-Token: $ADMIN_TOKEN" $NEUTRON/v2.0/srv6_domains/$DOMAIN \
        | jq -r .srv6_domain.sid_function)
echo "function id $FID -> VRF sv6vrf-$FID, table $((10000 + FID))"

# every registered node, and when its agent last reported
curl -s -H "X-Auth-Token: $ADMIN_TOKEN" $NEUTRON/v2.0/srv6_locators | jq
```

On each compute node:

```bash
ip -d link show sv6vrf-$FID                      # the VRF
ip route show table $((10000 + FID))             # connected subnets + encap routes to remote VMs
ip -6 route show table main | grep seg6local     # this node's decap SID for the domain
```

The table should also hold `unreachable default metric 4278198272` for IPv4 and IPv6. That route
seals the VRF (`MIGRATION-PLAN.md` §8.6): without it, a VM could reach other domains' SIDs.

Next to the seal there must be one rule per remote node, letting the domain's encapsulated traffic
out toward that node's decap SID (`P5-PLAN.md` §4.6):

```bash
ip -6 rule show | grep '^999:'
# 999: from all to fc00:0:2:<hex fid>:: iif sv6vrf-<fid> lookup main
```

If the VRF and the routes are there but this rule is missing, pings between nodes fail silently.
`nstat -az Ip6InNoRoutes` then climbs by one per packet.

Then ping from the instance on `net-a` to the instance on `net-b`.

## 6. Undo

```bash
# detach one network
ASSOC=$(api $NEUTRON/v2.0/srv6_domains/$DOMAIN/network_associations \
          | jq -r '.network_associations[0].id')
api -X DELETE $NEUTRON/v2.0/srv6_domains/$DOMAIN/network_associations/$ASSOC

# delete the domain (its VRF, SID, gateway ports and table go from every node)
api -X DELETE $NEUTRON/v2.0/srv6_domains/$DOMAIN
```

Deleting a domain also deletes any admin TE paths defined on it. The server logs a warning with
their count.

## 7. Steer a domain's traffic (admin only)

A **TE path** sends a domain's traffic through an explicit list of waypoints (`P6-PLAN.md`). It is
admin-only, every verb and every attribute, because it names compute hosts and resolves to underlay
SIDs. A tenant listing TE paths gets `[]`.

A path selects its traffic with **exactly one** of:
- **`destination_host`**: all of the domain's traffic toward that compute node;
- **`destination_port_id`**: traffic toward a single VM, meaning that port's fixed IPs only.

It then lists `via_hosts`, in traversal order, with at most 6 entries. If both kinds of path apply to
a route, **port path > host path > direct**.

> **Status.** Written 2026-09-11 against the P6 code. Like §1–§6, it has not yet been run against
> the live testbed.

```bash
source ~/devstack/openrc admin admin
ADMIN_TOKEN=$(openstack token issue -f value -c id)
aapi() { curl -s -H "X-Auth-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' "$@"; }

# valid host names: exactly what via_hosts and destination_host accept
aapi $NEUTRON/v2.0/srv6_locators | jq -r '.srv6_locators[].host'

# steer one VM's traffic via node 2 (on two nodes: via == destination, a depth-2 SRH)
PORT=$(openstack port list --server vm-n2 -f value -c ID)
TE_PATH=$(aapi -X POST $NEUTRON/v2.0/srv6_te_paths -d "{\"srv6_te_path\": {
            \"domain_id\": \"$DOMAIN\", \"destination_port_id\": \"$PORT\",
            \"via_hosts\": [\"ale-XPS-15-9570\"]}}" | jq -r .srv6_te_path.id)
aapi $NEUTRON/v2.0/srv6_te_paths/$TE_PATH | jq '.srv6_te_path | {segments, status}'
# {"segments": ["fc00:0:2:fff0::", "fc00:0:2:<hex fid>::"], "status": "ACTIVE"}
```

- **`segments`** lists the via End SIDs, then the destination's decap SID.
- **`status`** is what the *server* resolved; no agent confirms it.
  - `DEGRADED` means a host on the path has no registered locator, or the port is unbound. The
    routes then fall back to the direct path until the host registers again.

**Re-steer or un-steer** without a delete/create window. A PUT **replaces** `via_hosts`, and `[]`
means explicitly direct:

```bash
aapi -X PUT $NEUTRON/v2.0/srv6_te_paths/$TE_PATH -d '{"srv6_te_path": {"via_hosts": []}}'
aapi -X DELETE $NEUTRON/v2.0/srv6_te_paths/$TE_PATH
```

The selectors cannot be changed: to steer something else, delete the path and create a new one.
Deleting the port also deletes its path.

**On the ingress node** (node 1 in the example above):

```bash
ip route show table $((10000 + FID))    # the steered VM: segs 2 [ fc00:0:2:fff0:: fc00:0:2:<hex fid>:: ]
ip -6 rule show | grep '^999:'          # plus a rule toward fc00:0:2:fff0::
sudo nft list table inet srv6_edge      # the edge filter; its drop counters should stay 0
```

The rule toward an End SID exists only while the **edge filter** (`inet srv6_edge`) is active and
covers that SID. Without it, a tenant could forge an SRH through the End SID into another domain
(`MIGRATION-PLAN.md` §8.7). If `nft` is missing or the filter fails to apply, the agent logs an
error, the route is still installed but sealed, and steered traffic is dropped. It fails closed.

---

## What answers what

| Situation | Answer |
|---|---|
| Network owned by another project — even as admin | **403** |
| Another project's private network, as a tenant | **404** (you cannot see it) |
| Another project's domain | **404** |
| External network | **400** |
| Network that already has a router interface | **400** |
| Adding a router interface to an attached network | **409** |
| The same network twice | **409** |
| `srv6_behavior` other than `End.DT46`, or changing it later | **400** |
| Function id pool exhausted | **409** |
| TE paths as a tenant: list / show / create | **`[]`** / **404** / **403** |
| TE path with both selectors, or neither | **400** |
| TE path with an unregistered host, or more than 6 `via_hosts` | **400** |
| TE path toward a port not on one of the domain's networks | **400** |
| A second TE path for the same host or port in one domain | **409** |
| Changing a TE path's selector | **400** |

## Compared with the BGPVPN build

| | BGPVPN | SRv6 domain |
|---|---|---|
| Who creates the group | admin only (`--route-target`, `--project`) | the tenant |
| Commands | `openstack bgpvpn ...` | REST calls with a token (no CLI yet) |
| Identity of the group | route target | server-allocated SID function id, admin-only to read |
| Network ownership check | same owner | same owner, whoever calls; the owner's shared networks are allowed |
| Attaching | `network association create` | `POST .../network_associations` |

**Shared networks.** Attaching a shared network that your project owns also brings in the
instances other projects have on it: on every node, the domain's gateway port answers for that
network's gateway. Sharing the network and then attaching it is the owner's decision.

**A domain is an island.** Instances inside it reach each other. North-south traffic (floating
IPs, SNAT) and routing to other domains do not go through it. Instances keep their ordinary
Neutron connectivity on any other interfaces they have (`MIGRATION-PLAN.md` §8.1).
