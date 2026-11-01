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

"""openstack srv6 link list

Admin-only and read-only: the measured state of each directed underlay link,
as the fabric nodes posted it (PLAN-LINK-STATE-TE). Mbit/s is the load the
sender offered, Delivered what the receiver got, Peak the highest load in
the server's link_state_window, Reserved what bandwidth reservations hold on
the link, Capacity the topology file's.
"""

from osc_lib.command import command
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources

_attr_map = (
    ('from_node', 'From', column_util.LIST_BOTH),
    ('to_node', 'To', column_util.LIST_BOTH),
    ('up', 'Up', column_util.LIST_BOTH),
    ('mbps', 'Mbit/s', column_util.LIST_BOTH),
    ('delivered_mbps', 'Delivered', column_util.LIST_BOTH),
    ('peak_mbps', 'Peak', column_util.LIST_BOTH),
    ('reserved_mbps', 'Reserved', column_util.LIST_BOTH),
    ('capacity_mbps', 'Capacity', column_util.LIST_BOTH),
    ('age', 'Age (s)', column_util.LIST_BOTH),
    ('stale', 'Stale', column_util.LIST_BOTH),
    ('reporter', 'Reporter', column_util.LIST_LONG_ONLY),
    ('drops', 'Drops', column_util.LIST_LONG_ONLY),
    ('measured_at', 'Measured At', column_util.LIST_LONG_ONLY),
)


class ListLinkState(command.Lister):
    _description = _("List the measured state of the underlay links")

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--from', dest='from_node', metavar='<node>',
            help=_("Only links leaving this node"))
        parser.add_argument(
            '--to', dest='to_node', metavar='<node>',
            help=_("Only links entering this node"))
        parser.add_argument(
            '--long', action='store_true', default=False,
            help=_("Also the reporter, drops and measurement time"))
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.network
        filters = {k: getattr(parsed_args, k) for k in ('from_node', 'to_node')
                   if getattr(parsed_args, k) is not None}
        return common.list_columns(
            client._list(resources.Srv6LinkState, **filters), _attr_map,
            parsed_args.long)
