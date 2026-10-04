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

"""Project > Network > SRv6 Domains, request by request.

Each test drives a real URL through Django's test client, with the
api/srv6.py functions mocked (their own contract is pinned by
api_tests/). What is checked is the panel's side of FEATURE §5.2: which
functions a page calls, with which arguments, and what the user gets
back -- the page, the redirect, the message -- on success and on error.
"""

from django.urls import reverse
from openstack_dashboard.test import helpers as test

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.test import helpers

INDEX_URL = reverse('horizon:project:srv6:index')
CREATE_URL = reverse('horizon:project:srv6:create')


def _detail_url(domain_id, tab=None):
    url = reverse('horizon:project:srv6:detail', args=[domain_id])
    if tab:
        url += '?tab=domain_details__%s' % tab
    return url


class IndexTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('domain_list', 'network_list')})
    def test_index(self):
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()

        res = self.client.get(INDEX_URL)

        self.assertTemplateUsed(res, 'horizon/common/_data_table_view.html')
        self.assertCountEqual(
            [d.id for d in self.srv6_domains.list()],
            [d.id for d in res.context['table'].data])
        # The current project's domains only, even for an admin (D14),
        # and one network request for the whole table (D15).
        self.mock_domain_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=self.tenant.id)
        self.mock_network_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=self.tenant.id)
        # red's network shows by name, linked to its network page.
        net1 = self.networks.first()
        self.assertContains(res, '<a href="%s">%s</a>' % (
            reverse('horizon:project:networks:detail', args=[net1.id]),
            net1.name))

    @test.create_mocks({srv6_api: ('domain_list', 'network_list')})
    def test_index_without_networks_skips_network_request(self):
        blue = self.srv6_domains.list()[1]
        self.assertEqual([], blue.networks)
        self.mock_domain_list.return_value = [blue]

        res = self.client.get(INDEX_URL)

        self.assertEqual(200, res.status_code)
        self.mock_network_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_list',)})
    def test_index_error(self):
        self.mock_domain_list.side_effect = self.exceptions.neutron

        res = self.client.get(INDEX_URL)

        self.assertTemplateUsed(res, 'horizon/common/_data_table_view.html')
        self.assertEqual(0, len(res.context['table'].data))
        self.assertMessageCount(res, error=1)

    @test.create_mocks({srv6_api: ('domain_list', 'network_list')})
    def test_index_network_names_error_shows_ids(self):
        # The page must still say which networks are attached.
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.side_effect = self.exceptions.neutron

        res = self.client.get(INDEX_URL)

        self.assertEqual(2, len(res.context['table'].data))
        self.assertContains(res, '>%s</a>' % self.networks.first().id)
        self.assertMessageCount(res, error=1)


class CreateTests(helpers.TestCase):

    def test_create_get(self):
        res = self.client.get(CREATE_URL)

        self.assertTemplateUsed(res, 'project/srv6/create.html')
        self.assertContains(res, 'End.DT46')

    @test.create_mocks({srv6_api: ('domain_create',)})
    def test_create_post(self):
        self.mock_domain_create.return_value = self.srv6_domains.first()

        res = self.client.post(CREATE_URL, {'name': 'green',
                                            'srv6_behavior': 'End.DT46'})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, INDEX_URL)
        # A tenant never sends project_id: the server takes the token's.
        self.mock_domain_create.assert_called_once_with(
            test.IsHttpRequest(), name='green', srv6_behavior='End.DT46')

    @test.create_mocks({srv6_api: ('domain_create',)})
    def test_create_post_error(self):
        # e.g. 409 Srv6NoFunctionIdAvailable, or the quota.
        self.mock_domain_create.side_effect = self.exceptions.neutron

        res = self.client.post(CREATE_URL, {'name': 'green',
                                            'srv6_behavior': 'End.DT46'})

        self.assertRedirectsNoFollow(res, INDEX_URL)
        self.assertMessageCount(error=1)

    @test.create_mocks({srv6_api: ('domain_create',)})
    def test_create_rejects_unknown_behavior(self):
        # The choices are the server's VALID_BEHAVIORS (D12): a forged
        # value stops at the form, before any request.
        res = self.client.post(CREATE_URL, {'name': 'green',
                                            'srv6_behavior': 'End.DT4'})

        self.assertFormErrors(res, 1)
        self.mock_domain_create.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_create',)})
    def test_create_requires_name(self):
        res = self.client.post(CREATE_URL, {'name': '',
                                            'srv6_behavior': 'End.DT46'})

        self.assertFormErrors(res, 1)
        self.mock_domain_create.assert_not_called()


class UpdateTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('domain_get',)})
    def test_update_get(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain

        res = self.client.get(reverse('horizon:project:srv6:update',
                                      args=[domain.id]))

        self.assertTemplateUsed(res, 'project/srv6/update.html')
        self.assertEqual(domain.name, res.context['form'].initial['name'])
        self.mock_domain_get.assert_called_once_with(test.IsHttpRequest(),
                                                     domain.id)

    @test.create_mocks({srv6_api: ('domain_get', 'domain_update')})
    def test_update_post(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain
        self.mock_domain_update.return_value = domain

        res = self.client.post(
            reverse('horizon:project:srv6:update', args=[domain.id]),
            {'domain_id': domain.id, 'name': 'renamed'})

        self.assertNoFormErrors(res)
        self.assertRedirectsNoFollow(res, INDEX_URL)
        self.mock_domain_update.assert_called_once_with(
            test.IsHttpRequest(), domain.id, name='renamed')

    @test.create_mocks({srv6_api: ('domain_get',)})
    def test_update_unknown_domain_redirects(self):
        self.mock_domain_get.side_effect = self.exceptions.neutron

        res = self.client.get(reverse('horizon:project:srv6:update',
                                      args=['nope']))

        self.assertRedirectsNoFollow(res, INDEX_URL)


class DeleteTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'domain_delete')})
    def test_delete(self):
        domain = self.srv6_domains.first()
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_domain_delete.return_value = None

        res = self.client.post(INDEX_URL, {
            'action': 'srv6_domains__delete__%s' % domain.id})

        self.assertRedirectsNoFollow(res, INDEX_URL)
        self.mock_domain_delete.assert_called_once_with(
            test.IsHttpRequest(), domain.id)

    @test.create_mocks({srv6_api: ('domain_list', 'network_list',
                                   'domain_delete')})
    def test_delete_error(self):
        domain = self.srv6_domains.first()
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_domain_delete.side_effect = self.exceptions.neutron

        res = self.client.post(INDEX_URL, {
            'action': 'srv6_domains__delete__%s' % domain.id})

        self.assertRedirectsNoFollow(res, INDEX_URL)
        self.assertMessageCount(error=1)


class DetailTests(helpers.TestCase):

    @test.create_mocks({srv6_api: ('domain_get', 'network_list',
                                   'network_association_list')})
    def test_detail_overview(self):
        domain = self.srv6_domains.first()
        self.mock_domain_get.return_value = domain
        self.mock_network_list.return_value = self.networks.list()

        res = self.client.get(_detail_url(domain.id))

        self.assertTemplateUsed(res, 'horizon/common/_detail.html')
        self.assertTemplateUsed(res, 'project/srv6/_detail_overview.html')
        self.assertContains(res, domain.id)
        self.assertContains(res, self.networks.first().name)
        self.mock_domain_get.assert_called_once_with(test.IsHttpRequest(),
                                                     domain.id)
        # The networks of the domain's project: one request (D15).
        self.mock_network_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=domain.project_id)
        # The associations tab is not preloaded: no request until opened.
        self.mock_network_association_list.assert_not_called()

    @test.create_mocks({srv6_api: ('domain_get', 'network_list',
                                   'network_association_list')})
    def test_detail_associations_tab(self):
        domain = self.srv6_domains.first()
        assoc = self.srv6_network_associations.first()
        self.mock_domain_get.return_value = domain
        self.mock_network_list.return_value = self.networks.list()
        self.mock_network_association_list.return_value = [assoc]

        res = self.client.get(_detail_url(domain.id,
                                          'network_associations'))

        self.mock_network_association_list.assert_called_once_with(
            test.IsHttpRequest(), domain.id)
        table = res.context['network_associations_table']
        self.assertEqual([assoc.id], [a.id for a in table.data])
        # The row names the network, from the page's one network request.
        self.assertEqual(self.networks.first().name,
                         table.data[0].network_name)

    @test.create_mocks({srv6_api: ('domain_get',)})
    def test_detail_unknown_domain_redirects(self):
        self.mock_domain_get.side_effect = self.exceptions.neutron

        res = self.client.get(_detail_url('nope'))

        self.assertRedirectsNoFollow(res, INDEX_URL)

    @test.create_mocks({srv6_api: ('domain_get', 'network_list',
                                   'network_association_list',
                                   'network_association_delete')})
    def test_disassociate(self):
        domain = self.srv6_domains.first()
        assoc = self.srv6_network_associations.first()
        self.mock_domain_get.return_value = domain
        self.mock_network_list.return_value = self.networks.list()
        self.mock_network_association_list.return_value = [assoc]
        self.mock_network_association_delete.return_value = None
        url = _detail_url(domain.id, 'network_associations')

        res = self.client.post(url, {
            'action': 'network_associations__delete__%s' % assoc.id})

        self.mock_network_association_delete.assert_called_once_with(
            test.IsHttpRequest(), domain.id, assoc.id)
        self.assertRedirectsNoFollow(res, url)


