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

"""openstack srv6 network association create/list/show/delete

Shaped like the `openstack bgpvpn network association` commands this plugin
replaces: the domain comes first, as a positional argument.
"""

from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

_attr_map = (
    ('id', 'ID', column_util.LIST_BOTH),
    ('network_id', 'Network ID', column_util.LIST_BOTH),
    ('project_id', 'Project', column_util.LIST_LONG_ONLY),
)
_column_map = {attr: header for attr, header, _unused in _attr_map}
# An association has no name, and domain_id is part of its URL, not of the
# body; the domain is shown by its own command.
_hidden = ('name', 'domain_id')


def _add_domain_argument(parser):
    parser.add_argument(
        'domain',
        metavar='<domain>',
        help=_("SRv6 domain (name or ID)"))


class CreateNetworkAssociation(command.ShowOne):
    _description = _("Attach a network to an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_domain_argument(parser)
        parser.add_argument(
            'network',
            metavar='<network>',
            help=_("Network to attach (name or ID)"))
        common.add_project_options(
            parser, _("Owner's project (name or ID), admin only. The server "
                      "always uses the domain's project"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        domain = common.find_domain(client, parsed_args.domain)
        network = client.find_network(parsed_args.network,
                                      ignore_missing=False)
        attrs = {'domain_id': domain.id, 'network_id': network.id}
        project_id = common.project_id(self.app.client_manager, parsed_args)
        if project_id is not None:
            attrs['project_id'] = project_id
        obj = client._create(resources.Srv6NetworkAssociation, **attrs)
        return common.show_columns(obj, _column_map, hidden=_hidden)


class ListNetworkAssociation(command.Lister):
    _description = _("List the networks attached to an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_domain_argument(parser)
        parser.add_argument(
            '--network',
            metavar='<network>',
            help=_("List only the association of this network "
                   "(name or ID)"))
        parser.add_argument(
            '--long',
            action='store_true',
            help=_("List additional fields in output"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        domain = common.find_domain(client, parsed_args.domain)
        filters = {}
        if parsed_args.network is not None:
            filters['network_id'] = client.find_network(
                parsed_args.network, ignore_missing=False).id
        return common.list_columns(
            client._list(resources.Srv6NetworkAssociation,
                         domain_id=domain.id, **filters),
            _attr_map, parsed_args.long)


class ShowNetworkAssociation(command.ShowOne):
    _description = _("Show a network association of an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_domain_argument(parser)
        parser.add_argument(
            'association',
            metavar='<association>',
            help=_("Association ID, or the attached network (name or ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        domain = common.find_domain(client, parsed_args.domain)
        obj = common.find_association(client, domain.id,
                                      parsed_args.association)
        return common.show_columns(obj, _column_map, hidden=_hidden)


class DeleteNetworkAssociation(command.Command):
    _description = _("Detach network(s) from an SRv6 domain")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_domain_argument(parser)
        parser.add_argument(
            'associations',
            metavar='<association>',
            nargs='+',
            help=_("Association ID(s), or the attached network(s) "
                   "(name or ID)"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        domain = common.find_domain(client, parsed_args.domain)

        def delete(value):
            assoc = common.find_association(client, domain.id, value)
            client._delete(resources.Srv6NetworkAssociation, assoc.id,
                           ignore_missing=False, domain_id=domain.id)

        common.delete_all(parsed_args.associations, delete,
                          _("network association"))
