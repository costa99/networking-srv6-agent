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

"""How the CLI is wired into OSC, and what it may import.

An entry point with a typo does not fail any other test: OSC only warns
when it loads the command, on someone's terminal.
"""

import importlib
import os
import subprocess
import sys
import tomllib
from unittest import mock

from osc_lib.command import command
import testtools

import networking_srv6_agent
from networking_srv6_agent.cli import plugin

_ROOT = os.path.dirname(os.path.dirname(
    os.path.abspath(networking_srv6_agent.__file__)))
_PYPROJECT = os.path.join(_ROOT, 'pyproject.toml')


def _entry_points():
    with open(_PYPROJECT, 'rb') as f:
        return tomllib.load(f)['project']['entry-points']


class TestEntryPoints(testtools.TestCase):

    def setUp(self):
        super().setUp()
        if not os.path.exists(_PYPROJECT):
            self.skipTest('not running from a source tree')
        self.entry_points = _entry_points()

    def test_plugin_module(self):
        self.assertEqual(
            {'srv6': 'networking_srv6_agent.cli.plugin'},
            self.entry_points['openstack.cli.extension'])

    def test_group_matches_plugin(self):
        # OSC builds the group name from the plugin's API_NAME and the major
        # version of DEFAULT_API_VERSION.
        group = 'openstack.%s.v%s' % (
            plugin.API_NAME, plugin.DEFAULT_API_VERSION.split('.')[0])
        self.assertIn(group, self.entry_points)

    def test_every_command_resolves(self):
        commands = self.entry_points['openstack.srv6.v2']
        self.assertEqual(16, len(commands))
        for name, target in commands.items():
            self.assertTrue(name.startswith('srv6_'), name)
            module, attr = target.split(':')
            cls = getattr(importlib.import_module(module), attr)
            self.assertTrue(issubclass(cls, command.Command), target)

    def test_make_client_is_the_network_proxy(self):
        instance = mock.Mock()
        self.assertIs(instance.sdk_connection.network,
                      plugin.make_client(instance))


class TestNoServerImports(testtools.TestCase):

    def test_cli_does_not_import_neutron(self):
        modules = ['networking_srv6_agent.cli.plugin',
                   'networking_srv6_agent.cli.v2.domain',
                   'networking_srv6_agent.cli.v2.network_association',
                   'networking_srv6_agent.cli.v2.locator',
                   'networking_srv6_agent.cli.v2.te_path']
        code = ('import sys\n'
                'for m in %r: __import__(m)\n'
                'print(sorted(m for m in sys.modules\n'
                '             if m.split(".")[0] in ("neutron",'
                ' "neutron_lib")))' % modules)
        out = subprocess.check_output([sys.executable, '-c', code],
                                      cwd=_ROOT, text=True)
        self.assertEqual('[]', out.strip())
