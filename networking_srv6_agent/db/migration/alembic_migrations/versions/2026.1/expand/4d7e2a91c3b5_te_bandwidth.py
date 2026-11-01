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

"""expand te bandwidth

A TE path may reserve bandwidth; the plugin then computes its hops itself
(PLAN-PATH-COMPUTATION, FEATURE-PATH-COMPUTATION).

    srv6_te_paths.bandwidth     new, nullable INTEGER, Mbit/s; NULL for every
                                existing row: an operator-chosen path that
                                reserves nothing

Revision ID: 4d7e2a91c3b5
Revises: 9c244f3b0b07
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '4d7e2a91c3b5'
down_revision = '9c244f3b0b07'


def upgrade():
    op.add_column('srv6_te_paths', sa.Column('bandwidth', sa.Integer(),
                                             nullable=True))
