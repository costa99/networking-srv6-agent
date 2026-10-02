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

"""Unit tests for the SID arithmetic.

sid.py is the one place where a locator, a function id and a table number
are turned into each other. The server and the agent both go through it, so
a bug here is a bug that makes two nodes disagree about which SID means
which domain -- silently, because both sides still produce syntactically valid
addresses. Hence the emphasis on exact expected strings rather than
round-trip properties.
"""

import ipaddress

from neutron.tests import base

from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid


LOCATOR = 'fc00:0:1::/48'


class TestParseLocator(base.BaseTestCase):

    def test_valid_48(self):
        net, prefixlen = sid.parse_locator(LOCATOR)
        self.assertEqual(48, prefixlen)
        self.assertEqual('fc00:0:1::/48', str(net))

    def test_valid_112_is_the_longest_that_fits(self):
        # 112 + 16 function bits = exactly 128.
        net, prefixlen = sid.parse_locator('fc00:0:1:2:3:4:5::/112')
        self.assertEqual(112, prefixlen)

    def test_too_long_for_the_function_field(self):
        self.assertRaises(sid.InvalidLocator,
                          sid.parse_locator, 'fc00:0:1:2:3:4:5::/120')

    def test_host_bits_set_is_rejected(self):
        # strict=True: 'fc00:0:1::5/48' names an address, not a locator, and
        # accepting it would silently drop the ::5.
        self.assertRaises(sid.InvalidLocator,
                          sid.parse_locator, 'fc00:0:1::5/48')

    def test_ipv4_is_rejected(self):
        self.assertRaises(sid.InvalidLocator,
                          sid.parse_locator, '10.0.0.0/24')

    def test_garbage_is_rejected(self):
        self.assertRaises(sid.InvalidLocator, sid.parse_locator, 'not-a-sid')

    def test_none_is_rejected(self):
        self.assertRaises(sid.InvalidLocator, sid.parse_locator, None)


class TestSidFor(base.BaseTestCase):

    def test_known_vector(self):
        self.assertEqual('fc00:0:1:7::', sid.format_sid(LOCATOR, 7))

    def test_function_id_is_hexadecimal_in_the_address(self):
        # The trap the module docstring warns about: function id 17 is
        # written :11: in the SID but is table 10017, and both name the
        # same domain.
        self.assertEqual('fc00:0:1:11::', sid.format_sid(LOCATOR, 17))
        self.assertEqual(10017, sid.vrf_table(17, 10000))

    def test_function_id_zero_is_the_locator_itself(self):
        self.assertEqual('fc00:0:1::', sid.format_sid(LOCATOR, 0))

    def test_highest_function_id_in_the_field(self):
        self.assertEqual('fc00:0:1:ffff::', sid.format_sid(LOCATOR, 0xffff))

    def test_locator_longer_than_48(self):
        # With a /64 the function field sits in the fifth hextet.
        self.assertEqual('fc00:0:1:2:7::',
                         sid.format_sid('fc00:0:1:2::/64', 7))

    def test_two_locators_same_function_id_differ(self):
        # This is the whole point of a per-node locator: the same domain has a
        # different SID on every node.
        self.assertNotEqual(sid.format_sid('fc00:0:1::/48', 7),
                            sid.format_sid('fc00:0:2::/48', 7))

    def test_function_id_too_large(self):
        self.assertRaises(sid.InvalidLocator,
                          sid.sid_for, LOCATOR, 1 << constants.FUNCTION_BITS)

    def test_function_id_negative(self):
        self.assertRaises(sid.InvalidLocator, sid.sid_for, LOCATOR, -1)

    def test_bad_locator_propagates(self):
        self.assertRaises(sid.InvalidLocator, sid.sid_for, 'nonsense', 1)


