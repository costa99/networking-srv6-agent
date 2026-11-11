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

"""API definition: measured link state of the underlay.

Admin-only. A collector (the fabric agent on each spine and leaf) POSTs a
batch of measurements, one item per directed link it sees:

    POST /v2.0/srv6_link_states
    {"srv6_link_states": [{"reporter": "l1", "from_node": "l1",
                           "to_node": "s1", "up": true, "mbps": 42.5,
                           "drops": 0}, ...]}

reporter is the node that measured: from_node for what it sent (the load
offered to the link), to_node for what it received (what the link
delivered). The batch is stored as a whole or refused as a whole; the
server stamps it with its own clock (measured_at), never the reporter's.

GET returns one entry per directed link of the topology file, computed
from the stored measurements and its capacity: the read-only attributes
below. The id is "<from>:<to>".
"""

from neutron_lib.api import converters

from networking_srv6_agent.api.definitions import srv6 as srv6_def


NAME = 'SRv6 link states'
ALIAS = 'srv6-link-states'
DESCRIPTION = ("Measured load and state of every underlay link, posted by "
               "the fabric nodes")
UPDATED_TIMESTAMP = '2026-11-03T00:00:00-00:00'
API_PREFIX = ''
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = False

RESOURCE_NAME = 'srv6_link_state'
COLLECTION_NAME = 'srv6_link_states'
NODE_FIELD_SIZE = 255

_node = {'allow_post': True, 'allow_put': False,
         'validate': {'type:not_empty_string': NODE_FIELD_SIZE},
         'is_visible': True}
_out = {'allow_post': False, 'allow_put': False, 'is_visible': True}

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id': dict(_out, primary_key=True),
        # --- posted: one measurement ---------------------------------------
        'reporter': dict(_node),
        'from_node': dict(_node, is_filter=True),
        'to_node': dict(_node, is_filter=True),
        'up': {'allow_post': True, 'allow_put': False,
               'convert_to': converters.convert_to_boolean,
               'is_visible': True},
        # Numbers or null (no rate for that period: a counter reset). The
        # plugin checks the type and range: neutron-lib has no validator
        # for a non-negative float.
        'mbps': {'allow_post': True, 'allow_put': False, 'default': None,
                 'is_visible': True},
        'drops': {'allow_post': True, 'allow_put': False, 'default': None,
                  'is_visible': True},
        # --- computed on GET -----------------------------------------------
        # Delivered load: the receiver's latest measurement (mbps is the
        # sender's when it reports, the receiver's otherwise).
        'delivered_mbps': dict(_out),
        'peak_mbps': dict(_out),            # highest mbps in the window
        'capacity_mbps': dict(_out),        # from the topology file
        'measured_at': dict(_out),
        'age': dict(_out),                  # seconds since measured_at
        'stale': dict(_out),
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {}
ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = [srv6_def.ALIAS]
OPTIONAL_EXTENSIONS = []
