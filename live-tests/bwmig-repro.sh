#!/bin/bash
# bwmig-repro.sh -- live reproduction of review finding 1 on the Clos: a
# bandwidth reservation to a PORT is accounted wrongly after the VM migrates.
#
# The plugin accounts a reservation as  source + pinned via hops + the port's
# CURRENT host  (Srv6Plugin._compute_hops). After a live migration the hops
# are still the ones computed for the OLD host, but the last node is the NEW
# host, so the accounted route is not the route the traffic takes. This
# script makes that visible with the hardware counters of the fabric: every
# Clos link is a Linux bridge on mac-server whose two ports are the VMs' taps,
# and a tap's rx_bytes is what that VM sent onto the link. Reading them
# before and after a 10 s UDP flow gives the real load of every directed link
# (s1>l3, l3>ml5, ...), to compare with what the server believes.
#
#   1  reserve 60 Mbit/s from ml1 to pa5 (on ml5, rack B); send 55 Mbit/s
#      pb1 -> pa5: the counters must show exactly the reserved hops
#   2  live-migrate pa5 to ml4 (rack A) and send again: the counters show the
#      route that really follows (up to the spine and back down), and the
#      server's accounted route contains a link that does not exist
#   3  admission: two 100 Mbit/s reservations ml5 -> rack A each fill one of
#      l3's two uplinks (cap 100) as far as the server knows; the migrated
#      flow, which the server cannot see, loads one of them too. Both are
#      accepted (201): the link is over-committed. The correct answer for
#      at least one of them is 409.
#   teardown: every reservation deleted, pa5 migrated back to ml5
#
# The script detects which plugin it meets and checks that one's behaviour:
#   0e282a3   a port reservation is kept after the migration: a PASS line
#             means the BUG is there (steps 2-3 as above)
#   d17948b   a port reservation is refused (400): the same reservation to
#             the HOST ml5, migrated, must keep real links
#   lifecycle (PLAN-RESERVATION-FOLLOWS-INSTANCE steps 1-3) a port
#             reservation is admitted and DELETED when the VM changes host:
#             GET answers 404, the neutron log has the WARNING, pa5's flow
#             takes the plain route; step 3 then checks reservable_fraction
#             (81 Mbit/s refused on a 100 link, 80 admitted)
# Needs the Clos ML2 cloud up in srv6-usid with the eval fixture and the
# plugin of 0e282a3 (topology_file set); see deploy/cloud-switch.sh clos.
#   export TESTBED_ENV=$PWD/testbed-mac-clos.env; bash bwmig-repro.sh
source "$(dirname "$0")/lib.sh"
need_eval_fixture || exit 1
load
MAC=stack@10.47.7.66
BW=60            # Mbit/s reserved for the migrating flow
RATE=55          # Mbit/s it really sends (within its reservation)
SECS=10
IDS=()           # every reservation made here, for teardown
PORT_BASE=5300   # iperf3 server port, one per flow

# --- fabric counters ----------------------------------------------------------
# snap: "<link network> <vm> <rx_bytes of the vm's tap>" for every Clos link
# port on mac-server (rx of the tap = frames the VM sent onto that link)
snap() {
    ssh -n -o BatchMode=yes $MAC 'for v in ale-ml1 ale-ml4 ale-ml5 ale-ml6 ale-s1 ale-s2 ale-l1 ale-l2 ale-l3 ale-l4; do
            virsh -c qemu:///system domiflist $v | awk -v v=$v "\$3 ~ /^clos-/ {print \$3, v, \$1}"
        done | while read n v t; do echo $n $v $(cat /sys/class/net/$t/statistics/rx_bytes); done'
}
# loads <snap before> <snap after> <secs>: Mbit/s of every directed link that
# carried at least 2 (host names without the ale- prefix); a link network has
# two ports, so the other VM of the network is the receiver
loads() {
    awk -v secs=$3 'NR == FNR { a[$1 " " $2] = $3; peer[$1] = peer[$1] " " $2; next }
        { d = ($3 - a[$1 " " $2]) * 8 / 1e6 / secs
          split(peer[$1], p, " "); o = (p[1] == $2) ? p[2] : p[1]
          sub(/^ale-/, "", $2); sub(/^ale-/, "", o)
          if (d >= 2) printf "      %-8s %6.1f Mbit/s\n", $2 ">" o, d }' $1 $2 | sort
}

