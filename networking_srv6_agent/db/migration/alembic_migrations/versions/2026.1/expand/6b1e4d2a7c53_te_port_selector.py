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

"""expand te port selector

P6 (P6-PLAN.md 2, MIGRATION-PLAN.md 8.5): a TE path selects its traffic by
exactly one of destination_host or destination_port_id.

    srv6_te_paths.destination_host      becomes nullable
    srv6_te_paths.destination_port_id   new, FK ports.id ON DELETE CASCADE:
                                        a path for a deleted VM is meaningless
    uniq (domain_id, destination_port_id)
                                        one path per port per domain

"Exactly one selector" is the plugin's check, not a CHECK constraint: CHECK
support is uneven across the MySQL and MariaDB versions neutron runs on.

A new revision rather than an edit to the initial one, which is already
deployed. Expand is right for all of it: neutron's expand check forbids only
drop operations (neutron/tests/functional/db/test_migrations.py,
DROP_OPERATIONS), and relaxing a NOT NULL is not one.

Revision ID: 6b1e4d2a7c53
Revises: 2f8a1b3c9d40
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '6b1e4d2a7c53'
down_revision = '2f8a1b3c9d40'


def upgrade():
    op.alter_column('srv6_te_paths', 'destination_host',
                    existing_type=sa.String(length=255),
                    nullable=True)
    op.add_column('srv6_te_paths',
                  sa.Column('destination_port_id', sa.String(length=36),
                            nullable=True))
    # Unnamed, like every foreign key in the initial revision and in the
    # model: the database names it.
    op.create_foreign_key(None, 'srv6_te_paths', 'ports',
                          ['destination_port_id'], ['id'],
                          ondelete='CASCADE')
    op.create_unique_constraint(
        'uniq_srv6_te_paths0domain_id0destination_port_id',
        'srv6_te_paths', ['domain_id', 'destination_port_id'])
