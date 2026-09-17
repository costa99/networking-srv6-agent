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

"""openstack srv6 locator list/show

Read-only and admin-only: the host names listed here are exactly what a TE
path's --destination-host and --via-host accept.
"""

from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

_attr_map = (
    ('host', 'Host', column_util.LIST_BOTH),
    ('locator', 'Locator', column_util.LIST_BOTH),
    ('updated_at', 'Updated At', column_util.LIST_BOTH),
)
_column_map = {attr: header for attr, header, _unused in _attr_map}
# The id is the host name, shown once.
_hidden = ('id', 'name')


class ListLocator(command.Lister):
    _description = _("List the SRv6 locators registered by the agents")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--host',
            metavar='<host>',
            help=_("List only the locator of this host"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        filters = {}
        if parsed_args.host is not None:
            filters['host'] = parsed_args.host
        return common.list_columns(
            client._list(resources.Srv6Locator, **filters), _attr_map, False)


class ShowLocator(command.ShowOne):
    _description = _("Show the SRv6 locator of a host")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'host',
            metavar='<host>',
            help=_("Host name, as the agent registered it"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        obj = client._get(resources.Srv6Locator, parsed_args.host)
        return common.show_columns(obj, _column_map, hidden=_hidden)
