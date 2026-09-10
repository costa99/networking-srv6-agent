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

"""Argument-vector tests for the privileged seg6 helpers.

These helpers were previously untested on the grounds that they need root.
Only `ip_route_cmd` does. Everything above it is pure list building, and it
is precisely where a wrong argument order produces a route that installs
cleanly and then blackholes -- so it is worth pinning without root.

The expected vectors are transcribed from the by-hand demo that was written
against a real kernel, so these tests assert the code emits what the manual
proof emits:

    implementation/demo-multihop/demo-topology.sh:157
        ip -6 route replace fc00:0:2:fff0::/128 \
            encap seg6local action End dev srv6-ul1
    implementation/demo-multihop/demo-vpn.sh:145-146
        ip -6 route replace <sid>/128 \
            encap seg6local action End.DT46 vrftable <table> dev <vrf>
    implementation/demo-multihop/demo-vpn.sh:155-156
        ip route replace <prefix> \
            encap seg6 mode encap segs <s1>,<s2> dev <ul> table <table>

Until a second compute node exists, this correspondence is the strongest
correctness evidence available for the encapsulation path.
"""

from unittest import mock

from neutron.tests import base

from networking_srv6_agent.privileged import seg6


class Seg6ArgvTestCase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(seg6, 'ip_route_cmd')
        self.ip_route_cmd = patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def argv(self):
        return self.ip_route_cmd.call_args[0][0]


class TestSeg6LocalRoute(Seg6ArgvTestCase):
    """Decapsulation and transit behaviours."""

    def test_end_dt46_carries_a_vrftable(self):
        seg6.replace_seg6local_route('fc00:0:1:7::', 'End.DT46', 'sv6vrf-7',
                                     vrf_table=10007)
        self.assertEqual(
            ['-6', 'route', 'replace', 'fc00:0:1:7::/128',
             'encap', 'seg6local', 'action', 'End.DT46',
             'vrftable', '10007', 'dev', 'sv6vrf-7'],
            self.argv)

    def test_end_omits_vrftable_entirely(self):
        # Not "passes an empty one" -- iproute2 rejects
        # `action End vrftable <n>`, so the token must be absent.
        seg6.replace_seg6local_route('fc00:0:2:fff0::', 'End', 'srv6-ul1')
        self.assertEqual(
            ['-6', 'route', 'replace', 'fc00:0:2:fff0::/128',
             'encap', 'seg6local', 'action', 'End', 'dev', 'srv6-ul1'],
            self.argv)
        self.assertNotIn('vrftable', self.argv)

    def test_end_matches_the_hand_built_demo_verbatim(self):
        # demo-topology.sh:157, with its own SID and device.
        seg6.replace_seg6local_route('fc00:0:2:fff0::', 'End', 'srv6-ul1')
        self.assertEqual(
            'ip -6 route replace fc00:0:2:fff0::/128 '
            'encap seg6local action End dev srv6-ul1',
            'ip ' + ' '.join(self.argv))


class TestSeg6EncapRoute(Seg6ArgvTestCase):
    """The encapsulation side, at depth 1 and deeper."""

    def test_single_segment(self):
        seg6.replace_seg6_route('10.20.2.5/32', ['fc00:0:2:7::'], 'ens4',
                                10007)
        self.assertEqual(
            ['route', 'replace', '10.20.2.5/32',
             'encap', 'seg6', 'mode', 'encap',
             'segs', 'fc00:0:2:7::',
             'dev', 'ens4', 'table', '10007'],
            self.argv)

    def test_multiple_segments_are_comma_joined_in_traversal_order(self):
        # `segs A,B` means visit A then B. The SRH stores the list reversed
        # (RFC 8754 s4.1) and iproute2 does that reversal -- so this code
        # must NOT reverse, and a capture showing B,A is correct.
        seg6.replace_seg6_route(
            '10.60.2.5/32', ['fc00:0:2:fff0::', 'fc00:0:3:9::'],
            'srv6-ul0', 10009)
        self.assertIn('segs', self.argv)
        self.assertEqual('fc00:0:2:fff0::,fc00:0:3:9::',
                         self.argv[self.argv.index('segs') + 1])

    def test_multi_segment_matches_the_hand_built_demo_verbatim(self):
        # demo-vpn.sh:155-156.
        seg6.replace_seg6_route(
            '10.60.2.5/32', ['fc00:0:2:fff0::', 'fc00:0:3:9::'],
            'srv6-ul0', 10009)
        self.assertEqual(
            'ip route replace 10.60.2.5/32 encap seg6 mode encap '
            'segs fc00:0:2:fff0::,fc00:0:3:9:: dev srv6-ul0 table 10009',
            'ip ' + ' '.join(self.argv))

    def test_v6_prefix_selects_the_v6_family(self):
        seg6.replace_seg6_route('2001:db8::5/128', ['fc00:0:2:7::'], 'ens4',
                                10007, family_v6=True)
        self.assertEqual('-6', self.argv[0])


