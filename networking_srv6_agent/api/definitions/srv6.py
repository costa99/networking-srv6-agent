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

"""API definition: SRv6 domains and their network associations.

Laid out exactly like a neutron-lib definition module, but kept in this
repository. That is the whole reason the neutron-lib fork can go: the old
build needed `neutron_lib.api.definitions.bgpvpn_srv6`, which exists only in
the fork, while APIExtensionDescriptor accepts any module-like object that
carries these names.

URLs have no path prefix and every collection is prefixed `srv6_`
(P4-PLAN.md 1.3): neutron derives the policy rule names from the collection
name, so a bare `domains` would register a global `create_domain` rule.
"""

from neutron_lib.db import constants as db_const

from networking_srv6_agent.common import constants


NAME = 'SRv6 domains'
ALIAS = 'srv6'
DESCRIPTION = ("SRv6 connectivity between compute nodes: a domain groups "
               "networks into one VRF, reachable across nodes over SRv6")
UPDATED_TIMESTAMP = '2026-09-10T00:00:00-00:00'
API_PREFIX = ''
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = False

RESOURCE_NAME = 'srv6_domain'
COLLECTION_NAME = 'srv6_domains'

# The sub-resource. Its collection name is shared with networking-bgpvpn's
# network_associations; neutron merges the two parameter maps in
# attributes.RESOURCES when both extensions load. That is harmless: the
# controller validates against the map passed to it, attribute-level rules
# are never built for a sub-resource on create (its RESOURCES entry holds
# 'parent' and 'parameters', not attribute names), and the GET-side checks
# pass for rules that are not registered.
NET_ASSOC_RESOURCE_NAME = 'network_association'
NET_ASSOC_COLLECTION_NAME = 'network_associations'

SRV6_BEHAVIOR = 'srv6_behavior'
SID_FUNCTION = 'sid_function'
NETWORKS = 'networks'

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True,
               'is_filter': True,
               'is_sort_key': True},
        'project_id': {'allow_post': True, 'allow_put': False,
                       'validate': {
                           'type:string': db_const.PROJECT_ID_FIELD_SIZE},
                       'required_by_policy': True,
                       'is_visible': True,
                       'is_filter': True,
                       'is_sort_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'default': '',
                 'validate': {'type:string': db_const.NAME_FIELD_SIZE},
                 'is_visible': True,
                 'is_filter': True,
                 'is_sort_key': True},
        # Exactly one valid value (MIGRATION-PLAN.md 8.3): the API offers no
        # choice the dataplane cannot honour. Create-only, because changing
        # it would re-program every node's decap SID under live traffic.
        SRV6_BEHAVIOR: {'allow_post': True, 'allow_put': False,
                        'default': constants.DEFAULT_BEHAVIOR,
                        'validate': {
                            'type:values': constants.VALID_BEHAVIORS},
                        'is_visible': True,
                        'enforce_policy': True},
        # Server-allocated, and ADMIN-only to read (decided 2026-09-10): it
        # is half of an underlay SID, like the locators.
        SID_FUNCTION: {'allow_post': False, 'allow_put': False,
                       'is_visible': True,
                       'enforce_policy': True},
        NETWORKS: {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {
    NET_ASSOC_COLLECTION_NAME: {
        'parent': {'collection_name': COLLECTION_NAME,
                   'member_name': RESOURCE_NAME},
        'parameters': {
            'id': {'allow_post': False, 'allow_put': False,
                   'validate': {'type:uuid': None},
                   'is_visible': True,
                   'primary_key': True,
                   'is_filter': True},
            # Accepted so neutron can authorise the request, then FORCED to
            # the domain's project by the plugin: an admin associating on a
            # tenant's behalf still produces the tenant's association.
            'project_id': {'allow_post': True, 'allow_put': False,
                           'validate': {
                               'type:string': db_const.PROJECT_ID_FIELD_SIZE},
                           'required_by_policy': True,
                           'is_visible': True,
                           'is_filter': True},
            'network_id': {'allow_post': True, 'allow_put': False,
                           'validate': {'type:uuid': None},
                           'is_visible': True,
                           'is_filter': True},
        },
    },
}

ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = []
OPTIONAL_EXTENSIONS = []
