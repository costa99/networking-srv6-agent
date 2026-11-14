=====================
networking-srv6-agent
=====================

SRv6 connectivity and traffic engineering for OpenStack Neutron (ML2/OVS).

.. contents::
   :local:
   :depth: 1


What it does
------------

The plugin replaces the Neutron router for east-west traffic between a
project's networks. You group networks into an **SRv6 domain**; traffic
between them is routed by the Linux kernel of each compute node and carried
across the IPv6 underlay with Segment Routing over IPv6 (SRv6), with no
network node in the path.

- **Domains.** A domain is a routing context, like a VRF. Every node that
  hosts a port of the domain gets a Linux VRF, a local gateway for each
  attached network, and one SID that decapsulates traffic into that VRF
  (``End.DT46``). Domains are isolated from each other.
- **Distributed routing.** Each node routes the packets of its own VMs and
  encapsulates them directly toward the destination node's SID: one hop, no
  hairpin through a central router.
- **Two encodings.** ``usid`` (RFC 9800 microSID, default): a path of up to
  four waypoints fits in a 40-byte outer header with no SRH. ``encap``: full
  128-bit SIDs in an SRH.
- **Traffic engineering (admin).** A TE path forces a domain's traffic through
  chosen waypoints (other hosts or, with a topology file, fabric switches).
  It can match all traffic toward a host, only traffic toward one port,
  and/or only traffic entering at one host or from one VM. A path through a
  dead waypoint is marked ``DEGRADED`` and falls back to the direct route.
- **Automatic steering (optional).** With an underlay topology file and the
  telemetry agent running on the fabric switches, the server watches link
  load and moves heavy VM pairs off congested links on its own, choosing
  their new path with a built-in path computation (CSPF). Not needed for
  anything else.
- **Tools.** An ``openstack srv6 ...`` CLI plugin and Horizon panels
  (project: domains and associations; admin: locators, TE paths, topology).

How it is built
~~~~~~~~~~~~~~~

========================  ==================================================
Component                 Role
========================  ==================================================
Service plugin ``srv6``   API, database, SID allocation, TE path computation;
                          tells the agents what to install (RPC). Programs no
                          kernel state itself.
L2 agent extension        Runs inside ``neutron-openvswitch-agent`` on every
``srv6``                  node; installs VRFs, SIDs, routes and the edge
                          filter with ``ip``/``ovs-vsctl``/``nft``.
``srv6_dashboard``        Horizon panels.
``tools/srv6-fabric-``    Optional telemetry agent for underlay switches
``agent.py``              (link load and heavy VM pairs).
========================  ==================================================

Each node announces its **locator** (an IPv6 prefix, e.g. ``fc00:0:1::/48``);
a domain's SID on that node is the locator plus the domain's function id.


Requirements
------------

- **ML2/OVS with VXLAN tunnels.** The agent side is an extension of the OVS
  L2 agent. ML2/OVN (no L2 agent) and linuxbridge are not supported.
- Linux kernel with ``CONFIG_IPV6_SEG6_LWTUNNEL`` and the ``vrf`` module
  (on Ubuntu cloud images: ``linux-modules-extra``). For ``usid``: kernel and
  iproute2 >= 6.1.
- An **IPv6-routable underlay** between all nodes: each node's locator must
  be reachable from every other node. With ``usid`` all locators are /48 in
  one /32 block.
- Python >= 3.11, Neutron of the same release.


Deploying with DevStack
-----------------------

**Quickest way: the test lab.** ``lab/lab.sh up`` builds a complete
multi-node DevStack with the plugin (on VMs it creates, or on your own
machines), sets up the IPv6 underlay, optionally a spine-leaf fabric with the
telemetry agents, and runs a smoke test. See ``lab/README.rst``. The rest of
this section is what it does, for your own deployment.

Enable the plugin in ``local.conf`` on **every** node (controller and
computes). It installs the package, enables the service plugin and the agent
extension, writes the options into ``neutron.conf`` /
``ml2_conf.ini``, runs the DB migrations (through ``neutron-db-manage``) and,
if Horizon is enabled, installs the panels.

