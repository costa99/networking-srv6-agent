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

"""The srv6-te API extension: descriptor and exceptions.

The file is srv6_te.py, so the class has to be `Srv6_te`: neutron derives
it with str.capitalize(), which lowercases everything after the first
character. `Srv6Te` would be skipped and GET /v2.0/srv6_te_paths would
simply 404 (P6-PLAN.md 8).
"""

from neutron.api.v2 import resource_helper
from neutron_lib.api import extensions as api_extensions
from neutron_lib import exceptions as n_exc

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_te as te_def


class Srv6TePathNotFound(n_exc.NotFound):
    message = _("SRv6 TE path %(id)s could not be found")


class Srv6TePathInvalid(n_exc.BadRequest):
    message = _("Invalid SRv6 TE path: %(reason)s")


class Srv6TePathExists(n_exc.Conflict):
    message = _("SRv6 domain %(domain_id)s already has a TE path for "
                "%(selector)s")


class Srv6_te(api_extensions.APIExtensionDescriptor):

    api_definition = te_def

    @classmethod
    def get_resources(cls):
        plural_mappings = resource_helper.build_plural_mappings(
            {}, te_def.RESOURCE_ATTRIBUTE_MAP)
        # Served by the srv6 service plugin, hence its alias as the service.
        # No bulk (the default): emulated bulk runs the plugin method in a
        # loop inside one transaction, which would fan RPC out before commit.
        return resource_helper.build_resource_info(
            plural_mappings, te_def.RESOURCE_ATTRIBUTE_MAP, srv6_def.ALIAS)
