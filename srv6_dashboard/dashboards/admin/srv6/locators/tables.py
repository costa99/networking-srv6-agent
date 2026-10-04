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

"""The Locators tab's table: GET /v2.0/srv6_locators, read-only.

A locator is written by the host's agent, never by a user, so the table
has no actions. It answers the questions an admin asks before steering:
which hosts can be a waypoint or a destination (only these, D6), and is
that host's agent still reporting.
"""

from django.template import defaultfilters as filters
from django.utils import html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
from horizon import tables
from horizon.utils import filters as utils_filters


def _code(value):
    return html.format_html('<code>{}</code>', value) if value else '-'


class LocatorsTable(tables.DataTable):
    host = tables.Column('host', verbose_name=_("Host"))
    locator = tables.Column('locator', verbose_name=_("Locator"),
                            filters=(_code,))
    # Refreshed on every agent report, not only when the locator changes
    # (db/srv6_db.py set_host_locator), so the age is a liveness hint.
    # The row stays after an agent stops: an old age is the sign.
    updated_at = tables.Column(
        'updated_at',
        verbose_name=pgettext_lazy('Time since the last report',
                                   'Last Reported'),
        filters=(utils_filters.parse_isotime, filters.timesince))

    class Meta:
        name = 'srv6_locators'
        verbose_name = _("Locators")
        hidden_title = False

    def get_object_id(self, locator):
        return locator.host
