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