Controller (all-in-one or API node)::

    [[local|localrc]]
    Q_AGENT=openvswitch
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=openvswitch,l2population
    Q_ML2_TENANT_NETWORK_TYPE=vxlan
    Q_ML2_PLUGIN_TYPE_DRIVERS=flat,vlan,vxlan,local
    enable_service q-svc q-agt q-dhcp q-l3 q-meta
    disable_service ovn-controller ovn-northd q-ovn-metadata-agent

    enable_plugin networking-srv6-agent https://github.com/costa99/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:1::/48        # unique per node

Compute node::

    [[local|localrc]]
    SERVICE_HOST=<controller IP>
    disable_all_services
    enable_service n-cpu q-agt placement-client
    Q_AGENT=openvswitch
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=openvswitch,l2population
    Q_ML2_TENANT_NETWORK_TYPE=vxlan
    Q_TUNNEL_TYPES=vxlan

    enable_plugin networking-srv6-agent https://github.com/costa99/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:2::/48

DevStack variables (defaults in ``devstack/settings``):

=============================  ==========================  ================================
Variable                       Default                     Meaning
=============================  ==========================  ================================
``SRV6_LOCATOR``               *(required on q-agt)*       This node's locator; unique per
                                                           node. Stack fails without it.
``SRV6_UNDERLAY_INTERFACE``    auto                        NIC of the IPv6 underlay. Unset:
                                                           found from the route to each
                                                           peer's locator.
``SRV6_DATAPLANE_ENCODING``    ``usid``                    ``usid`` or ``encap`` (server).
``SRV6_FUNCTION_ID_RANGES``    ``0xe000:0xefff``           Pool of domain function ids
                                                           (server); must stay in this
                                                           range.
``SRV6_TOPOLOGY_FILE``         *(unset)*                   Underlay topology for link state
                                                           and automatic steering (server).
``SRV6_VRF_TABLE_BASE``        ``0``                       VRF table = base + function id.
                                                           Same value on every node.
``SRV6_RESYNC_INTERVAL``       ``300``                     Seconds between full agent
                                                           resyncs.
``SRV6_DASHBOARD_ENABLE``      all panels                  Horizon panel files to install.
=============================  ==========================  ================================

The underlay is your job: give every node an IPv6 address and routes so that
``ping6`` to each other node's locator address works before stacking.

Check the deployment::

    openstack network agent list          # OVS agents alive on every node
    openstack srv6 locator list           # one locator per node (admin)
    openstack extension list | grep -i srv6

Without DevStack, the same result is: ``service_plugins = ...,srv6`` and a
``[srv6]`` section in ``neutron.conf``; ``extensions = srv6`` under
``[agent]`` and a ``[srv6_agent]`` section with ``locator`` in the OVS agent's
config; then ``neutron-db-manage upgrade heads``.


Using it
--------

Connect networks (any project)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    openstack network create net-a
    openstack subnet create --network net-a --subnet-range 10.0.1.0/24 sub-a
    openstack network create net-b
    openstack subnet create --network net-b --subnet-range 10.0.2.0/24 sub-b

    openstack srv6 domain create red
    openstack srv6 network association create red net-a
    openstack srv6 network association create red net-b

Boot VMs on ``net-a`` and ``net-b``: they reach each other through their
subnet gateway, which now lives on every node that hosts one of them. Removing
an association or deleting the domain removes the state from every node.

Rules an association must follow (the API refuses otherwise):

- the network belongs to the domain's project and is not external;
- the network has **no router interface** (and a router interface cannot be
  added later): a network is routed either by a Neutron router or by a domain;
- a network belongs to at most one domain;
- subnets in one domain must not overlap.

A domain routes traffic *between* its networks only. Traffic between two VMs
of the same network stays on L2 (VXLAN) and never uses SRv6.

Inspect::

    openstack srv6 domain list --long
    openstack srv6 domain show red        # function id, SIDs
    openstack srv6 network association list red

Traffic engineering (admin)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    # all of red's traffic toward node2 goes through node3
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host node3

    # only the traffic entering the domain at node1
    openstack srv6 te path create --domain red --destination-host node2 \
        --source-host node1 --via-host node3

    # only toward one VM port, or only from one VM port
    openstack srv6 te path create --domain red --destination-port vm-b-port \
        --via-host node3 --via-host node4
    openstack srv6 te path create --domain red --destination-host node2 \
        --source-port vm-a-port --via-host node4

    openstack srv6 te path list --domain red --long
    openstack srv6 te path set <id> --via-host node4   # re-steer
    openstack srv6 te path set <id> --no-via-host      # explicitly direct
    openstack srv6 te path delete <id>

