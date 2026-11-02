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

"""expand aggregate loads

Measured load per VM pair and link (PLAN-UNRESERVED-STEERING step 2): one row
per measurement a fabric node posts to srv6_aggregate_loads.

    srv6_aggregate_loads    new table; uuid primary key and an index
                            (reporter, measured_at) for the per-reporter
                            pruning, as srv6_link_states (b5d2e8f1a7c4)

Revision ID: d8f3a6c2e914
Revises: b5d2e8f1a7c4
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd8f3a6c2e914'
down_revision = 'b5d2e8f1a7c4'


def upgrade():
    op.create_table(
        'srv6_aggregate_loads',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('reporter', sa.String(255), nullable=False),
        sa.Column('from_node', sa.String(255), nullable=False),
        sa.Column('to_node', sa.String(255), nullable=False),
        sa.Column('sid', sa.String(39), nullable=False),
        sa.Column('source_address', sa.String(39), nullable=False),
        sa.Column('destination_address', sa.String(39), nullable=False),
        sa.Column('mbps', sa.Float, nullable=False),
        sa.Column('measured_at', sa.DateTime, nullable=False),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB',
    )
    op.create_index('ix_srv6_aggregate_loads_reporter_measured_at',
                    'srv6_aggregate_loads', ['reporter', 'measured_at'])
