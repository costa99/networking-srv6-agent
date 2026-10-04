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

"""The enabled/ files: what devstack copies into Horizon (FEATURE §2.1, §8).

A wrong dotted path in ADD_PANEL does not fail at import: Horizon logs it
when it applies the panel customization, on a live server. So each file is
resolved here: its panel class must exist, be a horizon.Panel, carry the
slug the file names, and be registered where the file says.
"""

import glob
import importlib
import os
import pkgutil

import horizon

import srv6_dashboard.enabled
from srv6_dashboard.test import helpers

_DIR = os.path.dirname(srv6_dashboard.enabled.__file__)


def _enabled_modules():
    return sorted(name for __, name, __ in
                  pkgutil.iter_modules(srv6_dashboard.enabled.__path__))


class EnabledFilesTests(helpers.TestCase):

    def test_the_three_panels(self):
        self.assertEqual(['_1496_project_srv6_panel',
                          '_2361_admin_srv6_panel',
                          '_2362_admin_srv6_topology_panel'],
                         _enabled_modules())

    def test_each_file_names_a_registered_panel(self):
        for name in _enabled_modules():
            conf = importlib.import_module('srv6_dashboard.enabled.' + name)
            module, cls = conf.ADD_PANEL.rsplit('.', 1)
            panel = getattr(importlib.import_module(module), cls)
            self.assertTrue(issubclass(panel, horizon.Panel), name)
            self.assertEqual(conf.PANEL, panel.slug, name)
            self.assertEqual('network', conf.PANEL_GROUP, name)
            dashboard = horizon.get_dashboard(conf.PANEL_DASHBOARD)
            self.assertIsInstance(dashboard.get_panel(conf.PANEL), panel,
                                  name)

    def test_devstack_glob_matches_every_file(self):
        # devstack/settings SRV6_DASHBOARD_ENABLE, relative to the repo.
        pattern = os.path.join(_DIR, '_[0-9]*_srv6_*panel.py')
        self.assertEqual(
            [m + '.py' for m in _enabled_modules()],
            sorted(os.path.basename(f) for f in glob.glob(pattern)))
