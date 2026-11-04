=====================
networking-srv6-agent
=====================

Adds support for SRv6 to Neutron, including a per-node agent that manages the local SRv6 configuration and a Neutron extension that allows users to create SRv6-enabled networks.
Derived from networking-bgpvpn.


Requirements
------------

**ML2/OVS only.** The per-node code runs as an extension of the Neutron OVS L2
agent, which supplies the integration bridge and the local-VLAN mapping it needs.
Under ML2/OVN there is no Neutron L2 agent, so this plugin has no host and will not
run; under linuxbridge it refuses to initialise. Kernel 5.x or newer with
``CONFIG_IPV6_SEG6_LWTUNNEL``, and an IPv6-routable underlay.


Limits
------

Every SRv6 domain takes a SID function id from a pool all projects share
(``[srv6] function_id_ranges``, 4096 ids at most), so a project may create 10
domains by default. Change it with ``[quotas] quota_srv6_domain`` (negative:
unlimited), or per project through Neutron's quota API
(``PUT /v2.0/quotas/<project>`` with ``srv6_domain``). Every agent needs a
locator of its own: the server refuses a sync whose locator overlaps the
locator of another live node or of a ``topology_file`` node.

A domain routes only the traffic *between* its networks. Inside one network
the instances share an L2 segment stretched over the nodes' tunnels, so
traffic between two of them, even on different nodes, is switched over VXLAN
and never enters the domain: no SRv6, and no TE path applies to it. A TE path
whose ``source_port_id`` and ``destination_port_id`` are on the same network
is refused. A path toward a node or from a source node is accepted, and
steers only the traffic that comes from, or goes to, other networks.


Command line
------------

The package installs an OpenStackClient plugin, so wherever it sits next to
``python-openstackclient`` the ``openstack srv6`` commands are available::

    openstack srv6 domain create red
    openstack srv6 network association create red net-a
    openstack srv6 network association create red net-b

    # admin only
    openstack srv6 locator list
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host node3
    # only the traffic that enters the domain at node1
    openstack srv6 te path create --domain red --destination-host node2 \
        --source-host node1 --via-host node3
    openstack srv6 te path set <te-path> --no-via-host

``openstack command list --group openstack.srv6.v2`` lists all of them.
