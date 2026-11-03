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

"""Measured link state: one row per measurement (PLAN-LINK-STATE-TE L9).

A row is what one node (reporter) measured on one directed link
(from_node -> to_node) in one period. Rows are never updated: the history
is what gives the peak over a window and "N samples in a row", and it lives
in the database, not in a process, so every API worker reads the same
numbers.

The table stays small by itself: each batch deletes its OWN reporters' rows
older than the cut-off. A node that stops posting keeps its last rows, which
is how the server tells "reported, then went silent" (down, L8) from "never
reported". About 32 directed links x 2 ends x 2 windows / period rows on
the Clos test bed.
"""

from neutron_lib.db import api as db_api
from neutron_lib.db import model_base
import sqlalchemy as sa


class SRv6LinkState(model_base.BASEV2, model_base.HasId):
    """One measurement of one directed link by one node."""
    __tablename__ = 'srv6_link_states'

    reporter = sa.Column(sa.String(255), nullable=False)
    from_node = sa.Column(sa.String(255), nullable=False)
    to_node = sa.Column(sa.String(255), nullable=False)
    up = sa.Column(sa.Boolean, nullable=False)
    # NULL: no rate for that period (a counter went backwards)
    mbps = sa.Column(sa.Float, nullable=True)
    drops = sa.Column(sa.BigInteger, nullable=True)
    # The server's clock at receipt, never the reporter's (L8)
    measured_at = sa.Column(sa.DateTime, nullable=False)

    # the pruning of add_link_states reads by reporter and age
    __table_args__ = (
        sa.Index('ix_srv6_link_states_reporter_measured_at',
                 'reporter', 'measured_at'),
        model_base.BASEV2.__table_args__,
    )


@db_api.CONTEXT_WRITER
def add_link_states(context, rows, measured_at, keep_after):
    """Store one batch, after deleting its reporters' rows before keep_after.

    rows: [{'reporter', 'from_node', 'to_node', 'up', 'mbps', 'drops'}].
    One transaction: the batch is stored whole or not at all.
    """
    reporters = sorted({r['reporter'] for r in rows})
    context.session.query(SRv6LinkState).filter(
        SRv6LinkState.reporter.in_(reporters),
        SRv6LinkState.measured_at < keep_after).delete(
            synchronize_session=False)
    for r in rows:
        context.session.add(SRv6LinkState(measured_at=measured_at, **r))


@db_api.CONTEXT_READER
def get_link_states(context):
    """Every stored measurement, oldest first, as column tuples.

    Rows with attribute access (r.mbps, r.measured_at), not ORM objects:
    the re-optimisation reads the whole table every pass, and building one
    mapped object and one dict per row was most of its CPU
    (PLAN-C4-OPTIMISATION O2: 5x cheaper with the read-once of O1).
    """
    return context.session.query(
        SRv6LinkState.reporter, SRv6LinkState.from_node,
        SRv6LinkState.to_node, SRv6LinkState.up, SRv6LinkState.mbps,
        SRv6LinkState.drops, SRv6LinkState.measured_at).order_by(
            SRv6LinkState.measured_at).all()


@db_api.CONTEXT_READER
def get_last_seen(context):
    """{reporter: its newest measured_at}.

    One grouped query, cheap enough for every payload build
    (Srv6Plugin._dead_hosts).
    """
    return dict(context.session.query(
        SRv6LinkState.reporter, sa.func.max(SRv6LinkState.measured_at)
    ).group_by(SRv6LinkState.reporter).all())
