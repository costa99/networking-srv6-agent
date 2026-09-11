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

"""Policies for TE paths: admin only, every verb and every attribute.

Steering is the admin's control over tenant VRFs (MIGRATION-PLAN.md 8.5), and
the read side is as much the exposure as the write side: a path names compute
hosts and resolves to underlay SIDs (TE-VS-SFC 4.3.1). A tenant listing gets
[] and a show gets 404, the same answers as for the locator registry.

The attribute rules are redundant with the verb rules while both say ADMIN.
They are registered anyway: every enforce_policy attribute needs a default
or oslo.policy fails closed on it, for admins too.
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


COLLECTION_PATH = '/srv6_te_paths'
RESOURCE_PATH = '/srv6_te_paths/{id}'

_POST = [{'method': 'POST', 'path': COLLECTION_PATH}]
_GET = [{'method': 'GET', 'path': COLLECTION_PATH},
        {'method': 'GET', 'path': RESOURCE_PATH}]
_PUT = [{'method': 'PUT', 'path': RESOURCE_PATH}]
_DELETE = [{'method': 'DELETE', 'path': RESOURCE_PATH}]


def _admin(name, description, operations):
    return policy.DocumentedRuleDefault(
        name=name,
        check_str=lib_rules.ADMIN,
        scope_types=['project'],
        description=description,
        operations=operations,
    )


rules = [
    _admin('create_srv6_te_path', 'Create an SRv6 TE path', _POST),
    _admin('create_srv6_te_path:destination_host',
           'Select a destination host when creating an SRv6 TE path', _POST),
    _admin('create_srv6_te_path:destination_port_id',
           'Select a destination port when creating an SRv6 TE path', _POST),
    _admin('create_srv6_te_path:via_hosts',
           'Specify ``via_hosts`` when creating an SRv6 TE path', _POST),
    _admin('get_srv6_te_path', 'Get SRv6 TE paths', _GET),
    _admin('get_srv6_te_path:destination_host',
           'Get ``destination_host`` of SRv6 TE paths', _GET),
    _admin('get_srv6_te_path:destination_port_id',
           'Get ``destination_port_id`` of SRv6 TE paths', _GET),
    _admin('get_srv6_te_path:via_hosts',
           'Get ``via_hosts`` of SRv6 TE paths', _GET),
    _admin('get_srv6_te_path:segments',
           'Get the resolved ``segments`` of SRv6 TE paths', _GET),
    _admin('update_srv6_te_path', 'Update an SRv6 TE path', _PUT),
    _admin('update_srv6_te_path:via_hosts',
           'Re-steer an SRv6 TE path by replacing ``via_hosts``', _PUT),
    _admin('delete_srv6_te_path', 'Delete an SRv6 TE path', _DELETE),
]


def list_rules():
    return rules
