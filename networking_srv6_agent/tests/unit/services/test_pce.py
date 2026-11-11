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

"""Online CSPF: topology file, path choice, admission.

The topology is the test bed's 2-spine / 4-leaf Clos: spine-leaf links
100 Mbit/s, host links 1000, hosts ml1/ml4 dual-homed to l1/l2 (rack A),
ml5/ml6 to l3/l4 (rack B).
"""

import collections
import os
import tempfile

from neutron.tests import base

from networking_srv6_agent.services import pce

CLOS = """
# the Clos test bed
node s1 fc00:0:a1::/48
node s2 fc00:0:a2::/48
node l1 fc00:0:b1::/48
node l2 fc00:0:b2::/48
node l3 fc00:0:b3::/48
node l4 fc00:0:b4::/48
node ml1 fc00:0:1::/48
node ml4 fc00:0:4::/48
node ml5 fc00:0:5::/48
node ml6 fc00:0:6::/48
link s1 l1 100
link s1 l2 100
link s1 l3 100
link s1 l4 100
link s2 l1 100
link s2 l2 100
link s2 l3 100
link s2 l4 100
link l1 ml1 1000
link l2 ml1 1000
link l1 ml4 1000
link l2 ml4 1000
link l3 ml5 1000
link l4 ml5 1000
link l3 ml6 1000
link l4 ml6 1000
"""


def _used(paths):
    """{(a, b): Mbit/s} that [(node path, Mbit/s)] put on each link."""
    used = collections.Counter()
    for path, bw in paths:
        for link in pce.links_of(path):
            used[link] += bw
    return used


class PceTestCase(base.BaseTestCase):

    def write(self, text):
        fd, path = tempfile.mkstemp(suffix='.topo')
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def setUp(self):
        super().setUp()
        self.locators, self.cap = pce.load(self.write(CLOS))


class TestLoad(PceTestCase):

    def test_clos(self):
        self.assertEqual(10, len(self.locators))
        self.assertEqual('fc00:0:a1::/48', self.locators['s1'])
        self.assertEqual(32, len(self.cap))           # 16 links, 2 directions
        self.assertEqual(100, self.cap[('l1', 's1')])
        self.assertEqual(1000, self.cap[('ml1', 'l1')])

    def test_bad_lines_are_refused(self):
        for text in ('node s1\n',                                # no locator
                     'node s1 not-a-prefix\n',
                     'node a fc00:0:a::/48\nlink a b 10\n',      # unknown node
                     'node a fc00:0:a::/48\nlink a a 10\n',      # self loop
                     'node a fc00:0:a::/48\nnode b fc00:0:b::/48\n'
                     'link a b zero\n',
                     'node a fc00:0:a::/48\nnode b fc00:0:b::/48\n'
                     'link a b 0\n',
                     'rack A\n'):
            self.assertRaises(pce.TopologyError, pce.load, self.write(text))


class TestCompute(PceTestCase):

    def test_inter_rack_is_one_spine_pinned_end_to_end(self):
        path = pce.compute(self.cap, {}, 'ml1', 'ml5', 10)
        self.assertEqual(5, len(path))
        self.assertEqual(('ml1', 'ml5'), (path[0], path[-1]))
        self.assertIn(path[1], ('l1', 'l2'))
        self.assertIn(path[2], ('s1', 's2'))
        self.assertIn(path[3], ('l3', 'l4'))

    def test_deterministic_when_everything_is_equal(self):
        self.assertEqual(['ml1', 'l1', 's1', 'l3', 'ml5'],
                         pce.compute(self.cap, {}, 'ml1', 'ml5', 10))

    def test_intra_rack_goes_through_a_leaf_only(self):
        self.assertEqual(['ml1', 'l1', 'ml4'],
                         pce.compute(self.cap, {}, 'ml1', 'ml4', 500))

    def test_the_least_used_spine_wins(self):
        used = _used([(['ml1', 'l1', 's1', 'l3', 'ml5'], 60)])
        path = pce.compute(self.cap, used, 'ml1', 'ml5', 10)
        # l1-s1 has 40 left, every other uplink 100: the widest avoids it
        self.assertNotIn(('l1', 's1'), pce.links_of(path))
        self.assertNotIn(('s1', 'l3'), pce.links_of(path))

    def test_four_60_flows_land_on_four_disjoint_spine_links(self):
        # the collide scenario: what ECMP stacks, CSPF spreads
        paths = []
        for src, dst in (('ml1', 'ml5'), ('ml4', 'ml6'),
                         ('ml1', 'ml5'), ('ml4', 'ml6')):
            path = pce.compute(self.cap, _used(paths), src, dst, 60)
            self.assertIsNotNone(path)
            paths.append((path, 60))
        used = _used(paths)
        spine = {k: v for k, v in used.items() if 's' in k[0] + k[1]
                 and 'ml' not in k[0] + k[1]}
        self.assertTrue(all(v <= 100 for v in spine.values()), spine)
        self.assertEqual(8, len(spine))           # 4 up + 4 down, all used

    def test_fill_then_reject(self):
        paths = []
        for _ in range(4):                        # 4 x 100 Mbit/s uplinks
            path = pce.compute(self.cap, _used(paths), 'ml1', 'ml5', 90)
            self.assertIsNotNone(path)
            paths.append((path, 90))
        self.assertIsNone(
            pce.compute(self.cap, _used(paths), 'ml1', 'ml5', 90))
        # what is left still fits a small one
        self.assertIsNotNone(
            pce.compute(self.cap, _used(paths), 'ml1', 'ml5', 10))

    def test_directions_are_independent(self):
        paths = [(p, 100) for p in (['ml1', 'l1', 's1', 'l3', 'ml5'],
                                    ['ml1', 'l1', 's2', 'l3', 'ml5'],
                                    ['ml1', 'l2', 's1', 'l4', 'ml5'],
                                    ['ml1', 'l2', 's2', 'l4', 'ml5'])]
        used = _used(paths)
        self.assertIsNone(pce.compute(self.cap, used, 'ml1', 'ml5', 1))
        # the reverse direction has not been touched
        self.assertIsNotNone(pce.compute(self.cap, used, 'ml5', 'ml1', 100))

    def test_too_big_for_any_link(self):
        self.assertIsNone(pce.compute(self.cap, {}, 'ml1', 'ml5', 101))

    def test_unknown_or_isolated_endpoints(self):
        self.assertIsNone(pce.compute(self.cap, {}, 'ml1', 'nowhere', 1))
        self.assertIsNone(pce.compute(self.cap, {}, 'nowhere', 'ml1', 1))
