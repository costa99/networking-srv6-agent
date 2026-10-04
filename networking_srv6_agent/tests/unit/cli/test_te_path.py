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

from unittest import mock

from osc_lib import exceptions

from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources
from networking_srv6_agent.cli.v2 import te_path
from networking_srv6_agent.tests.unit.cli import base

TE = resources.Srv6TePath
SEGMENTS = ['fc00:0:2:fff0::', 'fc00:0:2:7::']


def _path(**attrs):
    body = {'id': base.TE_PATH_ID, 'project_id': base.PROJECT_ID,
            'domain_id': base.DOMAIN_ID, 'destination_host': None,
            'destination_port_id': base.PORT_ID, 'source_host': None,
            'via_hosts': ['node2'],
            'segments': SEGMENTS, 'encoded_segments': SEGMENTS,
            'status': 'ACTIVE'}
    body.update(attrs)
    return TE.existing(**body)


class _Base(base.TestCommand):

    def setUp(self):
        super().setUp()
        self.network._find.return_value = mock.Mock(id=base.DOMAIN_ID)
        self.network.find_port.return_value = mock.Mock(id=base.PORT_ID)


class TestPathColumn(base.TestCommand):

    def test_keeps_the_traversal_order(self):
        column = common.PathColumn(['node3', 'node1'])
        self.assertEqual('node3, node1', column.human_readable())
        self.assertEqual(['node3', 'node1'], column.machine_readable())

    def test_empty(self):
        self.assertEqual('', common.PathColumn([]).human_readable())
        self.assertEqual('', common.PathColumn(None).human_readable())


class TestCreateTePath(_Base):

    def setUp(self):
        super().setUp()
        self.cmd = te_path.CreateTePath(self.app, None)
        self.network._create.return_value = _path()

    def test_port_selector_keeps_via_order(self):
        result = self.run_cmd(self.cmd, [
            '--domain', 'red', '--destination-port', 'vm-port',
            '--via-host', 'node3', '--via-host', 'node2'])
        self.network.find_port.assert_called_once_with(
            'vm-port', ignore_missing=False)
        self.network._create.assert_called_once_with(
            TE, domain_id=base.DOMAIN_ID, destination_port_id=base.PORT_ID,
            via_hosts=['node3', 'node2'])
        self.assertShown(result, {
            'ID': base.TE_PATH_ID,
            'Domain ID': base.DOMAIN_ID,
            'Destination Host': None,
            'Destination Port': base.PORT_ID,
            'Source Host': te_path.SourceHostColumn(None),
            'Via Hosts': common.PathColumn(['node2']),
            'Status': 'ACTIVE',
            'Segments': common.PathColumn(SEGMENTS),
            'Encoded Segments': common.PathColumn(SEGMENTS),
            'Project': base.PROJECT_ID,
        })

    def test_host_selector_without_via_leaves_default(self):
        self.run_cmd(self.cmd, ['--domain', 'red',
                                '--destination-host', 'node2'])
        self.network.find_port.assert_not_called()
        self.network._create.assert_called_once_with(
            TE, domain_id=base.DOMAIN_ID, destination_host='node2')

    def test_source_host_is_sent(self):
        self.run_cmd(self.cmd, ['--domain', 'red',
                                '--destination-host', 'node3',
                                '--source-host', 'node1',
                                '--via-host', 'node2'])
        self.network._create.assert_called_once_with(
            TE, domain_id=base.DOMAIN_ID, destination_host='node3',
            source_host='node1', via_hosts=['node2'])

    def test_both_selectors(self):
        self.assertParserFails(self.cmd, [
            '--domain', 'red', '--destination-host', 'node2',
            '--destination-port', 'vm-port'])

    def test_no_selector(self):
        self.assertParserFails(self.cmd, ['--domain', 'red'])

    def test_domain_required(self):
        self.assertParserFails(self.cmd, ['--destination-host', 'node2'])