# --- one UDP flow with the fabric counters around it --------------------------
# flow <src vm> <dst vm> <label>: RATE Mbit/s for SECS s, then the loads
flow() {
    local src=$1 dst=$2 port=$((PORT_BASE++)) before after
    pv_bg $dst "iperf3 -s -1 -p $port"; sleep 1
    before=$(mktemp); after=$(mktemp)
    snap > $before
    pv $src "iperf3 -c $(ev_ip $dst) -p $port -u -b ${RATE}M -t $SECS" >/dev/null 2>&1
    snap > $after
    echo "    $src -> $dst, $RATE Mbit/s for ${SECS}s: link loads"
    loads $before $after $SECS | tee /tmp/bwmig-loads.last
    rm -f $before $after
}

# --- reservations -------------------------------------------------------------
# reserve <src node> <dst vm> <Mbit/s> [want]: POST a bandwidth path from the
# given node (ingress) to the port of a VM; prints "<id> <via..> <status>"
reserve() {
    local srcn=$1 dst=$2 bw=$3 want=${4:-201} out sel
    # "host:<node name>" reserves to a host; a VM name reserves to its port
    case $dst in
        host:*) sel="\"destination_host\": \"${dst#host:}\"" ;;
        *)      sel="\"destination_port_id\": \"$(port_of $dst)\"" ;;
    esac
    out=$(api admin POST srv6_te_paths "{\"srv6_te_path\": {\"domain_id\": \"$DOMAIN_EVAL\",
          \"source_host\": \"$(host_of $srcn)\", $sel, \"bandwidth\": $bw}}")
    echo "$out" | code | grep -q "$want" || { echo "  reserve $srcn->$dst $bw: wanted $want, got: $(echo "$out" | tail -2 | xargs)" >&2; return 1; }
    [ "$want" = 201 ] || return 0
    echo "$out" | body | jq -r '.srv6_te_path | "\(.id) \(.via_hosts | join(" ")) \(.status)"'
}
# vm_host <vm>: the compute node the VM runs on now
vm_host() { os_demo "openstack server show $1 -f value -c OS-EXT-SRV-ATTR:host"; }
# accounted <path id>: what the SERVER believes this reservation occupies,
# built exactly as _compute_hops does: source + via hops + the port's host NOW.
# Every link of that chain is marked "(no such link)" when the topology file
# has no such link.
accounted() {
    local id=$1 p src dst nodes topo a b out=
    p=$(api admin GET "srv6_te_paths/$id" | body)
    dst=$(echo "$p" | jq -r '.srv6_te_path.destination_host // empty')
    [ -n "$dst" ] || dst=$(vm_host "$2")
    src=$(echo "$p" | jq -r '.srv6_te_path.source_host')
    nodes="$src $(echo "$p" | jq -r '.srv6_te_path.via_hosts | join(" ")') $dst"
    topo=$(n1 'cat /etc/neutron/srv6-topology')
    set -- $nodes
    while [ $# -gt 1 ]; do
        a=$1; b=$2; shift
        if grep -qE "^link +($a +$b|$b +$a) " <<< "$topo"; then out+="  $a>$b"
        else out+="  $a>$b(NO SUCH LINK)"; fi
    done
    echo "$out" | sed 's/ale-//g'
}

title "bandwidth reservation to a port, before and after migration (mode $(mode)$(usid && echo -usid))"
step "where things are: pa5 on $(vm_host pa5); racks: ml1+ml4 under l1/l2, ml5+ml6 under l3/l4"

# 1: reservation + traffic before the migration -------------------------------
step "1: reserve $BW Mbit/s ml1 -> pa5 (on ml5)"
FIXED=
if r=$(reserve 1 pa5 $BW 2>/dev/null); then
    :                                   # 0e282a3 or the lifecycle plugin
elif reserve 1 pa5 $BW 400 2>/dev/null; then
    # d17948b: a port can migrate, so only a host can be reserved
    FIXED=1
    check "FIXED: a reservation to a port is refused (400)" true
    echo "  the same reservation to the host $(host_of 5) instead:"
    r=$(reserve 1 host:$(host_of 5) $BW) || { check "reserve ml1->host ml5" false; exit 1; }
else
    check "reserve ml1->pa5 (201, or 400 from d17948b)" false; exit 1
fi
set -- $r; ID1=$1; IDS+=($ID1)
echo "  hops: $2 $3 $4 ($5)"
echo "  the server accounts: $(accounted $ID1 pa5)"
sleep 5                                     # the route to pa5 follows the path
flow pb1 pa5

# 2: migrate -------------------------------------------------------------------
step "2: live-migrate pa5 to ml4 (rack A)"
T_MIG=$(n1 date +%s)                      # epoch: ml1 logs in UTC
os_demo "openstack --os-compute-api-version 2.30 server migrate --live-migration --block-migration \
             --host $(host_of 4) --wait pa5" >/dev/null 2>&1
wait_for 120 test "$(vm_host pa5)" = "$(host_of 4)"
check "pa5 now on $(host_of 4)" test "$(vm_host pa5)" = "$(host_of 4)"
sleep 10                                    # routes and port binding settle
RELEASED=
if [ -z "$FIXED" ] && api admin GET "srv6_te_paths/$ID1" | code | grep -q 404; then
    # the lifecycle plugin: the reservation ended with the move
    RELEASED=1
    check "LIFECYCLE: the reservation is deleted when pa5 changes host (404)" true
    log=$(n1 "sudo journalctl -u devstack@neutron-api -u devstack@neutron-rpc-server --since @$T_MIG --no-pager" |
          grep "reservation $ID1 ")
    echo "  server log: $(sed 's/.*SRv6 TE: //' <<< "$log" | head -1)"
    check "LIFECYCLE: the server logged the deletion (WARNING)" grep -q WARNING <<< "$log"
    flow pb1 pa5
    check "LIFECYCLE: pa5's flow takes the plain route, no spine (both in rack A)" \
          bash -c '! grep -qE " (s[12]>|l[34]>)" /tmp/bwmig-loads.last'
else
    acc=$(accounted $ID1 pa5)
    echo "  the server accounts: $acc"
    if [ -n "$FIXED" ]; then
        check "FIXED: the accounted route is still made of real links" bash -c '! grep -q "NO SUCH LINK" <<< "$1"' _ "$acc"
    else
        check "BUG: the accounted route contains a link that does not exist" grep -q "NO SUCH LINK" <<< "$acc"
    fi
    flow pb1 pa5
fi

if [ -n "$RELEASED" ]; then
    # 3 (lifecycle): reservable_fraction 0.8 on the 100 Mbit/s spine links
    step "3: reservable_fraction: a spine link takes 80 Mbit/s of reservations"
    check "81 Mbit/s ml1 -> ml5 refused (409)" reserve 1 host:$(host_of 5) 81 409
    r4=$(reserve 1 host:$(host_of 5) 80) && { set -- $r4; IDS+=($1); echo "  80 Mbit/s ml1 -> ml5: $2 $3 $4 ($5)"; }
    check "80 Mbit/s ml1 -> ml5 admitted" test -n "$r4"
elif [ -n "$FIXED" ]; then
    # nothing more to show: pa5's traffic left the host path (it is no
    # longer to ml5), the reservation keeps holding its own links
    step "3: skipped, a reservation can no longer follow a VM to other links"
else

# 3: admission -----------------------------------------------------------------
step "3: two 100 Mbit/s reservations ml5 -> rack A (l3's uplinks have 100 each)"
# The server thinks l3>s1 and l3>s2 are empty (the migrated flow's way down
# is not in its books), so both fit exactly. The migrated flow really loads
# one of them with $RATE Mbit/s (the counters above): 100 + $RATE > 100.
r2=$(reserve 5 pa4 100) && { set -- $r2; IDS+=($1); echo "  ml5 -> pa4: $2 $3 $4 ($5)"; }
r3=$(reserve 5 pa1 100) && { set -- $r3; IDS+=($1); echo "  ml5 -> pa1: $2 $3 $4 ($5)"; }
check "BUG: both 100 Mbit/s reservations accepted although one l3 uplink already carries the migrated flow" \
      test -n "$r2" -a -n "$r3"
# the migrated flow again: it is still on the same l3 uplink
flow pb1 pa5
fi

# teardown ---------------------------------------------------------------------
step "teardown: reservations deleted, pa5 back on ml5"
for id in "${IDS[@]}"; do api admin DELETE "srv6_te_paths/$id" >/dev/null; done
os_demo "openstack --os-compute-api-version 2.30 server migrate --live-migration --block-migration \
             --host $(host_of 5) --wait pa5" >/dev/null 2>&1
wait_for 120 test "$(vm_host pa5)" = "$(host_of 5)"
echo "  pa5 on $(vm_host pa5); reservations left: $(api admin GET srv6_te_paths | body | jq '.srv6_te_paths | length')"
summary "bwmig-repro"
