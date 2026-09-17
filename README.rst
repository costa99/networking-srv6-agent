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
    openstack srv6 te path set <te-path> --no-via-host

``openstack command list --group openstack.srv6.v2`` lists all of them.
