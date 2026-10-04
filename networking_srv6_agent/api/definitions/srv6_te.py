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

"""API definition: operator-defined SRv6 traffic-engineering paths.

Admin-only and top level, not a child of srv6_domain:
every attribute here is infrastructure vocabulary -- compute host names and
underlay SIDs -- and a child of a tenant-readable object would leak the host
inventory to the tenant. domain_id is therefore a body field.

A path picks its traffic with exactly one selector:

    destination_host      all of the domain's traffic toward that node
    destination_port_id   traffic toward one VM, that port's fixed IPs only

and steers it through via_hosts, in traversal order. "Exactly one" is
enforced by the plugin, not here and not by a CHECK constraint.

It may also name source_host, the ingress node: the path then applies only
to the domain's traffic that enters the SRv6 domain at that node (is
encapsulated there). Without it the path applies at every ingress. Per route
and per ingress, the first of these that exists wins:

    destination port + this source    >  destination host + this source
    >  destination port, any source   >  destination host, any source
    >  direct

so a source path outranks every path without one (FEATURE-TE-SOURCE-HOST).

`segments` and `status` are what the SERVER resolved when the path was read,
not an acknowledgement from any agent.
"""

from neutron_lib.api import converters
from neutron_lib.db import constants as db_const

from networking_srv6_agent.api.definitions import srv6 as srv6_def


NAME = 'SRv6 traffic engineering'
ALIAS = 'srv6-te'
DESCRIPTION = ("Admin-only SRv6 paths that steer a domain's traffic toward a "
               "compute node, or a single port, through explicit waypoints")
UPDATED_TIMESTAMP = '2026-10-04T00:00:00-00:00'
API_PREFIX = ''
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = False

RESOURCE_NAME = 'srv6_te_path'
COLLECTION_NAME = 'srv6_te_paths'

DOMAIN_ID = 'domain_id'
DESTINATION_HOST = 'destination_host'
DESTINATION_PORT_ID = 'destination_port_id'
SOURCE_HOST = 'source_host'
VIA_HOSTS = 'via_hosts'
SEGMENTS = 'segments'
ENCODED_SEGMENTS = 'encoded_segments'
STATUS = 'status'

# ACTIVE: every host on the path has a registered locator, so `segments` is
# what the routes carry. DEGRADED: one does not (or the port is unbound), so
# the routes fall back to the direct path and `segments` is empty.
STATUS_ACTIVE = 'ACTIVE'
STATUS_DEGRADED = 'DEGRADED'

# The width of binding:host_id and of the srv6_host_locators key.
HOST_FIELD_SIZE = 255

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True,
               'is_filter': True},
        # Accepted so neutron can populate and authorise it, then FORCED to
        # the domain's project by the plugin, as associations are. A
        # read-only definition would break neutron's populate_project_id.
        'project_id': {'allow_post': True, 'allow_put': False,
                       'validate': {
                           'type:string': db_const.PROJECT_ID_FIELD_SIZE},
                       'required_by_policy': True,
                       'is_visible': True,
                       'is_filter': True},
        DOMAIN_ID: {'allow_post': True, 'allow_put': False,
                    'validate': {'type:uuid': None},
                    'is_visible': True,
                    'is_filter': True},
        # The selectors are create-only: a path that changed what it selects
        # is a different path, and a delete/create says so honestly.
        DESTINATION_HOST: {'allow_post': True, 'allow_put': False,
                           'default': None,
                           'validate': {
                               'type:string_or_none': HOST_FIELD_SIZE},
                           'is_visible': True,
                           'is_filter': True,
                           'enforce_policy': True},
        DESTINATION_PORT_ID: {'allow_post': True, 'allow_put': False,
                              'default': None,
                              'validate': {'type:uuid_or_none': None},
                              'is_visible': True,
                              'is_filter': True,
                              'enforce_policy': True},
        # The ingress the path is limited to; None: every ingress. Create-only
        # like the selectors, for the same reason.
        SOURCE_HOST: {'allow_post': True, 'allow_put': False,
                      'default': None,
                      'validate': {
                          'type:string_or_none': HOST_FIELD_SIZE},
                      'is_visible': True,
                      'is_filter': True,
                      'enforce_policy': True},
        # PUT REPLACES the list: re-steering without a delete/create window.
        # [] means "explicitly direct".
        VIA_HOSTS: {'allow_post': True, 'allow_put': True,
                    'default': [],
                    'convert_to': converters.convert_to_list,
                    'validate': {
                        'type:list_of_unique_strings': HOST_FIELD_SIZE},
                    'is_visible': True,
                    'enforce_policy': True},
        # [<via End SIDs>..., <destination decap SID>], or [] when the path
        # is not in effect. For a path with a source_host, without leading
        # SIDs of that node: it is the ingress, and drops them (the agent's
        # _transit_segments).
        SEGMENTS: {'allow_post': False, 'allow_put': False,
                   'is_visible': True,
                   'enforce_policy': True},
        # What the ingress actually pushes: `segments` again with full
        # SIDs; with dataplane_encoding = usid, the C-SID containers the
        # list packs into -- [fc00:0:1::, fc00:0:3:e005::] becomes
        # [fc00:0:1:3:e005::]. [] when the path is not in effect.
        ENCODED_SEGMENTS: {'allow_post': False, 'allow_put': False,
                           'is_visible': True,
                           'enforce_policy': True},
        STATUS: {'allow_post': False, 'allow_put': False,
                 'is_visible': True},
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {}
ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = [srv6_def.ALIAS]
OPTIONAL_EXTENSIONS = []