class AssociateTests(helpers.TestCase):

    def _url(self, domain):
        return reverse('horizon:project:srv6:associate', args=[domain.id])

    @test.create_mocks({srv6_api: ('domain_get', 'domain_list',
                                   'network_list')})
    def test_associate_get_offers_only_free_networks(self):
        domain = self.srv6_domains.list()[1]   # blue, empty
        self.mock_domain_get.return_value = domain
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()

        res = self.client.get(self._url(domain))

        self.assertTemplateUsed(res, 'project/srv6/associate.html')
        offered = {c[0] for c in res.context['form']
                   .fields['network_id'].choices if c[0]}
        # Not net1: red already holds it, and a network belongs to at
        # most one domain (F3). Not the external networks (400).
        taken = {nid for d in self.srv6_domains.list() for nid in d.networks}
        expected = {n.id for n in self.networks.list()
                    if not n.is_router_external and n.id not in taken}
        self.assertEqual(expected, offered)
        self.assertNotIn(self.networks.first().id, offered)
        self.mock_network_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=domain.project_id)
        self.mock_domain_list.assert_called_once_with(
            test.IsHttpRequest(), project_id=domain.project_id)

    def _free_network(self):
        return [n for n in self.networks.list()
                if not n.is_router_external
                and n.id != self.networks.first().id][0]

    @test.create_mocks({srv6_api: ('domain_get', 'domain_list',
                                   'network_list',
                                   'network_association_create')})
    def test_associate_post(self):
        domain = self.srv6_domains.list()[1]
        net = self._free_network()
        self.mock_domain_get.return_value = domain
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_network_association_create.return_value = (
            self.srv6_network_associations.first())

        res = self.client.post(self._url(domain), {'domain_id': domain.id,
                                                   'network_id': net.id})

        self.assertNoFormErrors(res)
        # Back on the domain, on the tab that shows the new association.
        self.assertRedirectsNoFollow(
            res, _detail_url(domain.id, 'network_associations'))
        self.mock_network_association_create.assert_called_once_with(
            test.IsHttpRequest(), domain.id, network_id=net.id)

    @test.create_mocks({srv6_api: ('domain_get', 'domain_list',
                                   'network_list',
                                   'network_association_create')})
    def test_associate_post_server_refuses(self):
        # What the form cannot see -- a router interface on the network,
        # an overlapping subnet (F10) -- comes back as the server's error.
        domain = self.srv6_domains.list()[1]
        net = self._free_network()
        self.mock_domain_get.return_value = domain
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()
        self.mock_network_association_create.side_effect = (
            self.exceptions.neutron)

        res = self.client.post(self._url(domain), {'domain_id': domain.id,
                                                   'network_id': net.id})

        self.assertRedirectsNoFollow(res, _detail_url(domain.id))
        self.assertMessageCount(error=1)

    @test.create_mocks({srv6_api: ('domain_get', 'domain_list',
                                   'network_list',
                                   'network_association_create')})
    def test_associate_rejects_network_in_a_domain(self):
        # net1 is in red: not a valid choice, so no request is sent.
        domain = self.srv6_domains.list()[1]
        self.mock_domain_get.return_value = domain
        self.mock_domain_list.return_value = self.srv6_domains.list()
        self.mock_network_list.return_value = self.networks.list()

        res = self.client.post(self._url(domain), {
            'domain_id': domain.id,
            'network_id': self.networks.first().id})

        self.assertFormErrors(res, 1)
        self.mock_network_association_create.assert_not_called()
