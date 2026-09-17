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

from osc_lib.cli import format_columns
from osc_lib import exceptions

from networking_srv6_agent.cli import common
from networking_srv6_agent.cli import resources
from networking_srv6_agent.cli.v2 import domain
from networking_srv6_agent.tests.unit.cli import base


def _domain(**attrs):
    body = {'id': base.DOMAIN_ID, 'name': 'red',
            'project_id': base.PROJECT_ID, 'srv6_behavior': 'End.DT46',
            'networks': [base.NETWORK_ID]}
    body.update(attrs)
    return resources.Srv6Domain.existing(**body)


class TestCreateDomain(base.TestCommand):

    def setUp(self):
        super().setUp()
        self.cmd = domain.CreateDomain(self.app, None)
        self.network._create.return_value = _domain()

    def test_name_only(self):
        result = self.run_cmd(self.cmd, ['red'])
        self.network._create.assert_called_once_with(
            resources.Srv6Domain, name='red')
        # A tenant's answer has no sid_function: no row for it.
        self.assertShown(result, {
            'ID': base.DOMAIN_ID,
            'Name': 'red',
            'Project': base.PROJECT_ID,
            'SRv6 Behavior': 'End.DT46',
            'Networks': format_columns.ListColumn([base.NETWORK_ID]),
        })

    def test_admin_sees_sid_function(self):
        self.network._create.return_value = _domain(sid_function=7)
        headers, values = self.run_cmd(self.cmd, ['red'])
        self.assertEqual(7, dict(zip(headers, values))['SID Function'])

    def test_behavior_and_project(self):
        with mock.patch.object(common.identity_common, 'find_project',
                               return_value=mock.Mock(id=base.PROJECT_ID)
                               ) as find_project:
            self.run_cmd(self.cmd, ['red', '--srv6-behavior', 'End.DT46',
                                    '--project', 'demo',
                                    '--project-domain', 'Default'])
        find_project.assert_called_once_with(
            self.app.client_manager.identity, 'demo', 'Default')
        self.network._create.assert_called_once_with(
            resources.Srv6Domain, name='red', srv6_behavior='End.DT46',
            project_id=base.PROJECT_ID)

    def test_name_required(self):
        self.assertParserFails(self.cmd, [])


class TestListDomain(base.TestCommand):

    def setUp(self):
        super().setUp()
        self.cmd = domain.ListDomain(self.app, None)
        self.network._list.return_value = iter([_domain()])

    def test_short(self):
        headers, rows = self.run_cmd(self.cmd, [])
        self.network._list.assert_called_once_with(resources.Srv6Domain)
        self.assertEqual(('ID', 'Name'), headers)
        self.assertEqual([(base.DOMAIN_ID, 'red')], list(rows))

    def test_long_with_filters(self):
        with mock.patch.object(common.identity_common, 'find_project',
                               return_value=mock.Mock(id=base.PROJECT_ID)):
            headers, rows = self.run_cmd(
                self.cmd, ['--long', '--name', 'red', '--project', 'demo'])
        self.network._list.assert_called_once_with(
            resources.Srv6Domain, name='red', project_id=base.PROJECT_ID)
        self.assertEqual(('ID', 'Name', 'Project', 'SRv6 Behavior',
                          'SID Function', 'Networks'), headers)
        self.assertEqual(
            [(base.DOMAIN_ID, 'red', base.PROJECT_ID, 'End.DT46', None,
              format_columns.ListColumn([base.NETWORK_ID]))],
            list(rows))


class TestShowDomain(base.TestCommand):

    def test_by_name(self):
        self.network._find.return_value = _domain()
        cmd = domain.ShowDomain(self.app, None)
        headers, values = self.run_cmd(cmd, ['red'])
        self.network._find.assert_called_once_with(
            resources.Srv6Domain, 'red', ignore_missing=False)
        self.assertEqual('red', dict(zip(headers, values))['Name'])


class TestSetDomain(base.TestCommand):

    def test_rename(self):
        self.network._find.return_value = _domain()
        cmd = domain.SetDomain(self.app, None)
        self.assertIsNone(self.run_cmd(cmd, ['red', '--name', 'blue']))
        self.network._update.assert_called_once_with(
            resources.Srv6Domain, base.DOMAIN_ID, name='blue')

    def test_nothing_to_set(self):
        self.assertParserFails(domain.SetDomain(self.app, None), ['red'])


class TestDeleteDomain(base.TestCommand):

    def setUp(self):
        super().setUp()
        self.cmd = domain.DeleteDomain(self.app, None)

    def test_several(self):
        self.network._find.side_effect = [_domain(id='a'), _domain(id='b')]
        self.run_cmd(self.cmd, ['red', 'blue'])
        self.assertEqual(
            [mock.call(resources.Srv6Domain, 'a', ignore_missing=False),
             mock.call(resources.Srv6Domain, 'b', ignore_missing=False)],
            self.network._delete.call_args_list)

    def test_partial_failure_still_deletes_the_rest(self):
        self.network._find.side_effect = [Exception('not found'),
                                          _domain(id='b')]
        e = self.assertRaises(exceptions.CommandError,
                              self.run_cmd, self.cmd, ['nope', 'blue'])
        self.assertIn('1 of 2', str(e))
        self.network._delete.assert_called_once_with(
            resources.Srv6Domain, 'b', ignore_missing=False)
