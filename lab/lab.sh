#!/bin/bash
# lab.sh <command> -- a DevStack test lab for networking-srv6-agent.
#
# Everything comes from lab.conf (copy lab.conf.example). Every command is
# idempotent: run it again after fixing whatever stopped it.
#
#   up         all of the below, in order: the whole lab from nothing
#   vms        VM mode: create the libvirt networks and VMs on VM_HOST
#   prep       check every host, install git and the vrf module, clone DevStack
#   underlay   install the IPv6 underlay (addresses, routes) on every node,
#              as lab-underlay.service, and ping across it
#   stack      write local.conf and run stack.sh: controller, then computes
#   fabric     with a fabric: start the telemetry agent on every switch
#   check      smoke test: two networks in a domain, VMs on two hosts, ping,
#              TE paths through a host (and a switch)
#   status     what exists and runs
#   ssh <node> [cmd]   a shell on a node (through VM_HOST in VM mode)
#   destroy    VM mode: delete every lab VM, disk and network on VM_HOST
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=${LAB_CONF:-$HERE/lab.conf}
[ -f "$CONF" ] || { echo "no $CONF: cp lab.conf.example lab.conf and edit it" >&2; exit 1; }

# defaults; lab.conf overrides any of them
VM_HOST= SSH_USER=stack HOSTS= FABRIC=none MGMT_NET=192.168.150
DEVSTACK_BRANCH=stable/2026.2
PLUGIN_REPO=https://github.com/costa99/networking-srv6-agent.git PLUGIN_BRANCH=main
ENCODING=usid ADMIN_PASSWORD=secret LOCAL_CONF_EXTRA=
VM_PREFIX=lab- CTRL_RAM=8192 CTRL_CPUS=4 HOST_RAM=4096 HOST_CPUS=4 HOST_DISK=40G
SWITCH_RAM=768 IMG_DIR=/var/lib/libvirt/images/lab
IMAGE_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
SSH_PUBKEY=
# shellcheck source=/dev/null
source "$CONF"
[ "$FABRIC" = none ] || FABRIC=$(cd "$(dirname "$CONF")" && realpath "$FABRIC")
[ "$FABRIC" = none ] || [ -n "$VM_HOST" ] ||
    { echo "a FABRIC needs VM_HOST: lab.sh builds the switches as VMs" >&2; exit 1; }

# one lab.sh at a time per lab (status and ssh only read)
case ${1:-} in status|ssh|'') ;; *)
    exec 9>"${XDG_RUNTIME_DIR:-/tmp}/lab-$(realpath "$CONF" | md5sum | cut -c1-8).lock"
    flock -n 9 || { echo "lab: another lab.sh is working on this lab" >&2; exit 1; } ;;
esac

gen() { LAB_HOSTS="$HOSTS" LAB_FABRIC="$FABRIC" LAB_MGMT_NET="$MGMT_NET" python3 "$HERE/labgen.py" "$@"; }
NODES_TXT=$(gen nodes)
col() { awk -v n="$1" -v c="$2" '$1 == n {print $c}' <<< "$NODES_TXT"; }
role() { col "$1" 2; }
nid() { col "$1" 3; }
mgmt() { col "$1" 4; }
mgmt_mac() { col "$1" 5; }
hosts() { awk '$2 == "host" {print $1}' <<< "$NODES_TXT"; }
switches() { awk '$2 == "switch" {print $1}' <<< "$NODES_TXT"; }
all_nodes() { awk '{print $1}' <<< "$NODES_TXT"; }
CTRL=$(hosts | head -1)
computes() { hosts | tail -n +2; }

# --- ssh -------------------------------------------------------------------
# One master connection per node (fast repeated calls). Lab VMs are rebuilt
# with new host keys, so their keys are not remembered.
SSH=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15
     -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/lab-%C" -o ControlPersist=120)
