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

"""TE paths in the admin panel: the tab, the forms, the detail page.

The forms carry most of the logic here -- host choices from locators,
ordered waypoints, one destination, a port on the domain's networks --
and each of their rules is checked twice: the valid case reaches the API
with exactly the body the CLI would send, the invalid one stops at the
form with no request at all.
"""

from django.urls import reverse
from openstack_dashboard.api import keystone
from openstack_dashboard.test import helpers as test

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.admin.srv6.te_paths import tabs as te_tabs
from srv6_dashboard.test import helpers

INDEX_URL = reverse('horizon:admin:srv6:index')
TE_TAB_URL = INDEX_URL + '?tab=srv6__te_paths'
CREATE_URL = reverse('horizon:admin:srv6:te_path_create')
HOSTS = ['ale', 'ale-XPS-15-9570', 'ale-node3']


class TePathsTabTests(helpers.BaseAdminViewTests):

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'te_path_list'),
                        keystone: ('tenant_list',)})
    def test_index_te_tab(self):
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_list.return_value = [self.tenants.list(), False]
        self.mock_te_path_list.return_value = self.srv6_te_paths.list()

        res = self.client.get(TE_TAB_URL)

        table = res.context['te_paths_table']
        self.assertEqual([p.id for p in self.srv6_te_paths.list()],
                         [p.id for p in table.data])
        self.mock_te_path_list.assert_called_once_with(test.IsHttpRequest())
        # Rows name their domain; the Domains tab and the TE tab share
        # one domain_list per page (AdminSrv6Tabs.get_domains).
        self.assertEqual('red', table.data[0].domain_name)
        self.mock_domain_list.assert_called_once_with(test.IsHttpRequest())
        # Segments as code, status as a label, waypoints in order.
        self.assertContains(res, '<code>fc00:0:2::</code>')
        self.assertContains(res, '<span class="label label-warning">'
                                 'DEGRADED</span>')
        self.assertContains(res, 'ale-XPS-15-9570')

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'te_path_list'),
                        keystone: ('tenant_list',)})
    def test_index_default_tab_does_not_fetch_te_paths(self):
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_list.return_value = [self.tenants.list(), False]

        self.client.get(INDEX_URL)

        self.mock_te_path_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_get', 'network_list',
                                   'te_path_list'),
                        keystone: ('tenant_get',)})
    def test_domain_te_tab(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_get.return_value = self.tenant
        self.mock_te_path_list.return_value = self.srv6_te_paths.list()

        res = self.client.get(
            reverse('horizon:admin:srv6:detail', args=[domain.id]) +
            '?tab=domain_details__te_paths')

        # The server filters by domain: GET /v2.0/srv6_te_paths?domain_id=
        self.mock_te_path_list.assert_called_once_with(
            test.IsHttpRequest(), domain_id=domain.id)
        # "Create TE Path" from here opens with this domain selected.
        self.assertContains(res, '%s?domain_id=%s' % (CREATE_URL, domain.id))

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'te_path_list', 'te_path_delete'),
                        keystone: ('tenant_list',)})
    def test_delete(self):
        path = self.srv6_te_paths.first()
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_list.return_value = [self.tenants.list(), False]
        self.mock_te_path_list.return_value = self.srv6_te_paths.list()
        self.mock_te_path_delete.return_value = None

        res = self.client.post(TE_TAB_URL, {
            'action': 'te_paths__delete__%s' % path.id})

        self.mock_te_path_delete.assert_called_once_with(
            test.IsHttpRequest(), path.id)
        self.assertRedirectsNoFollow(res, TE_TAB_URL)


