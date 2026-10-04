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

"""Admin > Network > SRv6, request by request (as test/project/).

BaseAdminViewTests logs in a user with the admin role. What these tests add
to the project ones is what is different for an admin: every project's
domains, the project and SID function, creating for a project, admin
URLs -- and that a non-admin cannot get in at all.
"""

from django.urls import reverse
from openstack_dashboard.api import keystone
from openstack_dashboard.test import helpers as test

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.test import helpers

INDEX_URL = reverse('horizon:admin:srv6:index')
CREATE_URL = reverse('horizon:admin:srv6:create')


def _detail_url(domain_id, tab=None):
    url = reverse('horizon:admin:srv6:detail', args=[domain_id])
    if tab:
        url += '?tab=domain_details__%s' % tab
    return url


class IndexDomainsTabTests(helpers.BaseAdminViewTests):

    @test.create_mocks({srv6_api: ('domain_list', 'network_list'),
                        keystone: ('tenant_list',)})
    def test_index(self):
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_list.return_value = [self.tenants.list(), False]

        res = self.client.get(INDEX_URL)

        self.assertTemplateUsed(res, 'admin/srv6/index.html')
        table = res.context['srv6_domains_table']
        self.assertCountEqual([d.id for d in self.srv6_domains.list()],
                              [d.id for d in table.data])
        # Every project's domains: no project filter (D14), and so no
        # project filter on the network names either.
        self.mock_domain_list.assert_called_once_with(test.IsHttpRequest())
        self.mock_network_list.assert_called_once_with(test.IsHttpRequest())
        self.mock_tenant_list.assert_called_once_with(test.IsHttpRequest())
        # The project by name; the SID function as decimal and hex.
        self.assertEqual(self.tenant.name, table.data[0].project_name)
        self.assertContains(res, '57344 (0xe000)')
        # Network links lead to the admin's network page.
        self.assertContains(res, reverse('horizon:admin:networks:detail',
                                         args=[self.networks.first().id]))

    @test.create_mocks({srv6_api: ('domain_list', 'network_list'),
                        keystone: ('tenant_list',)})
    def test_index_empty_makes_no_other_request(self):
        self.mock_domain_list.return_value = []

        res = self.client.get(INDEX_URL)

        self.assertEqual(200, res.status_code)
        self.mock_network_list.assert_not_called()
        self.mock_tenant_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list',)})
    def test_index_error(self):
        self.mock_domain_list.side_effect = self.exceptions.neutron

        res = self.client.get(INDEX_URL)

        self.assertEqual(0, len(res.context['srv6_domains_table'].data))
        self.assertMessageCount(res, error=1)


class CreateTests(helpers.BaseAdminViewTests):

    @test.create_mocks({keystone: ('tenant_list',)})
    def test_create_get_offers_projects(self):
        self.mock_tenant_list.return_value = [self.tenants.list(), False]

        res = self.client.get(CREATE_URL)

        self.assertTemplateUsed(res, 'admin/srv6/create.html')
        offered = {c[0] for c in
                   res.context['form'].fields['project_id'].choices if c[0]}
        self.assertEqual({t.id for t in self.tenants.list()}, offered)

    @test.create_mocks({srv6_api: ('domain_create',),
                        keystone: ('tenant_list',)})
    def test_create_post_for_a_project(self):
        self.mock_tenant_list.return_value = [self.tenants.list(), False]
        self.mock_domain_create.return_value = self.srv6_domains.first()
        project = self.tenants.list()[1]

        res = self.client.post(CREATE_URL, {'name': 'green',
                                            'project_id': project.id,
                                            'srv6_behavior': 'End.DT46'})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, INDEX_URL)
        # The admin form is the only one that sends project_id.
        self.mock_domain_create.assert_called_once_with(
            test.IsHttpRequest(), name='green', srv6_behavior='End.DT46',
            project_id=project.id)

    @test.create_mocks({srv6_api: ('domain_create',),
                        keystone: ('tenant_list',)})
    def test_create_requires_project(self):
        self.mock_tenant_list.return_value = [self.tenants.list(), False]

        res = self.client.post(CREATE_URL, {'name': 'green',
                                            'project_id': '',
                                            'srv6_behavior': 'End.DT46'})

        self.assertFormErrors(res, 1)
        self.mock_domain_create.assert_not_called()


