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

"""SRv6 test data, shaped like the testbed: three hosts, usid encoding.

Each object exists twice, as Horizon's own test data does:
  TEST.api_<x>   the SDK Resources networkclient() would return -- what a
                 mocked proxy method hands back;
  TEST.<x>       the wrappers api/srv6.py makes of them -- what a test
                 expects to come out.
The values are realistic so a failing test reads like a real answer.
They are what the plugin computes in usid mode, the testbed's mode,
taken from networking_srv6_agent.common.sid (LOG Op 3c.1):
  - locators are the testbed's fc00:0:N::/48;
  - SID functions are in the LIB range, 0xe000 and up;
  - a waypoint is named by its uN SID, the locator's own address
    (fc00:0:2::);
  - the destination by its End.DT46 SID (fc00:0:3:e000::);
  - pushed, they pack into one container (fc00:0:2:3:e000::).
"""

import copy

from openstack_dashboard.test.test_data import utils

from networking_srv6_agent.cli import resources
from srv6_dashboard.api import srv6


def data(TEST):
    TEST.srv6_domains = _container()
    TEST.api_srv6_domains = []
    TEST.srv6_network_associations = _container()
    TEST.api_srv6_network_associations = []
    TEST.srv6_locators = _container()
    TEST.api_srv6_locators = []
    TEST.srv6_te_paths = _container()
    TEST.api_srv6_te_paths = []

    # Horizon's own test networks and project: the domain holds net1, the
    # same network Horizon's neutron tests use, in the same project ('1').
    net1 = TEST.networks.first()
    project_id = net1.tenant_id

    # --- domains: one with a network, one empty ---------------------------
    domain_red = {
        'id': '5e0c3a48-6f3e-4d6c-9a3b-1b2b2f9a0c01',
        'name': 'red',
        'project_id': project_id,
        'tenant_id': project_id,
        'srv6_behavior': 'End.DT46',
        'sid_function': 0xe000,
        'networks': [net1.id],
    }
    domain_blue = {
        'id': '5e0c3a48-6f3e-4d6c-9a3b-1b2b2f9a0c02',
        'name': 'blue',
        'project_id': project_id,
        'tenant_id': project_id,
        'srv6_behavior': 'End.DT46',
        'sid_function': 0xe001,
        'networks': [],
    }
    for d in (domain_red, domain_blue):
        _add(TEST.api_srv6_domains, TEST.srv6_domains,
             resources.Srv6Domain, srv6.Srv6Domain, d)

    # --- the association that puts net1 in red ----------------------------
    assoc = {
        'id': '9a4f1c2e-3b5d-4e6f-8a7b-0c1d2e3f4a01',
        'project_id': project_id,
        'tenant_id': project_id,
        'network_id': net1.id,
    }
    TEST.api_srv6_network_associations.append(
        resources.Srv6NetworkAssociation(domain_id=domain_red['id'],
                                         **assoc))
    TEST.srv6_network_associations.add(srv6.NetworkAssociation(
        dict(copy.deepcopy(assoc), domain_id=domain_red['id'])))

    # --- three hosts, as on the testbed ------------------------------------
    for n, host in enumerate(('ale', 'ale-XPS-15-9570', 'ale-node3'),
                             start=1):
        loc = {
            'id': host,
            'host': host,
            'locator': 'fc00:0:%d::/48' % n,
            'updated_at': '2026-10-04T08:00:0%dZ' % n,
        }
        _add(TEST.api_srv6_locators, TEST.srv6_locators,
             resources.Srv6Locator, srv6.Locator, loc)

    # --- TE paths in red: one healthy, one whose waypoint lost its locator -
    path_via_node2 = {
        'id': 'c3d4e5f6-0a1b-4c2d-8e3f-4a5b6c7d8e01',
        'project_id': project_id,
        'tenant_id': project_id,
        'domain_id': domain_red['id'],
        'destination_host': 'ale-node3',
        'destination_port_id': None,
        'source_host': None,
        'via_hosts': ['ale-XPS-15-9570'],
        'segments': ['fc00:0:2::', 'fc00:0:3:e000::'],
        'encoded_segments': ['fc00:0:2:3:e000::'],
        'status': 'ACTIVE',
    }
    path_degraded = {
        'id': 'c3d4e5f6-0a1b-4c2d-8e3f-4a5b6c7d8e02',
        'project_id': project_id,
        'tenant_id': project_id,
        'domain_id': domain_red['id'],
        'destination_host': None,
        'destination_port_id': TEST.ports.first().id,
        'source_host': None,
        'via_hosts': ['gone-host'],
        'segments': [],
        'encoded_segments': [],
        'status': 'DEGRADED',
    }
    for p in (path_via_node2, path_degraded):
        _add(TEST.api_srv6_te_paths, TEST.srv6_te_paths,
             resources.Srv6TePath, srv6.TePath, p)
    # Not in the container (its tests count two paths): a path limited to
    # the ingress ale, whose first waypoint is ale itself -- the server
    # leaves that SID out of the segments (FEATURE-TE-SOURCE-HOST).
    TEST.srv6_te_path_from_source = srv6.TePath(dict(
        path_via_node2, id='c3d4e5f6-0a1b-4c2d-8e3f-4a5b6c7d8e03',
        source_host='ale', via_hosts=['ale', 'ale-XPS-15-9570'],
        segments=['fc00:0:2::', 'fc00:0:3:e000::'],
        encoded_segments=['fc00:0:2:3:e000::']))


def _container():
    return utils.TestDataContainer()


def _add(api_list, container, resource_cls, wrapper_cls, d):
    api_list.append(resource_cls(**d))
    container.add(wrapper_cls(copy.deepcopy(d)))
