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
