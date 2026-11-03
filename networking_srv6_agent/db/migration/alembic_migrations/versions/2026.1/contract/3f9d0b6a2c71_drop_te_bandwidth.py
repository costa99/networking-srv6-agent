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

"""contract drop te bandwidth

Bandwidth reservations are gone (PLAN-REMOVE-BANDWIDTH): a TE path is its
waypoint list only. Existing reservations become plain paths with the hops
they had.

    srv6_te_paths.bandwidth         dropped (added by 4d7e2a91c3b5)
    srv6_te_paths.reserved_host     dropped (added by 7e3c5a1f9b20)

Revision ID: 3f9d0b6a2c71
Revises: 1a2b3c4d5e60
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = '3f9d0b6a2c71'
down_revision = '1a2b3c4d5e60'
# both columns must exist before they are dropped
depends_on = ('7e3c5a1f9b20',)


def upgrade():
    op.drop_column('srv6_te_paths', 'bandwidth')
    op.drop_column('srv6_te_paths', 'reserved_host')
