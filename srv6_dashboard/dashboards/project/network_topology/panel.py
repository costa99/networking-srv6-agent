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

import os

from horizon import loaders
from openstack_dashboard.dashboards.project.network_topology import (
    panel as core_panel)


class NetworkTopology(core_panel.NetworkTopology):
    """Project > Network > Network Topology, with SRv6 domain links.

    Horizon's own panel (same slug, name and permissions), registered in
    its place by enabled/_1420_project_network_topology_panel.py, which
    shadows Horizon's file of the same name (FEATURE D24). Its urls.py
    swaps in two views; every other URL is Horizon's.
    """


# Horizon finds a panel's templates in the templates/ directory next to
# the module of the panel class it registered: here, this module, which
# has none. Point project/network_topology/ back at Horizon's directory,
# or the page's own includes (balloons, tabs, the SVG templates) are not
# found. This package keeps its one template under the SRv6 panel's
# directory for the same reason: this key can name one directory only.
loaders.panel_template_dirs['project/network_topology'] = os.path.join(
    os.path.dirname(core_panel.__file__), 'templates')
