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

"""The Network Associations tab's table.

It lives inside a domain's detail page: the tab group hands it the domain
as self.table.kwargs['domain'] (horizon/tabs/base.py TableTab), which is
where the domain_id of every call below comes from.
"""

from django.urls import reverse
from django.utils import html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy
from horizon import tables

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common


class AssociateNetwork(tables.LinkAction):
    name = 'associate'
    verbose_name = _("Associate Network")
    url = 'horizon:project:srv6:associate'
    classes = ('ajax-modal',)
    icon = 'link'

    def get_link_url(self, datum=None):
        return reverse(self.url, args=(self.table.kwargs['domain'].id,))


class DisassociateNetwork(tables.DeleteAction):
    help_text = _("The network leaves the domain: its ports lose the "
                  "domain's routes on every node. Removing the last network "
                  "also removes the domain's VRF everywhere.")

    @staticmethod
    def action_present(count):
        return ngettext_lazy("Disassociate Network",
                             "Disassociate Networks", count)

    @staticmethod
    def action_past(count):
        return ngettext_lazy("Disassociated Network",
                             "Disassociated Networks", count)

    def delete(self, request, obj_id):
        srv6_api.network_association_delete(
            request, self.table.kwargs['domain'].id, obj_id)


class NetworkColumn(tables.Column):
    network_url = srv6_common.PROJECT_NETWORK_URL

    def get_raw_data(self, assoc):
        url = reverse(self.network_url, args=[assoc.network_id])
        return html.format_html('<a href="{}">{}</a>', url,
                                assoc.network_name)


class NetworkAssociationsTable(tables.DataTable):
    network = NetworkColumn('network_id', verbose_name=_("Network"))
    network_id = tables.Column('network_id', verbose_name=_("Network ID"))
    id = tables.Column('id', verbose_name=_("Association ID"))

    class Meta:
        name = 'network_associations'
        verbose_name = _("Network Associations")
        table_actions = (AssociateNetwork, DisassociateNetwork)
        row_actions = (DisassociateNetwork,)
        hidden_title = False

    def get_object_display(self, assoc):
        # The confirmation dialog names the network, not the association
        # ID nobody remembers.
        return assoc.network_name
