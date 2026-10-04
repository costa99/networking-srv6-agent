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

"""A domain's detail page: Overview and Network Associations tabs.

The DetailView fetches the domain and the network names once and passes
them in as tab-group kwargs (domain=, network_names=); the tabs only read
them, so the page makes no request twice.
"""

from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import tabs

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common
from srv6_dashboard.dashboards.project.srv6.network_associations import (
    tables as assoc_tables)


class OverviewTab(tabs.Tab):
    name = _("Overview")
    slug = 'overview'
    template_name = 'project/srv6/_detail_overview.html'
    network_url = srv6_common.PROJECT_NETWORK_URL

    def get_context_data(self, request):
        domain = self.tab_group.kwargs['domain']
        return {
            'domain': domain,
            'networks': srv6_common.network_links(
                domain.networks, self.tab_group.kwargs['network_names'],
                self.network_url),
        }


class NetworkAssociationsTab(tabs.TableTab):
    name = _("Network Associations")
    slug = 'network_associations'
    table_classes = (assoc_tables.NetworkAssociationsTable,)
    template_name = 'horizon/common/_detail_table.html'
    preload = False

    def get_network_associations_data(self):
        domain = self.tab_group.kwargs['domain']
        names = self.tab_group.kwargs['network_names']
        try:
            assocs = srv6_api.network_association_list(self.request,
                                                       domain.id)
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve network associations.'))
            return []
        for assoc in assocs:
            assoc.network_name = names.get(assoc.network_id,
                                           assoc.network_id)
        return assocs


class DomainDetailTabs(tabs.DetailTabsGroup):
    slug = 'domain_details'
    tabs = (OverviewTab, NetworkAssociationsTab)
    sticky = True
