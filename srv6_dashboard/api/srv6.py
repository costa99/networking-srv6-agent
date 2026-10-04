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

"""The dashboard's only door to Neutron: one function per API operation.

Every call goes through Horizon's networkclient(request), an openstacksdk
network proxy authenticated with the logged-in user's token, so Neutron's
policy -- not this module -- decides what the user may see and do. The
proxy has no srv6_* methods; its generic _list/_get/_create/_update/_delete
take the CLI's own Resource classes (networking_srv6_agent/cli/resources.py),
so the GUI and `openstack srv6 ...` send byte-for-byte the same requests.

Results come back as SDK Resources and leave as NeutronAPIDictWrapper, the
dict-with-attributes Horizon tables expect. Errors are not caught here:
openstack.exceptions.* reach the views, whose exceptions.handle() turns them
into the message the user sees.

FEATURE-HORIZON-DASHBOARD.md: §3 (layers), §5 (one row per function), §6.
"""

import logging

from openstack_dashboard.api import neutron

from networking_srv6_agent.cli import resources

LOG = logging.getLogger(__name__)

networkclient = neutron.networkclient


class Srv6Domain(neutron.NeutronAPIDictWrapper):
    """An SRv6 domain: a tenant VRF carried over SRv6 (End.DT46)."""


class NetworkAssociation(neutron.NeutronAPIDictWrapper):
    """A network attached to a domain. Carries the domain_id it lives under."""


class Locator(neutron.NeutronAPIDictWrapper):
    """A host's SRv6 locator, as its agent reported it. Admin only."""


class TePath(neutron.NeutronAPIDictWrapper):
    """A traffic-engineering path inside a domain. Admin only."""


def _dict(res, **extra):
    # computed=False drops the SDK's client-side `location`; the URI
    # attributes (an association's domain_id) are not in the body either,
    # so the caller passes them back in.
    d = res.to_dict(computed=False)
    d.update(extra)
    return d


# --- domains -------------------------------------------------------------

def domain_list(request, **filters):
    """GET /v2.0/srv6_domains -- policy get_srv6_domain.

    A tenant sees its own project's domains, an admin every project's.
    sid_function is in the answer for admins only (get_srv6_domain:
    sid_function), and is None otherwise.
    """
    LOG.debug("domain_list(): filters=%s", filters)
    domains = networkclient(request)._list(resources.Srv6Domain, **filters)
    return [Srv6Domain(_dict(d)) for d in domains]


def domain_get(request, domain_id):
    """GET /v2.0/srv6_domains/{id} -- policy get_srv6_domain."""
    LOG.debug("domain_get(): domain_id=%s", domain_id)
    domain = networkclient(request)._get(resources.Srv6Domain, domain_id)
    return Srv6Domain(_dict(domain))


def domain_create(request, **attrs):
    """POST /v2.0/srv6_domains -- policy create_srv6_domain.

    attrs: name, srv6_behavior, and project_id when an admin creates the
    domain for another project. The server allocates sid_function.
    """
    LOG.debug("domain_create(): attrs=%s", attrs)
    domain = networkclient(request)._create(resources.Srv6Domain, **attrs)
    return Srv6Domain(_dict(domain))


def domain_update(request, domain_id, **attrs):
    """PUT /v2.0/srv6_domains/{id} -- policy update_srv6_domain.

    name is the only attribute the API lets anyone change.
    """
    LOG.debug("domain_update(): domain_id=%s, attrs=%s", domain_id, attrs)
    domain = networkclient(request)._update(resources.Srv6Domain, domain_id,
                                            **attrs)
    return Srv6Domain(_dict(domain))


def domain_delete(request, domain_id):
    """DELETE /v2.0/srv6_domains/{id} -- policy delete_srv6_domain.

    The server deletes the domain's associations and TE paths with it.
    """
    LOG.debug("domain_delete(): domain_id=%s", domain_id)
    networkclient(request)._delete(resources.Srv6Domain, domain_id,
                                   ignore_missing=False)


# --- network associations ------------------------------------------------

def network_association_list(request, domain_id, **filters):
    """GET /v2.0/srv6_domains/{id}/network_associations.

    Policy get_srv6_domain_network_association.
    """
    LOG.debug("network_association_list(): domain_id=%s, filters=%s",
              domain_id, filters)
    assocs = networkclient(request)._list(
        resources.Srv6NetworkAssociation, domain_id=domain_id, **filters)
    return [NetworkAssociation(_dict(a, domain_id=domain_id))
            for a in assocs]


