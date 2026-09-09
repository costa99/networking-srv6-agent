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

"""The SRv6 domain: the grouping and isolation object.

A domain says "the networks attached here share one VRF and reach each
other over SRv6, across compute nodes". It is what a BGPVPN was in the
original build, minus the federation vocabulary -- no route targets, no
route distinguishers, no import/export. Those are BGP concepts and this
package has no BGP.

Deliberately NOT a standard-attr resource. bgpvpn's associations are, which
buys them description/revision/timestamps and costs a row in
standardattributes plus the registration that goes with it. srv6_te_db set
the precedent of skipping it, and nothing here needs revision numbers.
"""

from neutron_lib.db import api as db_api
from neutron_lib.db import constants as db_const
from neutron_lib.db import model_base
from oslo_log import log as logging
from oslo_utils import uuidutils
import sqlalchemy as sa
from sqlalchemy import orm

# Imported for its side effect: defining SRv6FunctionAllocation registers the
# backref SRv6Domain.srv6_allocation that _make_domain_dict reads below. The
# allocated function id is not a column on this table -- it lives in the pool
# -- and the relationship is how the API layer reaches it without a context
# of its own.
from networking_srv6_agent.db import srv6_db  # noqa: F401


LOG = logging.getLogger(__name__)


class SRv6Domain(model_base.BASEV2, model_base.HasId):
    """One SRv6 domain."""
    __tablename__ = 'srv6_domains'

    # Declared inline rather than through model_base.HasProject so the column
    # can be NOT NULL: a domain with no owner has nothing to authorise
    # against.
    project_id = sa.Column(sa.String(db_const.PROJECT_ID_FIELD_SIZE),
                           index=True, nullable=False)
    name = sa.Column(sa.String(db_const.NAME_FIELD_SIZE), nullable=True)
    # Create-only. Changing the behaviour of a live domain would mean
    # re-programming every node's decap SID while traffic is flowing.
    srv6_behavior = sa.Column(sa.String(16), nullable=False)

    network_associations = orm.relationship(
        'SRv6DomainNetAssociation',
        lazy='joined',
        cascade='all, delete-orphan',
        backref=orm.backref('domain'))


class SRv6DomainNetAssociation(model_base.BASEV2, model_base.HasId):
    """A Neutron network attached to a domain."""
    __tablename__ = 'srv6_domain_network_associations'

    project_id = sa.Column(sa.String(db_const.PROJECT_ID_FIELD_SIZE),
                           index=True, nullable=False)
    domain_id = sa.Column(sa.String(36),
                          sa.ForeignKey('srv6_domains.id',
                                        ondelete='CASCADE'),
                          nullable=False)
    # CASCADE so deleting a Neutron network takes its association with it.
    # Without the FK a deleted network would leave a row pointing at nothing,
    # and the payload builder would keep asking the core plugin for it.
    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id', ondelete='CASCADE'),
                           nullable=False)

    # Assigned to __table_args__, unlike the bgpvpn model this is modelled
    # on: there, `sa.UniqueConstraint(bgpvpn_id, network_id)` sits in the
    # class body as a bare expression and therefore creates no constraint at
    # all. Attaching the same network twice is silently allowed upstream.
    __table_args__ = (
        sa.UniqueConstraint(
            'domain_id', 'network_id',
            name='uniq_srv6_domain_network_associations0domain_id0network_id'
        ),
        model_base.BASEV2.__table_args__,
    )


def _make_assoc_dict(assoc_db, fields=None):
    res = {
        'id': assoc_db.id,
        'project_id': assoc_db.project_id,
        'domain_id': assoc_db.domain_id,
        'network_id': assoc_db.network_id,
    }
    if fields:
        return {k: v for k, v in res.items() if k in fields}
    return res


def _make_domain_dict(domain_db, fields=None):
    """Model to dict.

    `sid_function` comes through the relationship srv6_db defines rather
    than from a column here, so there is one source of truth for the
    allocation. getattr rather than attribute access so a domain whose
    allocation row has gone still serialises.
    """
    allocation = getattr(domain_db, 'srv6_allocation', None)
    res = {
        'id': domain_db.id,
        'project_id': domain_db.project_id,
        'name': domain_db.name,
        'srv6_behavior': domain_db.srv6_behavior,
        'sid_function': allocation.function_id if allocation else None,
        'networks': [a.network_id for a in domain_db.network_associations],
    }
    if fields:
        return {k: v for k, v in res.items() if k in fields}
    return res


