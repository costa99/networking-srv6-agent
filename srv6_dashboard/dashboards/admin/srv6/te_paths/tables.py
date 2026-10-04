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

"""The TE paths table: on the admin index's TE Paths tab, and on a
domain's page, where it lists that domain's paths only.

A path has no name, so a row is identified by where it goes: the rows
carry domain_name (set by the tab), and the Destination column says
"host X" or links the port.
"""

from django.urls import reverse
from django.utils import html
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy
from horizon import tables

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.common import srv6 as srv6_common

PORT_URL = 'horizon:admin:networks:ports:detail'


class CreateTePath(tables.LinkAction):
    name = 'create'
    verbose_name = _("Create TE Path")
    url = 'horizon:admin:srv6:te_path_create'
    classes = ('ajax-modal',)
    icon = 'plus'

    def get_link_url(self, datum=None):
        # On a domain's page, the form opens with that domain selected.
        url = reverse(self.url)
        domain = self.table.kwargs.get('domain')
        if domain is not None:
            url += '?' + urlencode({'domain_id': domain.id})
        return url


class UpdateTePath(tables.LinkAction):
    name = 'update'
    verbose_name = _("Edit Waypoints")
    url = 'horizon:admin:srv6:te_path_update'
    classes = ('ajax-modal',)
    icon = 'pencil'


class DeleteTePath(tables.DeleteAction):
    help_text = _("The domain's traffic toward this destination goes "
                  "direct again, on every node.")

    @staticmethod
    def action_present(count):
        return ngettext_lazy("Delete TE Path", "Delete TE Paths", count)

    @staticmethod
    def action_past(count):
        return ngettext_lazy("Deleted TE Path", "Deleted TE Paths", count)

    def delete(self, request, obj_id):
        srv6_api.te_path_delete(request, obj_id)


def destination(path):
    if path.destination_host:
        return html.format_html('{} <code>{}</code>', _("host"),
                                path.destination_host)
    if path.destination_port_id:
        return html.format_html(
            '{} <a href="{}">{}</a>', _("port"),
            reverse(PORT_URL, args=[path.destination_port_id]),
            path.destination_port_id)
    return '-'


def domain_link(path):
    return html.format_html(
        '<a href="{}">{}</a>',
        reverse('horizon:admin:srv6:detail', args=[path.domain_id]),
        getattr(path, 'domain_name', None) or path.domain_id)


class TePathsTable(tables.DataTable):
    id = tables.Column('id', verbose_name=_("ID"),
                       link='horizon:admin:srv6:te_path_detail',
                       truncate=13)
    domain = tables.Column(domain_link, verbose_name=_("Domain"))
    destination = tables.Column(destination, verbose_name=_("Destination"))
    via_hosts = tables.Column('via_hosts', verbose_name=_("Via"),
                              filters=(srv6_common.via_hosts,))
    segments = tables.Column('segments', verbose_name=_("Segments"),
                             filters=(srv6_common.sid_list,))
    encoded_segments = tables.Column('encoded_segments',
                                     verbose_name=_("Pushed SIDs"),
                                     filters=(srv6_common.sid_list,))
    status = tables.Column('status', verbose_name=_("Status"),
                           filters=(srv6_common.te_status,))

    class Meta:
        name = 'te_paths'
        verbose_name = _("TE Paths")
        table_actions = (CreateTePath, DeleteTePath)
        row_actions = (UpdateTePath, DeleteTePath)
        hidden_title = False

    def get_object_display(self, path):
        # What the delete confirmation names: where the path goes.
        return '%s → %s' % (
            getattr(path, 'domain_name', None) or path.domain_id,
            path.destination_host or path.destination_port_id)
