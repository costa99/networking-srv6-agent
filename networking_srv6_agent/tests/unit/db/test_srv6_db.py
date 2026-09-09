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

"""Unit tests for the SID function id pool and the host locator registry."""

import datetime
from unittest import mock

from neutron.tests import base
from neutron.tests.unit import testlib_api
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api

# The allocation table has a foreign key onto srv6_domains, so that model
# has to be registered before the schema is created.
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import srv6_db


class Srv6DbTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.ctx = n_context.get_admin_context()

    def _create_domain(self):
        """A real srv6_domains row.

        The allocation table has a foreign key onto srv6_domains and the
        test database enforces it, so an allocation cannot be made against
        an id that does not exist -- which is also true in production and is
        the reason the FK is there.

        Simpler than the domain fixture this replaces: a domain is not a
        standard-attr resource, so there is no standardattributes row to
        create first.
        """
        return domain_db.create_domain(
            self.ctx, {'project_id': 'test-project',
                       'name': 'd', 'srv6_behavior': 'End.DT46'})['id']

    def _rows(self):
        return {row.function_id: row.domain_id for row in
                self.ctx.session.query(srv6_db.SRv6FunctionAllocation).all()}


class TestParseFunctionIdRanges(base.BaseTestCase):

    def test_single_range(self):
        self.assertEqual([(1, 4095)],
                         srv6_db.parse_function_id_ranges(['1:4095']))

    def test_several_ranges_and_whitespace(self):
        self.assertEqual(
            [(1, 10), (100, 200)],
            srv6_db.parse_function_id_ranges(['1:10', ' 100 : 200 ']))

    def test_single_id_range(self):
        self.assertEqual([(5, 5)], srv6_db.parse_function_id_ranges(['5:5']))

    def test_malformed_entry(self):
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['1-10'])

    def test_non_numeric_entry(self):
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['a:b'])

    def test_reversed_range(self):
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['10:1'])

    def test_zero_is_not_allocatable(self):
        # Function id 0 makes the SID equal to the bare locator, which is
        # the node's own address block rather than a VPN.
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['0:10'])

    def test_topology_range_is_refused(self):
        # A VPN allocated an id in 0xf000-0xffff would collide with a node's
        # own End SID.
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['61440:61450'])

    def test_range_ending_in_the_topology_range_is_refused(self):
        self.assertRaises(ValueError,
                          srv6_db.parse_function_id_ranges, ['1:65535'])


class TestAllocationPool(Srv6DbTestCase):

    def test_sync_populates_every_configured_id(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3), (10, 11)])
        self.assertEqual({1: None, 2: None, 3: None, 10: None, 11: None},
                         self._rows())

    def test_sync_is_idempotent(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        self.assertEqual({1: None, 2: None, 3: None}, self._rows())

    def test_sync_removes_free_ids_that_left_the_configuration(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        srv6_db.sync_allocation_pool(self.ctx, [(1, 2)])
        self.assertEqual({1: None, 2: None}, self._rows())

    def test_sync_keeps_an_allocated_id_that_left_the_configuration(self):
        # Deleting it would orphan a live VPN's VRF on every compute node.
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        vpn = self._create_domain()
        with mock.patch.object(srv6_db.random, 'shuffle',
                               side_effect=lambda x: x.sort()):
            self.assertEqual(1, srv6_db.allocate_function_id(self.ctx, vpn))
        srv6_db.sync_allocation_pool(self.ctx, [(2, 3)])
        self.assertEqual({1: vpn, 2: None, 3: None}, self._rows())

    def test_sync_extends_an_existing_pool(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 2)])
        srv6_db.sync_allocation_pool(self.ctx, [(1, 4)])
        self.assertEqual({1: None, 2: None, 3: None, 4: None}, self._rows())


