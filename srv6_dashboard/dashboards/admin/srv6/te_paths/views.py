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

"""TE path pages: create, edit waypoints, detail (FEATURE §5.2)."""

from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import forms
from horizon import tabs
from horizon.utils import memoized

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.admin.srv6.te_paths import forms as te_forms
from srv6_dashboard.dashboards.admin.srv6.te_paths import tables as te_tables
from srv6_dashboard.dashboards.admin.srv6.te_paths import tabs as te_tabs

INDEX_URL = 'horizon:admin:srv6:index'


def _te_tab_url():
    return reverse(INDEX_URL) + te_forms.TE_TAB


class CreateView(forms.ModalFormView):
    form_class = te_forms.CreateTePathForm
    form_id = 'create_te_path_form'
    modal_header = _("Create TE Path")
    submit_label = _("Create TE Path")
    submit_url = reverse_lazy('horizon:admin:srv6:te_path_create')
    template_name = 'admin/srv6/te_path_create.html'
    page_title = _("Create TE Path")

    def get_initial(self):
        # ?domain_id= when opened from a domain's TE Paths tab.
        return {'domain_id': self.request.GET.get('domain_id', '')}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Keep ?domain_id= on the POST, so a failed validation re-renders
        # the form with the same domain.
        if self.request.GET.get('domain_id'):
            context['submit_url'] = '%s?domain_id=%s' % (
                self.submit_url, self.request.GET['domain_id'])
        return context

    def get_success_url(self):
        return _te_tab_url()


class PathMixin:

    @memoized.memoized_method
    def get_path(self):
        try:
            return srv6_api.te_path_get(self.request, self.kwargs['path_id'])
        except Exception:
            exceptions.handle(self.request,
                              _('Unable to retrieve the TE path.'),
                              redirect=_te_tab_url())


class UpdateView(PathMixin, forms.ModalFormView):
    form_class = te_forms.UpdateTePathForm
    form_id = 'update_te_path_form'
    modal_header = _("Edit Waypoints")
    submit_label = _("Save Changes")
    submit_url = 'horizon:admin:srv6:te_path_update'
    template_name = 'admin/srv6/te_path_update.html'
    page_title = _("Edit Waypoints")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submit_url'] = reverse(self.submit_url,
                                        args=[self.kwargs['path_id']])
        context['path'] = self.get_path()
        return context

    def get_initial(self):
        path = self.get_path()
        return {'path_id': path.id, 'via_hosts': path.via_hosts}

    def get_success_url(self):
        return _te_tab_url()


class DetailView(PathMixin, tabs.TabView):
    tab_group_class = te_tabs.TePathDetailTabs
    template_name = 'horizon/common/_detail.html'
    page_title = _("TE Path {{ path.id }}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        path = self.get_path()
        context['path'] = path
        context['url'] = _te_tab_url()
        context['actions'] = te_tables.TePathsTable(
            self.request).render_row_actions(path)
        return context

    def get_tabs(self, request, *args, **kwargs):
        return self.tab_group_class(request, path=self.get_path(), **kwargs)
