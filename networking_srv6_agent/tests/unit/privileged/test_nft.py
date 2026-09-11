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

"""argv and stdin of the edge filter's privileged entrypoint (P6-PLAN.md 5).
"""

from unittest import mock

from neutron.tests import base

from networking_srv6_agent import privileged
from networking_srv6_agent.privileged import nft


class TestApplyRuleset(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        # Run the entrypoint in this process instead of through a privsep
        # daemon: what is under test is what reaches nft, not privsep.
        privileged.default.set_client_mode(False)
        self.addCleanup(privileged.default.set_client_mode, True)
        patcher = mock.patch.object(nft.processutils, 'execute',
                                    return_value=('', ''))
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_ruleset_goes_on_stdin(self):
        nft.apply_ruleset('table inet srv6_edge {}\n')
        self.execute.assert_called_once_with(
            'nft', '-f', '-', process_input='table inet srv6_edge {}\n',
            check_exit_code=True)

    def test_nothing_from_the_ruleset_reaches_the_command_line(self):
        nft.apply_ruleset('flush ruleset\n')
        self.assertEqual(('nft', '-f', '-'), self.execute.call_args[0])

    def test_a_failure_propagates(self):
        # The dataplane turns it into "filter inactive".
        self.execute.side_effect = nft.processutils.ProcessExecutionError(
            stderr='Error: syntax error')
        self.assertRaises(nft.processutils.ProcessExecutionError,
                          nft.apply_ruleset, 'bogus\n')
