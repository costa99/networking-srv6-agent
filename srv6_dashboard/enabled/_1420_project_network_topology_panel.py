# Copyright 2026 networking-srv6-agent contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Project > Network > Network Topology, with SRv6 domain links (FEATURE
# D24). The file name is Horizon's own: a file in local/enabled/ with the
# same name as one in openstack_dashboard/enabled/ replaces it, so this
# registers our subclass in place of Horizon's panel, at the same place
# in the menu. Deleting this file brings Horizon's panel back.
PANEL = 'network_topology'
PANEL_DASHBOARD = 'project'
PANEL_GROUP = 'network'

ADD_PANEL = ('srv6_dashboard.dashboards.project.network_topology.panel.'
             'NetworkTopology')
