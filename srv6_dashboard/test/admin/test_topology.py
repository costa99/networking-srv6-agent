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

"""The topology panel: the data it draws, and the two URLs.

topology.build() decides everything the picture shows, so it is tested
directly; the views are tested for their API calls and their answers.
"""

import json

from django.urls import reverse
from openstack_dashboard.api import neutron
from openstack_dashboard.test import helpers as test

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.admin.srv6_topology import topology
from srv6_dashboard.test import helpers

INDEX_URL = reverse('horizon:admin:srv6_topology:index')
DATA_URL = reverse('horizon:admin:srv6_topology:data')


def _port(port_id, network_id, host, owner='compute:nova'):
    # The SDK's attribute names, as api.port_list() wraps them.
    return neutron.Port({'id': port_id, 'network_id': network_id,
                         'device_owner': owner, 'binding_host_id': host,
                         'fixed_ips': [], 'name': ''})


class BuildTests(helpers.TestCase):

    def setUp(self):
        super().setUp()
        self.net1 = self.networks.first().id
        self.ports_ = [
            _port('p-vm-3a', self.net1, 'ale-node3'),
            _port('p-vm-3b', self.net1, 'ale-node3'),
            _port('p-vm-1', self.net1, 'ale'),
            # Not VMs: no presence for the domain.
            _port('p-dhcp', self.net1, 'ale', owner='network:dhcp'),
            # A VM on a host whose agent never registered a locator.
            _port('p-vm-x', self.net1, 'mystery-host'),
            # A VM port not bound yet.
            _port('p-unbound', self.net1, None),
        ]

    def _build(self, paths=None, ports=None):
        return topology.build(
            self.srv6_locators.list(), self.srv6_domains.list(),
            self.ports_ if ports is None else ports,
            self.srv6_te_paths.list() if paths is None else paths,
            lambda pid: '/path/%s/' % pid)

    def test_hosts_include_unregistered_ones_flagged(self):
        data = self._build()

        hosts = {h['host']: h for h in data['hosts']}
        self.assertEqual(['ale', 'ale-XPS-15-9570', 'ale-node3',
                          'gone-host', 'mystery-host'],
                         [h['host'] for h in data['hosts']])
        self.assertEqual('fc00:0:3::/48', hosts['ale-node3']['locator'])
        # Drawn, but flagged: it can be neither waypoint nor destination.
        # mystery-host runs a VM; gone-host is the DEGRADED path's
        # waypoint, and without it that path would not be drawn at all
        # (found by rendering the page, LOG Op 4.4).
        self.assertIsNone(hosts['mystery-host']['locator'])
        self.assertIsNone(hosts['gone-host']['locator'])

    def test_domain_presence_counts_vms_only(self):
        data = self._build()

        red = [d for d in data['domains'] if d['name'] == 'red'][0]
        # Two VMs on node 3, one on node 1; the DHCP and unbound ports
        # do not count.
        self.assertEqual({'ale': 1, 'ale-node3': 2, 'mystery-host': 1},
                         red['hosts'])
        self.assertEqual(1, red['networks'])
        blue = [d for d in data['domains'] if d['name'] == 'blue'][0]
        self.assertEqual({}, blue['hosts'])

    def test_host_path_hops(self):
        data = self._build()

        path = data['te_paths'][0]
        self.assertEqual(['ale-XPS-15-9570', 'ale-node3'], path['hops'])
        self.assertEqual('host ale-node3', path['destination'])
        self.assertEqual('ACTIVE', path['status'])
        self.assertEqual('/path/%s/' % path['id'], path['url'])

    def test_port_path_ends_where_the_port_is_bound(self):
        path = self.srv6_te_paths.list()[1]
        port = _port(path.destination_port_id, self.net1, 'ale')

        data = self._build(paths=[path], ports=[port])

        self.assertEqual(['gone-host', 'ale'], data['te_paths'][0]['hops'])
        self.assertEqual('port %s on ale' % path.destination_port_id[:8],
                         data['te_paths'][0]['destination'])

    def test_port_path_to_unbound_port_has_no_destination_hop(self):
        path = self.srv6_te_paths.list()[1]

        data = self._build(paths=[path], ports=[])

        self.assertEqual(['gone-host'], data['te_paths'][0]['hops'])
        self.assertEqual('port %s' % path.destination_port_id[:8],
                         data['te_paths'][0]['destination'])

    def test_a_source_path_starts_at_its_source(self):
        # Limited to the ingress ale, first waypoint ale itself: the hops
        # start at ale once, then the real waypoint, then the destination.
        data = self._build(paths=[self.srv6_te_path_from_source])

        path = data['te_paths'][0]
        self.assertEqual('ale', path['source'])
        self.assertEqual(['ale', 'ale-XPS-15-9570', 'ale-node3'],
                         path['hops'])

    def test_an_any_source_path_has_no_source(self):
        self.assertIsNone(self._build()['te_paths'][0]['source'])

    def test_is_json_serialisable(self):
        json.dumps(self._build())