class CreateTePathTests(helpers.BaseAdminViewTests):

    def _mock_choices(self):
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_locator_list.return_value = self.srv6_locators.list()
        self.mock_port_list.return_value = self.ports.list()

    def _vm_port(self, network_id):
        return [p for p in self.ports.list()
                if p.network_id == network_id and
                p.device_owner.startswith('compute:')][0]

    def _post(self, **fields):
        red = self.srv6_domains.first()
        data = {'domain_id': red.id, 'destination_type': 'host',
                'destination_host': '', 'destination_port_id': ''}
        data.update(fields)
        return self.client.post(CREATE_URL, data)

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list')})
    def test_create_get_choices(self):
        self._mock_choices()
        net1 = self.networks.first()

        res = self.client.get(CREATE_URL)

        form = res.context['form']
        # Hosts: exactly the registered locators (D6), for the
        # destination and for every waypoint.
        self.assertEqual(HOSTS, [c[0] for c in
                                 form.fields['destination_host'].choices
                                 if c[0]])
        self.assertEqual(HOSTS, [c[0] for c in
                                 form.fields['via_host_1'].choices if c[0]])
        # As many waypoint fields as the server accepts (D12).
        self.assertIn('via_host_6', form.fields)
        self.assertNotIn('via_host_7', form.fields)
        # Ports: the VM ports on the domains' networks, in one request.
        self.mock_port_list.assert_called_once_with(test.IsHttpRequest(),
                                                    network_id=[net1.id])
        ports = [c[0] for c in form.fields['destination_port_id'].choices
                 if c[0]]
        self.assertEqual([self._vm_port(net1.id).id], ports)

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list')})
    def test_create_get_preselects_domain(self):
        self._mock_choices()
        blue = self.srv6_domains.list()[1]

        res = self.client.get(CREATE_URL + '?domain_id=%s' % blue.id)

        self.assertEqual(blue.id, res.context['form'].initial['domain_id'])

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_host_path_with_waypoints(self):
        self._mock_choices()
        self.mock_te_path_create.return_value = self.srv6_te_paths.first()
        red = self.srv6_domains.first()

        res = self._post(destination_host='ale-node3',
                         via_host_1='ale-XPS-15-9570', via_host_2='ale')

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, TE_TAB_URL)
        # Waypoints in the order given; no destination_port_id at all.
        self.mock_te_path_create.assert_called_once_with(
            test.IsHttpRequest(), domain_id=red.id,
            destination_host='ale-node3',
            via_hosts=['ale-XPS-15-9570', 'ale'])

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_direct_path_sends_no_via_hosts(self):
        # As the CLI: no waypoints, no via_hosts key (same body).
        self._mock_choices()
        self.mock_te_path_create.return_value = self.srv6_te_paths.first()
        red = self.srv6_domains.first()

        res = self._post(destination_host='ale-node3')

        self.assertNoFormErrors(res)
        self.mock_te_path_create.assert_called_once_with(
            test.IsHttpRequest(), domain_id=red.id,
            destination_host='ale-node3')

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_port_path(self):
        self._mock_choices()
        self.mock_te_path_create.return_value = self.srv6_te_paths.first()
        red = self.srv6_domains.first()
        port = self._vm_port(self.networks.first().id)

        # A stale destination_host from the hidden field is dropped.
        res = self._post(destination_type='port', destination_host='ale',
                         destination_port_id=port.id, via_host_1='ale')

        self.assertNoFormErrors(res)
        self.mock_te_path_create.assert_called_once_with(
            test.IsHttpRequest(), domain_id=red.id,
            destination_port_id=port.id, via_hosts=['ale'])

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_port_not_in_domain(self):
        # net1's port, but domain blue: _validate_destination_port's rule.
        self._mock_choices()
        blue = self.srv6_domains.list()[1]
        port = self._vm_port(self.networks.first().id)

        res = self._post(domain_id=blue.id, destination_type='port',
                         destination_port_id=port.id)

        self.assertFormErrors(res, 1,
                              'This port is not on a network of the '
                              'selected domain.')
        self.mock_te_path_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_requires_the_chosen_destination(self):
        # destination_type=host, but no host: the "exactly one" rule.
        self._mock_choices()

        res = self._post()

        self.assertFormErrors(res, 1, 'Select the destination host.')
        self.mock_te_path_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_unknown_host_is_refused(self):
        # Not a registered locator: not a valid choice (D6).
        self._mock_choices()

        res = self._post(destination_host='no-such-host')

        self.assertFormErrors(res, 1)
        self.mock_te_path_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_waypoint_gap_is_refused(self):
        self._mock_choices()

        res = self._post(destination_host='ale-node3', via_host_2='ale')

        self.assertFormErrors(res, 1,
                              'Fill the waypoints in order, without gaps.')
        self.mock_te_path_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_duplicate_waypoint_is_refused(self):
        # The API's list_of_unique_strings validator would 400.
        self._mock_choices()

        res = self._post(destination_host='ale-node3', via_host_1='ale',
                         via_host_2='ale')

        self.assertFormErrors(res, 1,
                              'A host can be a waypoint only once.')
        self.mock_te_path_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'locator_list',
                                   'port_list', 'te_path_create')})
    def test_create_server_refuses(self):
        # e.g. 409 Srv6TePathExists: the same destination twice.
        self._mock_choices()
        self.mock_te_path_create.side_effect = self.exceptions.neutron

        res = self._post(destination_host='ale-node3')

        self.assertRedirectsNoFollow(res, TE_TAB_URL)
        self.assertMessageCount(error=1)


