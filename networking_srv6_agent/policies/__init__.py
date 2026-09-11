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

"""Policy defaults, loaded by neutron through the neutron.policies entry
point (and by oslopolicy-* tools through oslo.policy.policies).

oslo.policy FAILS CLOSED on a rule it never registered: the first symptom of
a missing default is an admin getting 403 with nothing in the log naming
the rule. tests/unit/policies/test_policies.py walks every attribute map
and fails if an enforce_policy attribute has no rule here.
"""

import itertools

from networking_srv6_agent.policies import network_association
from networking_srv6_agent.policies import srv6_domain
from networking_srv6_agent.policies import srv6_locator
from networking_srv6_agent.policies import srv6_te_path


def list_rules():
    return itertools.chain(
        srv6_domain.list_rules(),
        network_association.list_rules(),
        srv6_locator.list_rules(),
        srv6_te_path.list_rules(),
    )
