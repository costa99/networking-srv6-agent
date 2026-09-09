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
