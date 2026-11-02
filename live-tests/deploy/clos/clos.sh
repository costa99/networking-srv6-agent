#!/bin/bash
# clos.sh <action> [node...]   -- the Clos underlay of PLAN-CLOS-TESTBED.md
#
# Builds and checks a 2-spine / 6-leaf routed fabric of small VMs on mac-server
# (10.47.7.66) under the ML2 cloud's hosts ubuntu-ovs1..6 (racks l1/l2,
# l3/l4, l5/l6). clos.topo is the only input; clos.py derives the networks,
# MACs, addresses and routes from it.
# Every action is idempotent.
#
#   nets     one isolated libvirt network per link (bridge br-<a><b>, MTU 9000)
#   fabric   the spine/leaf VMs: disk, cloud-init seed, fixed NAT lease, all NICs;
#            wait for SSH, then push
#   hosts    attach the fabric NICs to every host VM of both clouds (ubuntu-ovs1..6
#            and ubuntu-ovn1..6 share a slot's NICs; persistent, live too if the
#            VM runs), and any NIC a fabric VM lacks after links were added to
#            clos.topo; then push to the running ones
#
# CLOS_CLOUD=ovn: the host slots are the OVN cloud's VMs (clos.topo).
#   push     install /usr/local/sbin/clos-routes.sh + clos-routes.service on the
#            listed (default: all running) nodes and run it; on spines and
#            leaves also the fabric agent (srv6-fabric-agent.service, link
#            telemetry of PLAN-LINK-STATE-TE: journalctl -u srv6-fabric-agent)
#   tunnels  put the tenant tunnel endpoint of every running host on its IPv4
#            fabric loopback 10.255.0.<id> (PLAN-PATH-COMPUTATION U5; IPv4 by
#            the user's decision of 2026-11-01), so VXLAN (ML2: [ovs] local_ip,
#            [ml2] overlay_ip_version = 4 on the controller) or Geneve (OVN:
#            ovn-encap-ip) crosses the Clos in every mode; restarts the agents
#            whose setting changed. A restack reverts it: run again.
#   shape [off]  tc tbf at each link's clos.topo capacity on both of its taps on
#            mac-server (/usr/local/sbin/clos-shape.sh), or remove it (off).
#            Taps are recreated when a VM starts: cloud-switch.sh runs it after
#            the start; after restarting a VM by hand, run it again.
#   topology  install the SRv6 plugin's topology file (clos.py te) on the
#            controller as /etc/neutron/srv6-topology, set [srv6] topology_file
#            and restart the neutron server units (ML2 cloud only: the OVN cloud
#            runs no SRv6 plugin)
#   check    per link ping, loopback ping mesh, and which spine a path crosses
#   status   networks, VMs, service state
#   destroy  undo everything above (host NICs detached, fabric VMs and their
#            disks deleted, networks removed)
#
# Fabric VMs are reached over libvirt's NAT network: ssh -J stack@10.47.7.66
# stack@192.168.122.<x>. They do not autostart (only one cloud fits in RAM).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=stack@10.47.7.66
SSH=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
IMG_DIR=/var/lib/libvirt/images
BASE=$IMG_DIR/base/noble-server-cloudimg-amd64.img
PUBKEY=$(cat ~/.ssh/id_ed25519.pub)
ACTION=${1:-status}; shift || true

