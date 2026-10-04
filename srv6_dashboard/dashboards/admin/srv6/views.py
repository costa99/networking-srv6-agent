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

"""Admin > Network > SRv6: the views behind each URL (FEATURE §5.2).

The domain views are the project panel's, with admin URLs, forms, tables
and tabs. The index is different: a tabbed page with one tab per resource.
"""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from horizon import tabs

from srv6_dashboard.dashboards.admin.srv6 import forms as admin_forms
from srv6_dashboard.dashboards.admin.srv6 import tables as admin_tables
from srv6_dashboard.dashboards.admin.srv6 import tabs as admin_tabs
from srv6_dashboard.dashboards.project.srv6 import views as project_views


class IndexView(tabs.TabbedTableView):
    tab_group_class = admin_tabs.AdminSrv6Tabs
    template_name = 'admin/srv6/index.html'
    page_title = _("SRv6")


class CreateView(project_views.CreateView):
    form_class = admin_forms.CreateDomainForm
    submit_url = reverse_lazy('horizon:admin:srv6:create')
    success_url = reverse_lazy('horizon:admin:srv6:index')
    template_name = 'admin/srv6/create.html'


class UpdateView(project_views.UpdateView):
    form_class = admin_forms.UpdateDomainForm
    submit_url = 'horizon:admin:srv6:update'
    success_url = reverse_lazy('horizon:admin:srv6:index')
    index_url = 'horizon:admin:srv6:index'


class AssociateNetworkView(project_views.AssociateNetworkView):
    form_class = admin_forms.AssociateNetworkForm
    submit_url = 'horizon:admin:srv6:associate'
    detail_url = 'horizon:admin:srv6:detail'
    index_url = 'horizon:admin:srv6:index'


class DetailView(project_views.DetailView):
    tab_group_class = admin_tabs.DomainDetailTabs
    table_class = admin_tables.DomainsTable
    index_url = 'horizon:admin:srv6:index'
