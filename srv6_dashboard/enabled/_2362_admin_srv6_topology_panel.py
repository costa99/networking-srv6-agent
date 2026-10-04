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

# The admin's topology picture: Admin > Network > SRv6 Topology (FEATURE
# D8, D22). Its script is inline in the template, so the panel needs no
# static files and no ADD_INSTALLED_APPS (Deviation V6).

PANEL = 'srv6_topology'
PANEL_DASHBOARD = 'admin'
PANEL_GROUP = 'network'

ADD_PANEL = ('srv6_dashboard.dashboards.admin.srv6_topology.panel.'
             'Srv6Topology')
