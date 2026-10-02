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

"""openstacksdk resources for the SRv6 API.

They live here rather than in openstacksdk: the network proxy has no srv6_*
methods, so the commands pass these classes to its generic _create/_get/...
calls. Base paths carry no /v2.0 -- the network proxy's endpoint already does.

The attribute names mirror api/definitions/, which is not imported: it pulls
in neutron_lib.
"""

from openstack import resource


class Srv6Domain(resource.Resource):
    resource_key = 'srv6_domain'
    resources_key = 'srv6_domains'
    base_path = '/srv6_domains'

    _allow_unknown_attrs_in_body = True

    allow_create = True
    allow_fetch = True
    allow_commit = True
    allow_delete = True
    allow_list = True

    _query_mapping = resource.QueryParameters('name', 'project_id')

    id = resource.Body('id')
    name = resource.Body('name')
    project_id = resource.Body('project_id', alias='tenant_id')
    tenant_id = resource.Body('tenant_id', deprecated=True)
    #: Create-only; End.DT46 is the one value the server accepts.
    srv6_behavior = resource.Body('srv6_behavior')
    #: Server-allocated, and visible to admins only.
    sid_function = resource.Body('sid_function', type=int)
    #: IDs of the associated networks.
    networks = resource.Body('networks', type=list)


class Srv6NetworkAssociation(resource.Resource):
    resource_key = 'network_association'
    resources_key = 'network_associations'
    base_path = '/srv6_domains/%(domain_id)s/network_associations'

    _allow_unknown_attrs_in_body = True

    allow_create = True
    allow_fetch = True
    allow_commit = False
    allow_delete = True
    allow_list = True

    _query_mapping = resource.QueryParameters('network_id', 'project_id')

    id = resource.Body('id')
    domain_id = resource.URI('domain_id')
    #: Forced to the domain's project by the server.
    project_id = resource.Body('project_id', alias='tenant_id')
    tenant_id = resource.Body('tenant_id', deprecated=True)
    network_id = resource.Body('network_id')


class Srv6Locator(resource.Resource):
    """Read-only and admin-only. The host name is the id."""

    resource_key = 'srv6_locator'
    resources_key = 'srv6_locators'
    base_path = '/srv6_locators'

    allow_fetch = True
    allow_list = True

    _query_mapping = resource.QueryParameters('host')

    id = resource.Body('id')
    host = resource.Body('host')
    locator = resource.Body('locator')
    updated_at = resource.Body('updated_at')


class Srv6TePath(resource.Resource):
    """Admin-only. Has no name, so it is found by ID only."""

    resource_key = 'srv6_te_path'
    resources_key = 'srv6_te_paths'
    base_path = '/srv6_te_paths'

    _allow_unknown_attrs_in_body = True

    allow_create = True
    allow_fetch = True
    allow_commit = True
    allow_delete = True
    allow_list = True

    _query_mapping = resource.QueryParameters(
        'domain_id', 'destination_host', 'destination_port_id', 'project_id')

    id = resource.Body('id')
    project_id = resource.Body('project_id', alias='tenant_id')
    tenant_id = resource.Body('tenant_id', deprecated=True)
    domain_id = resource.Body('domain_id')
    #: Exactly one of the two selectors is set; both are create-only.
    destination_host = resource.Body('destination_host')
    destination_port_id = resource.Body('destination_port_id')
    #: Traversal order. An update replaces the whole list.
    via_hosts = resource.Body('via_hosts', type=list)
    #: What the server resolved on read: via End SIDs, then the decap SID.
    segments = resource.Body('segments', type=list)
    #: What the ingress pushes: the same SIDs, or microSID containers.
    encoded_segments = resource.Body('encoded_segments', type=list)
    #: ACTIVE or DEGRADED.
    status = resource.Body('status')
