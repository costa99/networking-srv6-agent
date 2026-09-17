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

"""Scaffolding for the command tests.

Not osc_lib.tests.utils: importing it needs requests_mock, which nothing
else here does. The network proxy is a Mock, so a test asserts which generic
proxy call a command made and with which resource class.
"""

import io
from unittest import mock

import fixtures
import testtools

DOMAIN_ID = '11111111-1111-1111-1111-111111111111'
NETWORK_ID = '22222222-2222-2222-2222-222222222222'
PORT_ID = '33333333-3333-3333-3333-333333333333'
PROJECT_ID = '44444444444444444444444444444444'
ASSOC_ID = '55555555-5555-5555-5555-555555555555'
TE_PATH_ID = '66666666-6666-6666-6666-666666666666'


class TestCommand(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.app = mock.Mock()
        self.network = self.app.client_manager.network

    def parse(self, cmd, args):
        return cmd.get_parser('openstack').parse_args(args)

    def assertParserFails(self, cmd, args):
        # argparse reports on stderr and exits; keep the test output clean.
        self.useFixture(fixtures.MonkeyPatch('sys.stderr', io.StringIO()))
        self.assertRaises(SystemExit, self.parse, cmd, args)

    def run_cmd(self, cmd, args):
        return cmd.take_action(self.parse(cmd, args))

    def assertShown(self, result, expected):
        """result is (headers, values); expected maps header -> value."""
        headers, values = result
        self.assertEqual(expected, dict(zip(headers, values)))
