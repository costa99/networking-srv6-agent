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

"""The srv6-locators API extension.

The class name is the trap: neutron derives it from the file name with
str.capitalize(), which lowercases everything after the first character. So
srv6_locators.py must define `Srv6_locators`. `Srv6Locators` would never be
found, and GET /v2.0/srv6_locators would simply 404.
"""

from neutron.api.v2 import resource_helper
from neutron_lib.api import extensions as api_extensions

from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def


class Srv6_locators(api_extensions.APIExtensionDescriptor):

    api_definition = loc_def

    @classmethod
    def get_resources(cls):
        plural_mappings = resource_helper.build_plural_mappings(
            {}, loc_def.RESOURCE_ATTRIBUTE_MAP)
        # Served by the srv6 service plugin, hence its alias as the service.
        return resource_helper.build_resource_info(
            plural_mappings, loc_def.RESOURCE_ATTRIBUTE_MAP, srv6_def.ALIAS)