LAB_SSH=("${SSH[@]}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
[ -z "$VM_HOST" ] || { LAB_SSH+=(-J "$VM_HOST"); SSH_USER=stack; }
on() { local n=$1; shift; ssh "${LAB_SSH[@]}" "$SSH_USER@$(mgmt "$n")" "$@"; }
vh() { ssh "${SSH[@]}" "$VM_HOST" "$@"; }
virsh_() { vh "sudo virsh -c qemu:///system $*"; }
say() { printf '\n== %s\n' "$*"; }
die() { echo "lab: $*" >&2; exit 1; }
need_vm_host() { [ -n "$VM_HOST" ] || die "$1 needs VM_HOST (VM mode)"; }

# --- vms -------------------------------------------------------------------
pubkey() {
    if [ -n "$SSH_PUBKEY" ]; then cat "$SSH_PUBKEY"; return; fi
    for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
        [ -f "$k" ] && { cat "$k"; return; }
    done
    die "no SSH public key: set SSH_PUBKEY"
}

user_data() {   # <node>
    cat <<EOF
#cloud-config
hostname: $1
fqdn: $1
manage_etc_hosts: true
users:
  - name: stack
    homedir: /opt/stack
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys: [$(pubkey)]
ssh_pwauth: false
growpart: {mode: auto, devices: ['/']}
package_update: true
packages: [git, tcpdump, nftables]
bootcmd:
  # no background apt: it takes the dpkg lock from stack.sh
  - systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service
EOF
    [ "$(role "$1")" = switch ] && return
    cat <<'EOF'
swap: {filename: /swap.img, size: 2147483648}
runcmd:
  # DevStack runs services as stack, whose home must be traversable
  - chmod 755 /opt/stack
  # the cloud image's kernel has no vrf.ko: it is in linux-modules-extra
  - DEBIAN_FRONTEND=noninteractive apt-get install -y linux-modules-extra-$(uname -r)
  - echo vrf > /etc/modules-load.d/vrf.conf
  - modprobe vrf
EOF
}

network_config() {   # <node>: static address on the management network
    cat <<EOF
version: 2
ethernets:
  mgmt:
    match: {macaddress: "$(mgmt_mac "$1")"}
    set-name: enp1s0
    mtu: 9000
    addresses: [$(mgmt "$1")/24]
    routes: [{to: default, via: $MGMT_NET.1}]
    nameservers: {addresses: [$MGMT_NET.1]}
EOF
}

define_net() {   # <name> <xml>
    if ! virsh_ net-info "$1" >/dev/null 2>&1; then
        vh "cat > /tmp/$1.xml && sudo virsh -c qemu:///system net-define /tmp/$1.xml >/dev/null && rm /tmp/$1.xml" <<< "$2"
    fi
    virsh_ net-info "$1" | grep -q '^Active: *yes' || virsh_ net-start "$1" >/dev/null
    virsh_ net-autostart "$1" >/dev/null
    echo "network $1: active"
}

create_vm() {   # <node>
    local n=$1 vm=$VM_PREFIX$1 ram cpus disk nets
    if virsh_ dominfo "$vm" >/dev/null 2>&1; then echo "$vm: exists"; return; fi
    case "$(role "$n")" in
        switch) ram=$SWITCH_RAM cpus=1 disk=8G ;;
        *) ram=$HOST_RAM cpus=$HOST_CPUS disk=$HOST_DISK
           [ "$n" = "$CTRL" ] && ram=$CTRL_RAM cpus=$CTRL_CPUS ;;
    esac
    nets=$(gen nics "$n" | awk '{printf " --network network=%s,model=virtio,mac=%s", $1, $2}')
    vh "cat > /tmp/ud-$vm" < <(user_data "$n")
    vh "cat > /tmp/nc-$vm" < <(network_config "$n")
    vh "set -e
        sudo qemu-img create -q -f qcow2 -F qcow2 -b $IMG_DIR/base.img $IMG_DIR/$vm.qcow2 $disk
        sudo cloud-localds --network-config=/tmp/nc-$vm $IMG_DIR/seed-$vm.iso /tmp/ud-$vm
        rm -f /tmp/ud-$vm /tmp/nc-$vm
        sudo virt-install -q --connect qemu:///system --import --name $vm \
            --memory $ram --vcpus $cpus --cpu host-passthrough \
            --osinfo detect=on,name=ubuntu24.04 \
            --disk path=$IMG_DIR/$vm.qcow2,bus=virtio \
            --disk path=$IMG_DIR/seed-$vm.iso,device=cdrom \
            --network network=lab-mgmt,model=virtio,mac=$(mgmt_mac "$n") $nets \
            --graphics none --noautoconsole --autostart"
    echo "$vm: created ($(mgmt "$n"), $cpus vCPU, $ram MiB)"
}

