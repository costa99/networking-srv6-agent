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

"""The admin panel's tabs.

AdminSrv6Tabs is the panel's index page: one tab per resource the admin
manages. DomainDetailTabs is a domain's page, as in the project panel plus
what only an admin sees.
"""

from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import tabs
from horizon.utils import memoized
from openstack_dashboard.api import keystone

from networking_srv6_agent.common import constants
from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common
from srv6_dashboard.dashboards.admin.srv6.locators import (
    tables as locator_tables)
from srv6_dashboard.dashboards.admin.srv6 import tables as admin_tables
from srv6_dashboard.dashboards.admin.srv6.te_paths import tables as te_tables
from srv6_dashboard.dashboards.project.srv6 import tabs as project_tabs
from srv6_dashboard.dashboards.project.srv6 import views as project_views


def project_names(request):
    """{project id: name}, for the Project column. One keystone request."""
    try:
        projects, __ = keystone.tenant_list(request)
    except Exception:
        exceptions.handle(request, _('Unable to retrieve projects.'))
        return {}
    return {p.id: p.name for p in projects}


# --- the index page -------------------------------------------------------

class DomainsTab(tabs.TableTab):
    name = _("Domains")
    slug = 'domains'
    table_classes = (admin_tables.DomainsTable,)
    template_name = 'horizon/common/_detail_table.html'

    def get_srv6_domains_data(self):
        domains = self.tab_group.get_domains()
        project_views.add_network_names(self.request, domains)
        names = project_names(self.request) if domains else {}
        for domain in domains:
            domain.project_name = names.get(domain.project_id,
                                            domain.project_id)
        return domains


class TePathsTab(tabs.TableTab):
    name = _("TE Paths")
    slug = 'te_paths'
    table_classes = (te_tables.TePathsTable,)
    template_name = 'horizon/common/_detail_table.html'
    # Fetched when opened, not with the Domains tab.
    preload = False

    def get_te_paths_data(self):
        try:
            paths = srv6_api.te_path_list(self.request)
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve TE paths.'))
            return []
        names = {d.id: d.name_or_id for d in self.tab_group.get_domains()}
        for path in paths:
            path.domain_name = names.get(path.domain_id, path.domain_id)
        return paths


class LocatorsTab(tabs.TableTab):
    name = _("Locators")
    slug = 'locators'
    table_classes = (locator_tables.LocatorsTable,)
    template_name = 'horizon/common/_detail_table.html'
    preload = False

    def get_srv6_locators_data(self):
        try:
            return sorted(srv6_api.locator_list(self.request),
                          key=lambda loc: loc.host)
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve SRv6 locators.'))
            return []


class AdminSrv6Tabs(tabs.TabGroup):
    slug = 'srv6'
    tabs = (DomainsTab, TePathsTab, LocatorsTab)
    sticky = True

    @memoized.memoized_method
    def get_domains(self):
        """Every project's domains, fetched once per page.

        The Domains tab is always loaded (preload), so a page opened on
        another tab that also needs domain names would otherwise list the
        domains twice.
        """
        try:
            # No project filter: every project's domains (D14).
            return srv6_api.domain_list(self.request)
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve SRv6 domains.'))
            return []


# --- a domain's page ------------------------------------------------------

class OverviewTab(project_tabs.OverviewTab):
    template_name = 'admin/srv6/_detail_overview.html'
    network_url = srv6_common.ADMIN_NETWORK_URL

    def get_context_data(self, request):
        context = super().get_context_data(request)
        domain = context['domain']
        context['sid_function'] = srv6_common.sid_function(
            domain.sid_function)
        # The name every node gives the domain's VRF device: what an admin
        # greps for in `ip vrf` on a node (constants.VRF_PREFIX).
        if domain.sid_function is not None:
            context['vrf_name'] = '%s%d' % (constants.VRF_PREFIX,
                                            domain.sid_function)
        try:
            context['project_name'] = keystone.tenant_get(
                request, domain.project_id).name
        except Exception:
            context['project_name'] = domain.project_id
        return context


class NetworkAssociationsTab(project_tabs.NetworkAssociationsTab):
    table_classes = (admin_tables.NetworkAssociationsTable,)


class DomainTePathsTab(tabs.TableTab):
    """The domain's own TE paths: GET /v2.0/srv6_te_paths?domain_id=."""

    name = _("TE Paths")
    slug = 'te_paths'
    table_classes = (te_tables.TePathsTable,)
    template_name = 'horizon/common/_detail_table.html'
    preload = False

    def get_te_paths_data(self):
        domain = self.tab_group.kwargs['domain']
        try:
            paths = srv6_api.te_path_list(self.request, domain_id=domain.id)
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve TE paths.'))
            return []
        for path in paths:
            path.domain_name = domain.name_or_id
        return paths


class DomainDetailTabs(tabs.DetailTabsGroup):
    slug = 'domain_details'
    tabs = (OverviewTab, NetworkAssociationsTab, DomainTePathsTab)
    sticky = True
