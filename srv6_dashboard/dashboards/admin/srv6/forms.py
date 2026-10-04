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

"""Admin forms: the project ones, plus a project choice on create."""

from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import forms
from openstack_dashboard.api import keystone

from srv6_dashboard.dashboards.project.srv6 import forms as project_forms


class CreateDomainForm(project_forms.CreateDomainForm):
    project_id = forms.ThemableChoiceField(label=_("Project"))

    field_order = ['name', 'project_id', 'srv6_behavior']
    failure_url = 'horizon:admin:srv6:index'

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        try:
            projects, __ = keystone.tenant_list(request)
        except Exception:
            projects = []
            exceptions.handle(request, _('Unable to retrieve projects.'))
        self.fields['project_id'].choices = (
            [('', _("Select a project"))] +
            sorted(((p.id, p.name) for p in projects), key=lambda c: c[1]))

    def _attrs(self, data):
        # Creating for another project is what needs admin: Neutron allows
        # a project_id other than the token's only to an admin.
        return dict(super()._attrs(data), project_id=data['project_id'])


class UpdateDomainForm(project_forms.UpdateDomainForm):
    failure_url = 'horizon:admin:srv6:index'


class AssociateNetworkForm(project_forms.AssociateNetworkForm):
    failure_url = 'horizon:admin:srv6:detail'
