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

--source-host limits a path to the traffic entering the domain at one node;
such a path outranks every path without one (FEATURE-TE-SOURCE-HOST).
"""

from cliff import columns as cliff_columns
from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

# Columns of the output: (field name, header, shown in list/long list).
_attr_map = (
    ('id', 'ID', column_util.LIST_BOTH),
    ('domain_id', 'Domain ID', column_util.LIST_BOTH),
    ('destination_host', 'Destination Host', column_util.LIST_BOTH),
    ('destination_port_id', 'Destination Port', column_util.LIST_BOTH),
    ('source_host', 'Source Host', column_util.LIST_BOTH),
    ('via_hosts', 'Via Hosts', column_util.LIST_BOTH),
    ('status', 'Status', column_util.LIST_BOTH),
    ('segments', 'Segments', column_util.LIST_LONG_ONLY),
    ('encoded_segments', 'Encoded Segments', column_util.LIST_LONG_ONLY),
    ('project_id', 'Project', column_util.LIST_LONG_ONLY),
)
# Field name -> column header.
_column_map = {attr: header for attr, header, _unused in _attr_map}


class SourceHostColumn(cliff_columns.FormattableColumn):
    """A path's source: None means every ingress, and says so.

    An empty cell would read as "unknown"; machine-readable output keeps
    the API's None.
    """

    def human_readable(self):
        return self._value or _('any')

    def machine_readable(self):
        return self._value


# Show these lists as plain comma-separated text, keeping their order,
# and the source as "any" when the path has none.
_formatters = {
    'source_host': SourceHostColumn,
    'via_hosts': common.PathColumn,
    'segments': common.PathColumn,
    'encoded_segments': common.PathColumn,
}
# A path has no name, so do not show that column.
_hidden = ('name',)


# Build the (headers, values) pair printed by show/create.
def _show(obj):
    return common.show_columns(obj, _column_map, _formatters, _hidden)


# --via-host can be repeated; each value is added to a list.
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
        # Give exactly one: a destination host or a destination port.
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
        parser.add_argument(
            '--source-host',
            metavar='<host>',
            help=_("Steer only the traffic that enters the domain at this "
                   "host (the ingress node). Such a path outranks every path "
                   "without a source. Default: every ingress"))
        _add_via_host_argument(
            parser, _("Waypoint host, in traversal order (repeat option "
                      "for more hosts). Without it the path is direct"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        # Turn the domain name into its ID.
        attrs = {
            'domain_id': common.find_domain(client, parsed_args.domain).id,
        }
        # Use the host as given, or turn the port name into its ID.
        if parsed_args.destination_host is not None:
            attrs['destination_host'] = parsed_args.destination_host
        else:
            attrs['destination_port_id'] = client.find_port(
                parsed_args.destination_port, ignore_missing=False).id
        # Only send a source if the user gave one: None is "any ingress".
        if parsed_args.source_host is not None:
            attrs['source_host'] = parsed_args.source_host
        # Only send waypoints if the user gave some.
        if parsed_args.via_hosts is not None:
            attrs['via_hosts'] = parsed_args.via_hosts
        # Create the path and print it.
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
        # A source, or none at all: one or the other.
        source = parser.add_mutually_exclusive_group()
        source.add_argument(
            '--source-host',
            metavar='<host>',
            help=_("List only the paths limited to this ingress host"))
        source.add_argument(
            '--any-source',
            action='store_true',
            help=_("List only the paths without a source host (they apply "
                   "at every ingress)"))
        parser.add_argument(
            '--long',
            action='store_true',
            help=_("List additional fields in output"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        # Add a filter for each option the user gave.
        filters = {}
        if parsed_args.domain is not None:
            filters['domain_id'] = common.find_domain(
                client, parsed_args.domain).id
        if parsed_args.destination_host is not None:
            filters['destination_host'] = parsed_args.destination_host
        if parsed_args.destination_port is not None:
            filters['destination_port_id'] = client.find_port(
                parsed_args.destination_port, ignore_missing=False).id
        if parsed_args.source_host is not None:
            filters['source_host'] = parsed_args.source_host
        elif parsed_args.any_source:
            # The server stores "any" as '' and matches it as a filter
            # value (it needs neutron's empty-string-filtering, which ML2
            # loads).
            filters['source_host'] = ''
        # Get the matching paths and print them as a table.
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
        # Get the path by ID and print it.
        return _show(client._get(resources.Srv6TePath, parsed_args.te_path))


class SetTePath(command.Command):
    _description = _("Re-steer an SRv6 traffic-engineering path")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'te_path',
            metavar='<te-path>',
            help=_("TE path to modify (ID)"))
        # Only the waypoints can change. Give either --via-host or
        # --no-via-host.
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
        # An empty list means: no waypoints, go direct.
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

        # Delete one path; fail if it does not exist.
        def delete(value):
            client._delete(resources.Srv6TePath, value, ignore_missing=False)

        # Try every path, then report all failures at the end.
        common.delete_all(parsed_args.te_paths, delete, _("TE path"))
