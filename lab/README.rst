===============
SRv6 test lab
===============

``lab.sh`` builds a multi-node DevStack cloud with networking-srv6-agent and
checks that it works, from one config file. Use it to try the plugin, to
develop on it, or as a starting point for your own setup.

::

    cp lab.conf.example lab.conf    # edit: where, how many hosts, which fabric
    ./lab.sh up                     # ~1 h: VMs, underlay, DevStack, smoke test
    ./lab.sh ssh node1              # the controller: source devstack/openrc


Two ways to run it
------------------

**VM mode** (``VM_HOST`` set). One Linux machine with KVM becomes the whole
lab: ``lab.sh`` creates the hosts (and the switches) as VMs on a private NAT
network, ``192.168.150.0/24``, and reaches them through that machine with
SSH. Needs ``libvirt``, ``virt-install``, ``cloud-image-utils``, ``curl`` and
nested virtualisation (the hosts run their own instances). Three hosts take
about 16 GB of RAM and 30 GB of disk; each switch adds under 1 GB.

**Your own machines** (``VM_HOST`` empty). Ubuntu 24.04 hosts on one L2
segment, each with its host name set to the name in ``HOSTS`` and a user
with passwordless sudo that you can SSH into with a key. ``lab.sh`` skips
the VM part and does everything else.


Two kinds of underlay
---------------------

``FABRIC=none``
  The hosts reach each other on their management segment: each gets an
  underlay address ``fd00:1ab::<id>/64`` and a route to every other host's
  locator. Enough for domains, routing, TE paths through hosts and waypoint
  failover.

``FABRIC=<topology file>`` (VM mode)
  Switches as VMs and one virtual cable per link, routed with static ECMP
  (what an IGP would install). The switches can be TE waypoints, run the
  fabric telemetry agent (``tools/srv6-fabric-agent.py``), and the plugin gets
  the topology file, so link state, path computation and automatic steering
  work. See ``fabric.topo.example``: any connected graph of switches and
  hosts, with a capacity per link.

Node ``<id>`` (hosts 1, 2, …, switches 0x101, …) has locator
``fc00:0:<id>::/48``. The underlay is applied by
``/usr/local/sbin/lab-underlay.sh`` (``lab-underlay.service``) on every node,
so it survives reboots. Tenant VXLAN stays on the management network.


Commands
--------

Every command can be run again: it skips what is already done.

============  ================================================================
``up``        everything below, in order
``vms``       VM mode: networks and VMs, wait for cloud-init
``prep``      check hosts, install git and the ``vrf`` module, clone DevStack
``underlay``  install the underlay on every node, ping every node
``stack``     write ``local.conf``, run ``stack.sh`` on the controller, then
              the computes in parallel, then discover them
``fabric``    with a fabric: start the telemetry agent on every switch
``check``     smoke test (below)
``status``    VMs, underlay, stack.sh result, fabric agents per node
``ssh``       ``lab.sh ssh <node> [command]``
``destroy``   VM mode: delete the lab's VMs, disks and networks
============  ================================================================

``check`` creates (or reuses) two networks ``lab-net-a``/``lab-net-b`` in
domain ``lab-domain``, a cirros VM on the first and second host, and checks:

- every OVS agent alive, one locator per host;
- ``lab-vm-a`` pings ``lab-vm-b`` across the domain;
- with a third host: a TE path via it is ``ACTIVE``, the route on the first
  host carries its SID, ping still works;
- with a fabric: link state is reported, and the same for a TE path via a
  switch.

The fixture is kept so you can continue by hand from it.


Adapting it
-----------

- **More or fewer hosts**: edit ``HOSTS``; ``lab.sh up`` adds the new ones
  (remove one with ``destroy`` and ``up``).
- **Your own fabric shape**: copy ``fabric.topo.example``.
- **Your own plugin branch**: ``PLUGIN_REPO`` and ``PLUGIN_BRANCH``.
- **Full SIDs instead of microSID**: ``ENCODING=encap``.
- **Extra DevStack settings** (e.g. drop Horizon, which is on with the SRv6 panels):
  ``LOCAL_CONF_EXTRA``.
- **Restack a node**: ``lab.sh ssh <node> ./devstack/unstack.sh``, delete
  ``/opt/stack/lab-stack.rc`` on it, ``lab.sh stack``.
- **Reach Horizon or the API from your machine** in VM mode:
  ``ssh -L 8080:192.168.150.11:80 <VM_HOST>``, then http://localhost:8080.

``labgen.py`` derives every address, route, link map and the plugin's
topology file from ``lab.conf``; ``python3 labgen.py test`` checks it.
