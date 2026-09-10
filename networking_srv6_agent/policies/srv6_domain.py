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

"""Policies for SRv6 domains.

A domain is a project resource: its project creates, reads and deletes it.
The one exception is `sid_function`, readable by admins only (decided
2026-09-10). It is half of an underlay SID -- infrastructure vocabulary like
the locators -- and since ids are handed out round-robin, seeing your own
makes your neighbours' guessable. Neutron's attribute-level GET check drops
the field from a non-admin's response; the request itself still succeeds.

The personas are neutron_lib.policy.rules, the live ones, not the
RULE_ADMIN_OR_OWNER family that neutron-lib now files under deprecated.
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


COLLECTION_PATH = '/srv6_domains'
RESOURCE_PATH = '/srv6_domains/{id}'

_GET_OPERATIONS = [
    {'method': 'GET', 'path': COLLECTION_PATH},
    {'method': 'GET', 'path': RESOURCE_PATH},
]

rules = [
    policy.DocumentedRuleDefault(
        name='create_srv6_domain',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Create an SRv6 domain',
        operations=[{'method': 'POST', 'path': COLLECTION_PATH}],
    ),
    policy.DocumentedRuleDefault(
        name='create_srv6_domain:srv6_behavior',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Specify ``srv6_behavior`` when creating an SRv6 domain',
        operations=[{'method': 'POST', 'path': COLLECTION_PATH}],
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_domain',
        check_str=lib_rules.ADMIN_OR_PROJECT_READER,
        scope_types=['project'],
        description='Get SRv6 domains',
        operations=_GET_OPERATIONS,
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_domain:srv6_behavior',
        check_str=lib_rules.ADMIN_OR_PROJECT_READER,
        scope_types=['project'],
        description='Get ``srv6_behavior`` of SRv6 domains',
        operations=_GET_OPERATIONS,
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_domain:sid_function',
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description='Get ``sid_function`` of SRv6 domains',
        operations=_GET_OPERATIONS,
    ),
    policy.DocumentedRuleDefault(
        name='update_srv6_domain',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Update an SRv6 domain',
        operations=[{'method': 'PUT', 'path': RESOURCE_PATH}],
    ),
    policy.DocumentedRuleDefault(
        name='delete_srv6_domain',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Delete an SRv6 domain',
        operations=[{'method': 'DELETE', 'path': RESOURCE_PATH}],
    ),
]


def list_rules():
    return rules