class UpdateTePathTests(helpers.BaseAdminViewTests):

    def _url(self, path):
        return reverse('horizon:admin:srv6:te_path_update', args=[path.id])

    @test.create_mocks({srv6_api: ('te_path_get', 'locator_list')})
    def test_update_get_keeps_lost_waypoint(self):
        # The DEGRADED path's waypoint has no locator any more; the form
        # must still show it, or saving would silently drop it.
        path = self.srv6_te_paths.list()[1]
        self.mock_te_path_get.return_value = path
        self.mock_locator_list.return_value = self.srv6_locators.list()

        res = self.client.get(self._url(path))

        form = res.context['form']
        self.assertEqual('gone-host', form.fields['via_host_1'].initial)
        self.assertIn('gone-host',
                      [c[0] for c in form.fields['via_host_1'].choices])

    @test.create_mocks({srv6_api: ('te_path_get', 'locator_list',
                                   'te_path_update')})
    def test_update_replaces_list(self):
        path = self.srv6_te_paths.first()
        self.mock_te_path_get.return_value = path
        self.mock_locator_list.return_value = self.srv6_locators.list()
        self.mock_te_path_update.return_value = path

        res = self.client.post(self._url(path), {
            'path_id': path.id, 'via_host_1': 'ale',
            'via_host_2': 'ale-XPS-15-9570'})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, TE_TAB_URL)
        self.mock_te_path_update.assert_called_once_with(
            test.IsHttpRequest(), path.id, ['ale', 'ale-XPS-15-9570'])

    @test.create_mocks({srv6_api: ('te_path_get', 'locator_list',
                                   'te_path_update')})
    def test_update_clear_all_makes_direct(self):
        path = self.srv6_te_paths.first()
        self.mock_te_path_get.return_value = path
        self.mock_locator_list.return_value = self.srv6_locators.list()
        self.mock_te_path_update.return_value = path

        res = self.client.post(self._url(path), {'path_id': path.id})

        self.assertNoFormErrors(res)
        self.mock_te_path_update.assert_called_once_with(
            test.IsHttpRequest(), path.id, [])


class TePathDetailTests(helpers.BaseAdminViewTests):

    def _url(self, path):
        return reverse('horizon:admin:srv6:te_path_detail', args=[path.id])

    @test.create_mocks({srv6_api: ('te_path_get', 'domain_get')})
    def test_detail_explains_each_segment(self):
        path = self.srv6_te_paths.first()
        self.mock_te_path_get.return_value = path
        self.mock_domain_get.return_value = self.srv6_domains.first()

        res = self.client.get(self._url(path))

        self.assertTemplateUsed(res, 'admin/srv6/_te_path_overview.html')
        self.assertContains(res, 'Waypoint ale-XPS-15-9570')
        self.assertContains(res, 'End.DT46 on the destination')
        # The pushed form: one microSID container here.
        self.assertContains(res, '<code>fc00:0:2:3:e000::</code>')
        self.assertContains(res, '>red</a>')
        self.mock_domain_get.assert_called_once_with(test.IsHttpRequest(),
                                                     path.domain_id)

    @test.create_mocks({srv6_api: ('te_path_get', 'domain_get')})
    def test_detail_degraded_says_why(self):
        path = self.srv6_te_paths.list()[1]
        self.mock_te_path_get.return_value = path
        self.mock_domain_get.return_value = self.srv6_domains.first()

        res = self.client.get(self._url(path))

        self.assertContains(res, 'has no registered SRv6 locator')

    @test.create_mocks({srv6_api: ('te_path_get',)})
    def test_detail_unknown_redirects(self):
        self.mock_te_path_get.side_effect = self.exceptions.neutron

        res = self.client.get(reverse('horizon:admin:srv6:te_path_detail',
                                      args=['nope']))

        self.assertRedirectsNoFollow(res, TE_TAB_URL)


class SegmentRolesTests(helpers.TestCase):

    def test_roles_line_up_with_waypoints(self):
        path = self.srv6_te_paths.first()

        roles = te_tabs.segment_roles(path)

        self.assertEqual(path.segments, [s for s, __ in roles])
        self.assertIn('ale-XPS-15-9570', str(roles[0][1]))

    def test_no_roles_when_lists_disagree(self):
        # Never guess which SID is which.
        path = self.srv6_te_paths.first()
        path._apidict['via_hosts'] = []

        roles = te_tabs.segment_roles(path)

        self.assertEqual([''] * len(path.segments),
                         [r for __, r in roles])
