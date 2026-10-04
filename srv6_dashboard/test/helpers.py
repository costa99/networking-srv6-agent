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

"""Horizon's test cases with the SRv6 test data loaded on top.

openstack_dashboard's TestCase gives a logged-in fake user and request
(self.request), Horizon's test data (self.networks, self.tenants, ...) and
create_mocks; ours adds self.srv6_domains and friends (test_data/).
"""

from openstack_dashboard.test import helpers

from srv6_dashboard.test.test_data import utils


class TestCase(helpers.TestCase):

    def _setup_test_data(self):
        super()._setup_test_data()
        utils.load_test_data(self)


class BaseAdminViewTests(helpers.BaseAdminViewTests):

    def _setup_test_data(self):
        super()._setup_test_data()
        utils.load_test_data(self)


class APITestCase(helpers.APITestCase):

    def _setup_test_data(self):
        super()._setup_test_data()
        utils.load_test_data(self)
