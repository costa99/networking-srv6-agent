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

"""expand one domain per network

F3: a network belongs to one SRv6 domain.

    uniq (network_id) on srv6_domain_network_associations

The plugin refuses a second domain before the insert; this closes the race
between two concurrent attaches, which the check alone cannot. The old
(domain_id, network_id) constraint is now implied by this one but is kept:
dropping it would be a contract operation, for no gain.

A database that already holds a network in two domains cannot take the
constraint. Rather than pick which association to delete, the upgrade
stops and names them; detach the extra ones and run it again.

Revision ID: c2b7c83c3ad0
Revises: 6b1e4d2a7c53
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c2b7c83c3ad0'
down_revision = '6b1e4d2a7c53'

TABLE = 'srv6_domain_network_associations'


class DuplicateNetworkAssociations(Exception):
    pass


def upgrade():
    duplicates = op.get_bind().execute(sa.text(
        'SELECT network_id, COUNT(*) FROM %s GROUP BY network_id '
        'HAVING COUNT(*) > 1' % TABLE)).fetchall()
    if duplicates:
        raise DuplicateNetworkAssociations(
            'networks attached to more than one SRv6 domain: %s. Remove '
            'the extra network associations, then upgrade again.'
            % ', '.join('%s (%d domains)' % (row[0], row[1])
                        for row in duplicates))
    op.create_unique_constraint(
        'uniq_srv6_domain_network_associations0network_id',
        TABLE, ['network_id'])
