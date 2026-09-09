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

"""Unit tests for operator-defined SRv6 traffic-engineering paths.

A note on skips: neutron's SqlTestCase turns a DBReferenceError into a
*skip* rather than a failure, so a broken foreign key hides as a skipped
test. This module must report zero skips.
"""

from neutron.tests.unit import testlib_api
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from oslo_db import exception as db_exc
from oslo_utils import uuidutils

# Imported so the srv6_domains table exists: the path table has a foreign
# key onto it and the test database enforces that.
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import te_db


class Srv6TeDbTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.ctx = n_context.get_admin_context()
        self.domain_id = self._create_domain()

    def _create_domain(self):
        return domain_db.create_domain(
            self.ctx, {'project_id': 'test-project',
                       'name': 'd', 'srv6_behavior': 'End.DT46'})['id']

    def _create_path(self, destination_host='cn3', via_hosts=None,
                     domain_id=None):
        return te_db.create_te_path(
            self.ctx, domain_id or self.domain_id,
            {'project_id': 'test-project',
             'destination_host': destination_host,
             'via_hosts': via_hosts if via_hosts is not None else ['cn2']})


class TestCrud(Srv6TeDbTestCase):

    def test_create_returns_the_path(self):
        path = self._create_path()
        self.assertEqual('cn3', path['destination_host'])
        self.assertEqual(['cn2'], path['via_hosts'])
        self.assertEqual(self.domain_id, path['domain_id'])

    def test_segments_are_not_resolved_by_the_db_layer(self):
        # Resolving a SID needs the locator registry and the VPN's function
        # id; this layer has neither. The driver decorates the dict.
        self.assertEqual([], self._create_path()['segments'])

    def test_get_round_trips(self):
        created = self._create_path(via_hosts=['cn2', 'cn4'])
        fetched = te_db.get_te_path(self.ctx, created['id'], self.domain_id)
        self.assertEqual(created, fetched)

    def test_get_is_scoped_to_its_vpn(self):
        # A path id from another VPN must not be readable through this one.
        created = self._create_path()
        other_vpn = self._create_domain()
        self.assertIsNone(
            te_db.get_te_path(self.ctx, created['id'], other_vpn))

    def test_get_missing_returns_none(self):
        self.assertIsNone(te_db.get_te_path(
            self.ctx, uuidutils.generate_uuid(), self.domain_id))

    def test_list_only_returns_this_vpns_paths(self):
        self._create_path(destination_host='cn3')
        other_vpn = self._create_domain()
        self._create_path(destination_host='cn9', domain_id=other_vpn)
        paths = te_db.get_te_paths(self.ctx, self.domain_id)
        self.assertEqual(['cn3'], [p['destination_host'] for p in paths])

    def test_delete_returns_the_dict_it_destroyed(self):
        # The postcommit hook needs domain_id to know which VPN to re-push,
        # and after the delete there is nothing left to build it from.
        created = self._create_path()
        deleted = te_db.delete_te_path(self.ctx, created['id'],
                                       self.domain_id)
        self.assertEqual(self.domain_id, deleted['domain_id'])
        self.assertEqual('cn3', deleted['destination_host'])
        self.assertIsNone(te_db.get_te_path(self.ctx, created['id'],
                                            self.domain_id))

    def test_delete_missing_returns_none(self):
        self.assertIsNone(te_db.delete_te_path(
            self.ctx, uuidutils.generate_uuid(), self.domain_id))


class TestHopOrdering(Srv6TeDbTestCase):
    """Order is the entire content of a path, so it gets its own case."""

    def test_via_hosts_come_back_in_traversal_order(self):
        created = self._create_path(via_hosts=['cn2', 'cn4', 'cn5'])
        fetched = te_db.get_te_path(self.ctx, created['id'], self.domain_id)
        self.assertEqual(['cn2', 'cn4', 'cn5'], fetched['via_hosts'])

    def test_order_is_not_alphabetical(self):
        # The guard against a relationship that happens to look sorted.
        created = self._create_path(via_hosts=['cn9', 'cn1', 'cn5'])
        fetched = te_db.get_te_path(self.ctx, created['id'], self.domain_id)
        self.assertEqual(['cn9', 'cn1', 'cn5'], fetched['via_hosts'])

    def test_positions_are_zero_based_and_contiguous(self):
        created = self._create_path(via_hosts=['cn2', 'cn4', 'cn5'])
        rows = self.ctx.session.query(te_db.SRv6TePathHop).filter_by(
            path_id=created['id']).all()
        self.assertEqual([0, 1, 2], sorted(r.position for r in rows))

    def test_two_hops_cannot_share_a_position(self):
        created = self._create_path(via_hosts=['cn2'])

        def _add_colliding_hop():
            # The whole writer block is inside assertRaises so the context
            # manager sees the exception and rolls back. Catching it inside
            # the block instead leaves the session poisoned and the failure
            # surfaces later as an unrelated PendingRollbackError.
            with db_api.CONTEXT_WRITER.using(self.ctx):
                self.ctx.session.add(te_db.SRv6TePathHop(
                    id=uuidutils.generate_uuid(), path_id=created['id'],
                    position=0, via_host='cn4'))

        self.assertRaises(db_exc.DBDuplicateEntry, _add_colliding_hop)


