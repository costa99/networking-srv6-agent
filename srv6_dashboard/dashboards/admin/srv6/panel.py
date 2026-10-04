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

from django.utils.translation import gettext_lazy as _

import horizon


class Srv6(horizon.Panel):
    """Admin > Network > SRv6, at /admin/srv6/.

    policy_rules is what makes the panel admin-only. Being in the Admin
    dashboard is not enough: with policy checks on (Horizon's default,
    POLICY_CHECK_FUNCTION = openstack_auth.policy.check) the dashboard
    gates its URLs only through `permissions`, which it then does not set,
    and each panel must declare its own rule -- as every core admin panel
    does. Found live: without it a tenant opened this page (FEATURE D23).
    """

    name = _("SRv6")
    slug = 'srv6'
    policy_rules = (('network', 'context_is_admin'),)
