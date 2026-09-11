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

A path says "in this domain, traffic towards <selector> goes via these
transit nodes, in this order". The selector is exactly one of a destination
host -- all of the domain's traffic toward that node -- or a destination
port -- one VM's addresses only (P6, MIGRATION-PLAN.md 8.5). The plugin turns
each path into the prefix of the segment list on the matching encapsulation
routes; without a path, the segment list is just the destination's domain
SID and the packet follows the underlay shortest path.

Two tables rather than one comma-joined column, because the ORDER is the
entire semantic content of the object: a string column makes position
implicit, unconstrainable and unqueryable, and invites exactly the
"which end is the head?" confusion that the SRH's reversed on-wire encoding
already creates.

Nothing here resolves a host to a SID. Hosts stay symbolic in the database
and are resolved against the locator registry every time a payload is
built -- that is what makes a locator change self-healing rather than
something that has to invalidate stored addresses.
"""

from neutron_lib.db import api as db_api
from neutron_lib.db import constants as db_const
from neutron_lib.db import model_base
from oslo_utils import uuidutils
import sqlalchemy as sa
from sqlalchemy import orm


class SRv6TePath(model_base.BASEV2, model_base.HasId):
    """One operator-chosen path: (domain, selector) -> transits."""

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
    # The two selectors. Exactly one is set; the plugin enforces it rather
    # than a CHECK constraint, whose support is uneven across MySQL and
    # MariaDB versions.
    destination_host = sa.Column(sa.String(255), nullable=True)
    # CASCADE for the same reason as domain_id: a path for a deleted VM is
    # meaningless.
    destination_port_id = sa.Column(sa.String(36),
                                    sa.ForeignKey('ports.id',
                                                  ondelete='CASCADE'),
                                    nullable=True)

    # order_by is what makes a read return TRAVERSAL order rather than
    # whatever order the rows happen to come back in.
    hops = orm.relationship(
        'SRv6TePathHop',
        lazy='joined',
        order_by='SRv6TePathHop.position',
        cascade='all, delete-orphan',
        backref=orm.backref('path'))

    __table_args__ = (
        # One path per selector per domain. A second one would give two
        # answers to "how do I reach this", and nothing downstream could
        # choose between them. NULLs are distinct in a unique constraint,
        # so host paths and port paths do not collide on the unset column.
        sa.UniqueConstraint(
            'domain_id', 'destination_host',
            name='uniq_srv6_te_paths0domain_id0destination_host'),
        sa.UniqueConstraint(
            'domain_id', 'destination_port_id',
            name='uniq_srv6_te_paths0domain_id0destination_port_id'),
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

    `segments` and `status` are deliberately left unresolved here. Both need
    the locator registry, the domain's function id and the port's binding --
    none of which this layer has. The plugin fills them in.
    """
    res = {
        'id': path_db.id,
        'project_id': path_db.project_id,
        'domain_id': path_db.domain_id,
        'destination_host': path_db.destination_host,
        'destination_port_id': path_db.destination_port_id,
        'via_hosts': [hop.via_host for hop in path_db.hops],
        'segments': [],
        'status': None,
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
        destination_host=te_path.get('destination_host'),
        destination_port_id=te_path.get('destination_port_id'))
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


_FILTERS = ('id', 'domain_id', 'destination_host', 'destination_port_id',
            'project_id')


def _filtered(query, filters):
    for key in _FILTERS:
        values = (filters or {}).get(key)
        if values:
            query = query.filter(getattr(SRv6TePath, key).in_(values))
    return query


@db_api.CONTEXT_READER
def get_te_paths(context, domain_id, filters=None, fields=None):
    query = _filtered(
        context.session.query(SRv6TePath).filter_by(domain_id=domain_id),
        filters)
    return [_make_te_path_dict(path_db, fields)
            for path_db in query.order_by(SRv6TePath.destination_host,
                                          SRv6TePath.id).all()]


@db_api.CONTEXT_READER
def get_all_te_paths(context, filters=None, fields=None):
    """Every path, across domains: the top-level srv6_te_paths collection."""
    query = _filtered(context.session.query(SRv6TePath), filters)
    return [_make_te_path_dict(path_db, fields)
            for path_db in query.order_by(SRv6TePath.domain_id,
                                          SRv6TePath.id).all()]


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
    # it from, and the caller needs domain_id to know which domain to
    # re-push.
    res = _make_te_path_dict(path_db)
    context.session.delete(path_db)
    return res


@db_api.CONTEXT_READER
def get_te_paths_for_domain(context, domain_id):
    """Both selector maps for one domain.

        {'by_port': {port_id: [via_host, ...]},
         'by_host': {destination_host: [via_host, ...]}}

    One query per payload build, rather than one per route: a domain with
    200 ports would otherwise issue 200 identical lookups. The two maps are
    kept apart because precedence (port path > host path) is decided per
    route by the caller, which is the only one that knows both keys.
    """
    res = {'by_port': {}, 'by_host': {}}
    for path_db in context.session.query(SRv6TePath).filter_by(
            domain_id=domain_id).all():
        via_hosts = [hop.via_host for hop in path_db.hops]
        if path_db.destination_port_id:
            res['by_port'][path_db.destination_port_id] = via_hosts
        elif path_db.destination_host:
            res['by_host'][path_db.destination_host] = via_hosts
    return res


@db_api.CONTEXT_READER
def get_via_hosts(context, domain_id, destination_host):
    """The transits of one host path, or [] if no such path is defined.

    [] is returned both when no path exists and when a path exists with an
    empty via list -- deliberately, because the two mean the same thing to
    the dataplane: no transit SIDs, shortest path. Port paths are not
    consulted; get_te_paths_for_domain is the lookup that knows both.
    """
    path_db = context.session.query(SRv6TePath).filter_by(
        domain_id=domain_id, destination_host=destination_host).one_or_none()
    if path_db is None:
        return []
    return [hop.via_host for hop in path_db.hops]