class TopologyViewTests(helpers.BaseAdminViewTests):

    def test_index(self):
        res = self.client.get(INDEX_URL)

        self.assertTemplateUsed(res, 'admin/srv6_topology/index.html')
        self.assertContains(res, 'data-url="%s"' % DATA_URL)
        # The drawing script is inline: no static file to collect (D22).
        self.assertContains(res, "getElementById('srv6-topology')")

    @test.create_mocks({srv6_api: ('locator_list', 'domain_list',
                                   'port_list', 'te_path_list')})
    def test_data(self):
        self.mock_locator_list.return_value = self.srv6_locators.list()
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_port_list.return_value = []
        self.mock_te_path_list.return_value = self.srv6_te_paths.list()

        res = self.client.get(DATA_URL)

        self.assertEqual(200, res.status_code)
        body = json.loads(res.content)
        self.assertEqual(['hosts', 'domains', 'te_paths'], list(body))
        # The three locators, and the DEGRADED path's lost waypoint.
        self.assertEqual(4, len(body['hosts']))
        self.assertEqual(
            reverse('horizon:admin:srv6:te_path_detail',
                    args=[self.srv6_te_paths.first().id]),
            body['te_paths'][0]['url'])
        # Every domain network in one ports request; no filters elsewhere.
        self.mock_port_list.assert_called_once_with(
            test.IsHttpRequest(), network_id=[self.networks.first().id])
        self.mock_locator_list.assert_called_once_with(test.IsHttpRequest())
        self.mock_domain_list.assert_called_once_with(test.IsHttpRequest())
        self.mock_te_path_list.assert_called_once_with(test.IsHttpRequest())

    @test.create_mocks({srv6_api: ('locator_list', 'domain_list',
                                   'port_list', 'te_path_list')})
    def test_data_without_networks_skips_ports(self):
        self.mock_locator_list.return_value = self.srv6_locators.list()
        self.mock_domain_list.return_value = [self.srv6_domains.list()[1]]
        self.mock_te_path_list.return_value = []

        res = self.client.get(DATA_URL)

        self.assertEqual(200, res.status_code)
        self.mock_port_list.assert_not_called()

    @test.create_mocks({srv6_api: ('locator_list',)})
    def test_data_error_is_json(self):
        # The script shows the message; a redirect would mean nothing to
        # it.
        self.mock_locator_list.side_effect = self.exceptions.neutron

        res = self.client.get(DATA_URL)

        self.assertEqual(502, res.status_code)
        self.assertIn('error', json.loads(res.content))


class TopologyNonAdminTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('locator_list',)})
    def test_data_is_admin_only(self):
        res = self.client.get(DATA_URL)

        self.assertEqual(403, res.status_code)
        self.mock_locator_list.assert_not_called()
