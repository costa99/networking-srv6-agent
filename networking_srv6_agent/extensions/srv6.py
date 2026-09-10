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

"""The srv6 API extension: descriptor, plugin interface and exceptions.

Neutron discovers this module by scanning the directory and deriving the
class name from the FILE name with str.capitalize() (neutron/api/
extensions.py). srv6.py therefore has to hold a class called exactly `Srv6`;
any other spelling is silently skipped and the API answers 404.
"""

import abc

from neutron.api import extensions
from neutron.api.v2 import base
from neutron.api.v2 import resource_helper
from neutron.common import config as common_config
from neutron_lib.api import extensions as api_extensions
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from neutron_lib.services import base as libbase

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent import extensions as srv6_extensions


# api_extensions_path is a neutron option: register it before appending to
# it, as networking-bgpvpn does. Neutron also finds this directory on its
# own (get_extensions_path adds <plugin top package>.extensions for every
# service plugin); this covers anything that loads the module first.
common_config.register_common_config_options()
extensions.append_api_extensions_path(srv6_extensions.__path__)


class Srv6DomainNotFound(n_exc.NotFound):
    message = _("SRv6 domain %(id)s could not be found")


class Srv6DomainNetAssocNotFound(n_exc.NotFound):
    message = _("Network association %(id)s could not be found for SRv6 "
                "domain %(domain_id)s")


class Srv6DomainNetAssocAlreadyExists(n_exc.Conflict):
    message = _("Network %(network_id)s is already associated with SRv6 "
                "domain %(domain_id)s")


class Srv6ExternalNetworkNotAllowed(n_exc.BadRequest):
    message = _("External network %(network_id)s cannot be attached to an "
                "SRv6 domain: a domain carries no north-south traffic")


class Srv6RouteredNetworkNotAllowed(n_exc.BadRequest):
    message = _("Network %(network_id)s has an interface on router "
                "%(router_id)s and cannot be attached to an SRv6 domain: the "
                "domain's gateway port and the router would both route for "
                "the same subnet")


class Srv6NetworkNotOwned(n_exc.NotAuthorized):
    message = _("Network %(network_id)s does not belong to the project that "
                "owns SRv6 domain %(domain_id)s")


class Srv6NetworkInDomain(n_exc.Conflict):
    message = _("Network %(network_id)s is attached to SRv6 domain "
                "%(domain_id)s; remove the association before adding a "
                "router interface to it")


class Srv6NoFunctionIdAvailable(n_exc.Conflict):
    message = _("No SRv6 SID function id is available; the configured "
                "function_id_ranges pool is exhausted")


class Srv6LocatorNotFound(n_exc.NotFound):
    message = _("No SRv6 locator is registered for host %(id)s")


class Srv6(api_extensions.APIExtensionDescriptor):

    api_definition = srv6_def

    @classmethod
    def get_resources(cls):
        plural_mappings = resource_helper.build_plural_mappings(
            {}, srv6_def.RESOURCE_ATTRIBUTE_MAP)
        # register_quota: the function id pool is finite and shared by every
        # project, so an unquota'd create is a cross-tenant exhaustion
        # vector. The limit itself is quota_srv6_domain (unlimited unless
        # set).
        resources = resource_helper.build_resource_info(
            plural_mappings, srv6_def.RESOURCE_ATTRIBUTE_MAP, srv6_def.ALIAS,
            register_quota=True)
        plugin = directory.get_plugin(srv6_def.ALIAS)
        for collection_name, sub in (
                srv6_def.SUB_RESOURCE_ATTRIBUTE_MAP.items()):
            resource_name = collection_name[:-1]
            parent = sub['parent']
            params = sub['parameters']
            # allow_bulk=False: emulated bulk runs the plugin method in a loop
            # inside one transaction, which would fan RPC out before commit.
            controller = base.create_resource(
                collection_name, resource_name, plugin, params,
                allow_bulk=False, parent=parent,
                allow_pagination=True, allow_sorting=True)
            resources.append(extensions.ResourceExtension(
                collection_name, controller, parent, attr_map=params))
        return resources

    @classmethod
    def get_plugin_interface(cls):
        return Srv6PluginBase


class Srv6PluginBase(libbase.ServicePluginBase, metaclass=abc.ABCMeta):
    """The methods neutron's controllers call, by their derived names.

    No router-association methods at all (MIGRATION-PLAN.md 8.1): an
    endpoint that does not exist answers 404, which is honest; a method that
    only ever raises answers 500. No update for associations either: there
    is nothing on one to update.
    """

    supported_extension_aliases = [srv6_def.ALIAS]

    def get_plugin_type(self):
        return srv6_def.ALIAS

    def get_plugin_description(self):
        return "SRv6 connectivity and traffic engineering"

    @abc.abstractmethod
    def create_srv6_domain(self, context, srv6_domain):
        pass

    @abc.abstractmethod
    def get_srv6_domain(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def get_srv6_domains(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def update_srv6_domain(self, context, id, srv6_domain):
        pass

    @abc.abstractmethod
    def delete_srv6_domain(self, context, id):
        pass

    @abc.abstractmethod
    def create_srv6_domain_network_association(self, context, srv6_domain_id,
                                               network_association):
        pass

    @abc.abstractmethod
    def get_srv6_domain_network_association(self, context, id,
                                            srv6_domain_id, fields=None):
        pass

    @abc.abstractmethod
    def get_srv6_domain_network_associations(self, context, srv6_domain_id,
                                             filters=None, fields=None):
        pass

    @abc.abstractmethod
    def delete_srv6_domain_network_association(self, context, id,
                                               srv6_domain_id):
        pass