def _get_domain_db(context, domain_id):
    return context.session.query(SRv6Domain).filter_by(
        id=domain_id).one_or_none()


@db_api.CONTEXT_WRITER
def create_domain(context, domain):
    """Insert a domain. The caller allocates the function id separately."""
    domain_db = SRv6Domain(
        id=domain.get('id') or uuidutils.generate_uuid(),
        project_id=domain['project_id'],
        name=domain.get('name'),
        srv6_behavior=domain['srv6_behavior'])
    context.session.add(domain_db)
    context.session.flush()
    LOG.info("SRv6 domain %(id)s created for project %(p)s",
             {'id': domain_db.id, 'p': domain_db.project_id})
    return _make_domain_dict(domain_db)


@db_api.CONTEXT_READER
def get_domain(context, domain_id, fields=None):
    domain_db = _get_domain_db(context, domain_id)
    if domain_db is None:
        return None
    return _make_domain_dict(domain_db, fields)


@db_api.CONTEXT_READER
def get_domains(context, filters=None, fields=None):
    query = context.session.query(SRv6Domain)
    for key in ('id', 'project_id', 'name'):
        values = (filters or {}).get(key)
        if values:
            query = query.filter(getattr(SRv6Domain, key).in_(values))
    return [_make_domain_dict(d, fields)
            for d in query.order_by(SRv6Domain.id).all()]


@db_api.CONTEXT_WRITER
def update_domain(context, domain_id, domain):
    """Only `name` is updatable; srv6_behavior is create-only by design."""
    domain_db = _get_domain_db(context, domain_id)
    if domain_db is None:
        return None
    if 'name' in domain:
        domain_db.name = domain['name']
    context.session.flush()
    return _make_domain_dict(domain_db)


@db_api.CONTEXT_WRITER
def delete_domain(context, domain_id):
    """Remove a domain, returning the dict it destroyed.

    Read the dict BEFORE the delete: afterwards there is nothing to build it
    from, and the caller needs the network list to know which agents to tell.
    """
    domain_db = _get_domain_db(context, domain_id)
    if domain_db is None:
        return None
    domain = _make_domain_dict(domain_db)
    context.session.delete(domain_db)
    LOG.info("SRv6 domain %s deleted", domain_id)
    return domain


@db_api.CONTEXT_WRITER
def create_net_assoc(context, domain_id, assoc):
    assoc_db = SRv6DomainNetAssociation(
        id=uuidutils.generate_uuid(),
        project_id=assoc['project_id'],
        domain_id=domain_id,
        network_id=assoc['network_id'])
    context.session.add(assoc_db)
    context.session.flush()
    return _make_assoc_dict(assoc_db)


@db_api.CONTEXT_READER
def get_net_assoc(context, assoc_id, domain_id, fields=None):
    assoc_db = context.session.query(SRv6DomainNetAssociation).filter_by(
        id=assoc_id, domain_id=domain_id).one_or_none()
    if assoc_db is None:
        return None
    return _make_assoc_dict(assoc_db, fields)


@db_api.CONTEXT_READER
def get_net_assocs(context, domain_id, filters=None, fields=None):
    query = context.session.query(SRv6DomainNetAssociation).filter_by(
        domain_id=domain_id)
    for key in ('id', 'network_id', 'project_id'):
        values = (filters or {}).get(key)
        if values:
            query = query.filter(
                getattr(SRv6DomainNetAssociation, key).in_(values))
    return [_make_assoc_dict(a, fields)
            for a in query.order_by(SRv6DomainNetAssociation.id).all()]


@db_api.CONTEXT_WRITER
def delete_net_assoc(context, assoc_id, domain_id):
    assoc_db = context.session.query(SRv6DomainNetAssociation).filter_by(
        id=assoc_id, domain_id=domain_id).one_or_none()
    if assoc_db is None:
        return None
    assoc = _make_assoc_dict(assoc_db)
    context.session.delete(assoc_db)
    return assoc


@db_api.CONTEXT_READER
def get_domain_ids_for_network(context, network_id):
    """Which domains a network belongs to.

    One query per port event, which is why it returns ids rather than dicts:
    the caller re-reads only the domains it is about to push.
    """
    return [row.domain_id for row in
            context.session.query(SRv6DomainNetAssociation.domain_id)
            .filter_by(network_id=network_id).all()]


@db_api.CONTEXT_READER
def get_network_ids(context, domain_id):
    """The networks attached to one domain."""
    return [row.network_id for row in
            context.session.query(SRv6DomainNetAssociation.network_id)
            .filter_by(domain_id=domain_id).all()]
