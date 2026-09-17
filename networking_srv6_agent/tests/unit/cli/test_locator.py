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

from networking_srv6_agent.cli import resources
from networking_srv6_agent.cli.v2 import locator
from networking_srv6_agent.tests.unit.cli import base

UPDATED = '2026-09-15T10:00:00'


def _locator():
    return resources.Srv6Locator.existing(
        id='node1', host='node1', locator='fc00:0:1::/48', updated_at=UPDATED)


class TestListLocator(base.TestCommand):

    def test_list(self):
        self.network._list.return_value = iter([_locator()])
        headers, rows = self.run_cmd(locator.ListLocator(self.app, None), [])
        self.network._list.assert_called_once_with(resources.Srv6Locator)
        self.assertEqual(('Host', 'Locator', 'Updated At'), headers)
        self.assertEqual([('node1', 'fc00:0:1::/48', UPDATED)], list(rows))

    def test_filter_by_host(self):
        self.network._list.return_value = iter([])
        self.run_cmd(locator.ListLocator(self.app, None), ['--host', 'node1'])
        self.network._list.assert_called_once_with(
            resources.Srv6Locator, host='node1')


class TestShowLocator(base.TestCommand):

    def test_show(self):
        self.network._get.return_value = _locator()
        result = self.run_cmd(locator.ShowLocator(self.app, None), ['node1'])
        self.network._get.assert_called_once_with(
            resources.Srv6Locator, 'node1')
        # The id is the host: one row, not two.
        self.assertShown(result, {
            'Host': 'node1',
            'Locator': 'fc00:0:1::/48',
            'Updated At': UPDATED,
        })
