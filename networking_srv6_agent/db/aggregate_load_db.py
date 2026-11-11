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

"""Measured VM-pair load: one row per measurement.

A row is what one fabric node measured, in one period, for one VM pair on
one link it sends on: the outer SID and the inner addresses, as seen on the
wire. Like link_state_db, rows are never updated, and each batch deletes
its own reporter's rows older than the cut-off, so the table holds about
links x K pairs x two windows / period rows.
"""

from neutron_lib.db import api as db_api
from neutron_lib.db import model_base
import sqlalchemy as sa


class SRv6AggregateLoad(model_base.BASEV2, model_base.HasId):
    """One measurement of one VM pair on one directed link."""
    __tablename__ = 'srv6_aggregate_loads'

    reporter = sa.Column(sa.String(255), nullable=False)
    from_node = sa.Column(sa.String(255), nullable=False)
    to_node = sa.Column(sa.String(255), nullable=False)
    sid = sa.Column(sa.String(39), nullable=False)
    source_address = sa.Column(sa.String(39), nullable=False)
    destination_address = sa.Column(sa.String(39), nullable=False)
    mbps = sa.Column(sa.Float, nullable=False)
    # The server's clock at receipt, never the reporter's
    measured_at = sa.Column(sa.DateTime, nullable=False)

    __table_args__ = (
        sa.Index('ix_srv6_aggregate_loads_reporter_measured_at',
                 'reporter', 'measured_at'),
        model_base.BASEV2.__table_args__,
    )


@db_api.CONTEXT_WRITER
def add_aggregate_loads(context, rows, measured_at, keep_after):
    """Store one batch, after deleting its reporters' rows before keep_after.

    rows: [{'reporter', 'from_node', 'to_node', 'sid', 'source_address',
    'destination_address', 'mbps'}]. One transaction.
    """
    reporters = sorted({r['reporter'] for r in rows})
    context.session.query(SRv6AggregateLoad).filter(
        SRv6AggregateLoad.reporter.in_(reporters),
        SRv6AggregateLoad.measured_at < keep_after).delete(
            synchronize_session=False)
    for r in rows:
        context.session.add(SRv6AggregateLoad(measured_at=measured_at, **r))


@db_api.CONTEXT_READER
def get_aggregate_loads(context, since):
    """Every measurement at or after since, oldest first, as dicts."""
    return [{'reporter': r.reporter, 'from_node': r.from_node,
             'to_node': r.to_node, 'sid': r.sid,
             'source_address': r.source_address,
             'destination_address': r.destination_address,
             'mbps': r.mbps, 'measured_at': r.measured_at}
            for r in context.session.query(SRv6AggregateLoad).filter(
                SRv6AggregateLoad.measured_at >= since).order_by(
                    SRv6AggregateLoad.measured_at).all()]
