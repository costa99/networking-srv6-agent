# Copyright 2026 networking-srv6-agent contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Operator-defined SRv6 paths: which transit nodes to route through.

A path says "in this domain, traffic towards <destination_host> goes via these
transit nodes, in this order". The driver turns each one into the prefix of
the segment list on every encapsulation route towards that host; without a
path, the segment list is just the destination's domain SID and the packet
follows the underlay shortest path.

Two tables rather than one comma-joined column, because the ORDER is the
entire semantic content of the object: a string column makes position
implicit, unconstrainable and unqueryable, and invites exactly the
"which end is the head?" confusion that the SRH's reversed on-wire encoding
already creates.

Nothing here resolves a host to a SID. Hosts stay symbolic all the way to
the agent, which resolves them against the locator registry -- that is what
makes a locator change self-healing rather than something that has to
invalidate stored addresses.
"""

from neutron_lib.db import api as db_api
from neutron_lib.db import constants as db_const
from neutron_lib.db import model_base
from oslo_utils import uuidutils
import sqlalchemy as sa
from sqlalchemy import orm


class SRv6TePath(model_base.BASEV2, model_base.HasId):
    """One operator-chosen path: (vpn, destination host) -> transits."""

    __tablename__ = 'srv6_te_paths'

    # Declared inline rather than reusing a shared mixin so this module
    # needs no import of domain_db. Three lines against an import cycle
    # risk is the right trade.
    project_id = sa.Column(sa.String(db_const.PROJECT_ID_FIELD_SIZE),
                           index=True, nullable=False)
    # CASCADE, not SET NULL: a path with no domain is meaningless. This
    # differs from the function-id allocation, whose SET NULL is a
    # pool-reclamation safety net rather than a lifetime statement.
    domain_id = sa.Column(sa.String(36),
                          sa.ForeignKey('srv6_domains.id',
                                        ondelete='CASCADE'),
                          nullable=False)
    destination_host = sa.Column(sa.String(255), nullable=False)

    # order_by is what makes a read return TRAVERSAL order rather than
    # whatever order the rows happen to come back in.
    hops = orm.relationship(
        'SRv6TePathHop',
        lazy='joined',
        order_by='SRv6TePathHop.position',
        cascade='all, delete-orphan',
        backref=orm.backref('path'))

    __table_args__ = (
        # One path per destination per domain. A second one would give two
        # answers to "how do I reach this host", and nothing downstream
        # could choose between them.
        sa.UniqueConstraint(
            'domain_id', 'destination_host',
            name='uniq_srv6_te_paths0domain_id0destination_host'),
        model_base.BASEV2.__table_args__,
    )


class SRv6TePathHop(model_base.BASEV2, model_base.HasId):
    """One transit node. `position` is 0-based TRAVERSAL order."""

    __tablename__ = 'srv6_te_path_hops'

    path_id = sa.Column(
        sa.String(36),
        sa.ForeignKey('srv6_te_paths.id', ondelete='CASCADE'),
        nullable=False)
    position = sa.Column(sa.Integer, nullable=False)
    via_host = sa.Column(sa.String(255), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            'path_id', 'position',
            name='uniq_srv6_te_path_hops0path_id0position'),
        model_base.BASEV2.__table_args__,
    )


def _make_te_path_dict(path_db, fields=None):
    """Model to dict.

    `segments` is deliberately left empty here. Resolving it needs the
    locator registry, the domain's function id and vrf_table_base -- none of
    which this layer has. The driver fills it in.
    """
    res = {
        'id': path_db.id,
        'project_id': path_db.project_id,
        'domain_id': path_db.domain_id,
        'destination_host': path_db.destination_host,
        'via_hosts': [hop.via_host for hop in path_db.hops],
        'segments': [],
    }
    if fields:
        return {k: v for k, v in res.items() if k in fields}
    return res


def _get_path_db(context, path_id, domain_id):
    query = context.session.query(SRv6TePath).filter_by(id=path_id)
    if domain_id is not None:
        query = query.filter_by(domain_id=domain_id)
    return query.one_or_none()


def _set_hops(context, path_db, via_hosts):
    """Replace the hop set wholesale.

    Delete-and-reinsert rather than diff, mirroring update_port_assoc: the
    positions of everything after an inserted or removed hop shift anyway,
    so a diff saves nothing and gets the unique (path_id, position)
    constraint wrong halfway through.

    The old rows are cleared AND FLUSHED before the new ones are attached.
    Without that intervening flush SQLAlchemy is free to order the inserts
    ahead of the deletes inside one flush, and the new position 0 collides
    with the old position 0 on the unique constraint -- which is a
    DBDuplicateEntry raised from an ordinary update, not from anything the
    caller did wrong.
    """
    if path_db.hops:
        path_db.hops = []
        context.session.flush()
    path_db.hops = [
        SRv6TePathHop(id=uuidutils.generate_uuid(),
                      path_id=path_db.id,
                      position=position,
                      via_host=via_host)
        for position, via_host in enumerate(via_hosts or [])
    ]


@db_api.CONTEXT_WRITER
def create_te_path(context, domain_id, te_path):
    path_db = SRv6TePath(
        id=uuidutils.generate_uuid(),
        project_id=te_path['project_id'],
        domain_id=domain_id,
        destination_host=te_path['destination_host'])
    context.session.add(path_db)
    context.session.flush()
    _set_hops(context, path_db, te_path.get('via_hosts'))
    context.session.flush()
    return _make_te_path_dict(path_db)


@db_api.CONTEXT_READER
def get_te_path(context, path_id, domain_id=None, fields=None):
    path_db = _get_path_db(context, path_id, domain_id)
    if path_db is None:
        return None
    return _make_te_path_dict(path_db, fields)


@db_api.CONTEXT_READER
def get_te_paths(context, domain_id, filters=None, fields=None):
    query = context.session.query(SRv6TePath).filter_by(domain_id=domain_id)
    for key in ('id', 'destination_host', 'project_id'):
        values = (filters or {}).get(key)
        if values:
            query = query.filter(getattr(SRv6TePath, key).in_(values))
    return [_make_te_path_dict(path_db, fields)
            for path_db in query.order_by(SRv6TePath.destination_host).all()]


@db_api.CONTEXT_WRITER
def update_te_path(context, path_id, domain_id, te_path):
    path_db = _get_path_db(context, path_id, domain_id)
    if path_db is None:
        return None
    if 'via_hosts' in te_path:
        _set_hops(context, path_db, te_path['via_hosts'])
        context.session.flush()
        # Without the refresh the relationship can still hold the deleted
        # hop objects, so the returned dict describes the OLD path.
        context.session.refresh(path_db)
    return _make_te_path_dict(path_db)


@db_api.CONTEXT_WRITER
def delete_te_path(context, path_id, domain_id=None):
    path_db = _get_path_db(context, path_id, domain_id)
    if path_db is None:
        return None
    # Read the dict BEFORE the delete: afterwards there is nothing to build
    # it from, and the caller's postcommit hook needs domain_id to know
    # which domain to re-push.
    res = _make_te_path_dict(path_db)
    context.session.delete(path_db)
    return res


@db_api.CONTEXT_READER
def get_te_paths_for_vpn(context, domain_id):
    """{destination_host: [via_host, ...]} for one domain.

    One query per payload build, rather than one per route: a domain with 200
    ports would otherwise issue 200 identical lookups.
    """
    return {
        path_db.destination_host: [hop.via_host for hop in path_db.hops]
        for path_db in context.session.query(SRv6TePath).filter_by(
            domain_id=domain_id).all()
    }


@db_api.CONTEXT_READER
def get_via_hosts(context, domain_id, destination_host):
    """The transits for one destination, or [] if no path is defined.

    [] is returned both when no path exists and when a path exists with an
    empty via list -- deliberately, because the two mean the same thing to
    the dataplane: no transit SIDs, shortest path.
    """
    path_db = context.session.query(SRv6TePath).filter_by(
        domain_id=domain_id, destination_host=destination_host).one_or_none()
    if path_db is None:
        return []
    return [hop.via_host for hop in path_db.hops]
