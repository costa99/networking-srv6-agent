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

from openstack_dashboard.test.test_data import utils


def load_test_data(load_onto=None):
    """Add the SRv6 data on top of Horizon's.

    openstack_dashboard's TestCase has already loaded all of Horizon's own
    test data (networks, ports, tenants, ...) by the time this runs, and
    srv6_data builds on it, so only our loader runs here.
    """
    from srv6_dashboard.test.test_data import srv6_data

    if load_onto:
        srv6_data.data(load_onto)
        return load_onto
    return utils.TestData(srv6_data.data)
