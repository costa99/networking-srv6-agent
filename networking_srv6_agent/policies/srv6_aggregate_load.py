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

"""Policies for the measured VM-pair loads: admin-only.

Writing them moves traffic (the server steers the heavy pairs), and
reading them shows every tenant's traffic matrix, so both are admin-only, like
srv6_link_states. No update or delete rules on purpose: measurements are
only ever added (and pruned by the server).
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


rules = [
    policy.DocumentedRuleDefault(
        name='create_srv6_aggregate_load',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Post measured VM-pair loads (fabric collectors)',
        operations=[{'method': 'POST', 'path': '/srv6_aggregate_loads'}],
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_aggregate_load',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Get the measured load of each VM pair on each link',
        operations=[{'method': 'GET', 'path': '/srv6_aggregate_loads'},
                    {'method': 'GET',
                     'path': '/srv6_aggregate_loads/{id}'}],
    ),
]


def list_rules():
    return rules
