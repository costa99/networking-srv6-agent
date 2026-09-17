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

from networking_srv6_agent.cli import resources
from networking_srv6_agent.cli.v2 import network_association as na
from networking_srv6_agent.tests.unit.cli import base

ASSOC = resources.Srv6NetworkAssociation


def _assoc(**attrs):
    body = {'id': base.ASSOC_ID, 'domain_id': base.DOMAIN_ID,
            'network_id': base.NETWORK_ID, 'project_id': base.PROJECT_ID}
    body.update(attrs)
    return ASSOC.existing(**body)


class _Base(base.TestCommand):

    def setUp(self):
        super().setUp()
        self.network._find.return_value = mock.Mock(id=base.DOMAIN_ID)
        self.network.find_network.return_value = mock.Mock(
            id=base.NETWORK_ID)


class TestCreateNetworkAssociation(_Base):

    def test_by_names(self):
        self.network._create.return_value = _assoc()
        cmd = na.CreateNetworkAssociation(self.app, None)
        result = self.run_cmd(cmd, ['red', 'net-a'])
        self.network._find.assert_called_once_with(
            resources.Srv6Domain, 'red', ignore_missing=False)
        self.network.find_network.assert_called_once_with(
            'net-a', ignore_missing=False)
        self.network._create.assert_called_once_with(
            ASSOC, domain_id=base.DOMAIN_ID, network_id=base.NETWORK_ID)
        self.assertShown(result, {
            'ID': base.ASSOC_ID,
            'Network ID': base.NETWORK_ID,
            'Project': base.PROJECT_ID,
        })

    def test_network_required(self):
        self.assertParserFails(na.CreateNetworkAssociation(self.app, None),
                               ['red'])


class TestListNetworkAssociation(_Base):

    def test_list(self):
        self.network._list.return_value = iter([_assoc()])
        cmd = na.ListNetworkAssociation(self.app, None)
        headers, rows = self.run_cmd(cmd, ['red'])
        self.network._list.assert_called_once_with(
            ASSOC, domain_id=base.DOMAIN_ID)
        self.assertEqual(('ID', 'Network ID'), headers)
        self.assertEqual([(base.ASSOC_ID, base.NETWORK_ID)], list(rows))

    def test_filter_by_network(self):
        self.network._list.return_value = iter([])
        cmd = na.ListNetworkAssociation(self.app, None)
        headers, rows = self.run_cmd(cmd, ['red', '--network', 'net-a',
                                           '--long'])
        self.network._list.assert_called_once_with(
            ASSOC, domain_id=base.DOMAIN_ID, network_id=base.NETWORK_ID)
        self.assertEqual(('ID', 'Network ID', 'Project'), headers)


class TestShowNetworkAssociation(_Base):

    def setUp(self):
        super().setUp()
        self.network._list.return_value = iter([_assoc()])
        self.cmd = na.ShowNetworkAssociation(self.app, None)

    def test_by_id(self):
        headers, values = self.run_cmd(self.cmd, ['red', base.ASSOC_ID])
        self.network.find_network.assert_not_called()
        self.assertEqual(base.ASSOC_ID, dict(zip(headers, values))['ID'])

    def test_by_network_name(self):
        headers, values = self.run_cmd(self.cmd, ['red', 'net-a'])
        self.network.find_network.assert_called_once_with(
            'net-a', ignore_missing=False)
        self.assertEqual(base.ASSOC_ID, dict(zip(headers, values))['ID'])

    def test_network_not_attached(self):
        self.network.find_network.return_value = mock.Mock(id='other')
        self.assertRaises(exceptions.CommandError,
                          self.run_cmd, self.cmd, ['red', 'net-z'])


class TestDeleteNetworkAssociation(_Base):

    def test_by_network(self):
        self.network._list.return_value = iter([_assoc()])
        cmd = na.DeleteNetworkAssociation(self.app, None)
        self.run_cmd(cmd, ['red', 'net-a'])
        self.network._delete.assert_called_once_with(
            ASSOC, base.ASSOC_ID, ignore_missing=False,
            domain_id=base.DOMAIN_ID)

    def test_partial_failure(self):
        # Each value lists again: a fresh iterator per call.
        self.network._list.side_effect = lambda *a, **k: iter([_assoc()])
        self.network.find_network.side_effect = [
            exceptions.CommandError('no such network')]
        cmd = na.DeleteNetworkAssociation(self.app, None)
        e = self.assertRaises(exceptions.CommandError, self.run_cmd, cmd,
                              ['red', 'net-z', base.ASSOC_ID])
        self.assertIn('1 of 2', str(e))
        self.network._delete.assert_called_once_with(
            ASSOC, base.ASSOC_ID, ignore_missing=False,
            domain_id=base.DOMAIN_ID)
