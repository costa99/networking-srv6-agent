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

"""Policies for the locator registry: admin-only, read-only.

The read side IS the exposure: host names and underlay addresses are the
same infrastructure vocabulary as binding:host_id and provider:*. There are
no create/update/delete rules on purpose; oslo.policy fails closed on an
unregistered rule, which is the right answer for a collection only agents
write.
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


rules = [
    policy.DocumentedRuleDefault(
        name='get_srv6_locator',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Get the SRv6 locators registered by compute nodes',
        operations=[{'method': 'GET', 'path': '/srv6_locators'},
                    {'method': 'GET', 'path': '/srv6_locators/{id}'}],
    ),
]


def list_rules():
    return rules
