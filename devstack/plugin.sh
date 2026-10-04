#!/bin/bash
#
# networking-srv6-agent devstack plugin.
#
#   enable_plugin networking-srv6-agent <repo url> <branch>
#   SRV6_LOCATOR=fc00:0:1::/48            # required on every q-agt node
#   SRV6_UNDERLAY_INTERFACE=enp2s0f1
#
# Modelled on networking-bgpvpn's plugin, which already stacks on the
# testbed. Everything goes through devstack's own helpers:
#
#   - [srv6] lands in neutron.conf, never in a side file. networking-bgpvpn's
#     networking_bgpvpn.conf taught that: under uwsgi it is special-cased and
#     silently ignores any other option group (INSTALL 6.1).
#   - [agent] extensions is written by configure_l2_agent, an iniset. That
#     retires the trap the injection workflow had: appending a second [agent]
#     section splits the first and silently drops its keys, tunnel_types
#     included.
#   - The database needs nothing here. init_neutron runs
#     `neutron-db-manage upgrade head`, which finds this package's migrations
#     through its neutron.db.alembic_migrations entry point.

_XTRACE_NETWORKING_SRV6_AGENT=$(set +o | grep xtrace)
set -o xtrace

if [[ "$1" == "stack" && "$2" == "install" ]]; then
    echo_summary "Installing networking-srv6-agent"
    setup_develop $NETWORKING_SRV6_AGENT_DIR

elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
    echo_summary "Configuring networking-srv6-agent"

    # Every node, server or compute: see settings.
    iniset $NEUTRON_CONF srv6 vrf_table_base $SRV6_VRF_TABLE_BASE

    if is_service_enabled neutron-api || is_service_enabled q-svc; then
        neutron_service_plugin_class_add srv6
        iniset $NEUTRON_CONF srv6 function_id_ranges $SRV6_FUNCTION_ID_RANGES
        iniset $NEUTRON_CONF srv6 dataplane_encoding $SRV6_DATAPLANE_ENCODING
    fi

    if is_service_enabled neutron-agent || is_service_enabled q-agt; then
        if [[ -z "$SRV6_LOCATOR" ]]; then
            die $LINENO "SRV6_LOCATOR must be set on every node running " \
                "q-agt: the agent refuses to start without a locator"
        fi
        source $NEUTRON_DIR/devstack/lib/l2_agent
        plugin_agent_add_l2_agent_extension srv6
        configure_l2_agent
        iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent locator $SRV6_LOCATOR
        if [[ -n "$SRV6_UNDERLAY_INTERFACE" ]]; then
            iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent underlay_interface \
                $SRV6_UNDERLAY_INTERFACE
        fi
        iniset /$NEUTRON_CORE_PLUGIN_CONF srv6_agent resync_interval \
            $SRV6_RESYNC_INTERVAL
    fi

    # The Horizon panels, on the node serving the dashboard. post-config
    # runs before devstack's init_horizon, whose collectstatic and
    # compress must already see them. Nothing to install: setup_develop
    # above put srv6_dashboard in the same venv as Horizon. No
    # POLICY_FILES entry, unlike networking-bgpvpn: Neutron enforces the
    # policy and the admin panels sit behind the Admin dashboard's gate
    # (FEATURE-HORIZON-DASHBOARD D9).
    if is_service_enabled horizon; then
        cp $SRV6_DASHBOARD_ENABLE \
            $HORIZON_DIR/openstack_dashboard/local/enabled/
    fi

elif [[ "$1" == "clean" ]]; then
    # Undo the copy above; Horizon then starts without the SRv6 panels,
    # and with its own Network Topology panel again. That file has
    # Horizon's name, so remove it only if it is ours.
    rm -f $HORIZON_DIR/openstack_dashboard/local/enabled/_*_srv6_*panel.py
    _srv6_topology=$HORIZON_DIR/openstack_dashboard/local/enabled/_1420_project_network_topology_panel.py
    if grep -qs srv6_dashboard $_srv6_topology; then
        rm -f $_srv6_topology
    fi
fi

$_XTRACE_NETWORKING_SRV6_AGENT
