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

from django.utils.translation import gettext_lazy as _

import horizon


class Srv6Topology(horizon.Panel):
    """Admin > Network > SRv6 Topology, at /admin/srv6_topology/.

    Read-only (D8): changes go through the SRv6 panel's tested forms.
    policy_rules makes it admin-only, page and data/ alike (see the SRv6
    panel, FEATURE D23).
    """

    name = _("SRv6 Topology")
    slug = 'srv6_topology'
    policy_rules = (('network', 'context_is_admin'),)