wait_ready() {   # <node>: SSH answers and cloud-init is done
    for _ in $(seq 1 90); do
        if on "$1" 'cloud-init status --wait >/dev/null 2>&1; true' 2>/dev/null; then
            echo "$1: ready"; return
        fi
        sleep 5
    done
    die "$1: no SSH after 7 min (console: virsh console $VM_PREFIX$1 on $VM_HOST)"
}

do_vms() {
    need_vm_host vms
    say "VM host $VM_HOST"
    vh 'for c in virsh virt-install qemu-img cloud-localds curl; do
            command -v $c >/dev/null || { echo "missing $c on the VM host: sudo apt install qemu-kvm libvirt-daemon-system virtinst cloud-image-utils curl"; exit 1; }
        done
        test -e /dev/kvm || { echo "no /dev/kvm on the VM host"; exit 1; }'
    vh "sudo mkdir -p $IMG_DIR && { sudo test -s $IMG_DIR/base.img ||
        { echo 'downloading the Ubuntu 24.04 cloud image'; sudo curl -fsSL -o $IMG_DIR/base.img.part '$IMAGE_URL' &&
          sudo mv $IMG_DIR/base.img.part $IMG_DIR/base.img; }; }"
    define_net lab-mgmt "<network><name>lab-mgmt</name><forward mode='nat'/>
<bridge name='labbr0' stp='off' delay='0'/><mtu size='9000'/>
<ip address='$MGMT_NET.1' netmask='255.255.255.0'/></network>"
    local net
    for net in $(gen nets | awk '{print $1 ":" $2}'); do   # name:bridge
        define_net "${net%:*}" "<network><name>${net%:*}</name><bridge name='${net#*:}' stp='off' delay='0'/><mtu size='9000'/></network>"
    done
    local n
    for n in $(all_nodes); do create_vm "$n"; done
    for n in $(all_nodes); do wait_ready "$n"; done
}

