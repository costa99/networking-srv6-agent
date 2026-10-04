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

"""Project > Network > SRv6 Domains: the views behind each URL.

Which API calls each view makes, in order, is listed in FEATURE §5.2. The
admin panel subclasses every view here; the hooks it overrides are the
methods named _list_domains / _project_id and the class attributes ending
in _url.
"""

from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import forms
from horizon import tables
from horizon import tabs
from horizon.utils import memoized

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.project.srv6 import forms as srv6_forms
from srv6_dashboard.dashboards.project.srv6 import tables as srv6_tables
from srv6_dashboard.dashboards.project.srv6 import tabs as srv6_tabs


class IndexView(tables.DataTableView):
    table_class = srv6_tables.DomainsTable
    template_name = 'horizon/common/_data_table_view.html'
    page_title = _("SRv6 Domains")

    def _list_domains(self):
        # The project panel shows the current project's domains, even to
        # an admin, as every Project panel does (FEATURE D14). Without the
        # filter an admin would see every project's domains here.
        return srv6_api.domain_list(
            self.request, project_id=self.request.user.project_id)

    def _list_networks(self):
        return srv6_api.network_list(
            self.request, project_id=self.request.user.project_id)

    def get_data(self):
        try:
            domains = self._list_domains()
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve SRv6 domains.'))
            return []
        if any(d.networks for d in domains):
            try:
                names = srv6_api.network_names(self._list_networks())
            except Exception:
                # The table still lists the networks, by ID.
                names = {}
                exceptions.handle(self.request,
                                  _('Unable to retrieve network names.'))
            for domain in domains:
                domain.network_names = names
        return domains


class CreateView(forms.ModalFormView):
    form_class = srv6_forms.CreateDomainForm
    form_id = 'create_srv6_domain_form'
    modal_header = _("Create SRv6 Domain")
    submit_label = _("Create Domain")
    submit_url = reverse_lazy('horizon:project:srv6:create')
    success_url = reverse_lazy('horizon:project:srv6:index')
    template_name = 'project/srv6/create.html'
    page_title = _("Create SRv6 Domain")


class DomainMixin:
    """Fetch the domain in the URL once per request."""

    index_url = 'horizon:project:srv6:index'

    @memoized.memoized_method
    def get_domain(self):
        try:
            return srv6_api.domain_get(self.request,
                                       self.kwargs['domain_id'])
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve SRv6 domain details.'),
                              redirect=reverse(self.index_url))


class UpdateView(DomainMixin, forms.ModalFormView):
    form_class = srv6_forms.UpdateDomainForm
    form_id = 'update_srv6_domain_form'
    modal_header = _("Edit SRv6 Domain")
    submit_label = _("Save Changes")
    submit_url = 'horizon:project:srv6:update'
    success_url = reverse_lazy('horizon:project:srv6:index')
    template_name = 'project/srv6/update.html'
    page_title = _("Edit SRv6 Domain")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submit_url'] = reverse(self.submit_url,
                                        args=[self.kwargs['domain_id']])
        return context

    def get_initial(self):
        domain = self.get_domain()
        return {'domain_id': domain.id, 'name': domain.name}


class AssociateNetworkView(DomainMixin, forms.ModalFormView):
    form_class = srv6_forms.AssociateNetworkForm
    form_id = 'associate_srv6_network_form'
    modal_header = _("Associate Network")
    submit_label = _("Associate")
    submit_url = 'horizon:project:srv6:associate'
    detail_url = 'horizon:project:srv6:detail'
    template_name = 'project/srv6/associate.html'
    page_title = _("Associate Network")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submit_url'] = reverse(self.submit_url,
                                        args=[self.kwargs['domain_id']])
        context['domain'] = self.get_domain()
        return context

    def get_initial(self):
        domain = self.get_domain()
        # project_id: whose networks the form offers -- the domain's,
        # which for a tenant is their own project.
        return {'domain_id': domain.id, 'project_id': domain.project_id}

    def get_success_url(self):
        # Back to the domain, on the tab that shows the new association.
        return '%s?tab=%s__%s' % (
            reverse(self.detail_url, args=[self.kwargs['domain_id']]),
            srv6_tabs.DomainDetailTabs.slug,
            srv6_tabs.NetworkAssociationsTab.slug)


class DetailView(DomainMixin, tabs.TabbedTableView):
    tab_group_class = srv6_tabs.DomainDetailTabs
    table_class = srv6_tables.DomainsTable
    template_name = 'horizon/common/_detail.html'
    page_title = '{{ domain.name_or_id }}'

    @memoized.memoized_method
    def get_network_names(self):
        domain = self.get_domain()
        if not domain.networks:
            return {}
        try:
            return srv6_api.network_names(srv6_api.network_list(
                self.request, project_id=domain.project_id))
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve network names.'))
            return {}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        domain = self.get_domain()
        context['domain'] = domain
        context['url'] = reverse(self.index_url)
        # The same row actions as on the index, in the page header.
        context['actions'] = self.table_class(
            self.request).render_row_actions(domain)
        return context

    def get_tabs(self, request, *args, **kwargs):
        return self.tab_group_class(
            request, domain=self.get_domain(),
            network_names=self.get_network_names(), **kwargs)
