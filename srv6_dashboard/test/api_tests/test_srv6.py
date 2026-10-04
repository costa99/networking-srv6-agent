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

"""api/srv6.py against a mocked openstacksdk network proxy.

The mocks sit on the SDK Proxy class itself (as upstream networking-bgpvpn's
SDK tests do), so the real networkclient(request) runs: a test fails if a
wrapper builds its client some other way. What each test pins down is the
contract with Neutron -- which Resource class, which id, which attributes --
because that is what decides the HTTP request (FEATURE §5) -- and that the
answer comes back as the right wrapper with every field intact.
"""

from openstack.network.v2._proxy import Proxy
from openstack_dashboard.test import helpers as test

from networking_srv6_agent.cli import resources
from srv6_dashboard.api import srv6
from srv6_dashboard.test import helpers


class DomainApiTests(helpers.APITestCase):

    @test.create_mocks({Proxy: ('_list',)})
    def test_domain_list(self):
        self.mock__list.return_value = self.api_srv6_domains

        ret = srv6.domain_list(self.request)

        self.mock__list.assert_called_once_with(resources.Srv6Domain)
        self.assertEqual(len(self.srv6_domains.list()), len(ret))
        for got, exp in zip(ret, self.srv6_domains.list()):
            self.assertIsInstance(got, srv6.Srv6Domain)
            self.assertEqual(exp.id, got.id)
            self.assertEqual(exp.name, got.name)
            self.assertEqual(exp.networks, got.networks)
            self.assertEqual(exp.sid_function, got.sid_function)

    @test.create_mocks({Proxy: ('_list',)})
    def test_domain_list_passes_filters(self):
        self.mock__list.return_value = []

        srv6.domain_list(self.request, project_id='1')

        self.mock__list.assert_called_once_with(resources.Srv6Domain,
                                                project_id='1')

    @test.create_mocks({Proxy: ('_list',)})
    def test_domain_list_without_sid_function(self):
        # What a tenant gets: the server drops sid_function from the
        # answer (policy get_srv6_domain:sid_function is admin-only). The
        # wrapper must still have the attribute -- None -- or every table
        # column reading it would raise.
        dom = self.srv6_domains.first()
        self.mock__list.return_value = [resources.Srv6Domain(
            id=dom.id, name=dom.name, project_id=dom.project_id,
            srv6_behavior=dom.srv6_behavior, networks=dom.networks)]

        ret = srv6.domain_list(self.request)

        self.assertIsNone(ret[0].sid_function)

    @test.create_mocks({Proxy: ('_get',)})
    def test_domain_get(self):
        dom = self.srv6_domains.first()
        self.mock__get.return_value = self.api_srv6_domains[0]

        ret = srv6.domain_get(self.request, dom.id)

        self.mock__get.assert_called_once_with(resources.Srv6Domain, dom.id)
        self.assertIsInstance(ret, srv6.Srv6Domain)
        self.assertEqual(dom.id, ret.id)

    @test.create_mocks({Proxy: ('_create',)})
    def test_domain_create(self):
        dom = self.srv6_domains.first()
        self.mock__create.return_value = self.api_srv6_domains[0]
        attrs = {'name': dom.name, 'srv6_behavior': 'End.DT46',
                 'project_id': dom.project_id}

        ret = srv6.domain_create(self.request, **attrs)

        self.mock__create.assert_called_once_with(resources.Srv6Domain,
                                                  **attrs)
        self.assertIsInstance(ret, srv6.Srv6Domain)
        self.assertEqual(dom.sid_function, ret.sid_function)

    @test.create_mocks({Proxy: ('_update',)})
    def test_domain_update(self):
        dom = self.srv6_domains.first()
        self.mock__update.return_value = self.api_srv6_domains[0]

        ret = srv6.domain_update(self.request, dom.id, name='new')

        self.mock__update.assert_called_once_with(resources.Srv6Domain,
                                                  dom.id, name='new')
        self.assertIsInstance(ret, srv6.Srv6Domain)

    @test.create_mocks({Proxy: ('_delete',)})
    def test_domain_delete(self):
        # ignore_missing=False: deleting a domain someone else just deleted
        # must raise (and be reported), not pass silently.
        dom = self.srv6_domains.first()
        self.mock__delete.return_value = None

        srv6.domain_delete(self.request, dom.id)

        self.mock__delete.assert_called_once_with(
            resources.Srv6Domain, dom.id, ignore_missing=False)