def network_association_get(request, domain_id, assoc_id):
    """GET /v2.0/srv6_domains/{id}/network_associations/{assoc_id}.

    Policy get_srv6_domain_network_association.
    """
    LOG.debug("network_association_get(): domain_id=%s, assoc_id=%s",
              domain_id, assoc_id)
    assoc = networkclient(request)._get(
        resources.Srv6NetworkAssociation, assoc_id, domain_id=domain_id)
    return NetworkAssociation(_dict(assoc, domain_id=domain_id))


def network_association_create(request, domain_id, **attrs):
    """POST /v2.0/srv6_domains/{id}/network_associations.

    Policy create_srv6_domain_network_association. attrs: network_id. The
    plugin then checks that caller and network belong to the domain's
    project, that the network is in no other domain (F3), and that its
    subnets overlap none already in the domain (F10).
    """
    LOG.debug("network_association_create(): domain_id=%s, attrs=%s",
              domain_id, attrs)
    assoc = networkclient(request)._create(
        resources.Srv6NetworkAssociation, domain_id=domain_id, **attrs)
    return NetworkAssociation(_dict(assoc, domain_id=domain_id))


def network_association_delete(request, domain_id, assoc_id):
    """DELETE /v2.0/srv6_domains/{id}/network_associations/{assoc_id}.

    Policy delete_srv6_domain_network_association.
    """
    LOG.debug("network_association_delete(): domain_id=%s, assoc_id=%s",
              domain_id, assoc_id)
    networkclient(request)._delete(
        resources.Srv6NetworkAssociation, assoc_id, ignore_missing=False,
        domain_id=domain_id)


# --- locators ------------------------------------------------------------

def locator_list(request, **filters):
    """GET /v2.0/srv6_locators -- policy get_srv6_locator (admin).

    For anyone else Neutron answers an empty list, not a 403.
    """
    LOG.debug("locator_list(): filters=%s", filters)
    locators = networkclient(request)._list(resources.Srv6Locator,
                                            **filters)
    return [Locator(_dict(loc)) for loc in locators]


def locator_get(request, host):
    """GET /v2.0/srv6_locators/{host} -- policy get_srv6_locator (admin).

    The host name is the locator's ID.
    """
    LOG.debug("locator_get(): host=%s", host)
    locator = networkclient(request)._get(resources.Srv6Locator, host)
    return Locator(_dict(locator))


# --- TE paths ------------------------------------------------------------

def te_path_list(request, **filters):
    """GET /v2.0/srv6_te_paths -- policy get_srv6_te_path (admin).

    filters: domain_id, destination_host, destination_port_id. segments,
    encoded_segments and status are resolved by the server on every read.
    """
    LOG.debug("te_path_list(): filters=%s", filters)
    paths = networkclient(request)._list(resources.Srv6TePath, **filters)
    return [TePath(_dict(p)) for p in paths]


def te_path_get(request, path_id):
    """GET /v2.0/srv6_te_paths/{id} -- policy get_srv6_te_path (admin)."""
    LOG.debug("te_path_get(): path_id=%s", path_id)
    path = networkclient(request)._get(resources.Srv6TePath, path_id)
    return TePath(_dict(path))


def te_path_create(request, **attrs):
    """POST /v2.0/srv6_te_paths -- policy create_srv6_te_path (admin).

    attrs: domain_id, exactly one of destination_host and
    destination_port_id, and optionally via_hosts. The plugin forces the
    path's project to the domain's.
    """
    LOG.debug("te_path_create(): attrs=%s", attrs)
    path = networkclient(request)._create(resources.Srv6TePath, **attrs)
    return TePath(_dict(path))


def te_path_update(request, path_id, via_hosts):
    """PUT /v2.0/srv6_te_paths/{id} -- policy update_srv6_te_path (admin).

    via_hosts is the only attribute that can change, and the list is
    replaced whole: [] makes the path direct again.
    """
    LOG.debug("te_path_update(): path_id=%s, via_hosts=%s", path_id,
              via_hosts)
    path = networkclient(request)._update(resources.Srv6TePath, path_id,
                                          via_hosts=list(via_hosts))
    return TePath(_dict(path))


def te_path_delete(request, path_id):
    """DELETE /v2.0/srv6_te_paths/{id} -- policy delete_srv6_te_path."""
    LOG.debug("te_path_delete(): path_id=%s", path_id)
    networkclient(request)._delete(resources.Srv6TePath, path_id,
                                   ignore_missing=False)
