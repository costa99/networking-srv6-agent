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

"""expand link states

Measured link state of the underlay: one row per
measurement a fabric node posts to srv6_link_states.

    srv6_link_states    new table; id (uuid) as primary key, not
                        (reporter, link, measured_at): DATETIME keeps whole
                        seconds on MySQL, so a batch re-sent within the
                        same second would collide. Index (reporter,
                        measured_at) for the per-reporter pruning of each
                        batch

Revision ID: b5d2e8f1a7c4
Revises: 7e3c5a1f9b20
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b5d2e8f1a7c4'
down_revision = '7e3c5a1f9b20'


def upgrade():
    op.create_table(
        'srv6_link_states',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('reporter', sa.String(255), nullable=False),
        sa.Column('from_node', sa.String(255), nullable=False),
        sa.Column('to_node', sa.String(255), nullable=False),
        sa.Column('up', sa.Boolean, nullable=False),
        sa.Column('mbps', sa.Float, nullable=True),
        sa.Column('drops', sa.BigInteger, nullable=True),
        sa.Column('measured_at', sa.DateTime, nullable=False),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB',
    )
    op.create_index('ix_srv6_link_states_reporter_measured_at',
                    'srv6_link_states', ['reporter', 'measured_at'])