class TestNaming(base.BaseTestCase):

    def test_vrf_table_is_derived_from_the_base(self):
        self.assertEqual(10007, sid.vrf_table(7, 10000))
        self.assertEqual(20007, sid.vrf_table(7, 20000))

    def test_vrf_name(self):
        self.assertEqual('sv6vrf-7', sid.vrf_name(7))

    def test_gateway_port_name(self):
        self.assertEqual('svp-7-100', sid.gateway_port_name(7, 100))

    def test_names_fit_in_ifnamsiz(self):
        # IFNAMSIZ is 16 including the NUL, so 15 usable characters. The
        # worst case is the highest allocatable function id with the highest
        # local VLAN; if this fails the device simply cannot be created.
        worst_vrf = sid.vrf_name(constants.MAX_DOMAIN_FUNCTION_ID)
        worst_port = sid.gateway_port_name(constants.MAX_DOMAIN_FUNCTION_ID,
                                           4094)
        self.assertLessEqual(len(worst_vrf), 15)
        self.assertLessEqual(len(worst_port), 15)

    def test_names_are_distinct_per_vpn(self):
        self.assertNotEqual(sid.vrf_name(7), sid.vrf_name(8))
        self.assertNotEqual(sid.gateway_port_name(7, 100),
                            sid.gateway_port_name(7, 101))


class TestEncapOverhead(base.BaseTestCase):

    def test_single_segment(self):
        # outer IPv6 (40) + SRH fixed (8) + one 16-byte segment.
        self.assertEqual(64, sid.encap_overhead())
        self.assertEqual(64, sid.encap_overhead(1))

    def test_grows_per_segment(self):
        self.assertEqual(80, sid.encap_overhead(2))
        self.assertEqual(sid.encap_overhead(1) + constants.SRH_PER_SEGMENT,
                         sid.encap_overhead(2))


class TestTopologyFunctionIds(base.BaseTestCase):

    def test_vpn_range_is_not_topology(self):
        self.assertFalse(sid.is_topology_function_id(
            constants.MIN_DOMAIN_FUNCTION_ID))
        self.assertFalse(sid.is_topology_function_id(
            constants.MAX_DOMAIN_FUNCTION_ID))

    def test_boundaries(self):
        self.assertFalse(sid.is_topology_function_id(
            constants.MIN_TOPOLOGY_FUNCTION_ID - 1))
        self.assertTrue(sid.is_topology_function_id(
            constants.MIN_TOPOLOGY_FUNCTION_ID))
        self.assertTrue(sid.is_topology_function_id(
            constants.MAX_TOPOLOGY_FUNCTION_ID))

    def test_ranges_do_not_overlap(self):
        self.assertLess(constants.MAX_DOMAIN_FUNCTION_ID,
                        constants.MIN_TOPOLOGY_FUNCTION_ID)


# ----------------------------------------------------------------------
# microSID (USID-PLAN.md)
# ----------------------------------------------------------------------
def _loc(node):
    return 'fc00:0:%x::/48' % node


class TestReducedOverhead(base.BaseTestCase):
    """H.Encaps.Red: the first pushed element travels only in the DA."""

    def test_one_element_pushes_no_srh(self):
        self.assertEqual(40, sid.encap_overhead(1, reduced=True))

    def test_more_carry_all_but_the_first(self):
        self.assertEqual(64, sid.encap_overhead(2, reduced=True))
        self.assertEqual(80, sid.encap_overhead(3, reduced=True))

    def test_full_encap_is_unchanged(self):
        self.assertEqual(64, sid.encap_overhead(1, reduced=False))


