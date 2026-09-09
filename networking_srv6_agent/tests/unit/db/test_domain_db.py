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

"""Unit tests for the SRv6 domain and its network associations.

A note on skips, carried over from the module this replaces: neutron's
SqlTestCase turns a DBReferenceError into a *skip* rather than a failure, so
a broken foreign key hides as a skipped test. This module must report zero
skips -- which matters here more than anywhere, because the whole point of
these tables is that two foreign keys were retargeted off bgpvpns.id.
"""

from neutron.db import models_v2
from neutron.tests.unit import testlib_api
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib.db import standard_attr
from oslo_db import exception as db_exc
from oslo_utils import uuidutils

from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import srv6_db


class DomainDbTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.ctx = n_context.get_admin_context()

    def _create_domain(self, name='red', project_id='test-project'):
        return domain_db.create_domain(
            self.ctx, {'project_id': project_id, 'name': name,
                       'srv6_behavior': 'End.DT46'})

    def _create_network(self):
        """A real networks row.

        The association has a foreign key onto networks and the test
        database enforces it. Networks are a standard-attr resource, so the
        standardattributes row has to exist first.
        """
        with db_api.CONTEXT_WRITER.using(self.ctx):
            std = standard_attr.StandardAttribute(resource_type='networks')
            self.ctx.session.add(std)
            self.ctx.session.flush()
            net = models_v2.Network(id=uuidutils.generate_uuid(),
                                    project_id='test-project',
                                    name='net', admin_state_up=True,
                                    status='ACTIVE',
                                    standard_attr_id=std.id)
            self.ctx.session.add(net)
            self.ctx.session.flush()
            return net.id

    def _associate(self, domain_id, network_id=None):
        return domain_db.create_net_assoc(
            self.ctx, domain_id,
            {'project_id': 'test-project',
             'network_id': network_id or self._create_network()})


class TestDomainCrud(DomainDbTestCase):

    def test_create_returns_the_domain(self):
        domain = self._create_domain()
        self.assertEqual('red', domain['name'])
        self.assertEqual('End.DT46', domain['srv6_behavior'])
        self.assertEqual('test-project', domain['project_id'])
        self.assertEqual([], domain['networks'])

    def test_sid_function_is_none_until_one_is_allocated(self):
        # The id lives in the pool, not on this table; a domain that has not
        # been through the allocator reports None rather than raising.
        self.assertIsNone(self._create_domain()['sid_function'])

    def test_sid_function_comes_through_the_relationship(self):
        domain = self._create_domain()
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        fid = srv6_db.allocate_function_id(self.ctx, domain['id'])
        fetched = domain_db.get_domain(self.ctx, domain['id'])
        self.assertEqual(fid, fetched['sid_function'])

    def test_get_round_trips(self):
        created = self._create_domain()
        self.assertEqual(created, domain_db.get_domain(self.ctx,
                                                       created['id']))

    def test_get_missing_returns_none(self):
        self.assertIsNone(
            domain_db.get_domain(self.ctx, uuidutils.generate_uuid()))

    def test_list_filters_by_project(self):
        self._create_domain(name='a', project_id='p1')
        self._create_domain(name='b', project_id='p2')
        found = domain_db.get_domains(self.ctx,
                                      filters={'project_id': ['p1']})
        self.assertEqual(['a'], [d['name'] for d in found])

    def test_a_project_may_own_several_domains(self):
        # MIGRATION-PLAN 8.2: many per project, not one implicit domain.
        self._create_domain(name='prod')
        self._create_domain(name='dev')
        self.assertEqual(2, len(domain_db.get_domains(self.ctx)))

    def test_update_changes_the_name(self):
        domain = self._create_domain()
        updated = domain_db.update_domain(self.ctx, domain['id'],
                                          {'name': 'blue'})
        self.assertEqual('blue', updated['name'])

    def test_update_cannot_change_the_behavior(self):
        # Create-only by design: re-programming a live domain's decap SID
        # while traffic flows is not something a PUT should be able to do.
        domain = self._create_domain()
        domain_db.update_domain(self.ctx, domain['id'],
                                {'srv6_behavior': 'End.DT4'})
        self.assertEqual('End.DT46',
                         domain_db.get_domain(self.ctx,
                                              domain['id'])['srv6_behavior'])

    def test_update_missing_returns_none(self):
        self.assertIsNone(domain_db.update_domain(
            self.ctx, uuidutils.generate_uuid(), {'name': 'x'}))

    def test_delete_returns_the_dict_it_destroyed(self):
        domain = self._create_domain()
        deleted = domain_db.delete_domain(self.ctx, domain['id'])
        self.assertEqual(domain['id'], deleted['id'])
        self.assertIsNone(domain_db.get_domain(self.ctx, domain['id']))

    def test_delete_missing_returns_none(self):
        self.assertIsNone(
            domain_db.delete_domain(self.ctx, uuidutils.generate_uuid()))


