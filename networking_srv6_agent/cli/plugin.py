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

"""OpenStackClient plugin hooks for the `openstack srv6 ...` commands.

Shaped like neutronclient's plugin: API_NAME must not be 'network', which OSC
owns, so the commands get their own group (openstack.srv6.v2) but still talk
to the openstacksdk network proxy. The version option is OSC's own network
one, so the same `--os-network-api-version` selects the group.

Nothing under cli/ may import neutron or neutron_lib: this code runs on
client machines (tests/unit/cli/test_imports.py).
"""

API_NAME = 'srv6'
API_VERSION_OPTION = 'os_network_api_version'
DEFAULT_API_VERSION = '2.0'
API_VERSIONS = ('2.0', '2')


def make_client(instance):
    """Return the openstacksdk network proxy."""
    return instance.sdk_connection.network


def build_option_parser(parser):
    """No global options of our own: OSC already defines the version one."""
    return parser
