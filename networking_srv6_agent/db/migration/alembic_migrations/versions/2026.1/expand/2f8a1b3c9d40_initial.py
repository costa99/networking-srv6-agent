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

"""expand initial

Creates the whole schema in one revision. This is a NEW project, not a
migration of the networking-bgpvpn tables: nothing upgrades from those, the
column names differ (bgpvpn_id -> domain_id) and the table names differ, so
chaining onto them would be a fiction. A deployment moving off the BGPVPN
driver recreates its domains.

Five tables:

    srv6_domains                        the grouping and isolation object
    srv6_domain_network_associations    which networks are in a domain
    srv6_function_allocations           the SID function id pool
    srv6_host_locators                  what each agent reported
    srv6_te_paths / srv6_te_path_hops   operator-selected paths

Expand-only, and there is no downgrade -- neutron's expand/contract
migrations are not downgradable.

Revision ID: 2f8a1b3c9d40
Revises: start_networking_srv6_agent
"""

from alembic import op
from neutron.db.migration import cli
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '2f8a1b3c9d40'
down_revision = 'start_networking_srv6_agent'
branch_labels = (cli.EXPAND_BRANCH,)


def upgrade():
    op.create_table(
        'srv6_domains',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('srv6_behavior', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_srv6_domains_project_id'),
                    'srv6_domains', ['project_id'])

    op.create_table(
        'srv6_domain_network_associations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=255), nullable=False),
        sa.Column('domain_id', sa.String(length=36), nullable=False),
        sa.Column('network_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['domain_id'], ['srv6_domains.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['network_id'], ['networks.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'domain_id', 'network_id',
            name='uniq_srv6_domain_network_associations0domain_id0network_id'),
    )
    op.create_index(op.f('ix_srv6_domain_network_associations_project_id'),
                    'srv6_domain_network_associations', ['project_id'])

    # autoincrement=False: the ids are pre-populated from function_id_ranges,
    # not generated. Letting the database assign them would break the whole
    # point of a configured pool.
    op.create_table(
        'srv6_function_allocations',
        sa.Column('function_id', sa.Integer, autoincrement=False,
                  nullable=False),
        sa.Column('domain_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['domain_id'], ['srv6_domains.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('function_id'),
        sa.UniqueConstraint('domain_id'),
    )

    op.create_table(
        'srv6_host_locators',
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('locator', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('host'),
    )

    op.create_table(
        'srv6_te_paths',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=255), nullable=False),
        sa.Column('domain_id', sa.String(length=36), nullable=False),
        sa.Column('destination_host', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['domain_id'], ['srv6_domains.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'domain_id', 'destination_host',
            name='uniq_srv6_te_paths0domain_id0destination_host'),
    )
    op.create_index(op.f('ix_srv6_te_paths_project_id'),
                    'srv6_te_paths', ['project_id'])

    # The hops live in their own table rather than a comma-joined column
    # because the ORDER is the semantic content of a path -- a separate table
    # lets the database enforce that two hops cannot claim the same position.
    op.create_table(
        'srv6_te_path_hops',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('path_id', sa.String(length=36), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('via_host', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['path_id'], ['srv6_te_paths.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path_id', 'position',
                            name='uniq_srv6_te_path_hops0path_id0position'),
    )
