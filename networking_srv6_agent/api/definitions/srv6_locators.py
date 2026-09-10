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

"""API definition: the read-only, admin-only locator registry.

Rows are written only by an agent's sync_state. Without this collection an
admin has no way to learn the host names a TE path's via_hosts must use, and
a typo there resolves to an empty segment list -- "not steered", silently.

Until an agent runs (P5), the collection is legitimately empty.
"""

from networking_srv6_agent.api.definitions import srv6 as srv6_def


NAME = 'SRv6 locators'
ALIAS = 'srv6-locators'
DESCRIPTION = ("The SRv6 locator each compute node's agent registered, and "
               "when it last reported it")
UPDATED_TIMESTAMP = '2026-09-10T00:00:00-00:00'
API_PREFIX = ''
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = False

RESOURCE_NAME = 'srv6_locator'
COLLECTION_NAME = 'srv6_locators'

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        # The host name doubles as the id: it is the table's primary key,
        # and it is what an admin types into a TE path.
        'id': {'allow_post': False, 'allow_put': False,
               'is_visible': True,
               'primary_key': True},
        'host': {'allow_post': False, 'allow_put': False,
                 'is_visible': True,
                 'is_filter': True},
        'locator': {'allow_post': False, 'allow_put': False,
                    'is_visible': True},
        'updated_at': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {}
ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = [srv6_def.ALIAS]
OPTIONAL_EXTENSIONS = []
