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

"""openstack srv6 te path create/list/show/set/delete

Admin-only. A path has no name, so it is addressed by ID. The via-host count
and the host names are checked by the server, not here.
"""

from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

_attr_map = (
    ('id', 'ID', column_util.LIST_BOTH),
    ('domain_id', 'Domain ID', column_util.LIST_BOTH),
    ('destination_host', 'Destination Host', column_util.LIST_BOTH),
    ('destination_port_id', 'Destination Port', column_util.LIST_BOTH),
    ('via_hosts', 'Via Hosts', column_util.LIST_BOTH),
    ('status', 'Status', column_util.LIST_BOTH),
    ('segments', 'Segments', column_util.LIST_LONG_ONLY),
    ('project_id', 'Project', column_util.LIST_LONG_ONLY),
)
_column_map = {attr: header for attr, header, _unused in _attr_map}
_formatters = {
    'via_hosts': common.PathColumn,
    'segments': common.PathColumn,
}
_hidden = ('name',)


def _show(obj):
    return common.show_columns(obj, _column_map, _formatters, _hidden)


def _add_via_host_argument(parser, help_text):
    parser.add_argument(
        '--via-host',
        metavar='<host>',
        dest='via_hosts',
        action='append',
        help=help_text)


class CreateTePath(command.ShowOne):
    _description = _("Create an SRv6 traffic-engineering path")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--domain',
            metavar='<domain>',
            required=True,
            help=_("SRv6 domain whose traffic is steered (name or ID)"))
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument(
            '--destination-host',
            metavar='<host>',
            help=_("Steer all of the domain's traffic toward this host"))
        selector.add_argument(
            '--destination-port',
            metavar='<port>',
            help=_("Steer only the traffic toward this port's fixed IPs "
                   "(name or ID)"))
        _add_via_host_argument(
            parser, _("Waypoint host, in traversal order (repeat option "
                      "for more hosts). Without it the path is direct"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        attrs = {
            'domain_id': common.find_domain(client, parsed_args.domain).id,
        }
        if parsed_args.destination_host is not None:
            attrs['destination_host'] = parsed_args.destination_host
        else:
            attrs['destination_port_id'] = client.find_port(
                parsed_args.destination_port, ignore_missing=False).id
        if parsed_args.via_hosts is not None:
            attrs['via_hosts'] = parsed_args.via_hosts
        return _show(client._create(resources.Srv6TePath, **attrs))


class ListTePath(command.Lister):
    _description = _("List SRv6 traffic-engineering paths")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--domain',
            metavar='<domain>',
            help=_("List only the paths of this SRv6 domain (name or ID)"))
        parser.add_argument(
            '--destination-host',
            metavar='<host>',
            help=_("List only the paths toward this host"))
        parser.add_argument(
            '--destination-port',
            metavar='<port>',
            help=_("List only the path toward this port (name or ID)"))
        parser.add_argument(
            '--long',
            action='store_true',
            help=_("List additional fields in output"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        filters = {}
        if parsed_args.domain is not None:
            filters['domain_id'] = common.find_domain(
                client, parsed_args.domain).id
        if parsed_args.destination_host is not None:
            filters['destination_host'] = parsed_args.destination_host
        if parsed_args.destination_port is not None:
            filters['destination_port_id'] = client.find_port(
                parsed_args.destination_port, ignore_missing=False).id
        return common.list_columns(
            client._list(resources.Srv6TePath, **filters),
            _attr_map, parsed_args.long, _formatters)


class ShowTePath(command.ShowOne):
    _description = _("Show an SRv6 traffic-engineering path")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'te_path',
            metavar='<te-path>',
            help=_("TE path to show (ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        return _show(client._get(resources.Srv6TePath, parsed_args.te_path))


class SetTePath(command.Command):
    _description = _("Re-steer an SRv6 traffic-engineering path")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'te_path',
            metavar='<te-path>',
            help=_("TE path to modify (ID)"))
        # The selectors are create-only; the waypoints are all there is.
        waypoints = parser.add_mutually_exclusive_group(required=True)
        _add_via_host_argument(
            waypoints, _("Waypoint host, in traversal order (repeat option "
                         "for more hosts). REPLACES the current list"))
        waypoints.add_argument(
            '--no-via-host',
            action='store_true',
            help=_("Remove all waypoints: the path becomes explicitly "
                   "direct"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        via_hosts = [] if parsed_args.no_via_host else parsed_args.via_hosts
        client._update(resources.Srv6TePath, parsed_args.te_path,
                       via_hosts=via_hosts)


class DeleteTePath(command.Command):
    _description = _("Delete SRv6 traffic-engineering path(s)")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'te_paths',
            metavar='<te-path>',
            nargs='+',
            help=_("TE path(s) to delete (ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network

        def delete(value):
            client._delete(resources.Srv6TePath, value, ignore_missing=False)

        common.delete_all(parsed_args.te_paths, delete, _("TE path"))
