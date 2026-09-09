# Copyright 2026 networking-srv6-agent contributors.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""contract initial

Revision ID: 1a2b3c4d5e60
Revises: start_networking_srv6_agent
"""

from neutron.db.migration import cli

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e60'
down_revision = 'start_networking_srv6_agent'
branch_labels = (cli.CONTRACT_BRANCH,)


def upgrade():
    # Nothing to contract: this project has no legacy schema to narrow.
    # The branch exists because neutron-db-manage expects both heads.
    pass