py() { python3 "$HERE/clos.py" "$@"; }
h() { ssh -n "${SSH[@]}" $HOST "$@"; }
v() { ssh -n "${SSH[@]}" $HOST "virsh -c qemu:///system $*"; }
field() { py nodes | awk -v n="$1" -v c="$2" '$1 == n {print $c}'; }
role() { field "$1" 2; }
vm() { field "$1" 4; }
mgmt() { field "$1" 5; }
# on <node> <cmd>: fabric nodes through mac-server, hosts directly
on() {
    local n=$1; shift
    if [ "$(role $n)" = host ]; then ssh "${SSH[@]}" stack@$(mgmt $n) "$@"
    else ssh "${SSH[@]}" -J $HOST stack@$(mgmt $n) "$@"; fi
}
running() { [ "$(v domstate $(vm $1) 2>/dev/null)" = running ]; }
nodes_or_all() { if [ $# -gt 0 ]; then echo "$@"; else py nodes | awk '{print $1}'; fi; }

do_nets() {
    py nets | while read -r net br; do
        if ! v net-info $net >/dev/null 2>&1; then
            # no <ip>, no <forward>: libvirt only makes the bridge (D3)
            ssh "${SSH[@]}" $HOST "cat > /tmp/$net.xml && virsh -c qemu:///system net-define /tmp/$net.xml >/dev/null && rm /tmp/$net.xml" <<EOF
<network><name>$net</name><bridge name='$br' stp='off' delay='0'/><mtu size='9000'/></network>
EOF
            echo "$net: defined"
        fi
        v net-info $net | grep -q '^Active: *yes' || v net-start $net >/dev/null
        v net-autostart $net >/dev/null
        echo "$net ($br): active"
    done
}

mgmt_mac() { printf '52:54:00:c0:00:%s' "$(field $1 3)"; }

user_data() {
    cat <<EOF
#cloud-config
hostname: $(vm $1)
manage_etc_hosts: true
users:
  - name: stack
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys: [$PUBKEY]
ssh_pwauth: false
package_update: true
packages: [iperf3, tcpdump]
write_files:
  # same hardening as cloud-switch.sh: a networkd restart must not delete the
  # static routes, and no background upgrade may restart it
  - path: /etc/systemd/networkd.conf.d/10-keep-foreign.conf
    content: "[Network]\nManageForeignRoutingPolicyRules=no\nManageForeignRoutes=no\n"
runcmd:
  - systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service
EOF
}

network_config() {
    cat <<EOF
version: 2
ethernets:
  mgmt:
    match: {macaddress: "$(mgmt_mac $1)"}
    set-name: enp1s0
    dhcp4: true
EOF
}

create_fabric_vm() {
    local n=$1 name; name=$(vm $1)
    if v dominfo $name >/dev/null 2>&1; then echo "$name: exists, left alone"; return; fi
    # fixed lease on the NAT network, so clos.topo's address is the VM's
    v net-update default add-last ip-dhcp-host \
        "\"<host mac='$(mgmt_mac $n)' name='$name' ip='$(mgmt $n)'/>\"" --live --config >/dev/null 2>&1 || true
    local nets
    nets=$(py nics $n | awk '{printf " --network network=%s,model=virtio,mac=%s", $1, $2}')
    ssh "${SSH[@]}" $HOST "cat > /tmp/ud-$name" < <(user_data $n)
    ssh "${SSH[@]}" $HOST "cat > /tmp/nc-$name" < <(network_config $n)
    h "set -e
       sudo qemu-img create -q -f qcow2 -F qcow2 -b $BASE $IMG_DIR/$name.qcow2 8G
       sudo cloud-localds --network-config=/tmp/nc-$name $IMG_DIR/seed-$name.iso /tmp/ud-$name
       rm -f /tmp/ud-$name /tmp/nc-$name
       sudo virt-install -q --connect qemu:///system --import --name $name \
            --memory 1024 --vcpus 1 --cpu host-passthrough \
            --osinfo detect=on,name=ubuntu24.04 \
            --disk path=$IMG_DIR/$name.qcow2,bus=virtio \
            --disk path=$IMG_DIR/seed-$name.iso,device=cdrom \
            --network network=default,model=virtio,mac=$(mgmt_mac $n) $nets \
            --graphics none --noautoconsole"
    echo "$name: created ($(mgmt $n))"
}

wait_ssh() {
    for _ in $(seq 1 60); do
        on $1 'cloud-init status --wait >/dev/null 2>&1; true' 2>/dev/null && { echo "$1: SSH up"; return; }
        sleep 5
    done
    echo "$1: no SSH after 5 min"; return 1
}

do_fabric() {
    h "test -s $BASE" || { echo "missing $BASE"; exit 1; }
    local f; f=$(py nodes spine leaf | awk '{print $1}')
    for n in $f; do create_fabric_vm $n; done
    for n in $f; do wait_ssh $n; done
    do_push $f
}

vm_running() { [ "$(v domstate $1 2>/dev/null)" = running ]; }
host_vms() { py vms | awk 'NR == FNR {if ($2 == "host") h[$1]; next} $1 in h' <(py nodes) -; }

do_hosts() {
    local n name live
    py vms | while read -r n name; do
        live=; vm_running $name && live=--live
        py nics $n | while read -r net mac; do
            if v domiflist $name | grep -qi "$mac"; then echo "$name: $mac on $net already"; continue; fi
            v attach-interface --domain $name --type network --source $net --model virtio \
                --mac $mac --config $live >/dev/null
            echo "$name: attached $mac on $net ${live:+(live)}"
        done
    done
    local up=; for n in $(py nodes host | awk '{print $1}'); do running $n && up="$up $n"; done
    [ -n "$up" ] && do_push $up || echo "no host running: push at the next start (clos.sh push)"
}

UNIT='[Unit]
Description=Clos fabric NICs, addresses and static routes (PLAN-CLOS-TESTBED)
After=network.target
Before=devstack@q-agt.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/clos-routes.sh
[Install]
WantedBy=multi-user.target'

# The fabric agent (PLAN-LINK-STATE-TE): link telemetry of a spine/leaf,
# POSTed to Neutron's srv6_link_states every 5 s (steps 1-2).
# Restart=always: the server reads a silent node as a dead one (decision L8),
# so a crashed agent must come back within seconds. DynamicUser: it only reads
# /proc/net/dev and /sys, so it runs as a throwaway unprivileged user; systemd
# hands it the root-only credentials file through LoadCredential (%d).
# --aggregates (PLAN-UNRESERVED-STEERING step 2): it also installs and reads
# its own nft table of per-VM-pair counters, which needs CAP_NET_ADMIN, and
# only that: ambient (inherited by nft) and the whole bounding set (GATE A D4).
agent_unit() {   # [<extra ExecStart args>]: the unit file
    cat <<UNIT
[Unit]
Description=SRv6 fabric agent: link telemetry for Neutron (PLAN-LINK-STATE-TE)
After=clos-routes.service network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 -I /usr/local/sbin/srv6-fabric-agent.py --links /etc/srv6-fabric-agent.links --aggregates $1
${1:+LoadCredential=neutron:/etc/srv6-fabric-agent/credentials}
Restart=always
RestartSec=2
DynamicUser=yes
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
Nice=10
[Install]
WantedBy=multi-user.target
UNIT
}

# The agents' Keystone identity: devstack's own admin account (user decision,
# 2026-11-01: no extra user or role). srv6_link_states is admin-only. Tokens
# expire, so the agent gets the credentials, not a token, and fetches tokens
# itself. Read from devstack's openrc on the controller (host id 1); printed
# openrc-style. On the fabric nodes they stay root-only (do_agent).
fabric_credentials() {
    local c; c=$(py nodes host | awk '$3 == 1 {print $1}')
    running $c || return 1
    on $c 'source /opt/stack/devstack/openrc admin admin >/dev/null &&
           printf "OS_AUTH_URL=%s\nOS_USERNAME=%s\nOS_PASSWORD=%s\nOS_PROJECT_NAME=%s\nOS_USER_DOMAIN_ID=%s\nOS_PROJECT_DOMAIN_ID=%s\n" \
               "$OS_AUTH_URL" "$OS_USERNAME" "$OS_PASSWORD" "$OS_PROJECT_NAME" \
               "${OS_USER_DOMAIN_ID:-default}" "${OS_PROJECT_DOMAIN_ID:-default}"'
}

do_agent() {   # <node>: install the agent, its link map, credentials and unit; (re)start it
    local n=$1 args= c cred
    # the agent is part of the project (tools/), not of the test bed
    on $n 'sudo tee /usr/local/sbin/srv6-fabric-agent.py >/dev/null' < "$HERE/../../../tools/srv6-fabric-agent.py"
    py links $n | on $n 'sudo tee /etc/srv6-fabric-agent.links >/dev/null'
    # push to Neutron when the controller runs the SRv6 plugin (not the OVN
    # cloud); otherwise the agent only prints its reports to the journal
    c=$(py nodes host | awk '$3 == 1 {print $1}')
    if [ "${CLOS_CLOUD:-}" != ovn ] && cred=$(fabric_credentials) && [ -n "$cred" ]; then
        printf '%s\n' "$cred" | on $n 'sudo install -d -m 700 /etc/srv6-fabric-agent &&
                                       sudo install -m 600 /dev/stdin /etc/srv6-fabric-agent/credentials'
        args="--neutron-url http://$(mgmt $c)/networking --credentials %d/neutron"
    fi
    agent_unit "$args" | on $n 'sudo tee /etc/systemd/system/srv6-fabric-agent.service >/dev/null'
    on $n "sudo systemctl daemon-reload && sudo systemctl enable -q srv6-fabric-agent.service
           sudo systemctl restart srv6-fabric-agent.service" &&
        echo "$n: srv6-fabric-agent running ($([ -n "$args" ] && echo "pushing to $(mgmt $c)" || echo "journal only"))" ||
        echo "$n: srv6-fabric-agent FAILED"
}

do_push() {
    local n
    for n in $(nodes_or_all "$@"); do
        running $n || { echo "$n: not running, skipped"; continue; }
        py boot $n | on $n 'sudo tee /usr/local/sbin/clos-routes.sh >/dev/null && sudo chmod 755 /usr/local/sbin/clos-routes.sh'
        on $n "echo '$UNIT' | sudo tee /etc/systemd/system/clos-routes.service >/dev/null
               sudo systemctl daemon-reload && sudo systemctl enable -q clos-routes.service
               sudo systemctl restart clos-routes.service" &&
            echo "$n: clos-routes applied" || { echo "$n: clos-routes FAILED"; on $n 'sudo journalctl -u clos-routes -n 20 --no-pager'; }
        # hosts run the SRv6 agent instead; only spines and leaves report link state
        [ "$(role $n)" = host ] || do_agent $n
    done
}

ML2=/etc/neutron/plugins/ml2/ml2_conf.ini
do_tunnels() {
    local n id ep
    for n in $(py nodes host | awk '{print $1}'); do
        running $n || { echo "$n: not running, skipped"; continue; }
        id=$(field $n 3); ep=$(printf '10.255.0.%d' 0x$id)
        if [ "${CLOS_CLOUD:-}" = ovn ]; then
            # ovn-set-local-ip: without it the Geneve source is whatever the route
            # picks (a link address, fd01:...), and the peer, which knows this
            # chassis only as $ep, drops every packet (op 10)
            on $n "[ \"\$(sudo ovs-vsctl get open . external_ids:ovn-encap-ip | tr -d '\"')\" = $ep ] && echo '$n: ovn-encap-ip $ep already' ||
                   { sudo ovs-vsctl set open . external_ids:ovn-encap-ip=$ep && echo '$n: ovn-encap-ip -> $ep'; }
                   sudo ovs-vsctl set open . external_ids:ovn-set-local-ip=true"
            continue   # ovn-controller follows external_ids by itself
        fi
        # the controller (id 1) computes tenant MTUs with the overlay IP version
        [ $id = 1 ] && on $n "grep -q '^overlay_ip_version = 4' $ML2 ||
            { sudo sed -i 's/^overlay_ip_version = .*/overlay_ip_version = 4/' $ML2 &&
              u='devstack@neutron-api devstack@neutron-rpc-server devstack@neutron-periodic-workers'
              sudo systemctl kill -s KILL \$u 2>/dev/null; sudo systemctl stop \$u; sudo systemctl reset-failed \$u 2>/dev/null; sudo systemctl start \$u && echo '$n: overlay_ip_version 4, neutron server restarted'; }"
        on $n "grep -q '^local_ip = $ep\$' $ML2 && echo '$n: local_ip $ep already' ||
            { sudo sed -i 's/^local_ip = .*/local_ip = $ep/' $ML2 &&
              sudo systemctl kill -s KILL devstack@q-agt 2>/dev/null; sudo systemctl stop devstack@q-agt; sudo systemctl reset-failed devstack@q-agt 2>/dev/null; sudo systemctl start devstack@q-agt && echo '$n: local_ip -> $ep, q-agt restarted'; }"
    done
}

do_shape() {
    # plain ssh, not h: h's -n would feed the tee /dev/null
    py shape "$@" | ssh "${SSH[@]}" $HOST 'sudo tee /usr/local/sbin/clos-shape.sh >/dev/null && sudo chmod 755 /usr/local/sbin/clos-shape.sh &&
                       sudo /usr/local/sbin/clos-shape.sh' && echo "shaping ${1:-on}: $(h 'tc qdisc show | grep -c "tbf "') tbf qdiscs"
}

do_topology() {
    [ "${CLOS_CLOUD:-}" = ovn ] && { echo "the OVN cloud has no SRv6 plugin"; return 1; }
    local c; c=$(py nodes host | awk '$3 == 1 {print $1}')
    py te | on $c 'sudo tee /etc/neutron/srv6-topology >/dev/null'
    on $c "sudo sed -i '/^\[srv6\]/,/^\[/{/^topology_file *=/d}' /etc/neutron/neutron.conf
           sudo sed -i 's|^\[srv6\]\$|[srv6]\ntopology_file = /etc/neutron/srv6-topology|' /etc/neutron/neutron.conf
           grep -A3 '^\[srv6\]' /etc/neutron/neutron.conf | grep topology_file
           u='devstack@neutron-api devstack@neutron-rpc-server devstack@neutron-periodic-workers'
           sudo systemctl kill -s KILL \$u 2>/dev/null; sudo systemctl stop \$u; sudo systemctl reset-failed \$u 2>/dev/null
           sudo systemctl start \$u && echo '$c: topology_file set, neutron server restarted'"
}

do_check() {
    local bad=0 n p ids
    ids=$(py nodes | awk '{print $1 ":" $3}')
    local v4s; v4s=$(py nodes | while read -r nn _ id _; do printf '%s:10.255.0.%d ' $nn 0x$id; done)
    for n in $(py nodes | awk '{print $1}'); do
        running $n || { echo "$n: not running"; bad=1; continue; }
        # every NIC up with its peer answering, then every loopback
        local cmd='f=0; for pp in '"$(py boot $n | awk '$1 == "nic" {print $3 "/" $4}' | xargs)"'; do
                       peer=${pp%%/*}; a=${pp#*/}; b=${a%::*}::$(( ${a##*::} == 1 ? 2 : 1 ))
                       ping -6 -c1 -W2 $b >/dev/null 2>&1 || { echo "  link to $peer ($b) FAIL"; f=1; }; done
                   for t in '"$(echo $ids)"'; do
                       ping -6 -c1 -W2 fd00:cc::${t#*:} >/dev/null 2>&1 || { echo "  loopback ${t%%:*} FAIL"; f=1; }; done
                   for t in '"$v4s"'; do
                       ping -4 -c1 -W2 ${t#*:} >/dev/null 2>&1 || { echo "  v4 loopback ${t%%:*} (${t#*:}) FAIL"; f=1; }; done
                   exit $f'
        if on $n "$cmd"; then echo "$n: links and loopbacks ok"; else echo "$n: FAIL"; bad=1; fi
    done
    echo "path OVS1 -> OVS3 (traceroute over the fabric):"
    on OVS1 "tracepath -6 -n -m 6 fd00:cc::3 2>/dev/null | head -8" || true
    echo "path OVS1 -> OVS3 over IPv4:"
    on OVS1 "tracepath -4 -n -m 6 10.255.0.3 2>/dev/null | head -8" || true
    return $bad
}

do_status() {
    echo "== networks"; v net-list --all | grep -E 'clos-|default' || true
    echo "== VMs"
    for n in $(py nodes | awk '{print $1}'); do
        local s; s=$(v domstate $(vm $n) 2>/dev/null | xargs); s=${s:-absent}
        local svc=-; [ "$s" = running ] && svc=$(on $n 'systemctl is-active clos-routes 2>/dev/null' 2>/dev/null || echo "?")
        local ag=; [ "$s" = running ] && [ "$(role $n)" != host ] &&
            ag=" fabric-agent=$(on $n 'systemctl is-active srv6-fabric-agent 2>/dev/null' 2>/dev/null || echo "?")"
        printf '  %-4s %-8s %-10s clos-routes=%s%s\n' $n $(vm $n) "$s" "$svc" "$ag"
    done
}

do_destroy() {
    local n name
    host_vms | while read -r n name; do
        local live=; vm_running $name && live=--live
        py nics $n | while read -r net mac; do
            v domiflist $name | grep -qi "$mac" &&
                v detach-interface --domain $name --type network --mac $mac --config $live >/dev/null &&
                echo "$name: detached $mac"
        done
        [ "$name" = "$(vm $n)" ] && running $n && on $n 'sudo systemctl disable -q --now clos-routes 2>/dev/null; sudo rm -f /etc/systemd/system/clos-routes.service /usr/local/sbin/clos-routes.sh' || true
    done
    for n in $(py nodes spine leaf | awk '{print $1}'); do
        name=$(vm $n)
        v dominfo $name >/dev/null 2>&1 || continue
        v destroy $name >/dev/null 2>&1 || true
        v undefine $name >/dev/null
        h "sudo rm -f $IMG_DIR/$name.qcow2 $IMG_DIR/seed-$name.iso"
        v net-update default delete ip-dhcp-host "\"<host mac='$(mgmt_mac $n)'/>\"" --live --config >/dev/null 2>&1 || true
        echo "$name: deleted"
    done
    py nets | while read -r net br; do
        v net-info $net >/dev/null 2>&1 || continue
        v net-destroy $net >/dev/null 2>&1 || true
        v net-undefine $net >/dev/null && echo "$net: removed"
    done
}

case $ACTION in
    nets) do_nets ;;
    fabric) do_fabric ;;
    hosts) do_hosts ;;
    push) do_push "$@" ;;
    tunnels) do_tunnels ;;
    shape) do_shape "$@" ;;
    topology) do_topology ;;
    check) do_check ;;
    status) do_status ;;
    destroy) do_destroy ;;
    *) sed -n '2,24p' "$0" >&2; exit 2 ;;
esac
