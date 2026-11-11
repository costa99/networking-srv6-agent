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

"""expand te source port

A TE path may be limited to one source VM, and the server creates paths of
its own.

    srv6_te_paths.source_port_id   new, NULL or a port (ON DELETE CASCADE:
                                   a path for a deleted VM is meaningless)
    srv6_te_paths.source_port_key  new, NOT NULL DEFAULT '': the port id or
                                   '', for the unique constraints (NULLs are
                                   distinct there, as for source_host)
    srv6_te_paths.origin           new, NOT NULL DEFAULT 'user'; 'auto' for
                                   the server's own paths
    uniq (domain_id, destination_host, source_host, source_port_key)
    uniq (domain_id, destination_port_id, source_host, source_port_key)
                                   replace 9c244f3b0b07's two constraints,
                                   which would refuse a source-VM path next to
                                   an any-source path to the same destination,
                                   and two VMs of one host to one destination

As in 9c244f3b0b07, the old constraints are dropped here, in expand (new
server code must work after expand alone; old servers write '' and stay
exactly as unique), the new ones are created first (the domain_id foreign
key uses the first index that starts with domain_id, MySQL errno 1553), and
expand_drop_exceptions() lists the dropped names. The names are short: the
long form would pass MySQL's 64-character limit.

Revision ID: e5a7c1b94d36
Revises: d8f3a6c2e914
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e5a7c1b94d36'
down_revision = 'd8f3a6c2e914'

TABLE = 'srv6_te_paths'
OLD_HOST = 'uniq_srv6_te_paths0domain_id0destination_host0source_host'
OLD_PORT = 'uniq_srv6_te_paths0domain_id0destination_port_id0source_host'
NEW_HOST = 'uniq_srv6_te_paths0dst_host0src'
NEW_PORT = 'uniq_srv6_te_paths0dst_port0src'


def upgrade():
    op.add_column(TABLE, sa.Column('source_port_id', sa.String(36),
                                   nullable=True))
    # Unnamed, like every foreign key of this table (6b1e4d2a7c53).
    op.create_foreign_key(None, TABLE, 'ports', ['source_port_id'], ['id'],
                          ondelete='CASCADE')
    op.add_column(TABLE, sa.Column('source_port_key', sa.String(36),
                                   nullable=False, server_default=''))
    op.add_column(TABLE, sa.Column('origin', sa.String(16),
                                   nullable=False, server_default='user'))
    op.create_unique_constraint(
        NEW_HOST, TABLE,
        ['domain_id', 'destination_host', 'source_host', 'source_port_key'])
    op.create_unique_constraint(
        NEW_PORT, TABLE,
        ['domain_id', 'destination_port_id', 'source_host',
         'source_port_key'])
    op.drop_constraint(OLD_HOST, TABLE, type_='unique')
    op.drop_constraint(OLD_PORT, TABLE, type_='unique')


def expand_drop_exceptions():
    """The two constraints the new ones replace (see the module docstring).

    Neutron's migration tests refuse a drop in an expand revision unless it
    is listed here.
    """
    return {
        sa.Constraint: [OLD_HOST, OLD_PORT],
    }
