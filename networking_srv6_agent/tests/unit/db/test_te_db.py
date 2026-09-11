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
test. This module must report zero skips -- and since P6 the paths hold a
second foreign key, onto ports.
"""

from neutron.db import models_v2
from neutron.tests.unit import testlib_api
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib.db import standard_attr
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
                     domain_id=None, destination_port_id=None):
        return te_db.create_te_path(
            self.ctx, domain_id or self.domain_id,
            {'project_id': 'test-project',
             'destination_host': destination_host,
             'destination_port_id': destination_port_id,
             'via_hosts': via_hosts if via_hosts is not None else ['cn2']})

    def _create_port(self):
        """A real ports row, on a real network, for the port selector's FK.

        Both are standard-attr resources, so each needs its
        standardattributes row first (as in test_domain_db).
        """
        with db_api.CONTEXT_WRITER.using(self.ctx):
            net_std = standard_attr.StandardAttribute(
                resource_type='networks')
            port_std = standard_attr.StandardAttribute(resource_type='ports')
            self.ctx.session.add_all([net_std, port_std])
            self.ctx.session.flush()
            net = models_v2.Network(id=uuidutils.generate_uuid(),
                                    project_id='test-project', name='net',
                                    admin_state_up=True, status='ACTIVE',
                                    standard_attr_id=net_std.id)
            self.ctx.session.add(net)
            self.ctx.session.flush()
            port = models_v2.Port(id=uuidutils.generate_uuid(),
                                  project_id='test-project', name='vm',
                                  network_id=net.id,
                                  mac_address='fa:16:3e:%02x:%02x:%02x' % (
                                      port_std.id % 256, 0, 1),
                                  admin_state_up=True, status='ACTIVE',
                                  device_id='vm', device_owner='compute:nova',
                                  standard_attr_id=port_std.id)
            self.ctx.session.add(port)
            self.ctx.session.flush()
            return port.id

    def _create_port_path(self, port_id=None, via_hosts=None,
                          domain_id=None):
        return self._create_path(destination_host=None,
                                 destination_port_id=(port_id or
                                                      self._create_port()),
                                 via_hosts=via_hosts, domain_id=domain_id)


class TestCrud(Srv6TeDbTestCase):

    def test_create_returns_the_path(self):
        path = self._create_path()
        self.assertEqual('cn3', path['destination_host'])
        self.assertIsNone(path['destination_port_id'])
        self.assertEqual(['cn2'], path['via_hosts'])
        self.assertEqual(self.domain_id, path['domain_id'])

    def test_segments_and_status_are_not_resolved_by_the_db_layer(self):
        # Resolving either needs the locator registry, the domain's function
        # id and the port binding; this layer has none of them.
        path = self._create_path()
        self.assertEqual([], path['segments'])
        self.assertIsNone(path['status'])

    def test_get_round_trips(self):
        created = self._create_path(via_hosts=['cn2', 'cn4'])
        fetched = te_db.get_te_path(self.ctx, created['id'], self.domain_id)
        self.assertEqual(created, fetched)

    def test_get_is_scoped_to_its_domain_when_asked(self):
        # A path id from another domain must not be readable through this
        # one.
        created = self._create_path()
        other_domain = self._create_domain()
        self.assertIsNone(
            te_db.get_te_path(self.ctx, created['id'], other_domain))

    def test_get_without_a_domain_finds_it_anywhere(self):
        # The top-level API knows only the path id.
        created = self._create_path()
        self.assertEqual(created, te_db.get_te_path(self.ctx, created['id']))

    def test_get_missing_returns_none(self):
        self.assertIsNone(te_db.get_te_path(
            self.ctx, uuidutils.generate_uuid(), self.domain_id))

    def test_list_only_returns_this_domains_paths(self):
        self._create_path(destination_host='cn3')
        other_domain = self._create_domain()
        self._create_path(destination_host='cn9', domain_id=other_domain)
        paths = te_db.get_te_paths(self.ctx, self.domain_id)
        self.assertEqual(['cn3'], [p['destination_host'] for p in paths])

    def test_delete_returns_the_dict_it_destroyed(self):
        # The caller needs domain_id to know which domain to re-push, and
        # after the delete there is nothing left to build it from.
        created = self._create_path()
        deleted = te_db.delete_te_path(self.ctx, created['id'])
        self.assertEqual(self.domain_id, deleted['domain_id'])
        self.assertEqual('cn3', deleted['destination_host'])
        self.assertIsNone(te_db.get_te_path(self.ctx, created['id']))

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

    def test_update_without_a_domain(self):
        # What the top-level PUT does: it knows only the path id.
        created = self._create_path(via_hosts=['cn2'])
        updated = te_db.update_te_path(self.ctx, created['id'], None,
                                       {'via_hosts': ['cn4']})
        self.assertEqual(['cn4'], updated['via_hosts'])

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

    def test_one_path_per_destination_per_domain(self):
        self._create_path(destination_host='cn3')
        self.assertRaises(db_exc.DBDuplicateEntry,
                          self._create_path, destination_host='cn3')

    def test_the_same_destination_in_another_domain_is_fine(self):
        self._create_path(destination_host='cn3')
        other_domain = self._create_domain()
        path = self._create_path(destination_host='cn3',
                                 domain_id=other_domain)
        self.assertEqual(other_domain, path['domain_id'])

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


class TestPortSelector(Srv6TeDbTestCase):
    """P6: a path may select one port instead of a whole host."""

    def test_a_port_path_has_no_host(self):
        port_id = self._create_port()
        path = self._create_port_path(port_id)
        self.assertEqual(port_id, path['destination_port_id'])
        self.assertIsNone(path['destination_host'])

    def test_one_path_per_port_per_domain(self):
        port_id = self._create_port()
        self._create_port_path(port_id)
        self.assertRaises(db_exc.DBDuplicateEntry,
                          self._create_port_path, port_id)

    def test_the_same_port_in_another_domain_is_fine(self):
        port_id = self._create_port()
        self._create_port_path(port_id)
        other_domain = self._create_domain()
        path = self._create_port_path(port_id, domain_id=other_domain)
        self.assertEqual(other_domain, path['domain_id'])

    def test_unset_selectors_do_not_collide(self):
        # NULLs are distinct in a unique constraint: many port paths share
        # a NULL destination_host, many host paths a NULL port.
        self._create_port_path()
        self._create_port_path()
        self._create_path(destination_host='cn3')
        self._create_path(destination_host='cn4')
        self.assertEqual(4, len(te_db.get_te_paths(self.ctx,
                                                   self.domain_id)))

    def test_a_port_that_does_not_exist_is_refused(self):
        # The foreign key, doing its job -- and a failure here rather than
        # a skip is what shows the constraint is really enforced.
        self.assertRaises(db_exc.DBReferenceError,
                          self._create_port_path, uuidutils.generate_uuid())

    def test_deleting_the_port_cascades_its_path(self):
        # A path for a deleted VM is meaningless.
        port_id = self._create_port()
        path = self._create_port_path(port_id, via_hosts=['cn2'])
        with db_api.CONTEXT_WRITER.using(self.ctx):
            self.ctx.session.query(models_v2.Port).filter_by(
                id=port_id).delete()
        self.assertIsNone(te_db.get_te_path(self.ctx, path['id']))
        self.assertEqual([], self.ctx.session.query(
            te_db.SRv6TePathHop).filter_by(path_id=path['id']).all())


class TestGetAllTePaths(Srv6TeDbTestCase):
    """The top-level collection, across domains."""

    def setUp(self):
        super().setUp()
        self.other_domain = self._create_domain()
        self.port_id = self._create_port()
        self.host_path = self._create_path(destination_host='cn3')
        self.port_path = self._create_port_path(self.port_id)
        self.other_path = self._create_path(destination_host='cn3',
                                            domain_id=self.other_domain)

    def _ids(self, **filters):
        return sorted(p['id'] for p in te_db.get_all_te_paths(self.ctx,
                                                              filters))

    def test_every_domain(self):
        self.assertEqual(sorted([self.host_path['id'],
                                 self.port_path['id'],
                                 self.other_path['id']]), self._ids())

    def test_by_domain(self):
        self.assertEqual([self.other_path['id']],
                         self._ids(domain_id=[self.other_domain]))

    def test_by_destination_host(self):
        self.assertEqual(sorted([self.host_path['id'],
                                 self.other_path['id']]),
                         self._ids(destination_host=['cn3']))

    def test_by_destination_port(self):
        self.assertEqual([self.port_path['id']],
                         self._ids(destination_port_id=[self.port_id]))

    def test_by_id_and_project(self):
        self.assertEqual([self.host_path['id']],
                         self._ids(id=[self.host_path['id']]))
        self.assertEqual([], self._ids(project_id=['someone-else']))

    def test_fields(self):
        paths = te_db.get_all_te_paths(self.ctx, fields=['id'])
        self.assertEqual([{'id'}], list({frozenset(p) for p in paths}))


class TestPayloadHelpers(Srv6TeDbTestCase):
    """The lookups the plugin uses when building RPC payloads."""

    def test_paths_for_domain_keeps_the_two_selectors_apart(self):
        port_id = self._create_port()
        self._create_path(destination_host='cn3', via_hosts=['cn2'])
        self._create_path(destination_host='cn4', via_hosts=['cn2', 'cn5'])
        self._create_port_path(port_id, via_hosts=['cn5'])
        self.assertEqual(
            {'by_port': {port_id: ['cn5']},
             'by_host': {'cn3': ['cn2'], 'cn4': ['cn2', 'cn5']}},
            te_db.get_te_paths_for_domain(self.ctx, self.domain_id))

    def test_paths_for_domain_is_empty_without_policy(self):
        self.assertEqual(
            {'by_port': {}, 'by_host': {}},
            te_db.get_te_paths_for_domain(self.ctx, self.domain_id))

    def test_paths_for_domain_is_scoped_to_the_domain(self):
        other_domain = self._create_domain()
        self._create_path(destination_host='cn3', domain_id=other_domain)
        self.assertEqual(
            {'by_port': {}, 'by_host': {}},
            te_db.get_te_paths_for_domain(self.ctx, self.domain_id))

    def test_an_explicitly_direct_path_is_still_listed(self):
        # [] is a decision -- "direct" -- and must win over a host path for
        # its port, so it cannot simply be left out of the map.
        port_id = self._create_port()
        self._create_port_path(port_id, via_hosts=[])
        self.assertEqual({port_id: []}, te_db.get_te_paths_for_domain(
            self.ctx, self.domain_id)['by_port'])

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
