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

"""The domains table and its actions (FEATURE §5.2, §6.2).

The admin panel subclasses these, changing only the `url` of each action
and adding columns (admin/srv6/tables.py).
"""

from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy
from horizon import tables

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common

URL_PREFIX = 'horizon:project:srv6:'


class CreateDomain(tables.LinkAction):
    name = 'create'
    verbose_name = _("Create Domain")
    url = URL_PREFIX + 'create'
    classes = ('ajax-modal',)
    icon = 'plus'


class UpdateDomain(tables.LinkAction):
    name = 'update'
    verbose_name = _("Edit Domain")
    url = URL_PREFIX + 'update'
    classes = ('ajax-modal',)
    icon = 'pencil'


class AssociateNetwork(tables.LinkAction):
    name = 'associate'
    verbose_name = _("Associate Network")
    url = URL_PREFIX + 'associate'
    classes = ('ajax-modal',)
    icon = 'link'


class DeleteDomain(tables.DeleteAction):
    help_text = _("The domain's network associations and TE paths are "
                  "deleted with it. The networks themselves are kept, and "
                  "every node removes the domain's VRF.")

    @staticmethod
    def action_present(count):
        return ngettext_lazy("Delete Domain", "Delete Domains", count)

    @staticmethod
    def action_past(count):
        return ngettext_lazy("Deleted Domain", "Deleted Domains", count)

    def delete(self, request, obj_id):
        # No try/except: BatchAction.handle() catches the error, reports
        # "Unable to delete" with Neutron's message, and goes on with the
        # other selected rows.
        srv6_api.domain_delete(request, obj_id)


class NetworksColumn(tables.Column):
    """The domain's networks, as links named after the network.

    The view puts the {id: name} map on each row (row.network_names), so
    the whole table costs one network request, not one per network (D15).
    """

    network_url = srv6_common.PROJECT_NETWORK_URL

    def get_raw_data(self, domain):
        return srv6_common.network_links(
            domain.networks, getattr(domain, 'network_names', {}),
            self.network_url)


class DomainsTable(tables.DataTable):
    name = tables.WrappingColumn('name_or_id', verbose_name=_("Name"),
                                 link=URL_PREFIX + 'detail')
    srv6_behavior = tables.Column('srv6_behavior',
                                  verbose_name=_("Behavior"))
    networks = NetworksColumn('networks', verbose_name=_("Networks"))

    class Meta:
        name = 'srv6_domains'
        verbose_name = _("SRv6 Domains")
        table_actions = (CreateDomain, DeleteDomain)
        row_actions = (AssociateNetwork, UpdateDomain, DeleteDomain)