class TestListTePath(_Base):

    def setUp(self):
        super().setUp()
        self.cmd = te_path.ListTePath(self.app, None)
        self.network._list.return_value = iter([_path()])

    def test_short(self):
        headers, rows = self.run_cmd(self.cmd, [])
        self.network._list.assert_called_once_with(TE)
        self.assertEqual(('ID', 'Domain ID', 'Destination Host',
                          'Destination Port', 'Source Host', 'Via Hosts',
                          'Status'),
                         headers)
        self.assertEqual(
            [(base.TE_PATH_ID, base.DOMAIN_ID, None, base.PORT_ID,
              te_path.SourceHostColumn(None),
              common.PathColumn(['node2']), 'ACTIVE')],
            list(rows))

    def test_long_with_filters(self):
        headers, rows = self.run_cmd(self.cmd, [
            '--long', '--domain', 'red', '--destination-host', 'node2',
            '--destination-port', 'vm-port'])
        self.network._list.assert_called_once_with(
            TE, domain_id=base.DOMAIN_ID, destination_host='node2',
            destination_port_id=base.PORT_ID)
        self.assertEqual(('Segments', 'Encoded Segments', 'Project'),
                         headers[-3:])

    def test_by_source_host(self):
        self.run_cmd(self.cmd, ['--source-host', 'node1'])
        self.network._list.assert_called_once_with(TE, source_host='node1')

    def test_any_source_sends_the_empty_value(self):
        self.run_cmd(self.cmd, ['--any-source'])
        self.network._list.assert_called_once_with(TE, source_host='')

    def test_a_source_and_any_source_exclude_each_other(self):
        self.assertParserFails(self.cmd, ['--source-host', 'node1',
                                          '--any-source'])


class TestSourceHostColumn(base.TestCommand):

    def test_none_reads_any_for_people_and_none_for_machines(self):
        self.assertEqual('any',
                         te_path.SourceHostColumn(None).human_readable())
        self.assertIsNone(te_path.SourceHostColumn(None).machine_readable())

    def test_a_host_is_itself(self):
        self.assertEqual('node1',
                         te_path.SourceHostColumn('node1').human_readable())


class TestShowTePath(_Base):

    def test_show(self):
        self.network._get.return_value = _path(status='DEGRADED',
                                               segments=[])
        headers, values = self.run_cmd(te_path.ShowTePath(self.app, None),
                                       [base.TE_PATH_ID])
        self.network._get.assert_called_once_with(TE, base.TE_PATH_ID)
        shown = dict(zip(headers, values))
        self.assertEqual('DEGRADED', shown['Status'])
        self.assertEqual(common.PathColumn([]), shown['Segments'])


class TestSetTePath(_Base):

    def setUp(self):
        super().setUp()
        self.cmd = te_path.SetTePath(self.app, None)

    def test_replace_via_hosts(self):
        self.run_cmd(self.cmd, [base.TE_PATH_ID,
                                '--via-host', 'node3', '--via-host', 'node1'])
        self.network._update.assert_called_once_with(
            TE, base.TE_PATH_ID, via_hosts=['node3', 'node1'])

    def test_no_via_host_is_explicitly_direct(self):
        self.run_cmd(self.cmd, [base.TE_PATH_ID, '--no-via-host'])
        self.network._update.assert_called_once_with(
            TE, base.TE_PATH_ID, via_hosts=[])

    def test_both(self):
        self.assertParserFails(self.cmd, [base.TE_PATH_ID, '--no-via-host',
                                          '--via-host', 'node1'])

    def test_neither(self):
        self.assertParserFails(self.cmd, [base.TE_PATH_ID])


class TestDeleteTePath(_Base):

    def test_partial_failure(self):
        self.network._delete.side_effect = [Exception('404'), None]
        e = self.assertRaises(exceptions.CommandError, self.run_cmd,
                              te_path.DeleteTePath(self.app, None),
                              ['a', 'b'])
        self.assertIn('1 of 2', str(e))
        self.assertEqual(
            [mock.call(TE, 'a', ignore_missing=False),
             mock.call(TE, 'b', ignore_missing=False)],
            self.network._delete.call_args_list)
