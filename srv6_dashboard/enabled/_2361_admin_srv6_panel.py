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

# The admin's panel: Admin > Network > SRv6 (FEATURE §1, §2.1). Being in
# the Admin dashboard makes it admin-only (FEATURE §2.2). The number sorts
# it right after networking-bgpvpn's _2360 (D10).

PANEL = 'srv6'
PANEL_DASHBOARD = 'admin'
PANEL_GROUP = 'network'

ADD_PANEL = 'srv6_dashboard.dashboards.admin.srv6.panel.Srv6'
