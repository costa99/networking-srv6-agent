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

"""A TE path's page: what the admin asked for, and what the server made
of it, SID by SID."""

from django.utils.translation import gettext_lazy as _
from horizon import tabs

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common
from srv6_dashboard.dashboards.admin.srv6.te_paths import tables as te_tables


def segment_roles(path):
    """[(SID, what it does)] for an ACTIVE path's segments.

    The server builds segments as [End SID of each via host..., the
    destination's End.DT46 SID] (services/plugin.py _resolve_te_path), so
    the list lines up with via_hosts plus the destination.
    """
    segments = path.segments or []
    via = path.via_hosts or []
    if len(segments) != len(via) + 1:
        return [(s, '') for s in segments]
    roles = [_("End on %s: forward to the next segment") % h for h in via]
    roles.append(_("End.DT46 on the destination: decapsulate into the "
                   "domain's VRF"))
    return list(zip(segments, roles))


class OverviewTab(tabs.Tab):
    name = _("Overview")
    slug = 'overview'
    template_name = 'admin/srv6/_te_path_overview.html'

    def get_context_data(self, request):
        path = self.tab_group.kwargs['path']
        try:
            path.domain_name = srv6_api.domain_get(
                request, path.domain_id).name_or_id
        except Exception:
            # The page still works by ID.
            path.domain_name = path.domain_id
        return {
            'path': path,
            'domain': te_tables.domain_link(path),
            'destination': te_tables.destination(path),
            'via': srv6_common.via_hosts(path.via_hosts),
            'segments': segment_roles(path),
            'encoded': srv6_common.sid_list(path.encoded_segments),
            'status': srv6_common.te_status(path.status),
        }


class TePathDetailTabs(tabs.DetailTabsGroup):
    slug = 'te_path_details'
    tabs = (OverviewTab,)
