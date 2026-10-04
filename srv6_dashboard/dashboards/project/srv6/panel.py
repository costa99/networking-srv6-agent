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


class Srv6Domains(horizon.Panel):
    """Project > Network > SRv6 Domains, at /project/srv6/.

    No permissions or policy_rules: every project member may create and
    attach (policy ADMIN_OR_PROJECT_MEMBER), and Neutron enforces it on
    every request anyway (FEATURE D5, D9).
    """

    name = _("SRv6 Domains")
    slug = 'srv6'
