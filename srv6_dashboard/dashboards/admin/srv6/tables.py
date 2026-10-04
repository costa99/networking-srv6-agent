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

"""The admin's domain tables: the project ones, pointed at admin URLs.

What the admin sees in addition: the owning project and the SID function,
which only an admin receives from the API (get_srv6_domain:sid_function).
"""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from horizon import tables

from srv6_dashboard.common import srv6 as srv6_common
from srv6_dashboard.dashboards.project.srv6.network_associations import (
    tables as project_assoc_tables)
from srv6_dashboard.dashboards.project.srv6 import tables as project_tables

URL_PREFIX = 'horizon:admin:srv6:'


class CreateDomain(project_tables.CreateDomain):
    url = URL_PREFIX + 'create'


class UpdateDomain(project_tables.UpdateDomain):
    url = URL_PREFIX + 'update'


class AssociateNetwork(project_tables.AssociateNetwork):
    url = URL_PREFIX + 'associate'


class NetworksColumn(project_tables.NetworksColumn):
    network_url = srv6_common.ADMIN_NETWORK_URL


class DomainsTable(tables.DataTable):
    name = tables.WrappingColumn('name_or_id', verbose_name=_("Name"),
                                 link=URL_PREFIX + 'detail')
    project = tables.Column('project_name', verbose_name=_("Project"))
    srv6_behavior = tables.Column('srv6_behavior',
                                  verbose_name=_("Behavior"))
    sid_function = tables.Column('sid_function',
                                 verbose_name=_("SID Function"),
                                 filters=(srv6_common.sid_function,))
    networks = NetworksColumn('networks', verbose_name=_("Networks"))

    class Meta:
        name = 'srv6_domains'
        verbose_name = _("SRv6 Domains")
        table_actions = (CreateDomain, project_tables.DeleteDomain)
        row_actions = (AssociateNetwork, UpdateDomain,
                       project_tables.DeleteDomain)


# --- the Network Associations tab of an admin's domain page ---------------

class AssociateNetworkFromTab(project_assoc_tables.AssociateNetwork):
    url = URL_PREFIX + 'associate'

    def get_link_url(self, datum=None):
        return reverse(self.url, args=(self.table.kwargs['domain'].id,))


class AssocNetworkColumn(project_assoc_tables.NetworkColumn):
    network_url = srv6_common.ADMIN_NETWORK_URL


class NetworkAssociationsTable(project_assoc_tables.NetworkAssociationsTable):
    network = AssocNetworkColumn('network_id', verbose_name=_("Network"))

    class Meta(project_assoc_tables.NetworkAssociationsTable.Meta):
        table_actions = (AssociateNetworkFromTab,
                         project_assoc_tables.DisassociateNetwork)
