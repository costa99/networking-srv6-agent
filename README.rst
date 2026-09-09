=====================
networking-srv6-agent
=====================

An **additive** Neutron service plugin providing SRv6 connectivity between compute
nodes, plus operator traffic engineering. No BGP, no VPN federation.

The dataplane is the **Linux kernel** (VRFs and ``seg6`` routes), driven by an L2
agent extension, layered beside OVS rather than replacing it -- hence ``-agent``.
This is a separate line of work from ``costa99/networking-srv6``, which explores
an OVN-native dataplane instead and shares no code with this repo.

Status: **planning**. No code has been ported yet -- see ``MIGRATION-PLAN.md``.

Why this exists
---------------

The SRv6 dataplane was first built as a service driver inside ``networking-bgpvpn``.
That plugin loads exactly one driver, so enabling SRv6 *replaced* the BGP/MPLS VPN
service rather than adding to it: an operator could not run bagpipe for federation
and SRv6 for intra-DC traffic at the same time.

This repo extracts the SRv6 half into a plugin of its own so the two coexist, and
so traffic engineering can be a first-class resource instead of an admin-only child
of a tenant-owned VPN object.

What it provides
----------------

``srv6_domain``
    Project-scoped grouping and isolation: the networks attached to one domain share
    a VRF and reach each other over SRv6, across compute nodes, with no tunnel.

``srv6_te_path``
    Admin-only, top level. An operator-selected path -- an ordered list of transit
    nodes whose ``End`` SIDs are prepended to the segment list.

``srv6_locator``
    Admin-only, read-only. Which nodes have registered an SRv6 locator, and when.

What it does not provide
------------------------

Route targets, route distinguishers, and federation with VPNs outside the cloud.
Those are BGP capabilities: use ``networking-bgpvpn`` for them. The two are designed
to run side by side.