class TestUpdate(Srv6TeDbTestCase):

    def test_update_replaces_rather_than_appends(self):
        created = self._create_path(via_hosts=['cn2', 'cn4'])
        updated = te_db.update_te_path(
            self.ctx, created['id'], self.domain_id, {'via_hosts': ['cn5']})
        self.assertEqual(['cn5'], updated['via_hosts'])

    def test_update_leaves_no_orphan_hops(self):
        created = self._create_path(via_hosts=['cn2', 'cn4', 'cn5'])
        te_db.update_te_path(self.ctx, created['id'], self.domain_id,
                             {'via_hosts': ['cn9']})
        rows = self.ctx.session.query(te_db.SRv6TePathHop).filter_by(
            path_id=created['id']).all()
        self.assertEqual(['cn9'], [r.via_host for r in rows])

    def test_update_to_empty_un_steers_the_path(self):
        # The row stays; it now says "explicitly the shortest path". This is
        # what an operator PUTs to un-steer without a delete window.
        created = self._create_path(via_hosts=['cn2'])
        updated = te_db.update_te_path(
            self.ctx, created['id'], self.domain_id, {'via_hosts': []})
        self.assertEqual([], updated['via_hosts'])
        self.assertIsNotNone(te_db.get_te_path(self.ctx, created['id'],
                                               self.domain_id))

    def test_update_without_via_hosts_leaves_them_alone(self):
        created = self._create_path(via_hosts=['cn2'])
        updated = te_db.update_te_path(self.ctx, created['id'],
                                       self.domain_id, {})
        self.assertEqual(['cn2'], updated['via_hosts'])

    def test_update_missing_returns_none(self):
        self.assertIsNone(te_db.update_te_path(
            self.ctx, uuidutils.generate_uuid(), self.domain_id,
            {'via_hosts': []}))


class TestConstraintsAndCascades(Srv6TeDbTestCase):

    def test_one_path_per_destination_per_vpn(self):
        self._create_path(destination_host='cn3')
        self.assertRaises(db_exc.DBDuplicateEntry,
                          self._create_path, destination_host='cn3')

    def test_the_same_destination_in_another_vpn_is_fine(self):
        self._create_path(destination_host='cn3')
        other_vpn = self._create_domain()
        path = self._create_path(destination_host='cn3',
                                 domain_id=other_vpn)
        self.assertEqual(other_vpn, path['domain_id'])

    def test_deleting_a_path_cascades_its_hops(self):
        created = self._create_path(via_hosts=['cn2', 'cn4'])
        te_db.delete_te_path(self.ctx, created['id'], self.domain_id)
        rows = self.ctx.session.query(te_db.SRv6TePathHop).filter_by(
            path_id=created['id']).all()
        self.assertEqual([], rows)

    def test_deleting_the_domain_cascades_its_paths(self):
        self._create_path()
        with db_api.CONTEXT_WRITER.using(self.ctx):
            self.ctx.session.query(domain_db.SRv6Domain).filter_by(
                id=self.domain_id).delete()
        self.assertEqual([], te_db.get_te_paths(self.ctx, self.domain_id))


class TestPayloadHelpers(Srv6TeDbTestCase):
    """The two lookups the driver uses when building RPC payloads."""

    def test_paths_for_domain_maps_destination_to_via_hosts(self):
        self._create_path(destination_host='cn3', via_hosts=['cn2'])
        self._create_path(destination_host='cn4', via_hosts=['cn2', 'cn5'])
        self.assertEqual(
            {'cn3': ['cn2'], 'cn4': ['cn2', 'cn5']},
            te_db.get_te_paths_for_vpn(self.ctx, self.domain_id))

    def test_paths_for_vpn_is_empty_without_policy(self):
        self.assertEqual(
            {}, te_db.get_te_paths_for_vpn(self.ctx, self.domain_id))

    def test_via_hosts_for_one_destination(self):
        self._create_path(destination_host='cn3', via_hosts=['cn2', 'cn5'])
        self.assertEqual(
            ['cn2', 'cn5'],
            te_db.get_via_hosts(self.ctx, self.domain_id, 'cn3'))

    def test_via_hosts_is_empty_for_an_unsteered_destination(self):
        self.assertEqual(
            [], te_db.get_via_hosts(self.ctx, self.domain_id, 'cn3'))

    def test_no_path_and_an_empty_path_are_indistinguishable(self):
        # Deliberate: both mean "no transit SIDs" to the dataplane.
        self._create_path(destination_host='cn3', via_hosts=[])
        self.assertEqual(
            te_db.get_via_hosts(self.ctx, self.domain_id, 'cn3'),
            te_db.get_via_hosts(self.ctx, self.domain_id, 'cn-unsteered'))
