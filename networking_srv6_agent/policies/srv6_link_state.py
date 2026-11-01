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

"""Policies for the measured link state: admin-only.

Writing it moves traffic (paths are rerouted and admitted from it), so only
an admin credential -- the fabric collectors' -- may post. Reading it shows
the underlay, infrastructure vocabulary like srv6_locators. No update or
delete rules on purpose: oslo.policy fails closed on an unregistered rule,
and measurements are only ever added (and pruned by the server).
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


rules = [
    policy.DocumentedRuleDefault(
        name='create_srv6_link_state',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Post measured link states (fabric collectors)',
        operations=[{'method': 'POST', 'path': '/srv6_link_states'}],
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_link_state',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Get the measured state of the underlay links',
        operations=[{'method': 'GET', 'path': '/srv6_link_states'},
                    {'method': 'GET', 'path': '/srv6_link_states/{id}'}],
    ),
]


def list_rules():
    return rules
