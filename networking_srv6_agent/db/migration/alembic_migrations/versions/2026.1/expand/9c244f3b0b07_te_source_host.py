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

"""expand te source host

A TE path may name the ingress node it applies to (FEATURE-TE-SOURCE-HOST).

    srv6_te_paths.source_host     new, NOT NULL DEFAULT '': '' is "any
                                  ingress", the meaning of every existing row
    uniq (domain_id, destination_host, source_host)
    uniq (domain_id, destination_port_id, source_host)
                                  replace the two (domain_id, <destination>)
                                  constraints, which would refuse a source
                                  path next to an any-source path to the same
                                  destination

'' rather than NULL because NULLs are distinct in a unique constraint: two
"any source" paths to one destination would both be accepted.

The old constraints are dropped HERE, in expand, not in a contract revision.
Neutron's rule is that new server code must work once expand alone has run,
and with the old constraints still in place it would answer 409 to the very
thing this revision exists for. The drop is safe for servers still running
the old code: their rows get source_host '', so under the new constraints
they are exactly as unique as before, and they still get their 409s. This is
the pattern of neutron's own d72db3e25539 (stein, port forwarding), and the
reason expand_drop_exceptions() below lists the two names. (c2b7c83c3ad0 kept
an old constraint to stay out of contract; there it was only redundant, here
it is wrong.)

Order matters on MySQL. The domain_id foreign key has no index of its own: it
uses uniq(domain_id, destination_host), the first index that starts with
domain_id, and an index a foreign key needs cannot be dropped (errno 1553).
So the new constraints, which also start with domain_id, are created first.
The destination_port_id foreign key got its own index when 6b1e4d2a7c53
created it, so it does not depend on either constraint.

Revision ID: 9c244f3b0b07
Revises: c2b7c83c3ad0
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '9c244f3b0b07'
down_revision = 'c2b7c83c3ad0'

TABLE = 'srv6_te_paths'
OLD_HOST = 'uniq_srv6_te_paths0domain_id0destination_host'
OLD_PORT = 'uniq_srv6_te_paths0domain_id0destination_port_id'


def upgrade():
    op.add_column(TABLE, sa.Column('source_host', sa.String(length=255),
                                   nullable=False, server_default=''))
    op.create_unique_constraint(
        'uniq_srv6_te_paths0domain_id0destination_host0source_host',
        TABLE, ['domain_id', 'destination_host', 'source_host'])
    op.create_unique_constraint(
        'uniq_srv6_te_paths0domain_id0destination_port_id0source_host',
        TABLE, ['domain_id', 'destination_port_id', 'source_host'])
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