class NetworkAssociationApiTests(helpers.APITestCase):

    @test.create_mocks({Proxy: ('_list',)})
    def test_network_association_list(self):
        dom = self.srv6_domains.first()
        self.mock__list.return_value = self.api_srv6_network_associations

        ret = srv6.network_association_list(self.request, dom.id)

        # domain_id goes to the SDK as a URI attribute: it fills the
        # %(domain_id)s in the base path, it is not a query filter.
        self.mock__list.assert_called_once_with(
            resources.Srv6NetworkAssociation, domain_id=dom.id)
        exp = self.srv6_network_associations.first()
        self.assertIsInstance(ret[0], srv6.NetworkAssociation)
        self.assertEqual(exp.network_id, ret[0].network_id)
        # A URI attribute is not in to_dict(); the wrapper must put it
        # back, or the tables could not build the delete URL.
        self.assertEqual(dom.id, ret[0].domain_id)

    @test.create_mocks({Proxy: ('_get',)})
    def test_network_association_get(self):
        exp = self.srv6_network_associations.first()
        self.mock__get.return_value = self.api_srv6_network_associations[0]

        ret = srv6.network_association_get(self.request, exp.domain_id,
                                           exp.id)

        self.mock__get.assert_called_once_with(
            resources.Srv6NetworkAssociation, exp.id,
            domain_id=exp.domain_id)
        self.assertEqual(exp.domain_id, ret.domain_id)

    @test.create_mocks({Proxy: ('_create',)})
    def test_network_association_create(self):
        exp = self.srv6_network_associations.first()
        self.mock__create.return_value = (
            self.api_srv6_network_associations[0])

        ret = srv6.network_association_create(
            self.request, exp.domain_id, network_id=exp.network_id)

        self.mock__create.assert_called_once_with(
            resources.Srv6NetworkAssociation, domain_id=exp.domain_id,
            network_id=exp.network_id)
        self.assertIsInstance(ret, srv6.NetworkAssociation)
        self.assertEqual(exp.id, ret.id)

    @test.create_mocks({Proxy: ('_delete',)})
    def test_network_association_delete(self):
        exp = self.srv6_network_associations.first()
        self.mock__delete.return_value = None

        srv6.network_association_delete(self.request, exp.domain_id, exp.id)

        self.mock__delete.assert_called_once_with(
            resources.Srv6NetworkAssociation, exp.id, ignore_missing=False,
            domain_id=exp.domain_id)


class LocatorApiTests(helpers.APITestCase):

    @test.create_mocks({Proxy: ('_list',)})
    def test_locator_list(self):
        self.mock__list.return_value = self.api_srv6_locators

        ret = srv6.locator_list(self.request)

        self.mock__list.assert_called_once_with(resources.Srv6Locator)
        self.assertEqual(['ale', 'ale-XPS-15-9570', 'ale-node3'],
                         [loc.host for loc in ret])
        self.assertEqual('fc00:0:1::/48', ret[0].locator)

    @test.create_mocks({Proxy: ('_get',)})
    def test_locator_get(self):
        self.mock__get.return_value = self.api_srv6_locators[2]

        ret = srv6.locator_get(self.request, 'ale-node3')

        # The host name is the locator's ID.
        self.mock__get.assert_called_once_with(resources.Srv6Locator,
                                               'ale-node3')
        self.assertIsInstance(ret, srv6.Locator)
        self.assertEqual('fc00:0:3::/48', ret.locator)


class TePathApiTests(helpers.APITestCase):

    @test.create_mocks({Proxy: ('_list',)})
    def test_te_path_list(self):
        dom = self.srv6_domains.first()
        self.mock__list.return_value = self.api_srv6_te_paths

        ret = srv6.te_path_list(self.request, domain_id=dom.id)

        self.mock__list.assert_called_once_with(resources.Srv6TePath,
                                                domain_id=dom.id)
        exp = self.srv6_te_paths.list()
        self.assertEqual([p.id for p in exp], [p.id for p in ret])
        # The server-resolved fields survive the round trip untouched.
        self.assertEqual(exp[0].segments, ret[0].segments)
        self.assertEqual(exp[0].encoded_segments, ret[0].encoded_segments)
        self.assertEqual('DEGRADED', ret[1].status)

    @test.create_mocks({Proxy: ('_get',)})
    def test_te_path_get(self):
        exp = self.srv6_te_paths.first()
        self.mock__get.return_value = self.api_srv6_te_paths[0]

        ret = srv6.te_path_get(self.request, exp.id)

        self.mock__get.assert_called_once_with(resources.Srv6TePath, exp.id)
        self.assertIsInstance(ret, srv6.TePath)
        self.assertEqual(exp.via_hosts, ret.via_hosts)

    @test.create_mocks({Proxy: ('_create',)})
    def test_te_path_create(self):
        exp = self.srv6_te_paths.first()
        self.mock__create.return_value = self.api_srv6_te_paths[0]
        attrs = {'domain_id': exp.domain_id,
                 'destination_host': exp.destination_host,
                 'via_hosts': exp.via_hosts}

        ret = srv6.te_path_create(self.request, **attrs)

        self.mock__create.assert_called_once_with(resources.Srv6TePath,
                                                  **attrs)
        self.assertEqual('ACTIVE', ret.status)

    @test.create_mocks({Proxy: ('_update',)})
    def test_te_path_update_replaces_list(self):
        # The API replaces via_hosts whole, so the wrapper sends exactly
        # the list it is given, as a list (a form may hand it a tuple).
        exp = self.srv6_te_paths.first()
        self.mock__update.return_value = self.api_srv6_te_paths[0]

        srv6.te_path_update(self.request, exp.id, ('ale', 'ale-node3'))

        self.mock__update.assert_called_once_with(
            resources.Srv6TePath, exp.id, via_hosts=['ale', 'ale-node3'])

    @test.create_mocks({Proxy: ('_update',)})
    def test_te_path_update_empty_makes_direct(self):
        exp = self.srv6_te_paths.first()
        self.mock__update.return_value = self.api_srv6_te_paths[0]

        srv6.te_path_update(self.request, exp.id, [])

        self.mock__update.assert_called_once_with(
            resources.Srv6TePath, exp.id, via_hosts=[])

    @test.create_mocks({Proxy: ('_delete',)})
    def test_te_path_delete(self):
        exp = self.srv6_te_paths.first()
        self.mock__delete.return_value = None

        srv6.te_path_delete(self.request, exp.id)

        self.mock__delete.assert_called_once_with(
            resources.Srv6TePath, exp.id, ignore_missing=False)
