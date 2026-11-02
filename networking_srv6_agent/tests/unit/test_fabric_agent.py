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

"""The fabric agent (tools/srv6-fabric-agent.py) passes its own self-test.

The agent runs on the fabric switches, not in Neutron, and imports nothing
of this package: it is a standalone stdlib script, run here as one.
"""

import os
import subprocess
import sys

import testtools

import networking_srv6_agent

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(
        networking_srv6_agent.__file__))), 'tools', 'srv6-fabric-agent.py')


class TestFabricAgent(testtools.TestCase):

    def test_self_test(self):
        if not os.path.exists(_TOOL):
            self.skipTest('not running from a source tree')
        out = subprocess.run([sys.executable, '-I', _TOOL, 'test'],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn('self-test ok', out.stdout)
