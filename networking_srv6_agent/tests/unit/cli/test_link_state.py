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
from networking_srv6_agent.cli.v2 import link_state
from networking_srv6_agent.tests.unit.cli import base

AT = '2026-11-01T12:00:00'


def _entry():
    return resources.Srv6LinkState.existing(
        id='l1:s1', from_node='l1', to_node='s1', reporter='l1', up=True,
        mbps=150.0, delivered_mbps=100.0, peak_mbps=156.6, drops=0,
        capacity_mbps=100,
        measured_at=AT, age=3, stale=False)


class TestListLinkState(base.TestCommand):

    def test_list(self):
        self.network._list.return_value = iter([_entry()])
        headers, rows = self.run_cmd(
            link_state.ListLinkState(self.app, None), [])
        self.network._list.assert_called_once_with(resources.Srv6LinkState)
        self.assertEqual(('From', 'To', 'Up', 'Mbit/s', 'Delivered', 'Peak',
                          'Capacity', 'Age (s)', 'Stale'),
                         headers)
        self.assertEqual(
            [('l1', 's1', True, 150.0, 100.0, 156.6, 100, 3, False)],
            list(rows))

    def test_long_adds_reporter_drops_and_time(self):
        self.network._list.return_value = iter([_entry()])
        headers, rows = self.run_cmd(
            link_state.ListLinkState(self.app, None), ['--long'])
        self.assertEqual(('Reporter', 'Drops', 'Measured At'), headers[-3:])
        self.assertEqual(('l1', 0, AT), list(rows)[0][-3:])

    def test_filters(self):
        self.network._list.return_value = iter([])
        self.run_cmd(link_state.ListLinkState(self.app, None),
                     ['--from', 'l1', '--to', 's1'])
        self.network._list.assert_called_once_with(
            resources.Srv6LinkState, from_node='l1', to_node='s1')
