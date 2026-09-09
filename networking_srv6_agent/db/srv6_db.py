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

"""SID function id pool and per-node locator registry.

The allocator is the ML2 tunnel-type pattern (vni_ranges -> VxlanAllocation)
transplanted: the pool table is pre-populated with every id in the configured
ranges, and allocation is a compare-and-swap UPDATE rather than a
SELECT-then-INSERT. Two simultaneous domain-create requests therefore cannot
be handed the same id -- one of the two UPDATEs matches zero rows and retries.
"""

import random

from neutron_lib.db import api as db_api
from neutron_lib.db import model_base
from oslo_log import log as logging
import sqlalchemy as sa
from sqlalchemy import orm

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import sid


LOG = logging.getLogger(__name__)


class SRv6FunctionAllocation(model_base.BASEV2):
    """One row per allocatable SID function id."""
    __tablename__ = 'srv6_function_allocations'

    function_id = sa.Column(sa.Integer, nullable=False, primary_key=True,
                            autoincrement=False)
    # NULL means free. UNIQUE means a domain cannot hold two ids, which
    # turns a double-allocation bug into an IntegrityError instead of a
    # silent leak. ondelete='SET NULL' is a safety net under the explicit
    # release the plugin performs: if it ever fails to release an id, the
    # database frees it when the domain row goes rather than leaking it out
    # of the pool permanently.
    domain_id = sa.Column(sa.String(36),
                          sa.ForeignKey('srv6_domains.id',
                                        ondelete='SET NULL'),
                          nullable=True, unique=True)

    # Gives SRv6Domain.srv6_allocation, so the API layer can expose the
    # allocated function id without needing a context of its own.
    domain = orm.relationship(
        'SRv6Domain', backref=orm.backref('srv6_allocation', uselist=False))


class SRv6HostLocator(model_base.BASEV2):
    """The locator each agent reported for its node.

    This is how the server knows where to point encap routes, and is
    analogous to the way l2pop learns tunnel endpoints from the agent DB.
    A host that never called sync_state has no row, and ports on it are
    skipped rather than advertised with a guessed SID.
    """
    __tablename__ = 'srv6_host_locators'

    host = sa.Column(sa.String(255), nullable=False, primary_key=True)
    locator = sa.Column(sa.String(64), nullable=False)
    updated_at = sa.Column(sa.DateTime, nullable=True)


def parse_function_id_ranges(range_strings):
    """['1:4095'] -> [(1, 4095)], validated against the reserved range."""
    ranges = []
    for entry in range_strings:
        try:
            start, end = (int(x.strip()) for x in entry.split(':'))
        except ValueError:
            raise ValueError(_("invalid function_id_ranges entry %r, "
                               "expected <start>:<end>") % entry)
        if start > end:
            raise ValueError(_("invalid function_id_ranges entry %r: "
                               "start is greater than end") % entry)
        if start < 1:
            raise ValueError(_("invalid function_id_ranges entry %r: "
                               "function id 0 is not allocatable") % entry)
        if sid.is_topology_function_id(start) or \
                sid.is_topology_function_id(end):
            raise ValueError(
                _("function_id_ranges entry %r overlaps the range reserved "
                  "for per-node topology SIDs (0xf000-0xffff). A domain "
                  "allocated an id in that range would collide with a "
                  "node's "
                  "own End SID.") % entry)
        ranges.append((start, end))
    return ranges


@db_api.CONTEXT_WRITER
def sync_allocation_pool(context, ranges):
    """Make the pool table match the configured ranges.

    Adds rows for newly configured ids and removes rows for ids that have
    left the configuration *and are free*. An id that has left the config
    but is still allocated is kept and logged: silently deleting it would
    orphan a live domain's VRF on every compute node.
    """
    wanted = set()
    for start, end in ranges:
        wanted.update(range(start, end + 1))

    existing = {row.function_id: row for row in
                context.session.query(SRv6FunctionAllocation).all()}

    for function_id in sorted(wanted - set(existing)):
        context.session.add(
            SRv6FunctionAllocation(function_id=function_id, domain_id=None))

    stale = set(existing) - wanted
    for function_id in sorted(stale):
        row = existing[function_id]
        if row.domain_id is None:
            context.session.delete(row)
        else:
            LOG.warning("SRv6 function id %(fid)s is allocated to domain "
                        "%(dom)s but is no longer in function_id_ranges; "
                        "keeping the allocation. Delete the domain to "
                        "release it.",
                        {'fid': function_id, 'dom': row.domain_id})

    LOG.info("SRv6 function id pool synchronised: %(n)d id(s) configured",
             {'n': len(wanted)})


@db_api.CONTEXT_WRITER
def allocate_function_id(context, domain_id):
    """Claim a free function id for a domain. Returns the id.

    Round-robin rather than lowest-free: reusing a just-released id
    immediately would risk colliding with a node that has not yet processed
    the domain_deleted message and still holds the old VRF.
    """
    query = context.session.query(SRv6FunctionAllocation).filter_by(
        domain_id=None)
    free = [row.function_id for row in query.all()]
    if not free:
        raise NoFunctionIdAvailable()

    random.shuffle(free)
    for function_id in free:
        # Compare-and-swap: the domain_id=None in the WHERE clause is what
        # makes this safe against a concurrent allocation of the same id.
        updated = context.session.query(SRv6FunctionAllocation).filter_by(
            function_id=function_id, domain_id=None).update(
                {'domain_id': domain_id})
        if updated:
            LOG.debug("allocated SRv6 function id %(fid)s to domain %(dom)s",
                      {'fid': function_id, 'dom': domain_id})
            return function_id
    raise NoFunctionIdAvailable()


@db_api.CONTEXT_WRITER
def release_function_id(context, domain_id):
    """Free this domain's id. Returns it, or None if it held none."""
    row = context.session.query(SRv6FunctionAllocation).filter_by(
        domain_id=domain_id).first()
    if row is None:
        return None
    function_id = row.function_id
    row.domain_id = None
    LOG.debug("released SRv6 function id %(fid)s from domain %(dom)s",
              {'fid': function_id, 'dom': domain_id})
    return function_id


@db_api.CONTEXT_READER
def get_function_id(context, domain_id):
    row = context.session.query(SRv6FunctionAllocation).filter_by(
        domain_id=domain_id).first()
    return row.function_id if row else None


@db_api.CONTEXT_WRITER
def set_host_locator(context, host, locator, when):
    """Upsert the locator an agent reported."""
    row = context.session.query(SRv6HostLocator).filter_by(host=host).first()
    if row is None:
        context.session.add(SRv6HostLocator(host=host, locator=locator,
                                            updated_at=when))
        LOG.info("SRv6 agent on %(host)s registered locator %(loc)s",
                 {'host': host, 'loc': locator})
        return
    if row.locator != locator:
        LOG.warning("SRv6 agent on %(host)s changed locator from %(old)s to "
                    "%(new)s; remote nodes will be told on their next resync",
                    {'host': host, 'old': row.locator, 'new': locator})
    row.locator = locator
    row.updated_at = when


@db_api.CONTEXT_READER
def get_host_locators(context):
    """{host: locator} for every node that has ever registered."""
    return {row.host: row.locator
            for row in context.session.query(SRv6HostLocator).all()}


class NoFunctionIdAvailable(Exception):
    """The configured function id pool is exhausted."""
