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

"""openstack srv6 domain create/list/show/set/delete"""

from osc_lib.cli import format_columns
from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

_attr_map = (
    ('id', 'ID', column_util.LIST_BOTH),
    ('name', 'Name', column_util.LIST_BOTH),
    ('project_id', 'Project', column_util.LIST_LONG_ONLY),
    ('srv6_behavior', 'SRv6 Behavior', column_util.LIST_LONG_ONLY),
    ('sid_function', 'SID Function', column_util.LIST_LONG_ONLY),
    ('networks', 'Networks', column_util.LIST_LONG_ONLY),
)
_column_map = {attr: header for attr, header, _unused in _attr_map}
_formatters = {'networks': format_columns.ListColumn}


def _show(obj):
    # sid_function is admin-only: a tenant's answer has no such key, and a
    # "None" row would read as "not allocated".
    hidden = ('sid_function',) if obj.sid_function is None else ()
    return common.show_columns(obj, _column_map, _formatters, hidden)


class CreateDomain(command.ShowOne):
    _description = _("Create an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'name',
            metavar='<name>',
            help=_("Name of the SRv6 domain"))
        parser.add_argument(
            '--srv6-behavior',
            metavar='<behavior>',
            help=_("Decap behavior of the domain's SIDs; End.DT46 is the "
                   "default and the only value accepted"))
        common.add_project_options(
            parser, _("Owner's project (name or ID), admin only"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        attrs = {'name': parsed_args.name}
        if parsed_args.srv6_behavior is not None:
            attrs['srv6_behavior'] = parsed_args.srv6_behavior
        project_id = common.project_id(self.app.client_manager, parsed_args)
        if project_id is not None:
            attrs['project_id'] = project_id
        return _show(client._create(resources.Srv6Domain, **attrs))


class ListDomain(command.Lister):
    _description = _("List SRv6 domains")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--name',
            metavar='<name>',
            help=_("List only the domains with this name"))
        common.add_project_options(
            parser, _("List only the domains of this project (name or ID)"))
        parser.add_argument(
            '--long',
            action='store_true',
            help=_("List additional fields in output"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        filters = {}
        if parsed_args.name is not None:
            filters['name'] = parsed_args.name
        project_id = common.project_id(self.app.client_manager, parsed_args)
        if project_id is not None:
            filters['project_id'] = project_id
        return common.list_columns(
            client._list(resources.Srv6Domain, **filters),
            _attr_map, parsed_args.long, _formatters)


class ShowDomain(command.ShowOne):
    _description = _("Show an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'domain',
            metavar='<domain>',
            help=_("SRv6 domain to show (name or ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        return _show(common.find_domain(client, parsed_args.domain))


class SetDomain(command.Command):
    _description = _("Set SRv6 domain properties")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'domain',
            metavar='<domain>',
            help=_("SRv6 domain to modify (name or ID)"))
        # The only updatable attribute: the behavior is create-only.
        parser.add_argument(
            '--name',
            metavar='<name>',
            required=True,
            help=_("New name of the SRv6 domain"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        domain = common.find_domain(client, parsed_args.domain)
        client._update(resources.Srv6Domain, domain.id,
                       name=parsed_args.name)


class DeleteDomain(command.Command):
    _description = _("Delete SRv6 domain(s), with their associations and "
                     "TE paths")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            'domains',
            metavar='<domain>',
            nargs='+',
            help=_("SRv6 domain(s) to delete (name or ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network

        def delete(value):
            domain = common.find_domain(client, value)
            client._delete(resources.Srv6Domain, domain.id,
                           ignore_missing=False)

        common.delete_all(parsed_args.domains, delete, _("SRv6 domain"))
