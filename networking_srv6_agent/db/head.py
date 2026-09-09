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

"""Model metadata for the models-vs-migrations test.

Every model module is imported EXPLICITLY. networking-bgpvpn reached its
srv6 models through a side-effect import inside bgpvpn_db and never imported
srv6_te_db at all, so the TE tables existed in the migration but not in the
metadata -- a drift the sync test would report and nobody would expect.
"""

from neutron.db.migration.models import head

# pylint: disable=unused-import
from networking_srv6_agent.db import domain_db  # noqa: F401
from networking_srv6_agent.db import srv6_db  # noqa: F401
from networking_srv6_agent.db import te_db  # noqa: F401


def get_metadata():
    return head.model_base.BASEV2.metadata
