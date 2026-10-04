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

"""Django settings for the dashboard tests.

Horizon's own test settings, plus our enabled/ files applied the way a real
Horizon applies local/enabled/ (FEATURE §2.1): so the tests register the
panels exactly as production does, and a broken enabled file fails here.
"""

# Start from Horizon's test settings, so every key Horizon reads exists.
from horizon.test.settings import *  # noqa: F401,F403
from openstack_dashboard.test.settings import *  # noqa: F401,F403

import openstack_dashboard.enabled
from openstack_dashboard.utils import settings

import srv6_dashboard.enabled

# update_dashboards fills these in again; popping them first avoids its
# deprecation warning about statically configured dashboards.
HORIZON_CONFIG.pop('dashboards', None)  # noqa: F405
HORIZON_CONFIG.pop('default_dashboard', None)  # noqa: F405

settings.update_dashboards(
    [
        openstack_dashboard.enabled,
        srv6_dashboard.enabled,
    ],
    HORIZON_CONFIG,  # noqa: F405
    INSTALLED_APPS,  # noqa: F405
)

# update_dashboards may add an app twice (once per enabled file naming it).
INSTALLED_APPS = list(dict.fromkeys(INSTALLED_APPS))  # noqa: F405
