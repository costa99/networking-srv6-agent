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

"""Create, rename, and associate-network forms (FEATURE §5.2).

Each handle() makes exactly one write call. On failure it reports through
exceptions.handle(), which puts the Neutron error text -- F3's "already
attached to SRv6 domain ...", F10's overlapping subnet, a 403 -- in front of
the user and redirects; the form never second-guesses the server.
"""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import forms
from horizon import messages

from networking_srv6_agent.common import constants
from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common

INDEX_URL = 'horizon:project:srv6:index'
DETAIL_URL = 'horizon:project:srv6:detail'


class CreateDomainForm(forms.SelfHandlingForm):
    name = forms.CharField(max_length=255, label=_("Name"))
    srv6_behavior = forms.ThemableChoiceField(
        label=_("SRv6 behavior"),
        # The server's own list (D12): a value it would refuse is never
        # offered.
        choices=[(b, b) for b in constants.VALID_BEHAVIORS],
        initial=constants.BEHAVIOR_END_DT46,
        help_text=srv6_common.BEHAVIOR_HELP)

    failure_url = INDEX_URL

    def _attrs(self, data):
        """The POST body. The admin form adds project_id."""
        return {'name': data['name'],
                'srv6_behavior': data['srv6_behavior']}

    def handle(self, request, data):
        try:
            domain = srv6_api.domain_create(request, **self._attrs(data))
        except Exception:
            exceptions.handle(
                request,
                _('Unable to create SRv6 domain "%s".') % data['name'],
                redirect=reverse(self.failure_url))
            return False
        messages.success(request,
                         _('Created SRv6 domain "%s".') % data['name'])
        return domain


class UpdateDomainForm(forms.SelfHandlingForm):
    domain_id = forms.CharField(widget=forms.HiddenInput())
    # The only attribute the API lets anyone change (allow_put).
    name = forms.CharField(max_length=255, label=_("Name"))

    failure_url = INDEX_URL

    def handle(self, request, data):
        try:
            domain = srv6_api.domain_update(request, data['domain_id'],
                                            name=data['name'])
        except Exception:
            exceptions.handle(request,
                              _('Unable to update SRv6 domain "%s".')
                              % data['name'],
                              redirect=reverse(self.failure_url))
            return False
        messages.success(request,
                         _('Updated SRv6 domain "%s".') % data['name'])
        return domain


class AssociateNetworkForm(forms.SelfHandlingForm):
    domain_id = forms.CharField(widget=forms.HiddenInput())
    network_id = forms.ThemableChoiceField(label=_("Network"))

    failure_url = DETAIL_URL

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        # The view puts the domain's project in initial: the user's own
        # here, the domain's in the admin panel.
        project_id = self.initial['project_id']
        self.fields['network_id'].choices = self._choices(request,
                                                          project_id)

    @staticmethod
    def _choices(request, project_id):
        """The networks this domain could take, by name (FEATURE D16).

        Offered: the domain project's own networks, minus external ones
        and minus any already in one of the project's domains. The plugin
        refuses those three cases anyway (403 Srv6NetworkNotOwned, 400
        Srv6ExternalNetworkNotAllowed, 409 Srv6NetworkInAnotherDomain),
        so offering them would only invite an error. The cases the form
        cannot see cheaply -- a router interface on the network, an
        overlapping subnet -- are left to the server's message.
        """
        try:
            networks = srv6_api.network_list(request, project_id=project_id)
            taken = {nid for d in srv6_api.domain_list(
                request, project_id=project_id) for nid in d.networks}
        except Exception:
            exceptions.handle(request, _('Unable to retrieve networks.'))
            return [('', _("No network available"))]
        free = sorted(
            ((n.id, n.name_or_id) for n in networks
             if not n.get('is_router_external') and n.id not in taken),
            key=lambda c: c[1])
        if not free:
            return [('', _("No network available"))]
        return [('', _("Select a network"))] + free

    def handle(self, request, data):
        name = dict(self.fields['network_id'].choices).get(
            data['network_id'], data['network_id'])
        try:
            assoc = srv6_api.network_association_create(
                request, data['domain_id'], network_id=data['network_id'])
        except Exception:
            exceptions.handle(
                request,
                _('Unable to associate network "%s".') % name,
                redirect=reverse(self.failure_url,
                                 args=[data['domain_id']]))
            return False
        messages.success(request,
                         _('Associated network "%s".') % name)
        return assoc