class TestNetworkAssociations(DomainDbTestCase):

    def test_associate_lists_the_network_on_the_domain(self):
        domain = self._create_domain()
        assoc = self._associate(domain['id'])
        fetched = domain_db.get_domain(self.ctx, domain['id'])
        self.assertEqual([assoc['network_id']], fetched['networks'])

    def test_the_same_network_cannot_be_attached_twice(self):
        # The constraint the model this is based on failed to create: there
        # `sa.UniqueConstraint(bgpvpn_id, network_id)` sits in the class body
        # as a bare expression and produces no constraint at all.
        domain = self._create_domain()
        net_id = self._create_network()
        self._associate(domain['id'], net_id)
        self.assertRaises(
            db_exc.DBDuplicateEntry,
            self._associate, domain['id'], net_id)

    def test_one_network_may_be_in_two_domains(self):
        net_id = self._create_network()
        a = self._create_domain(name='a')
        b = self._create_domain(name='b')
        self._associate(a['id'], net_id)
        self._associate(b['id'], net_id)
        self.assertEqual(
            {a['id'], b['id']},
            set(domain_db.get_domain_ids_for_network(self.ctx, net_id)))

    def test_get_assoc_is_scoped_to_its_domain(self):
        a = self._create_domain(name='a')
        b = self._create_domain(name='b')
        assoc = self._associate(a['id'])
        self.assertIsNone(
            domain_db.get_net_assoc(self.ctx, assoc['id'], b['id']))

    def test_delete_assoc_returns_the_dict(self):
        domain = self._create_domain()
        assoc = self._associate(domain['id'])
        deleted = domain_db.delete_net_assoc(self.ctx, assoc['id'],
                                             domain['id'])
        self.assertEqual(assoc['id'], deleted['id'])
        self.assertEqual([], domain_db.get_network_ids(self.ctx,
                                                       domain['id']))

    def test_delete_assoc_missing_returns_none(self):
        domain = self._create_domain()
        self.assertIsNone(domain_db.delete_net_assoc(
            self.ctx, uuidutils.generate_uuid(), domain['id']))

    def test_deleting_the_domain_cascades_its_associations(self):
        domain = self._create_domain()
        self._associate(domain['id'])
        domain_db.delete_domain(self.ctx, domain['id'])
        rows = self.ctx.session.query(
            domain_db.SRv6DomainNetAssociation).filter_by(
                domain_id=domain['id']).all()
        self.assertEqual([], rows)

    def test_deleting_the_network_cascades_its_association(self):
        # The FK onto networks.id exists so a deleted network cannot leave a
        # row pointing at nothing for the payload builder to chase.
        domain = self._create_domain()
        net_id = self._create_network()
        self._associate(domain['id'], net_id)
        with db_api.CONTEXT_WRITER.using(self.ctx):
            self.ctx.session.query(models_v2.Network).filter_by(
                id=net_id).delete()
        self.assertEqual([], domain_db.get_network_ids(self.ctx,
                                                       domain['id']))


class TestAllocationLifetime(DomainDbTestCase):

    def test_deleting_a_domain_frees_its_function_id(self):
        # ondelete='SET NULL' is the safety net under the explicit release:
        # even if the plugin never calls release_function_id, the id returns
        # to the pool rather than leaking out of it permanently.
        domain = self._create_domain()
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        fid = srv6_db.allocate_function_id(self.ctx, domain['id'])
        domain_db.delete_domain(self.ctx, domain['id'])
        rows = {row.function_id: row.domain_id for row in
                self.ctx.session.query(srv6_db.SRv6FunctionAllocation).all()}
        self.assertIsNone(rows[fid])