# --- prep --------------------------------------------------------------------
do_prep() {
    local n
    for n in $(hosts); do
        say "prep $n ($(mgmt "$n"))"
        on "$n" "set -e
            [ \"\$(hostname)\" = $n ] || { echo \"hostname is \$(hostname), must be $n: it becomes the Neutron host name\"; exit 1; }
            sudo -n true 2>/dev/null || { echo \"user \$USER needs passwordless sudo\"; exit 1; }
            grep -q 'VERSION_ID=\"24.04\"' /etc/os-release || echo 'warning: tested on Ubuntu 24.04 only'
            for _ in \$(seq 120); do sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; sleep 5; done
            sudo mkdir -p /opt/stack && sudo chown \$USER /opt/stack && sudo chmod 755 /opt/stack
            command -v git >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git
            sudo modprobe vrf 2>/dev/null || { sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq linux-modules-extra-\$(uname -r) && sudo modprobe vrf; }
            echo vrf | sudo tee /etc/modules-load.d/vrf.conf >/dev/null
            [ -d /opt/stack/devstack/.git ] || git clone -q -b $DEVSTACK_BRANCH https://opendev.org/openstack/devstack /opt/stack/devstack
            echo \"devstack \$(git -C /opt/stack/devstack rev-parse --abbrev-ref HEAD) \$(git -C /opt/stack/devstack log -1 --format=%h)\""
    done
}

# --- underlay ----------------------------------------------------------------
UNIT='[Unit]
Description=SRv6 lab underlay (addresses and routes)
After=network-online.target
Wants=network-online.target
Before=devstack@q-agt.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/lab-underlay.sh
[Install]
WantedBy=multi-user.target'

do_underlay() {
    local n t bad=0 targets
    say "underlay ($([ "$FABRIC" = none ] && echo "flat" || echo "fabric $FABRIC"))"
    for n in $(all_nodes); do
        gen underlay "$n" | on "$n" 'sudo install -m 755 /dev/stdin /usr/local/sbin/lab-underlay.sh'
        # networkd must not delete these routes when it is restarted
        on "$n" "sudo mkdir -p /etc/systemd/networkd.conf.d &&
            printf '[Network]\nManageForeignRoutingPolicyRules=no\nManageForeignRoutes=no\n' |
                sudo tee /etc/systemd/networkd.conf.d/10-lab-keep-routes.conf >/dev/null
            echo '$UNIT' | sudo tee /etc/systemd/system/lab-underlay.service >/dev/null
            sudo systemctl daemon-reload && sudo systemctl enable -q lab-underlay.service &&
            sudo systemctl restart lab-underlay.service" ||
            { on "$n" 'sudo journalctl -u lab-underlay -n 20 --no-pager'; die "$n: lab-underlay failed"; }
    done
    # every host reaches every node's underlay address
    targets=$(awk '{printf "%s:fd00:1ab::%x ", $1, $3}' <<< "$NODES_TXT")
    for n in $(hosts); do
        if on "$n" "f=0; for t in $targets; do ping -6 -c1 -W2 \${t#*:} >/dev/null 2>&1 || { echo \"  -> \${t%%:*} (\${t#*:}) FAIL\"; f=1; }; done; exit \$f"; then
            echo "$n: reaches every node"
        else bad=1; fi
    done
    [ $bad = 0 ] || die "underlay incomplete"
}

# --- stack -------------------------------------------------------------------
local_conf() {   # <host>
    local n=$1 c=$CTRL
    cat <<EOF
[[local|localrc]]
HOST_IP=$(mgmt "$n")
ADMIN_PASSWORD=$ADMIN_PASSWORD
DATABASE_PASSWORD=\$ADMIN_PASSWORD
RABBIT_PASSWORD=\$ADMIN_PASSWORD
SERVICE_PASSWORD=\$ADMIN_PASSWORD
LOGFILE=\$DEST/logs/stack.sh.log
MULTI_HOST=1
export SETUPTOOLS_ENABLE_FEATURES=legacy-editable

# ML2/OVS with VXLAN: the plugin's agent side runs inside the OVS agent
Q_AGENT=openvswitch
Q_ML2_PLUGIN_MECHANISM_DRIVERS=openvswitch,l2population
Q_ML2_PLUGIN_TYPE_DRIVERS=flat,vlan,vxlan
Q_ML2_TENANT_NETWORK_TYPE=vxlan
Q_TUNNEL_TYPES=vxlan
EOF
    if [ "$n" = "$c" ]; then
        cat <<EOF
enable_service q-svc q-agt q-dhcp q-l3 q-meta n-cpu
disable_service ovn-controller ovn-northd q-ovn-metadata-agent ovs-vswitchd ovsdb-server
disable_service c-api c-sch c-vol c-bak etcd3 tempest dstat
EOF
    else
        cat <<EOF
SERVICE_HOST=$(mgmt "$c")
MYSQL_HOST=\$SERVICE_HOST
RABBIT_HOST=\$SERVICE_HOST
GLANCE_HOSTPORT=\$SERVICE_HOST:9292
disable_all_services
enable_service n-cpu q-agt placement-client
EOF
    fi
    on "$n" 'test -e /dev/kvm' || echo "LIBVIRT_TYPE=qemu   # no /dev/kvm here: slow, but works"
    cat <<EOF

enable_plugin networking-srv6-agent $PLUGIN_REPO $PLUGIN_BRANCH
SRV6_LOCATOR=fc00:0:$(printf %x "$(nid "$n")")::/48
SRV6_DATAPLANE_ENCODING=$ENCODING
EOF
    [ "$n" = "$c" ] && [ "$FABRIC" != none ] && echo "SRV6_TOPOLOGY_FILE=/opt/stack/srv6-topology"
    [ -z "$LOCAL_CONF_EXTRA" ] || printf '%s\n' "$LOCAL_CONF_EXTRA"
}

stack_running() { on "$1" 'pgrep -f "[b]ash ./stack.sh" >/dev/null'; }

start_stack() {   # <host>: stack.sh detached; its exit code lands in lab-stack.rc
    local n=$1
    if stack_running "$n"; then
        echo "$n: stack.sh already running, waiting for it"; return 0
    fi
    if [ "$(on "$n" 'cat /opt/stack/lab-stack.rc 2>/dev/null')" = 0 ]; then
        echo "$n: already stacked (to restack: ./devstack/unstack.sh, rm /opt/stack/lab-stack.rc)"
        return 1
    fi
    local_conf "$n" | on "$n" 'cat > /opt/stack/devstack/local.conf'
    # the whole background job off the ssh session's stdout, or ssh waits for it
    on "$n" "rm -f /opt/stack/lab-stack.rc && cd /opt/stack/devstack &&
        { setsid nohup bash -c './stack.sh >/opt/stack/lab-stack.out 2>&1; echo \$? > /opt/stack/lab-stack.rc' & } </dev/null >/dev/null 2>&1"
    echo "$n: stack.sh started (log: /opt/stack/logs/stack.sh.log)"
}

wait_stack() {   # <host>
    local n=$1 rc= t=0
    while :; do
        rc=$(on "$n" 'cat /opt/stack/lab-stack.rc 2>/dev/null' || true)
        [ -n "$rc" ] && ! stack_running "$n" && break
        if [ $((t % 300)) = 0 ] && [ $t -gt 0 ]; then
            echo "$n: $(on "$n" 'tail -n1 /opt/stack/logs/stack.sh.log 2>/dev/null | cut -c1-110' || true)"
        fi
        [ $t -ge 7200 ] && die "$n: stack.sh still running after 2 h"
        sleep 30; t=$((t + 30))
    done
    if [ "$rc" = 0 ]; then echo "$n: stack.sh done ($((t / 60)) min)"; return; fi
    on "$n" 'tail -n 30 /opt/stack/lab-stack.out'
    die "$n: stack.sh failed (exit $rc): /opt/stack/logs/stack.sh.log on $n"
}

do_stack() {
    local n started=
    say "stack (DevStack $DEVSTACK_BRANCH, plugin $PLUGIN_BRANCH, $ENCODING)"
    [ "$FABRIC" = none ] || gen topology | on "$CTRL" 'cat > /opt/stack/srv6-topology'
    start_stack "$CTRL" && wait_stack "$CTRL"
    for n in $(computes); do start_stack "$n" && started="$started $n"; done
    for n in $started; do wait_stack "$n"; done
    on "$CTRL" 'source /opt/stack/devstack/openrc admin admin >/dev/null &&
        nova-manage cell_v2 discover_hosts >/dev/null 2>&1 && echo "computes discovered"'
}

# --- fabric ------------------------------------------------------------------
# DynamicUser: the agent only reads /proc and /sys, so it runs as a throwaway
# user with CAP_NET_ADMIN only (its nft counters for --aggregates); systemd
# hands it the root-only credentials file (LoadCredential, %d).
agent_unit() {
    cat <<EOF
[Unit]
Description=SRv6 fabric agent: link telemetry for Neutron
After=lab-underlay.service network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 -I /usr/local/sbin/srv6-fabric-agent.py --links /etc/srv6-fabric-agent/links --aggregates --neutron-url http://$(mgmt "$CTRL")/networking --credentials %d/neutron
LoadCredential=neutron:/etc/srv6-fabric-agent/credentials
Restart=always
RestartSec=2
DynamicUser=yes
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
[Install]
WantedBy=multi-user.target
EOF
}

do_fabric() {
    [ "$FABRIC" != none ] || { echo "no FABRIC: nothing to do"; return; }
    say "fabric agents"
    local n cred
    # devstack's admin account; srv6_link_states is admin-only
    cred=$(on "$CTRL" 'source /opt/stack/devstack/openrc admin admin >/dev/null &&
        printf "OS_AUTH_URL=%s\nOS_USERNAME=%s\nOS_PASSWORD=%s\nOS_PROJECT_NAME=%s\nOS_USER_DOMAIN_ID=default\nOS_PROJECT_DOMAIN_ID=default\n" \
            "$OS_AUTH_URL" "$OS_USERNAME" "$OS_PASSWORD" "$OS_PROJECT_NAME"')
    for n in $(switches); do
        on "$n" 'sudo install -m 755 /dev/stdin /usr/local/sbin/srv6-fabric-agent.py' < "$HERE/../tools/srv6-fabric-agent.py"
        gen links "$n" | on "$n" 'sudo install -D -m 644 /dev/stdin /etc/srv6-fabric-agent/links'
        printf '%s\n' "$cred" | on "$n" 'sudo install -m 600 /dev/stdin /etc/srv6-fabric-agent/credentials'
        agent_unit | on "$n" 'sudo tee /etc/systemd/system/srv6-fabric-agent.service >/dev/null &&
            sudo systemctl daemon-reload && sudo systemctl enable -q srv6-fabric-agent &&
            sudo systemctl restart srv6-fabric-agent'
        echo "$n: srv6-fabric-agent running"
    done
}

# --- check -------------------------------------------------------------------
# Runs on the controller. VMs of a domain network have no router, hence no
# floating IP: the check enters them over SSH from the DHCP namespace of
# their network, which spans every node.
CHECK_SCRIPT='
source /opt/stack/devstack/openrc admin admin >/dev/null 2>&1   # not under set -u
set -u
pass=0 fail=0
ok() { if "$@" >/dev/null 2>&1; then pass=$((pass + 1)); echo "  ok    $CHECK"; else fail=$((fail + 1)); echo "  FAIL  $CHECK"; fi; }
os() { openstack "$@" 2>/dev/null; }
set -- $HOSTS
H1=$1 H2=${2:-} H3=${3:-}

every_host() {   # <list> <suffix>: every host has a line "<host><suffix>"
    local h; for h in $HOSTS; do grep -qx "$h$2" <<< "$1" || return 1; done
}
CHECK="every OVS agent alive ($HOSTS)"
ok every_host "$(os network agent list --agent-type open-vswitch -f value -c Host -c Alive)" " True"
CHECK="one SRv6 locator per host"
ok every_host "$(os srv6 locator list -f value -c Host)" ""
[ -n "$H2" ] || { echo "one host only: no traffic check"; echo "== $pass passed, $fail failed"; exit $fail; }

# the fixture, reused when it exists
os network show lab-net-a >/dev/null || { os network create lab-net-a >/dev/null &&
    os subnet create --network lab-net-a --subnet-range 10.91.1.0/24 lab-sub-a >/dev/null; }
os network show lab-net-b >/dev/null || { os network create lab-net-b >/dev/null &&
    os subnet create --network lab-net-b --subnet-range 10.91.2.0/24 lab-sub-b >/dev/null; }
os srv6 domain show lab-domain >/dev/null || os srv6 domain create lab-domain >/dev/null
for net in lab-net-a lab-net-b; do
    os srv6 network association list lab-domain -f value | grep -q "$(os network show $net -f value -c id)" ||
        os srv6 network association create lab-domain $net >/dev/null
done
CHECK="domain lab-domain joins lab-net-a and lab-net-b"
ok test "$(os srv6 network association list lab-domain -f value | wc -l)" = 2
[ -f ~/.ssh/lab-key ] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/lab-key
os keypair show lab-key >/dev/null || os keypair create --public-key ~/.ssh/lab-key.pub lab-key >/dev/null
sg=$(os security group list --project admin -f value -c ID -c Name | awk "\$2 == \"default\" {print \$1; exit}")
os security group rule create --ingress --protocol icmp $sg >/dev/null
os security group rule create --ingress --protocol tcp --dst-port 22 $sg >/dev/null
image=$(os image list -f value -c Name | grep -m1 -i cirros)
boot() {   # <name> <net> <host>
    os server show $1 >/dev/null || os server create --image "$image" --flavor m1.tiny \
        --network $2 --key-name lab-key --config-drive true \
        --availability-zone nova:$3 $1 >/dev/null
}
boot lab-vm-a lab-net-a $H1; boot lab-vm-b lab-net-b $H2
for _ in $(seq 60); do
    [ "$(os server show lab-vm-a -f value -c status)$(os server show lab-vm-b -f value -c status)" = ACTIVEACTIVE ] && break
    sleep 5
done
CHECK="lab-vm-a on $H1 and lab-vm-b on $H2 ACTIVE"
ok test "$(os server show lab-vm-a -f value -c status)$(os server show lab-vm-b -f value -c status)" = ACTIVEACTIVE
ipa=$(os port list --server lab-vm-a -f value -c "Fixed IP Addresses" | grep -o "10\.91\.1\.[0-9]*")
ipb=$(os port list --server lab-vm-b -f value -c "Fixed IP Addresses" | grep -o "10\.91\.2\.[0-9]*")
ns=qdhcp-$(os network show lab-net-a -f value -c id)
in_a() { sudo ip netns exec $ns ssh -n -i ~/.ssh/lab-key -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 cirros@$ipa "$@"; }
for _ in $(seq 40); do in_a true 2>/dev/null && break; sleep 5; done
ping_b() { in_a "ping -c 3 -W 2 $ipb" 2>/dev/null | grep -q "3 packets received\|3 received"; }
CHECK="lab-vm-a ($ipa, $H1) pings lab-vm-b ($ipb, $H2) over the domain"
ok ping_b
route_b() { ip route show table all | grep "^$ipb .*seg6"; }
echo "  route on $H1: $(route_b)"

te() {   # <via> <locator id>: a TE path toward H2 through <via>
    os srv6 te path list --domain lab-domain -f value -c ID | xargs -r -n1 openstack srv6 te path delete 2>/dev/null
    os srv6 te path create --domain lab-domain --destination-host $H2 --via-host $1 >/dev/null
    for _ in $(seq 15); do route_b | grep -q "fc00:0:$2:" && break; sleep 2; done
    CHECK="TE path via $1: ACTIVE"
    ok test "$(os srv6 te path list --domain lab-domain -f value -c Status)" = ACTIVE
    CHECK="TE path via $1: the route on $H1 carries $1 (fc00:0:$2:)"
    ok grep -q "fc00:0:$2:" <<< "$(route_b)"
    echo "  route on $H1: $(route_b)"
    CHECK="TE path via $1: ping still works"
    ok ping_b
}
[ -n "$H3" ] && te $H3 3
if [ -n "$SWITCH" ]; then
    CHECK="fabric agents report link state"
    ok test "$(os srv6 link list -f value -c Stale | grep -c False)" -gt 0
    te $SWITCH $SWITCH_ID
fi
os srv6 te path list --domain lab-domain -f value -c ID | xargs -r -n1 openstack srv6 te path delete 2>/dev/null
echo "== $pass passed, $fail failed (fixture kept: lab-net-a/b, lab-domain, lab-vm-a/b)"
exit $fail
'

do_check() {
    local sw; sw=$(switches | head -1)
    say "check"
    on "$CTRL" "HOSTS='$(hosts | xargs)' SWITCH='$sw' SWITCH_ID='$([ -z "$sw" ] || printf %x "$(nid "$sw")")' bash -s" <<< "$CHECK_SCRIPT"
}

# --- status, destroy ---------------------------------------------------------
do_status() {
    local n s
    [ -z "$VM_HOST" ] || { say "VMs on $VM_HOST"; virsh_ list --all | grep " $VM_PREFIX" || echo "  none"; }
    say "nodes"
    for n in $(all_nodes); do
        if s=$(on "$n" 'printf "underlay=%s" "$(systemctl is-active lab-underlay 2>/dev/null)"
            if pgrep -f "[b]ash ./stack.sh" >/dev/null; then printf " stack.sh=running"
            elif [ -f /opt/stack/lab-stack.rc ]; then printf " stack.sh=exit-%s" "$(cat /opt/stack/lab-stack.rc)"; fi
            systemctl list-unit-files srv6-fabric-agent.service >/dev/null 2>&1 &&
                printf " fabric-agent=%s" "$(systemctl is-active srv6-fabric-agent)"; true' 2>/dev/null); then
            printf '  %-10s %-8s %-15s %s\n' "$n" "$(role "$n")" "$(mgmt "$n")" "$s"
        else
            printf '  %-10s %-8s %-15s unreachable\n' "$n" "$(role "$n")" "$(mgmt "$n")"
        fi
    done
}

do_destroy() {
    need_vm_host destroy
    local n vm net
    for n in $(all_nodes); do
        vm=$VM_PREFIX$n
        virsh_ dominfo "$vm" >/dev/null 2>&1 || continue
        virsh_ destroy "$vm" >/dev/null 2>&1 || true
        virsh_ undefine "$vm" >/dev/null
        vh "sudo rm -f $IMG_DIR/$vm.qcow2 $IMG_DIR/seed-$vm.iso"
        echo "$vm: deleted"
    done
    for net in lab-mgmt $(gen nets | awk '{print $1}'); do
        virsh_ net-info "$net" >/dev/null 2>&1 || continue
        virsh_ net-destroy "$net" >/dev/null 2>&1 || true
        virsh_ net-undefine "$net" >/dev/null && echo "network $net: removed"
    done
    echo "kept: $IMG_DIR/base.img on $VM_HOST (the downloaded image)"
}

case ${1:-} in
    up) [ -z "$VM_HOST" ] || do_vms; do_prep; do_underlay; do_stack; do_fabric; do_check ;;
    vms) do_vms ;;
    prep) do_prep ;;
    underlay) do_underlay ;;
    stack) do_stack ;;
    fabric) do_fabric ;;
    check) do_check ;;
    status) do_status ;;
    ssh) shift; [ $# -ge 1 ] || die "ssh <node> [cmd]"; n=$1; shift
         [ -n "$(mgmt "$n")" ] || die "no node $n"
         if [ $# -eq 0 ]; then ssh -t "${LAB_SSH[@]}" "$SSH_USER@$(mgmt "$n")"; else on "$n" "$@"; fi ;;
    destroy) do_destroy ;;
    *) sed -n '2,22p' "$0" >&2; exit 2 ;;
esac
