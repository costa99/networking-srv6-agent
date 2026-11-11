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

"""API definition: measured load per VM pair and link.

Input of the automatic steering. Admin-only. Each fabric node
counts, per link it sends on, the traffic of every VM pair: the outer
destination SID and the inner source and destination addresses of the
SRv6 packets it forwards. Its agent POSTs the largest pairs of each link
once per period:

    POST /v2.0/srv6_aggregate_loads
    {"srv6_aggregate_loads": [{"reporter": "l1", "from_node": "l1",
                               "to_node": "s2", "sid": "fc00:0:3:e300::",
                               "source_address": "10.91.1.11",
                               "destination_address": "10.91.2.13",
                               "mbps": 19.8}, ...]}

The reporter is always from_node: the counters are per out-interface. The
batch is stored whole or refused whole, stamped with the server's clock.

GET returns one entry per (link, SID, source, destination) measured within
link_state_stale_after, with what the server resolved from it: the domain
(from the SID, or from the addresses when the SID is a waypoint's), the two
ports and their hosts, and whether the traffic is steered (its SID is not a
decap SID). The id is opaque.
"""

from networking_srv6_agent.api.definitions import srv6 as srv6_def


NAME = 'SRv6 aggregate loads'
ALIAS = 'srv6-aggregate-loads'
DESCRIPTION = ("Measured load of every VM pair on every underlay link, "
               "posted by the fabric nodes")
UPDATED_TIMESTAMP = '2026-11-02T00:00:00-00:00'
API_PREFIX = ''
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = False

RESOURCE_NAME = 'srv6_aggregate_load'
COLLECTION_NAME = 'srv6_aggregate_loads'
NODE_FIELD_SIZE = 255
ADDRESS_FIELD_SIZE = 39             # the longest IPv6 text form

_node = {'allow_post': True, 'allow_put': False,
         'validate': {'type:not_empty_string': NODE_FIELD_SIZE},
         'is_visible': True}
_address = {'allow_post': True, 'allow_put': False,
            'validate': {'type:not_empty_string': ADDRESS_FIELD_SIZE},
            'is_visible': True}
_out = {'allow_post': False, 'allow_put': False, 'is_visible': True}

RESOURCE_ATTRIBUTE_MAP = {
    COLLECTION_NAME: {
        'id': dict(_out, primary_key=True),
        # --- posted: one measurement ---------------------------------------
        'reporter': dict(_node),
        'from_node': dict(_node, is_filter=True),
        'to_node': dict(_node, is_filter=True),
        'sid': dict(_address),                  # the outer destination
        'source_address': dict(_address),       # inner
        'destination_address': dict(_address),  # inner
        # A number; the plugin checks the type and range (neutron-lib has
        # no validator for a non-negative float).
        'mbps': {'allow_post': True, 'allow_put': False,
                 'is_visible': True},
        # --- computed on GET -----------------------------------------------
        'domain_id': dict(_out, is_filter=True),
        'steered': dict(_out),
        'source_port_id': dict(_out),
        'source_host': dict(_out),
        'destination_port_id': dict(_out),
        'destination_host': dict(_out),
        'peak_mbps': dict(_out),                # highest in the window
        'measured_at': dict(_out),
        'age': dict(_out),
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {}
ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = [srv6_def.ALIAS]
OPTIONAL_EXTENSIONS = []