class DetailTests(helpers.BaseAdminViewTests):

    @test.create_mocks({srv6_api: ('domain_get', 'network_list'),
                        keystone: ('tenant_get',)})
    def test_detail_overview_shows_admin_fields(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain
        self.mock_network_list.return_value = self.networks.list()
        self.mock_tenant_get.return_value = self.tenant

        res = self.client.get(_detail_url(domain.id))

        self.assertTemplateUsed(res, 'admin/srv6/_detail_overview.html')
        self.assertContains(res, '57344 (0xe000)')
        # The VRF device name, as `ip vrf` on a node shows it.
        self.assertContains(res, '<code>sv6vrf-57344</code>')
        self.assertContains(res, self.tenant.name)
        self.mock_tenant_get.assert_called_once_with(test.IsHttpRequest(),
                                                     domain.project_id)

    @test.create_mocks({srv6_api: ('domain_get', 'network_list'),
                        keystone: ('tenant_get',)})
    def test_detail_without_sid_function(self):
        # A field the server did not send: no "None", no bogus VRF name.
        domain = self.srv6_domains.list()[1]
        domain._apidict['sid_function'] = None
        self.mock_domain_get.return_value = domain
        self.mock_tenant_get.return_value = self.tenant

        res = self.client.get(_detail_url(domain.id))

        self.assertNotContains(res, 'sv6vrf-')
        self.mock_network_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_get', 'domain_list',
                                   'network_list',
                                   'network_association_create')})
    def test_associate_returns_to_admin_detail(self):
        domain = self.srv6_domains.list()[1]
        net = [n for n in self.networks.list()
               if not n.is_router_external
               and n.id != self.networks.first().id][0]
        self.mock_domain_get.return_value = domain
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_network_association_create.return_value = (
            self.srv6_network_associations.first())

        res = self.client.post(
            reverse('horizon:admin:srv6:associate', args=[domain.id]),
            {'domain_id': domain.id, 'network_id': net.id})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(
            res, _detail_url(domain.id, 'network_associations'))
        # The domain's project's networks, not the admin's own project's.
        self.mock_network_list.assert_called_with(
            test.IsHttpRequest(), project_id=domain.project_id)

    @test.create_mocks({srv6_api: ('domain_get', 'domain_update')})
    def test_update_returns_to_admin_index(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain
        self.mock_domain_update.return_value = domain

        res = self.client.post(
            reverse('horizon:admin:srv6:update', args=[domain.id]),
            {'domain_id': domain.id, 'name': 'renamed'})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, INDEX_URL)


class NonAdminTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('domain_list',)})
    def test_non_admin_is_turned_away(self):
        # The Admin dashboard's own gate (FEATURE §2.2): a logged-in user
        # without the admin role gets 403 Forbidden, and the panel's views
        # never run, so not a single SRv6 request is made.
        res = self.client.get(INDEX_URL)

        self.assertEqual(403, res.status_code)
        self.mock_domain_list.assert_not_called()


class LocatorsTabTests(helpers.BaseAdminViewTests):

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'locator_list'),
                        keystone: ('tenant_list',)})
    def test_locators_tab(self):
        self.mock_domain_list.return_value = []
        self.mock_locator_list.return_value = list(
            reversed(self.srv6_locators.list()))

        res = self.client.get(INDEX_URL + '?tab=srv6__locators')

        self.mock_locator_list.assert_called_once_with(test.IsHttpRequest())
        table = res.context['srv6_locators_table']
        # Sorted by host whatever the API's order, as the TE forms are.
        self.assertEqual(['ale', 'ale-XPS-15-9570', 'ale-node3'],
                         [loc.host for loc in table.data])
        self.assertContains(res, '<code>fc00:0:3::/48</code>')
        # Read-only: an agent writes locators, nobody else can.
        self.assertEqual((), tuple(table.get_table_actions()))
        self.assertEqual([], table.get_row_actions(table.data[0]))

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'locator_list'),
                        keystone: ('tenant_list',)})
    def test_default_tab_does_not_fetch_locators(self):
        self.mock_domain_list.return_value = []

        self.client.get(INDEX_URL)

        self.mock_locator_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'locator_list'),
                        keystone: ('tenant_list',)})
    def test_locators_error(self):
        self.mock_domain_list.return_value = []
        self.mock_locator_list.side_effect = self.exceptions.neutron

        res = self.client.get(INDEX_URL + '?tab=srv6__locators')

        self.assertEqual(0, len(res.context['srv6_locators_table'].data))
        self.assertMessageCount(res, error=1)
