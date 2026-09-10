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

"""Policies for a domain's network associations.

The rule names are the ones neutron derives for a sub-resource: the action,
then the parent member name, then the collection --
create_srv6_domain_network_association, and so on.

ADMIN_OR_PROJECT_*, checked against the association's own project_id, and
NOT ADMIN_OR_PARENT_OWNER_* as P4-PLAN.md first proposed: neutron resolves
ext_parent_owner only for the parents in neutron-lib's
EXT_PARENT_RESOURCE_MAPPING (floatingip, router, local_ip, qos policy), so
for srv6_domain that rule could never pass for a tenant. What policy cannot
see -- that the caller owns the *domain*, and that the domain's project owns
the *network* -- the plugin checks (Srv6Plugin._get_domain_checked and
_validate_net_assoc), and the association's project_id is forced to the
domain's.
"""

from neutron_lib.policy import rules as lib_rules
from oslo_policy import policy


COLLECTION_PATH = '/srv6_domains/{srv6_domain_id}/network_associations'
RESOURCE_PATH = ('/srv6_domains/{srv6_domain_id}/network_associations/'
                 '{network_association_id}')

rules = [
    policy.DocumentedRuleDefault(
        name='create_srv6_domain_network_association',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Attach a network to an SRv6 domain',
        operations=[{'method': 'POST', 'path': COLLECTION_PATH}],
    ),
    policy.DocumentedRuleDefault(
        name='get_srv6_domain_network_association',
        check_str=lib_rules.ADMIN_OR_PROJECT_READER,
        scope_types=['project'],
        description="Get an SRv6 domain's network associations",
        operations=[{'method': 'GET', 'path': COLLECTION_PATH},
                    {'method': 'GET', 'path': RESOURCE_PATH}],
    ),
    policy.DocumentedRuleDefault(
        name='delete_srv6_domain_network_association',
        check_str=lib_rules.ADMIN_OR_PROJECT_MEMBER,
        scope_types=['project'],
        description='Detach a network from an SRv6 domain',
        operations=[{'method': 'DELETE', 'path': RESOURCE_PATH}],
    ),
]


def list_rules():
    return rules