``--via-host`` can be repeated; waypoints are hosts, or fabric nodes named in
the topology file. When several paths match, the most specific wins:
source + port > source + host > port > host > direct. A path whose source and destination ports are on the same
network is refused (that traffic never enters the domain).

Link state and automatic steering (admin, optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Everything above works without this part. It adds a feedback loop on the
underlay: telemetry agents on the fabric switches report link load and the
heaviest VM pairs; every ``te_reopt_interval`` the server looks for links
loaded above ``te_reopt_high`` and gives the largest pairs on them a path of
their own (``origin=auto``). The path is chosen by the built-in **path
computation** (CSPF: fewest hops over links with room, then the widest
bottleneck) and released when the pair goes idle or the link has room
again. Manual TE paths never use it: their waypoints are the ones you give.

What is needed:

- a topology file: without it, link state is disabled and fabric nodes cannot
  be waypoints;
- the fabric agents: without them there is nothing to measure, so the server
  never steers anything (it stays idle, no error).

1. Write a topology file on the controller and set ``SRV6_TOPOLOGY_FILE``
   (or ``[srv6] topology_file``)::

       node spine1 fc00:0:101::/48
       node leaf1  fc00:0:111::/48
       node node1  fc00:0:1::/48      # hosts use their Neutron host name
       link leaf1 spine1 10000        # capacity per direction, Mbit/s
       link node1 leaf1  10000

2. Run the telemetry agent on each fabric switch (Linux)::

       tools/srv6-fabric-agent.py --links links.txt --period 5 \
           --neutron-url <neutron endpoint> --credentials admin-openrc \
           --aggregates

   ``links.txt`` has one line per neighbour: ``<interface> <this node>
   <neighbour>``. ``--neutron-url`` is the network endpoint
   (``openstack endpoint list --service network``). ``srv6-fabric-agent.py
   test`` runs a self-check.

3. Watch it::

       openstack srv6 link list --long        # load per link, stale flag
       openstack srv6 aggregate list --long   # heavy VM pairs per link
       openstack srv6 te path list --long     # origin=auto: steered by server

Thresholds and limits are the ``te_reopt_*`` and ``te_auto_*`` options in
``[srv6]`` (``oslo-config-generator`` lists them all with help text).

Horizon
~~~~~~~

- *Project → Network → SRv6 Domains*: create domains, associate networks.
- *Project → Network → Network Topology*: replaced by a version that shows
  domains.
- *Admin → Network → SRv6*: all domains, locators, TE paths.
- *Admin → Network → SRv6 Topology*: the underlay graph with live link load.

``openstack command list --group openstack.srv6.v2`` lists every CLI command.


Configuration reference
-----------------------

Main options; see ``networking_srv6_agent/common/config.py`` for all of them.

``[srv6]`` in ``neutron.conf`` (server)
  ``dataplane_encoding`` (``usid``), ``function_id_ranges``
  (``0xe000:0xefff``), ``vrf_table_base`` (``0``), ``te_liveness_interval``
  (``15`` s; failover time ≈ Neutron's ``agent_down_time`` + this),
  ``topology_file``, ``te_reopt_interval`` (``10`` s, ``0`` = off),
  ``te_auto_steering`` (``true``).

``[srv6_agent]`` in the OVS agent config (every node)
  ``locator`` (required), ``underlay_interface`` (auto), ``resync_interval``
  (``300`` s), ``apply_sysctls`` (``true``; set ``false`` if config
  management owns ``seg6_enabled``, ``forwarding``, ``rp_filter``…).

``[quotas]``
  ``quota_srv6_domain`` (``10`` per project; ``-1`` unlimited). All projects
  share the 4096 function ids.

Changing ``dataplane_encoding`` requires a deployment with no domains:
restart the server, then the agents.


Development
-----------

::

    tox -e py3          # unit tests
    tox -e pep8         # flake8
    tox -e dashboard    # Horizon panel tests

License: Apache 2.0.
