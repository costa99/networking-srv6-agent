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

"""The srv6-aggregate-loads API extension.

The class must be `Srv6_aggregate_loads`: neutron derives the name from the
file name with str.capitalize() (see srv6_locators.py).
"""

from neutron.api.v2 import resource_helper
from neutron_lib.api import extensions as api_extensions
from neutron_lib import exceptions as n_exc

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_aggregate_loads as \
    ag_def


class Srv6AggregateLoadInvalid(n_exc.BadRequest):
    message = _("Invalid SRv6 aggregate load: %(reason)s")


class Srv6AggregateLoadNotFound(n_exc.NotFound):
    message = _("No SRv6 aggregate load %(id)s")


class Srv6_aggregate_loads(api_extensions.APIExtensionDescriptor):

    api_definition = ag_def

    @classmethod
    def get_resources(cls):
        plural_mappings = resource_helper.build_plural_mappings(
            {}, ag_def.RESOURCE_ATTRIBUTE_MAP)
        # allow_bulk: one batch per fabric node and period, stored in one
        # transaction (native bulk), as srv6_link_states.
        return resource_helper.build_resource_info(
            plural_mappings, ag_def.RESOURCE_ATTRIBUTE_MAP, srv6_def.ALIAS,
            allow_bulk=True)
