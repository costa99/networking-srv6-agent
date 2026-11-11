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

"""expand te reserved host

The host a bandwidth reservation's hops were computed for:
the books are built from it, never from
where a port is now, so a migrated port cannot make them charge a link that
does not exist.

    srv6_te_paths.reserved_host     new, nullable VARCHAR(255); NULL for an
                                    operator path. Existing reservations are
                                    all to a host (ports were refused): the
                                    plugin falls back to destination_host
                                    for them, so no data is migrated

Revision ID: 7e3c5a1f9b20
Revises: 4d7e2a91c3b5
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7e3c5a1f9b20'
down_revision = '4d7e2a91c3b5'


def upgrade():
    op.add_column('srv6_te_paths', sa.Column('reserved_host', sa.String(255),
                                             nullable=True))
