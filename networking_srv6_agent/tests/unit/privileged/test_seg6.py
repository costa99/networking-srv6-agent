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
from oslo_concurrency import processutils

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
    """The route that closes the fall-through."""

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


class TestRouteGetDev(Seg6ArgvTestCase):

    def test_argv_and_device(self):
        self.ip_route_cmd.return_value = (
            'fc00:0:2:: from :: via fd00:aa::2 dev wlp59s0 proto kernel '
            'src fd00:aa::1 metric 256 pref medium\n')
        self.assertEqual('wlp59s0', seg6.route_get_dev('fc00:0:2::'))
        self.assertEqual(['-6', '-o', 'route', 'get', 'fc00:0:2::'],
                         self.argv)

    def test_unreachable_is_none(self):
        self.ip_route_cmd.side_effect = processutils.ProcessExecutionError(
            stderr='RTNETLINK answers: Network is unreachable')
        self.assertIsNone(seg6.route_get_dev('fc00:0:2::'))

    def test_no_device_is_none(self):
        self.ip_route_cmd.return_value = 'unreachable fc00:0:2:: dev\n'
        self.assertIsNone(seg6.route_get_dev('fc00:0:2::'))


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


class TestSidRules(Seg6ArgvTestCase):
    """The rules that let a domain's outer headers out."""

    def test_add(self):
        seg6.add_sid_rule('sv6vrf-2394', 'fc00:0:2:95a::')
        self.assertEqual(
            ['-6', 'rule', 'add', 'pref', '999', 'iif', 'sv6vrf-2394',
             'to', 'fc00:0:2:95a::/128', 'lookup', 'main'],
            self.argv)

    def test_add_of_an_existing_rule_is_success(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(
                stderr='RTNETLINK answers: File exists')
        self.assertIsNone(seg6.add_sid_rule('sv6vrf-2394', 'fc00:0:2:95a::'))

    def test_add_other_errors_propagate(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(stderr='Invalid argument')
        self.assertRaises(seg6.processutils.ProcessExecutionError,
                          seg6.add_sid_rule, 'sv6vrf-2394', 'fc00:0:2:95a::')

    def test_delete(self):
        seg6.delete_sid_rule('sv6vrf-2394', 'fc00:0:2:95a::')
        self.assertEqual('del', self.argv[2])
        self.assertEqual(['iif', 'sv6vrf-2394', 'to', 'fc00:0:2:95a::/128',
                          'lookup', 'main'], self.argv[5:])

    def test_delete_of_a_missing_rule_is_success(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(
                stderr='RTNETLINK answers: No such file or directory')
        self.assertIsNone(seg6.delete_sid_rule('sv6vrf-2394',
                                               'fc00:0:2:95a::'))

    def test_list_keeps_only_this_packages_rules(self):
        self.ip_route_cmd.return_value = (
            '0:\tfrom all lookup local\n'
            '999:\tfrom all to fc00:0:2:95a:: iif sv6vrf-2394 lookup main\n'
            '999:\tfrom all to fc00:0:3:95a:: iif sv6vrf-2394 [detached] '
            'lookup main\n'
            '999:\tfrom all to 2001:db8::1 iif eth0 lookup main\n'
            '999:\tfrom all to fc00:0:2:9e8:: iif sv6vrf-2536 lookup 100\n'
            '998:\tfrom all to fc00:0:2:1:: iif sv6vrf-1 lookup main\n'
            '1000:\tfrom all lookup [l3mdev-table]\n'
            '32766:\tfrom all lookup main\n')
        self.assertEqual([('sv6vrf-2394', 'fc00:0:2:95a::'),
                          ('sv6vrf-2394', 'fc00:0:3:95a::')],
                         seg6.list_sid_rules())
        self.assertEqual(['-6', 'rule', 'show'], self.argv)

    def test_list_failure_raises(self):
        self.ip_route_cmd.side_effect = RuntimeError('boom')
        self.assertRaises(RuntimeError, seg6.list_sid_rules)


class TestAddressNoDad(Seg6ArgvTestCase):
    """F12: the IPv6 gateway address skips duplicate address detection."""

    def test_argv(self):
        seg6.add_address_nodad('svp-7-100', 'fd98:1::/64')
        self.assertEqual(['-6', 'addr', 'add', 'fd98:1::/64', 'dev',
                          'svp-7-100', 'nodad'], self.argv)

    def test_an_existing_address_is_success(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(
                stderr='RTNETLINK answers: File exists')
        self.assertIsNone(seg6.add_address_nodad('svp-7-100', 'fd98:1::/64'))


class TestLocalRule(Seg6ArgvTestCase):
    """F7: moving the kernel's `local` rule after l3mdev."""

    def test_prefs_of_the_plain_local_rule_only(self):
        self.ip_route_cmd.return_value = (
            '0:\tfrom all lookup local\n'
            '999:\tfrom all to fc00:0:2:95a:: iif sv6vrf-1 lookup main\n'
            '1000:\tfrom all lookup [l3mdev-table]\n'
            '2000:\tfrom all iif eth1 lookup local\n'
            '32765:\tfrom all lookup local\n'
            '32766:\tfrom all lookup main\n')
        self.assertEqual([0, 32765], seg6.local_rule_prefs(family_v6=True))
        self.assertEqual(['-6', 'rule', 'show'], self.argv)

    def test_ipv4_has_no_family_flag(self):
        self.ip_route_cmd.return_value = '0:\tfrom all lookup local\n'
        self.assertEqual([0], seg6.local_rule_prefs())
        self.assertEqual(['rule', 'show'], self.argv)

    def test_add(self):
        seg6.add_local_rule(32765, family_v6=True)
        self.assertEqual(['-6', 'rule', 'add', 'pref', '32765', 'lookup',
                          'local'], self.argv)

    def test_delete_names_the_table_too(self):
        # `del pref 0` alone would delete whatever rule sits at 0.
        seg6.delete_local_rule(0)
        self.assertEqual(['rule', 'del', 'pref', '0', 'lookup', 'local'],
                         self.argv)

    def test_add_of_an_existing_rule_is_success(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(
                stderr='RTNETLINK answers: File exists')
        self.assertIsNone(seg6.add_local_rule(32765))

    def test_delete_of_a_missing_rule_is_success(self):
        self.ip_route_cmd.side_effect = \
            seg6.processutils.ProcessExecutionError(
                stderr='RTNETLINK answers: No such file or directory')
        self.assertIsNone(seg6.delete_local_rule(0))


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


class TestMicroSidArgv(Seg6ArgvTestCase):
    """The microSID vectors.

    As checked against kernel 7.0 / iproute2 6.1 on node 2 on 2026-10-02
    (USID-PLAN.md s2.3).
    """

    def test_encap_red(self):
        seg6.replace_seg6_route('10.0.0.5/32', ['fc00:0:1:3:e005::'],
                                'eth0', 10007, mode='encap.red')
        self.assertEqual(
            'ip route replace 10.0.0.5/32 encap seg6 mode encap.red '
            'segs fc00:0:1:3:e005:: dev eth0 table 10007',
            'ip ' + ' '.join(self.argv))

    def test_an_unknown_mode_is_refused_before_ip_runs(self):
        self.assertRaises(ValueError, seg6.replace_seg6_route,
                          '10.0.0.5/32', ['fc00:0:2:7::'], 'eth0', 10007,
                          mode='inline')
        self.ip_route_cmd.assert_not_called()

    def test_un_is_a_48_with_the_next_csid_flavor(self):
        seg6.replace_seg6local_route('fc00:0:1::', 'End', 'eth0',
                                     prefixlen=48, next_csid=(32, 16))
        self.assertEqual(
            'ip -6 route replace fc00:0:1::/48 encap seg6local action End '
            'flavors next-csid lblen 32 nflen 16 dev eth0',
            'ip ' + ' '.join(self.argv))

    def test_decap_at_64(self):
        seg6.replace_seg6local_route('fc00:0:1:e005::', 'End.DT46',
                                     'sv6vrf-57349', vrf_table=67349,
                                     prefixlen=64)
        self.assertEqual(
            ['-6', 'route', 'replace', 'fc00:0:1:e005::/64',
             'encap', 'seg6local', 'action', 'End.DT46',
             'vrftable', '67349', 'dev', 'sv6vrf-57349'],
            self.argv)

    def test_lib_seal(self):
        seg6.replace_unreachable_route('fc00:0:1:e000::/52')
        self.assertEqual(['-6', 'route', 'replace', 'unreachable',
                          'fc00:0:1:e000::/52', 'table', 'main'], self.argv)

    def test_delete_names_the_route_type(self):
        seg6.delete_main_route('fc00:0:1::/48', route_type='local')
        self.assertEqual(['-6', 'route', 'del', 'local', 'fc00:0:1::/48',
                          'table', 'main'], self.argv)

    def test_delete_tolerates_absence(self):
        self.ip_route_cmd.side_effect = processutils.ProcessExecutionError(
            stderr='RTNETLINK answers: No such process')
        self.assertIsNone(seg6.delete_main_route('fc00:0:1::/48'))


class TestShowMainRoute(Seg6ArgvTestCase):

    def test_keeps_only_main(self):
        # `table all` output: a main entry names no table (iproute2 6.1
        # hides a local-type route from `show table main` altogether).
        self.ip_route_cmd.return_value = (
            'local fc00:0:1::/48 dev lo metric 1024 pref medium\n'
            'fc00:0:1::/48 via fd00::1 dev eth0 table 10007 metric 1024\n'
            'fc00:0:1::/48 dev lo table main proto boot metric 1024\n')
        self.assertEqual(
            ['local fc00:0:1::/48 dev lo metric 1024 pref medium',
             'fc00:0:1::/48 dev lo table main proto boot metric 1024'],
            seg6.show_main_route('fc00:0:1::/48'))
        self.assertEqual(['-6', 'route', 'show', 'table', 'all', 'exact',
                          'fc00:0:1::/48'], self.argv)

    def test_unreadable_is_empty(self):
        self.ip_route_cmd.side_effect = processutils.ProcessExecutionError()
        self.assertEqual([], seg6.show_main_route('fc00:0:1::/48'))


class TestProbeCapabilities(Seg6ArgvTestCase):

    UN = ('2001:db8:0:1::/64  encap seg6local action End flavors next-csid '
          'lblen 32 nflen 16 dev lo metric 1024 pref medium\n')
    RED = ('2001:db8:0:2::/64  encap seg6 mode encap.red segs 1 '
           '[ 2001:db8::1 ] dev lo metric 1024 pref medium\n')

    def setUp(self):
        super().setUp()
        flush = mock.patch.object(seg6, 'flush_table')
        self.flush = flush.start()
        self.addCleanup(flush.stop)

    def _answer(self, shown):
        def run(args):
            if args[2] == 'show':
                return shown.pop(0)
            return ''
        self.ip_route_cmd.side_effect = run

    def test_both_found(self):
        self._answer([self.UN, self.UN + self.RED])
        self.assertEqual(['next-csid', 'encap-red'],
                         seg6.probe_capabilities())
        self.flush.assert_called_once_with(4000000000, family_v6=True)

    def test_a_kernel_that_drops_the_flavor_is_caught_on_read_back(self):
        # Accepted, but shown without it: an older kernel ignoring the
        # nested attribute.
        self._answer(['2001:db8:0:1::/64 encap seg6local action End dev lo\n',
                      self.RED])
        self.assertEqual(['encap-red'], seg6.probe_capabilities())

    def test_an_iproute2_that_rejects_the_keyword(self):
        def run(args):
            if 'flavors' in args or 'encap.red' in args:
                raise processutils.ProcessExecutionError(
                    stderr='Error: argument "flavors" is wrong')
            return ''
        self.ip_route_cmd.side_effect = run
        self.assertEqual([], seg6.probe_capabilities())
        self.flush.assert_called_once()

    def test_the_probe_table_is_flushed_even_on_a_crash(self):
        self.ip_route_cmd.side_effect = RuntimeError('privsep died')
        self.assertRaises(RuntimeError, seg6.probe_capabilities)
        self.flush.assert_called_once()