class TestUsidLocator(base.BaseTestCase):

    def test_node_sid_is_the_locator_address(self):
        self.assertEqual('fc00:0:3::', sid.node_sid(_loc(3)))

    def test_node_id_and_block(self):
        self.assertEqual(3, sid.usid_node_id(_loc(3)))
        self.assertEqual('fc00::/32', sid.usid_block(_loc(3)))

    def test_lib_prefix_covers_exactly_the_lib(self):
        self.assertEqual('fc00:0:3:e000::/52', sid.lib_prefix(_loc(3)))
        net = ipaddress.IPv6Network(sid.lib_prefix(_loc(3)))
        self.assertIn(ipaddress.IPv6Address(
            sid.format_sid(_loc(3), constants.USID_LIB_MIN)), net)
        self.assertIn(ipaddress.IPv6Address(
            sid.format_sid(_loc(3), constants.USID_LIB_MAX)), net)
        self.assertNotIn(ipaddress.IPv6Address(
            sid.format_sid(_loc(3), constants.USID_LIB_MIN - 1)), net)
        # The End SID stays outside it.
        self.assertNotIn(ipaddress.IPv6Address(
            sid.format_sid(_loc(3), constants.NODE_END_FUNCTION_ID)), net)

    def test_only_a_48_is_f3216(self):
        self.assertRaises(sid.InvalidLocator, sid.node_sid,
                          'fc00:0:1:2::/64')

    def test_node_must_be_in_the_gib(self):
        sid.check_usid_locator(_loc(constants.USID_GIB_MAX))
        self.assertRaises(sid.InvalidLocator, sid.check_usid_locator,
                          'fc00::/48')
        self.assertRaises(sid.InvalidLocator, sid.check_usid_locator,
                          _loc(constants.USID_LIB_MIN))

    def test_lib_and_topology_ranges_are_disjoint(self):
        self.assertLess(constants.USID_GIB_MAX, constants.USID_LIB_MIN)
        self.assertLess(constants.USID_LIB_MAX,
                        constants.MIN_TOPOLOGY_FUNCTION_ID)

    def test_lib_names_fit_in_ifnamsiz(self):
        self.assertLessEqual(len(sid.vrf_name(constants.USID_LIB_MAX)), 15)
        self.assertLessEqual(
            len(sid.gateway_port_name(constants.USID_LIB_MAX, 4094)), 15)


class TestPackNextCsid(base.BaseTestCase):
    """The containers the ingress pushes (RFC 9800 s6.2, USID-PLAN.md D9)."""

    DECAP = 'fc00:0:8:e001::'

    def _path(self, k):
        return [sid.node_sid(_loc(i)) for i in range(1, k + 1)] + [self.DECAP]

    def test_direct_is_the_decap_sid_itself(self):
        self.assertEqual([self.DECAP], sid.pack_next_csid([self.DECAP]))

    def test_one_waypoint(self):
        self.assertEqual(['fc00:0:1:8:e001::'],
                         sid.pack_next_csid(self._path(1)))

    def test_four_waypoints_fill_the_container_exactly(self):
        self.assertEqual(['fc00:0:1:2:3:4:8:e001'],
                         sid.pack_next_csid(self._path(4)))

    def test_the_decap_pair_never_straddles_two_containers(self):
        # Five vias + the pair is 7 C-SIDs: the pair moves whole into the
        # second container, which is then a plain block:node:function SID.
        self.assertEqual(['fc00:0:1:2:3:4:5:0', self.DECAP],
                         sid.pack_next_csid(self._path(5)))

    def test_six_waypoints(self):
        self.assertEqual(['fc00:0:1:2:3:4:5:6', self.DECAP],
                         sid.pack_next_csid(self._path(6)))

    def test_overhead_matches_the_plan_table(self):
        # USID-PLAN.md s3: k -> (encap today, usid).
        expected = {0: (64, 40), 1: (80, 40), 2: (96, 40), 4: (128, 40),
                    5: (144, 64), 6: (160, 64)}
        for k, (full, usid) in expected.items():
            packed = sid.pack_next_csid(self._path(k))
            self.assertEqual(full, sid.encap_overhead(k + 1), k)
            self.assertEqual(usid, sid.encap_overhead(len(packed),
                                                      reduced=True), k)

    def test_different_blocks_cannot_share_a_container(self):
        self.assertRaises(sid.InvalidLocator, sid.pack_next_csid,
                          ['fc00:0:1::', 'fc01:0:3:e005::'])

    def test_a_sid_with_a_gap_is_refused(self):
        # A zero C-SID ends the container: fc00:0:3:0:5:: cannot be carried.
        self.assertRaises(sid.InvalidLocator, sid.pack_next_csid,
                          ['fc00:0:3:0:5::'])

    def test_the_bare_block_is_refused(self):
        self.assertRaises(sid.InvalidLocator, sid.pack_next_csid,
                          ['fc00::'])

    def test_garbage_is_refused(self):
        self.assertRaises(sid.InvalidLocator, sid.pack_next_csid, ['nope'])
