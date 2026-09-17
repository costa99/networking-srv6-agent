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

"""Helpers shared by the srv6 commands."""

import logging

from openstackclient.identity import common as identity_common
from osc_lib.cli import format_columns
from osc_lib import exceptions
from osc_lib import utils
from osc_lib.utils import columns as column_util

from networking_srv6_agent._i18n import _
from networking_srv6_agent.cli import resources

LOG = logging.getLogger(__name__)

# SDK bookkeeping, never part of the API's answer.
_ALWAYS_HIDDEN = ('location', 'tenant_id')


class PathColumn(format_columns.ListColumn):
    """A list whose order is the meaning: waypoints, segments.

    osc_lib's ListColumn sorts, which would print a segment list with the
    decap SID before the End SIDs it follows.
    """

    def human_readable(self):
        return ', '.join(self._value or [])


def show_columns(obj, column_map, formatters=None, hidden=()):
    """(headers, values) for a ShowOne, from an SDK resource."""
    hidden = _ALWAYS_HIDDEN + tuple(hidden)
    # osc_lib shows every mapped attribute, hidden or not.
    column_map = {k: v for k, v in column_map.items() if k not in hidden}
    display_columns, columns = utils.get_osc_show_columns_for_sdk_resource(
        obj, column_map, hidden)
    return display_columns, utils.get_dict_properties(
        obj, columns, formatters=formatters)


def list_columns(objs, attr_map, long_listing, formatters=None):
    """(headers, rows) for a Lister."""
    headers, columns = column_util.get_column_definitions(
        attr_map, long_listing=long_listing)
    return tuple(headers), (utils.get_item_properties(o, columns,
                                               formatters=formatters)
                     for o in objs)


def add_project_options(parser, help_text):
    parser.add_argument(
        '--project',
        metavar='<project>',
        help=help_text)
    identity_common.add_project_domain_option_to_parser(parser)


def project_id(client_manager, parsed_args):
    """The --project ID, or None when it was not given."""
    if not parsed_args.project:
        return None
    return identity_common.find_project(
        client_manager.identity, parsed_args.project,
        parsed_args.project_domain).id


def find_domain(client, name_or_id):
    return client._find(resources.Srv6Domain, name_or_id,
                        ignore_missing=False)


def find_association(client, domain_id, value):
    """An association of the domain, by its ID or by its network.

    Nobody remembers association IDs: the network is what a user attached,
    so a network name or ID is accepted too.
    """
    assocs = list(client._list(resources.Srv6NetworkAssociation,
                               domain_id=domain_id))
    for assoc in assocs:
        if assoc.id == value:
            return assoc
    network = client.find_network(value, ignore_missing=False)
    for assoc in assocs:
        if assoc.network_id == network.id:
            return assoc
    raise exceptions.CommandError(
        _("Network %(net)s is not associated with SRv6 domain "
          "%(domain)s") % {'net': value, 'domain': domain_id})


def delete_all(values, delete, kind):
    """Call delete(value) for each value, and fail once at the end."""
    failed = 0
    for value in values:
        try:
            delete(value)
        except Exception as e:
            failed += 1
            LOG.error("Failed to delete %(kind)s '%(value)s': %(e)s",
                      {'kind': kind, 'value': value, 'e': e})
    if failed:
        raise exceptions.CommandError(
            _("%(failed)s of %(total)s %(kind)s(s) failed to delete.") %
            {'failed': failed, 'total': len(values), 'kind': kind})