class TestAllocateAndRelease(Srv6DbTestCase):

    def setUp(self):
        super().setUp()
        self.domain = self._create_domain()
        self.other_domain = self._create_domain()

    def test_allocate_marks_the_row(self):
        srv6_db.sync_allocation_pool(self.ctx, [(7, 7)])
        self.assertEqual(
            7, srv6_db.allocate_function_id(self.ctx, self.domain))
        self.assertEqual({7: self.domain}, self._rows())

    def test_allocate_stays_inside_the_configured_range(self):
        srv6_db.sync_allocation_pool(self.ctx, [(100, 200)])
        function_id = srv6_db.allocate_function_id(self.ctx, self.domain)
        self.assertGreaterEqual(function_id, 100)
        self.assertLessEqual(function_id, 200)

    def test_two_domains_get_different_ids(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 10)])
        first = srv6_db.allocate_function_id(self.ctx, self.domain)
        second = srv6_db.allocate_function_id(self.ctx, self.other_domain)
        self.assertNotEqual(first, second)

    def test_exhausted_pool_raises(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 1)])
        srv6_db.allocate_function_id(self.ctx, self.domain)
        self.assertRaises(srv6_db.NoFunctionIdAvailable,
                          srv6_db.allocate_function_id,
                          self.ctx, self.other_domain)

    def test_empty_pool_raises(self):
        self.assertRaises(srv6_db.NoFunctionIdAvailable,
                          srv6_db.allocate_function_id, self.ctx, self.domain)

    def test_get_function_id(self):
        srv6_db.sync_allocation_pool(self.ctx, [(7, 7)])
        srv6_db.allocate_function_id(self.ctx, self.domain)
        self.assertEqual(7, srv6_db.get_function_id(self.ctx, self.domain))
        self.assertIsNone(srv6_db.get_function_id(self.ctx, self.other_domain))

    def test_release_frees_the_id_for_reuse(self):
        srv6_db.sync_allocation_pool(self.ctx, [(7, 7)])
        srv6_db.allocate_function_id(self.ctx, self.domain)
        self.assertEqual(7, srv6_db.release_function_id(self.ctx, self.domain))
        self.assertEqual({7: None}, self._rows())
        self.assertEqual(7, srv6_db.allocate_function_id(self.ctx,
                                                         self.other_domain))

    def test_release_of_an_unknown_domain_returns_none(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 3)])
        self.assertIsNone(srv6_db.release_function_id(self.ctx, self.domain))

    def test_release_after_the_foreign_key_nulled_the_row_returns_none(self):
        """Regression: the FK is why the driver cannot rely on release().

        domain_id has ondelete='SET NULL', so deleting the VPN clears the
        allocation row before delete_domain_postcommit runs. release() then
        finds nothing and returns None -- which the driver must not read as
        "this VPN had no SID", because agents still hold its VRF. Here the
        database's action is simulated directly; the driver-side half of
        this regression is in test_driver.py.
        """
        srv6_db.sync_allocation_pool(self.ctx, [(7, 7)])
        srv6_db.allocate_function_id(self.ctx, self.domain)
        with db_api.CONTEXT_WRITER.using(self.ctx):
            self.ctx.session.query(
                srv6_db.SRv6FunctionAllocation).filter_by(
                    function_id=7).update({'domain_id': None})
        self.assertIsNone(srv6_db.release_function_id(self.ctx, self.domain))

    def test_compare_and_swap_retries_when_an_id_is_taken_in_the_race(self):
        """The candidate list is read before the UPDATE, so it can go stale.

        Between the free-list SELECT and the compare-and-swap another server
        may have taken the id this one picked. The domain_id IS NULL in the
        WHERE clause is what turns that into an UPDATE matching zero rows,
        and the loop must then try the next candidate rather than returning
        an id somebody else owns.
        """
        srv6_db.sync_allocation_pool(self.ctx, [(1, 2)])
        thief = self._create_domain()
        stolen = []

        def steal_the_first_candidate(candidates):
            candidates.sort()
            self.ctx.session.query(
                srv6_db.SRv6FunctionAllocation).filter_by(
                    function_id=candidates[0]).update({'domain_id': thief})
            stolen.append(candidates[0])

        with mock.patch.object(srv6_db.random, 'shuffle',
                               side_effect=steal_the_first_candidate):
            allocated = srv6_db.allocate_function_id(self.ctx, self.domain)

        self.assertEqual([1], stolen)
        self.assertEqual(2, allocated)
        self.assertEqual({1: thief, 2: self.domain}, self._rows())

    def test_compare_and_swap_raises_when_the_last_id_is_taken(self):
        srv6_db.sync_allocation_pool(self.ctx, [(1, 1)])
        thief = self._create_domain()

        def steal_the_only_candidate(candidates):
            self.ctx.session.query(
                srv6_db.SRv6FunctionAllocation).filter_by(
                    function_id=candidates[0]).update({'domain_id': thief})

        with mock.patch.object(srv6_db.random, 'shuffle',
                               side_effect=steal_the_only_candidate):
            self.assertRaises(srv6_db.NoFunctionIdAvailable,
                              srv6_db.allocate_function_id,
                              self.ctx, self.domain)


class TestHostLocators(Srv6DbTestCase):

    def _now(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def test_registers_a_locator(self):
        srv6_db.set_host_locator(self.ctx, 'node1', 'fc00:0:1::/48',
                                 self._now())
        self.assertEqual({'node1': 'fc00:0:1::/48'},
                         srv6_db.get_host_locators(self.ctx))

    def test_no_locators_registered(self):
        self.assertEqual({}, srv6_db.get_host_locators(self.ctx))

    def test_second_report_from_the_same_host_updates_in_place(self):
        srv6_db.set_host_locator(self.ctx, 'node1', 'fc00:0:1::/48',
                                 self._now())
        srv6_db.set_host_locator(self.ctx, 'node1', 'fc00:0:9::/48',
                                 self._now())
        self.assertEqual({'node1': 'fc00:0:9::/48'},
                         srv6_db.get_host_locators(self.ctx))

    def test_several_hosts(self):
        srv6_db.set_host_locator(self.ctx, 'node1', 'fc00:0:1::/48',
                                 self._now())
        srv6_db.set_host_locator(self.ctx, 'node2', 'fc00:0:2::/48',
                                 self._now())
        self.assertEqual({'node1': 'fc00:0:1::/48',
                          'node2': 'fc00:0:2::/48'},
                         srv6_db.get_host_locators(self.ctx))

    def test_updated_at_is_recorded(self):
        when = self._now()
        srv6_db.set_host_locator(self.ctx, 'node1', 'fc00:0:1::/48', when)
        row = self.ctx.session.query(srv6_db.SRv6HostLocator).filter_by(
            host='node1').one()
        self.assertIsNotNone(row.updated_at)