class TestVrfSeal(Seg6ArgvTestCase):
    """MIGRATION-PLAN.md 8.6: the route that closes the fall-through."""

    def test_v4(self):
        seg6.replace_unreachable_default(13783)
        self.assertEqual(
            ['route', 'replace', 'unreachable', 'default',
             'metric', '4278198272', 'table', '13783'],
            self.argv)

    def test_v6(self):
        seg6.replace_unreachable_default(13783, family_v6=True)
        self.assertEqual('-6', self.argv[0])
        self.assertEqual('13783', self.argv[-1])

    def test_reconciliation_never_sees_it(self):
        """It carries no `encap seg6`, so reconciliation cannot delete it.

        The lines are what `ip route show table 13783` printed on node 1,
        plus the seal.
        """
        self.ip_route_cmd.return_value = (
            'unreachable default metric 4278198272 \n'
            '10.90.1.0/24 dev svp-3783-4 proto kernel scope link '
            'src 10.90.1.1 \n'
            '10.90.2.21  encap seg6 mode encap segs 1 [ fc00:0:2:ec7:: ] '
            'dev enp2s0f1 scope link \n')
        self.assertEqual(['10.90.2.21'], seg6.list_seg6_routes(13783))


class TestFlushTable(Seg6ArgvTestCase):

    def test_argv(self):
        seg6.flush_table(10007, family_v6=True)
        self.assertEqual(['-6', 'route', 'flush', 'table', '10007'],
                         self.argv)

    def test_an_error_is_tolerated(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError('boom')
        self.assertIsNone(seg6.flush_table(10007))


class TestListVrfNames(Seg6ArgvTestCase):

    def test_parses_one_line_per_device(self):
        # Verbatim shape of `ip -o link show type vrf` on node 1.
        self.ip_route_cmd.return_value = (
            '56: sv6vrf-3579: <NOARP,MASTER,UP,LOWER_UP> mtu 65575 qdisc '
            'noqueue state UP mode DEFAULT group default qlen 1000\\    '
            'link/ether 12:34:56:78:9a:bc brd ff:ff:ff:ff:ff:ff\n'
            '57: sv6vrf-3783: <NOARP,MASTER,UP,LOWER_UP> mtu 65575 qdisc '
            'noqueue state UP mode DEFAULT group default qlen 1000\n')
        self.assertEqual(['sv6vrf-3579', 'sv6vrf-3783'],
                         seg6.list_vrf_names())
        self.assertEqual(['-o', 'link', 'show', 'type', 'vrf'], self.argv)

    def test_no_vrfs(self):
        self.ip_route_cmd.return_value = ''
        self.assertEqual([], seg6.list_vrf_names())

    def test_a_failure_is_not_an_empty_list(self):
        # The agent garbage-collects from this list.
        self.ip_route_cmd.side_effect = RuntimeError('boom')
        self.assertRaises(RuntimeError, seg6.list_vrf_names)


class TestLocatorRoute(Seg6ArgvTestCase):

    def test_locator_route_lands_in_table_main(self):
        # `table main` is load-bearing: in the local table (ip rule priority
        # 0, ahead of main at 32766) this would shadow every per-SID route
        # and no seg6local behaviour would ever run.
        seg6.replace_locator_route('fc00:0:1::/48')
        self.assertEqual(
            ['-6', 'route', 'replace', 'local', 'fc00:0:1::/48',
             'dev', 'lo', 'table', 'main'],
            self.argv)
