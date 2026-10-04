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

"""Horizon's Network Topology with SRv6 domain links (FEATURE D24).

topology.srv6_domains() decides which links the page draws, so it is
tested directly; the views are tested for being Horizon's plus the
domains, and the panel for replacing Horizon's in place.
"""

import json

from django.urls import reverse
import horizon
from openstack_dashboard import api
from openstack_dashboard.dashboards.project.network_topology import (
    panel as core_panel)
from openstack_dashboard.test import helpers as test
from openstack_dashboard.usage import quotas

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.project.network_topology import panel
from srv6_dashboard.dashboards.project.network_topology import topology
from srv6_dashboard.test import helpers

INDEX_URL = reverse('horizon:project:network_topology:index')
JSON_URL = reverse('horizon:project:network_topology:json')


def _domain(domain_id, name, networks):
    return srv6_api.Srv6Domain({'id': domain_id, 'name': name,
                                'networks': networks})


class Srv6DomainsTests(helpers.TestCase):

    def _build(self, domains, network_ids=('n1', 'n2', 'n3', 'n4')):
        return topology.srv6_domains(domains, list(network_ids),
                                     lambda did: '/domain/%s/' % did)

    def test_two_networks_one_link(self):
        out = self._build([_domain('d1', 'red', ['n3', 'n1'])])

        self.assertEqual([{'id': 'd1', 'name': 'red', 'url': '/domain/d1/',
                           # In the topology's order, not the domain's.
                           'networks': ['n1', 'n3'],
                           'links': [['n1', 'n3']]}], out)

    def test_every_pair_is_linked(self):
        out = self._build([_domain('d1', 'red', ['n1', 'n2', 'n4'])])

        self.assertEqual([['n1', 'n2'], ['n1', 'n4'], ['n2', 'n4']],
                         out[0]['links'])

    def test_networks_not_drawn_are_left_out(self):
        # n9 is not on the page (another project's, or deleted): nothing
        # to attach a line to. Left with one network, blue links nothing.
        out = self._build([_domain('d1', 'red', ['n1', 'n9', 'n2']),
                           _domain('d2', 'blue', ['n3', 'n9'])])

        self.assertEqual(['red'], [d['name'] for d in out])
        self.assertEqual(['n1', 'n2'], out[0]['networks'])

    def test_domains_without_two_networks_are_skipped(self):
        out = self._build([_domain('d1', 'red', ['n1']),
                           _domain('d2', 'blue', [])])

        self.assertEqual([], out)

    def test_sorted_by_name(self):
        # The page colours domains by position: keep it stable.
        out = self._build([_domain('d1', 'red', ['n1', 'n2']),
                           _domain('d2', 'blue', ['n3', 'n4'])])

        self.assertEqual(['blue', 'red'], [d['name'] for d in out])


class PanelTests(helpers.TestCase):

    def test_replaces_horizons_panel_in_place(self):
        dashboard = horizon.get_dashboard('project')

        registered = dashboard.get_panel('network_topology')
        self.assertIsInstance(registered, panel.NetworkTopology)
        self.assertIsInstance(registered, core_panel.NetworkTopology)
        # Registered once, at Horizon's place in the Network group.
        slugs = [p.slug for p in dashboard.get_panel_group('network')]
        self.assertEqual(1, slugs.count('network_topology'))
        self.assertLess(slugs.index('network_topology'), slugs.index('srv6'))


class NetworkTopologyViewTests(helpers.TestCase):

    @test.create_mocks({quotas: ('tenant_quota_usages',)})
    def test_index(self):
        # Horizon's page reads the quotas for its Create buttons.
        self.mock_tenant_quota_usages.return_value = (
            self.quota_usages.first())

        res = self.client.get(INDEX_URL)

        self.assertTemplateUsed(res, 'project/srv6/network_topology.html')
        # Horizon's page, its includes found in Horizon's directory
        # (panel.py), with the script added.
        self.assertTemplateUsed(res, 'project/network_topology/index.html')
        self.assertContains(res, 'id="networktopology"')
        self.assertContains(res, 'data-networktopology="%s"' % JSON_URL)
        self.assertContains(res, 'model.srv6_domains')

    def _mock_horizon_reads(self, networks):
        # What Horizon's own JSON view reads (its tests mock the same).
        self.mock_server_list.return_value = [[], False]
        self.mock_network_list_for_tenant.return_value = networks
        self.mock_network_list.return_value = []
        self.mock_router_list.return_value = []
        self.mock_port_list.return_value = []

    @test.create_mocks({api.nova: ('server_list',),
                        api.neutron: ('network_list_for_tenant',
                                      'network_list', 'router_list',
                                      'port_list'),
                        srv6_api: ('domain_list',)})
    def test_json_adds_srv6_domains(self):
        nets = [n for n in self.networks.list()
                if not n['is_router_external']][:2]
        self._mock_horizon_reads(nets)
        self.mock_domain_list.return_value = [
            _domain('d1', 'red', [n.id for n in nets])]

        res = self.client.get(JSON_URL)

        self.assertEqual(200, res.status_code)
        self.assertEqual('text/json', res['Content-Type'])
        data = json.loads(res.content)
        # Horizon's answer is all there, plus the domains.
        self.assertEqual({'servers', 'networks', 'ports', 'routers',
                          'srv6_domains'}, set(data))
        self.assertEqual([n.id for n in nets],
                         [n['id'] for n in data['networks']])
        self.assertEqual(
            [{'id': 'd1', 'name': 'red',
              'url': reverse('horizon:project:srv6:detail', args=['d1']),
              'networks': [nets[0].id, nets[1].id],
              'links': [[nets[0].id, nets[1].id]]}],
            data['srv6_domains'])
        # The current project's domains, even for an admin (D14).
        self.mock_domain_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=self.tenant.id)

    @test.create_mocks({api.nova: ('server_list',),
                        api.neutron: ('network_list_for_tenant',
                                      'network_list', 'router_list',
                                      'port_list'),
                        srv6_api: ('domain_list',)})
    def test_json_domain_error_keeps_the_topology(self):
        nets = self.networks.list()[:1]
        self._mock_horizon_reads(nets)
        self.mock_domain_list.side_effect = self.exceptions.neutron

        res = self.client.get(JSON_URL)

        self.assertEqual(200, res.status_code)
        data = json.loads(res.content)
        self.assertEqual([], data['srv6_domains'])
        self.assertEqual(1, len(data['networks']))
