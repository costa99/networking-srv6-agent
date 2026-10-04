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

"""Formatting shared by the project and admin panels (FEATURE §6.2)."""

from django.urls import reverse
from django.utils import html
from django.utils.translation import gettext_lazy as _

#: Where a network link points, per dashboard: a tenant opens its own
#: network page, an admin the admin one (which shows any project's).
PROJECT_NETWORK_URL = 'horizon:project:networks:detail'
ADMIN_NETWORK_URL = 'horizon:admin:networks:detail'


def network_links(network_ids, names, url_name):
    """Comma-separated links to networks, by name when it is known.

    names comes from api.srv6.network_names(). An ID missing from it -- a
    network the user cannot see -- still gets a link, shown by its ID, so
    the column never hides that a network is attached.
    """
    if not network_ids:
        return '-'
    return html.format_html_join(
        ', ', '<a href="{}">{}</a>',
        ((reverse(url_name, args=[nid]), names.get(nid, nid))
         for nid in network_ids))


def sid_function(value):
    """The domain's SID function as the hex it has in every SID, or '-'.

    The server returns an integer (4096) and only to admins; in a SID it
    is the hex group after the locator (fc00:0:1:1000::), so that is how
    it is shown.
    """
    if value is None:
        return '-'
    return '%d (0x%x)' % (value, value)


BEHAVIOR_HELP = _(
    "End.DT46 is the only behavior the plugin implements: each node "
    "decapsulates the domain's traffic into the domain's VRF, for IPv4 "
    "and IPv6 alike (RFC 8986 §4.8).")
