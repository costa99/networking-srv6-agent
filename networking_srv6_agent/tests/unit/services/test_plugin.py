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

"""Tests for Srv6Plugin.

Two layers. The REST cases drive the real API through neutron's own test
harness -- extension loading, attribute validation, policy loaded from the
neutron.policies entry point, and a real database. The questions P4 has to
answer (is sid_function hidden from tenants, can a tenant attach another
project's network) are answered by that stack as a whole, not by the plugin
on its own.

The unit cases after them pin the payload and port-event logic against
mocks, as the old driver's tests did.
"""

import datetime
from unittest import mock

from neutron.api import extensions as api_extensions
from neutron import extensions as n_extensions
from neutron.tests import base
from neutron.tests.common import test_db_base_plugin_v2 as test_db_plugin
from neutron.tests.unit.extensions import test_l3
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_utils import uuidutils

from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.db import te_db
from networking_srv6_agent import extensions as srv6_extensions
from networking_srv6_agent.services import plugin


CORE_PLUGIN = 'neutron.tests.unit.extensions.test_l3.TestNoL3NatPlugin'
L3_PLUGIN = 'neutron.tests.unit.extensions.test_l3.TestL3NatServicePlugin'
SRV6_PLUGIN = 'networking_srv6_agent.services.plugin.Srv6Plugin'
OTHER_PROJECT = 'other-project'
DOMAINS = srv6_def.COLLECTION_NAME
ASSOCS = srv6_def.NET_ASSOC_COLLECTION_NAME


# ----------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------
class Srv6RestTestCase(test_db_plugin.NeutronDbPluginV2TestCase,
                       test_l3.L3NatTestCaseMixin):

    def setUp(self):
        notifier = mock.patch.object(plugin.rpc, 'Srv6AgentNotifyAPI')
        notifier.start()
        self.addCleanup(notifier.stop)
        # TestNoL3NatPlugin as core plugin: it carries external-net, which
        # the plain DB plugin does not, and router:external is one of the
        # things an association must refuse.
        ext_path = ':'.join(srv6_extensions.__path__ +
                            n_extensions.__path__)
        ext_mgr = api_extensions.PluginAwareExtensionManager(
            ext_path, {srv6_def.ALIAS: plugin.Srv6Plugin(),
                       'l3_plugin_name': test_l3.TestL3NatServicePlugin()})
        super().setUp(plugin=CORE_PLUGIN,
                      service_plugins={'srv6_plugin': SRV6_PLUGIN,
                                       'l3_plugin_name': L3_PLUGIN},
                      ext_mgr=ext_mgr)
        self.srv6 = directory.get_plugin(srv6_def.ALIAS)
        self.agent_rpc = self.srv6.agent_rpc
        self.admin_ctx = n_context.get_admin_context()

    # --- helpers ---------------------------------------------------------
    def _check(self, res, expected):
        self.assertEqual(expected, res.status_int, res.body)
        if res.body:
            return self.deserialize(self.fmt, res)

    def _domain(self, expected=201, project_id=None, as_admin=False,
                **attrs):
        data = {'srv6_domain': dict({'name': 'red'}, **attrs)}
        req = self.new_create_request(DOMAINS, data, project_id=project_id,
                                      as_admin=as_admin)
        body = self._check(req.get_response(self.ext_api), expected)
        return body['srv6_domain'] if expected == 201 else body

    def _show_domain(self, domain_id, expected=200, **kwargs):
        req = self.new_show_request(DOMAINS, domain_id, **kwargs)
        body = self._check(req.get_response(self.ext_api), expected)
        return body['srv6_domain'] if expected == 200 else body

    def _assoc(self, domain_id, network_id, expected=201, project_id=None,
               as_admin=False):
        data = {'network_association': {'network_id': network_id}}
        req = self.new_create_request(DOMAINS, data, id=domain_id,
                                      subresource=ASSOCS,
                                      project_id=project_id,
                                      as_admin=as_admin)
        body = self._check(req.get_response(self.ext_api), expected)
        return body['network_association'] if expected == 201 else body

    def _list_assocs(self, domain_id, **kwargs):
        req = self.new_list_request(DOMAINS, parent_id=domain_id,
                                    subresource=ASSOCS, **kwargs)
        return self._check(req.get_response(self.ext_api),
                           200)['network_associations']

    def _network(self, project_id=None, as_admin=False, **kwargs):
        return self._make_network(self.fmt, 'net', True,
                                  project_id=project_id or self._project_id,
                                  as_admin=as_admin, **kwargs)['network']

    def _subnet(self, network, cidr='10.0.0.0/24'):
        return self._make_subnet(self.fmt, {'network': network},
                                 gateway=cidr.replace('0/24', '1'),
                                 cidr=cidr)['subnet']


class TestDomainApi(Srv6RestTestCase):

    def test_create_allocates_but_hides_the_sid_from_the_tenant(self):
        domain = self._domain()
        self.assertEqual('End.DT46', domain['srv6_behavior'])
        self.assertEqual(self._project_id, domain['project_id'])
        # Admin-only to read (decided 2026-09-10); neutron drops the field
        # rather than refusing the request.
        self.assertNotIn('sid_function', domain)
        shown = self._show_domain(domain['id'], as_admin=True)
        self.assertIn(shown['sid_function'], range(1, 4096))

    def test_the_owner_reads_it_without_the_sid(self):
        domain = self._domain()
        shown = self._show_domain(domain['id'])
        self.assertEqual(domain['id'], shown['id'])
        self.assertNotIn('sid_function', shown)

    def test_another_project_cannot_see_it(self):
        domain = self._domain()
        self._show_domain(domain['id'], expected=404,
                          project_id=OTHER_PROJECT)

    def test_list_is_scoped_to_the_project(self):
        mine = self._domain()
        self._domain(project_id=OTHER_PROJECT, as_admin=True)
        req = self.new_list_request(DOMAINS)
        domains = self._check(req.get_response(self.ext_api),
                              200)['srv6_domains']
        self.assertEqual([mine['id']], [d['id'] for d in domains])

    def test_only_end_dt46_is_accepted(self):
        self._domain(expected=400, srv6_behavior='End.DT4')

    def test_explicit_end_dt46_is_accepted(self):
        self.assertEqual('End.DT46',
                         self._domain(srv6_behavior='End.DT46')[
                             'srv6_behavior'])

    def test_behavior_is_create_only(self):
        domain = self._domain()
        req = self.new_update_request(
            DOMAINS, {'srv6_domain': {'srv6_behavior': 'End.DT46'}},
            domain['id'])
        self._check(req.get_response(self.ext_api), 400)

    def test_rename(self):
        domain = self._domain()
        req = self.new_update_request(
            DOMAINS, {'srv6_domain': {'name': 'blue'}}, domain['id'])
        body = self._check(req.get_response(self.ext_api), 200)
        self.assertEqual('blue', body['srv6_domain']['name'])

    def test_delete_releases_the_id_and_tells_the_agents(self):
        domain = self._domain()
        fid = self._show_domain(domain['id'], as_admin=True)['sid_function']
        req = self.new_delete_request(DOMAINS, domain['id'])
        self._check(req.get_response(self.ext_api), 204)
        self._show_domain(domain['id'], expected=404, as_admin=True)
        self.assertIsNone(srv6_db.get_function_id(self.admin_ctx,
                                                  domain['id']))
        # D5: the id was read before the delete destroyed it.
        self.agent_rpc.domain_deleted.assert_called_once_with(
            mock.ANY, domain['id'], fid, 10000 + fid)

    def test_pool_exhaustion_rolls_the_domain_back(self):
        """One writer around create + allocate.

        Otherwise a failed allocation leaves a committed domain holding no
        function id.
        """
        cfg.CONF.set_override('function_id_ranges', ['1:1'], group='srv6')
        self._domain()
        self._domain(expected=409)
        req = self.new_list_request(DOMAINS, as_admin=True)
        self.assertEqual(1, len(self._check(req.get_response(self.ext_api),
                                            200)['srv6_domains']))

    def test_deleting_a_domain_logs_the_admin_te_paths_it_removes(self):
        domain = self._domain()
        te_db.create_te_path(self.admin_ctx, domain['id'],
                             {'project_id': self._project_id,
                              'destination_host': 'node2',
                              'via_hosts': ['node1']})
        with mock.patch.object(plugin.LOG, 'warning') as warning:
            req = self.new_delete_request(DOMAINS, domain['id'])
            self._check(req.get_response(self.ext_api), 204)
        self.assertEqual(1, warning.call_count)
        self.assertEqual(1, warning.call_args[0][1]['n'])
        self.assertEqual([], te_db.get_te_paths(self.admin_ctx,
                                                domain['id']))


class TestAssociationApi(Srv6RestTestCase):

    def test_own_network_is_attached_and_pushed(self):
        domain = self._domain()
        net = self._network()
        assoc = self._assoc(domain['id'], net['id'])
        self.assertEqual(net['id'], assoc['network_id'])
        self.assertEqual(self._project_id, assoc['project_id'])
        self.assertEqual(1, self.agent_rpc.domain_updated.call_count)
        payload = self.agent_rpc.domain_updated.call_args[0][1]
        self.assertEqual([net['id']],
                         [n['network_id'] for n in payload['networks']])

    def test_the_same_network_twice_is_a_conflict(self):
        domain = self._domain()
        net = self._network()
        self._assoc(domain['id'], net['id'])
        self._assoc(domain['id'], net['id'], expected=409)

    def test_external_network_is_refused(self):
        domain = self._domain()
        net = self._network(as_admin=True, **{'router:external': True})
        self._assoc(domain['id'], net['id'], expected=400)

    def test_routered_network_is_refused(self):
        domain = self._domain()
        net = self._network()
        subnet = self._subnet(net)
        router = self._make_router(self.fmt, self._project_id)['router']
        self._router_interface_action('add', router['id'], subnet['id'],
                                      None)
        self._assoc(domain['id'], net['id'], expected=400)

    def test_router_interface_on_an_attached_network_is_refused(self):
        # The race the association check alone leaves open.
        domain = self._domain()
        net = self._network()
        subnet = self._subnet(net)
        self._assoc(domain['id'], net['id'])
        router = self._make_router(self.fmt, self._project_id)['router']
        self._router_interface_action('add', router['id'], subnet['id'],
                                      None, expected_code=409)

    def test_another_projects_network_is_refused_even_for_an_admin(self):
        domain = self._domain()
        net = self._network(project_id=OTHER_PROJECT, as_admin=True)
        self._assoc(domain['id'], net['id'], expected=403, as_admin=True)

    def test_a_tenant_cannot_even_name_another_projects_network(self):
        domain = self._domain()
        net = self._network(project_id=OTHER_PROJECT, as_admin=True)
        self._assoc(domain['id'], net['id'], expected=404)

    def test_the_owners_shared_network_is_accepted(self):
        # Decided 2026-09-10: same owner, shared allowed.
        domain = self._domain()
        net = self._network(as_admin=True, shared=True)
        self._assoc(domain['id'], net['id'])

    def test_another_projects_shared_network_is_refused(self):
        domain = self._domain()
        net = self._network(project_id=OTHER_PROJECT, as_admin=True,
                            shared=True)
        self._assoc(domain['id'], net['id'], expected=403)

    def test_another_projects_domain_is_not_found(self):
        theirs = self._domain(project_id=OTHER_PROJECT, as_admin=True)
        net = self._network()
        self._assoc(theirs['id'], net['id'], expected=404)

    def test_an_admin_association_belongs_to_the_domains_project(self):
        domain = self._domain()
        net = self._network()
        assoc = self._assoc(domain['id'], net['id'], as_admin=True,
                            project_id='admin-project')
        self.assertEqual(self._project_id, assoc['project_id'])

    def test_list_and_delete(self):
        domain = self._domain()
        net = self._network()
        assoc = self._assoc(domain['id'], net['id'])
        self.assertEqual([assoc['id']],
                         [a['id'] for a in self._list_assocs(domain['id'])])
        req = self.new_delete_request(DOMAINS, domain['id'],
                                      subresource=ASSOCS, sub_id=assoc['id'])
        self._check(req.get_response(self.ext_api), 204)
        self.assertEqual([], self._list_assocs(domain['id']))
        # Pushed on attach and again on detach.
        self.assertEqual(2, self.agent_rpc.domain_updated.call_count)

    def test_listing_another_projects_domain_is_not_found(self):
        theirs = self._domain(project_id=OTHER_PROJECT, as_admin=True)
        req = self.new_list_request(DOMAINS, parent_id=theirs['id'],
                                    subresource=ASSOCS)
        self._check(req.get_response(self.ext_api), 404)


class TestLocatorApi(Srv6RestTestCase):

    def setUp(self):
        super().setUp()
        srv6_db.set_host_locator(self.admin_ctx, 'node1', 'fc00:0:1::/48',
                                 datetime.datetime.now(datetime.timezone.utc))

    def test_an_admin_sees_the_registry(self):
        req = self.new_list_request('srv6_locators', as_admin=True)
        locators = self._check(req.get_response(self.ext_api),
                               200)['srv6_locators']
        self.assertEqual(1, len(locators))
        self.assertEqual('node1', locators[0]['host'])
        self.assertEqual('fc00:0:1::/48', locators[0]['locator'])
        self.assertIsNotNone(locators[0]['updated_at'])

    def test_a_tenant_sees_nothing(self):
        # Neutron filters a collection by the per-item policy, so a list is
        # 200 and empty rather than 403; a single item is 404.
        req = self.new_list_request('srv6_locators')
        self.assertEqual([], self._check(req.get_response(self.ext_api),
                                         200)['srv6_locators'])
        req = self.new_show_request('srv6_locators', 'node1')
        self._check(req.get_response(self.ext_api), 404)

    def test_it_is_read_only_even_for_an_admin(self):
        req = self.new_create_request(
            'srv6_locators', {'srv6_locator': {'host': 'node9'}},
            as_admin=True)
        self.assertGreaterEqual(req.get_response(self.ext_api).status_int,
                                400)


# ----------------------------------------------------------------------
# unit
# ----------------------------------------------------------------------
def _domain(**kwargs):
    domain = {'id': uuidutils.generate_uuid(),
              'project_id': 'p1',
              'srv6_behavior': 'End.DT46',
              'networks': []}
    domain.update(kwargs)
    return domain


def _port(**kwargs):
    port = {'id': uuidutils.generate_uuid(),
            'network_id': 'net1',
            'device_owner': 'compute:nova',
            'status': n_const.PORT_STATUS_ACTIVE,
            'fixed_ips': [{'ip_address': '10.0.0.5'}],
            portbindings.HOST_ID: 'node1'}
    port.update(kwargs)
    return port


class PluginUnitTestCaseBase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        for name in ('srv6_db', 'domain_db', 'te_db'):
            patcher = mock.patch.object(plugin, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        notifier = mock.patch.object(plugin.rpc, 'Srv6AgentNotifyAPI')
        notifier.start()
        self.addCleanup(notifier.stop)
        self.core = mock.Mock()
        get_plugin = mock.patch.object(plugin.directory, 'get_plugin',
                                       return_value=self.core)
        get_plugin.start()
        self.addCleanup(get_plugin.stop)
        self.srv6_db.get_host_locators.return_value = {}
        self.plugin = plugin.Srv6Plugin()
        self.agent_rpc = self.plugin.agent_rpc
        self.ctx = mock.Mock(is_admin=False, project_id='p1')


class TestPayload(PluginUnitTestCaseBase):

    def test_vrf_table_is_the_base_plus_the_function_id(self):
        self.assertEqual(10007, self.plugin._vrf_table(7))

    def test_payload_shape(self):
        self.srv6_db.get_function_id.return_value = 7
        self.srv6_db.get_host_locators.return_value = {
            'node1': 'fc00:0:1::/48'}
        self.core.get_network.return_value = {'id': 'net1', 'mtu': 1450,
                                              'subnets': ['sub1']}
        self.core.get_subnet.return_value = {'id': 'sub1',
                                             'cidr': '10.0.0.0/24',
                                             'ip_version': 4,
                                             'gateway_ip': '10.0.0.1'}
        self.core.get_ports.return_value = []
        domain = _domain(networks=['net1'])
        payload = self.plugin._build_domain_payload(self.ctx, domain)
        self.assertEqual(domain['id'], payload['id'])
        self.assertEqual(7, payload['function_id'])
        self.assertEqual(10007, payload['vrf_table'])
        self.assertEqual('End.DT46', payload['behavior'])
        self.assertEqual({'node1': 'fc00:0:1::/48'}, payload['locators'])
        self.assertEqual(1450, payload['networks'][0]['mtu'])
        self.assertEqual([{'id': 'sub1', 'cidr': '10.0.0.0/24',
                           'ip_version': 4, 'gateway_ip': '10.0.0.1'}],
                         payload['networks'][0]['subnets'])

    def test_payload_is_built_with_an_elevated_context(self):
        # A shared network's other-project ports are invisible to the
        # owner's own context; their routes would silently go missing.
        self.srv6_db.get_function_id.return_value = 7
        self.core.get_ports.return_value = []
        self.core.get_network.return_value = {'subnets': []}
        self.plugin._build_domain_payload(self.ctx,
                                          _domain(networks=['net1']))
        admin = self.ctx.elevated.return_value
        self.core.get_ports.assert_called_once_with(
            admin, filters={'network_id': ['net1']})
        self.core.get_network.assert_called_once_with(admin, 'net1')

    def test_payload_is_none_without_an_allocation(self):
        self.srv6_db.get_function_id.return_value = None
        self.assertIsNone(self.plugin._build_domain_payload(self.ctx,
                                                            _domain()))

    def test_push_without_an_allocation_sends_nothing(self):
        self.srv6_db.get_function_id.return_value = None
        self.plugin._push_domain(self.ctx, _domain())
        self.agent_rpc.domain_updated.assert_not_called()


class TestPortSelection(PluginUnitTestCaseBase):

    locators = {'node1': 'fc00:0:1::/48'}

    def _routes(self, *ports):
        self.core.get_ports.return_value = list(ports)
        return self.plugin._routes_for_domain(self.ctx, ['net1'],
                                              self.locators)

    def test_workload_port_is_relevant(self):
        self.assertTrue(self.plugin._port_is_relevant(_port()))

    def test_infrastructure_ports_are_not(self):
        for owner in ('network:dhcp', 'network:router_interface',
                      'network:router_gateway'):
            self.assertFalse(
                self.plugin._port_is_relevant(_port(device_owner=owner)))

    def test_inactive_port_is_not(self):
        self.assertFalse(self.plugin._port_is_relevant(
            _port(status=n_const.PORT_STATUS_DOWN)))

    def test_route_for_an_ipv4_port_keeps_its_port_id(self):
        port = _port()
        routes = self._routes(port)
        self.assertEqual([{'prefix': '10.0.0.5/32', 'host': 'node1',
                           'port_id': port['id']}], routes)

    def test_route_for_an_ipv6_port(self):
        routes = self._routes(_port(fixed_ips=[{'ip_address':
                                                '2001:db8::5'}]))
        self.assertEqual('2001:db8::5/128', routes[0]['prefix'])

    def test_port_on_a_host_without_a_locator_is_skipped(self):
        # Guessing a SID for an unregistered host would install a blackhole
        # on every other node.
        self.assertEqual([], self._routes(
            _port(**{portbindings.HOST_ID: 'unknown-node'})))

    def test_unbound_port_is_skipped(self):
        self.assertEqual([], self._routes(
            _port(**{portbindings.HOST_ID: None})))

    def test_no_networks_means_no_query(self):
        self.assertEqual([], self.plugin._routes_for_domain(
            self.ctx, [], self.locators))
        self.core.get_ports.assert_not_called()


class TestDeleteOrdering(PluginUnitTestCaseBase):
    """Regression tests for delta D5.

    The allocation row references srv6_domains.id with ON DELETE SET NULL,
    so after the delete there is no function id left to read -- and an agent
    that is never told one cannot name the VRF it must remove.
    """

    def setUp(self):
        super().setUp()
        self.writer = mock.patch.object(plugin.db_api, 'CONTEXT_WRITER')
        self.writer.start()
        self.addCleanup(self.writer.stop)
        self.te_db.get_te_paths.return_value = []

    def test_the_function_id_is_read_before_the_delete(self):
        # A real int: a MagicMock id would record its own arithmetic
        # (10000 + id) in the same call list.
        self.srv6_db.get_function_id.return_value = 7
        calls = mock.Mock()
        calls.attach_mock(self.srv6_db.get_function_id, 'get_function_id')
        calls.attach_mock(self.domain_db.delete_domain, 'delete_domain')
        self.plugin.delete_srv6_domain(self.ctx, 'd1')
        self.assertEqual(['get_function_id', 'delete_domain'],
                         [c[0] for c in calls.mock_calls
                          if c[0] in ('get_function_id', 'delete_domain')])

    def test_agents_are_told_the_captured_id(self):
        # release() returning None is the exact shape of the old bug.
        self.srv6_db.get_function_id.return_value = 7
        self.srv6_db.release_function_id.return_value = None
        self.plugin.delete_srv6_domain(self.ctx, 'd1')
        self.agent_rpc.domain_deleted.assert_called_once_with(
            self.ctx, 'd1', 7, 10007)

    def test_no_id_means_nothing_to_name(self):
        self.srv6_db.get_function_id.return_value = None
        self.plugin.delete_srv6_domain(self.ctx, 'd1')
        self.agent_rpc.domain_deleted.assert_not_called()

    def test_an_unknown_domain_is_not_found(self):
        self.domain_db.delete_domain.return_value = None
        self.assertRaises(plugin.srv6_ext.Srv6DomainNotFound,
                          self.plugin.delete_srv6_domain, self.ctx, 'd1')
        self.agent_rpc.domain_deleted.assert_not_called()


class TestSyncState(PluginUnitTestCaseBase):

    def setUp(self):
        super().setUp()
        self.admin_ctx = mock.Mock()
        admin = mock.patch.object(plugin.n_context, 'get_admin_context',
                                  return_value=self.admin_ctx)
        admin.start()
        self.addCleanup(admin.stop)
        self.domain_db.get_domains.return_value = []

    def test_unusable_locator_is_not_registered(self):
        self.assertEqual([], self.plugin.handle_sync_state(
            self.ctx, 'node1', 'not-a-locator'))
        self.srv6_db.set_host_locator.assert_not_called()

    def test_locator_is_registered(self):
        self.plugin.handle_sync_state(self.ctx, 'node1', 'fc00:0:1::/48')
        args = self.srv6_db.set_host_locator.call_args[0]
        self.assertEqual(('node1', 'fc00:0:1::/48'), args[1:3])

    def test_missing_locator_still_returns_state(self):
        self.srv6_db.get_function_id.return_value = 7
        self.domain_db.get_domains.return_value = [_domain()]
        payloads = self.plugin.handle_sync_state(self.ctx, 'node1', None)
        self.srv6_db.set_host_locator.assert_not_called()
        self.assertEqual(1, len(payloads))

    def test_every_domain_is_rebroadcast(self):
        # A new locator changes what every *other* node must encapsulate
        # to, so convergence is one round trip, not one resync interval.
        self.srv6_db.get_function_id.return_value = 7
        self.domain_db.get_domains.return_value = [_domain(), _domain()]
        payloads = self.plugin.handle_sync_state(self.ctx, 'node1',
                                                 'fc00:0:1::/48')
        self.assertEqual(2, len(payloads))
        self.assertEqual(2, self.agent_rpc.domain_updated.call_count)

    def test_unallocated_domains_are_left_out(self):
        self.srv6_db.get_function_id.return_value = None
        self.domain_db.get_domains.return_value = [_domain()]
        self.assertEqual([], self.plugin.handle_sync_state(
            self.ctx, 'node1', 'fc00:0:1::/48'))


class TestPortEvents(PluginUnitTestCaseBase):

    def setUp(self):
        super().setUp()
        self.domain_db.get_domain_ids_for_network.return_value = ['d1']
        self.srv6_db.get_host_locators.return_value = {
            'node1': 'fc00:0:1::/48', 'node2': 'fc00:0:2::/48'}
        self.srv6_db.get_function_id.return_value = 7

    def _payload(self, port, original=None):
        return mock.Mock(context=self.ctx, latest_state=port,
                         states=[original if original is not None else {}])

    def test_a_network_in_no_domain_costs_one_query_and_nothing_else(self):
        self.domain_db.get_domain_ids_for_network.return_value = []
        self.plugin._notify_port(self.ctx, _port())
        self.srv6_db.get_host_locators.assert_not_called()
        self.agent_rpc.routes_updated.assert_not_called()

    def test_a_port_going_active_is_advertised(self):
        port = _port()
        self.plugin.registry_port_updated(
            None, None, None,
            self._payload(port, dict(port, status=n_const.PORT_STATUS_DOWN)))
        self.agent_rpc.routes_updated.assert_called_once_with(
            self.ctx, 'd1', add=[{'prefix': '10.0.0.5/32', 'host': 'node1',
                                  'port_id': port['id']}])

    def test_a_moved_port_is_withdrawn_before_it_is_advertised(self):
        port = _port(**{portbindings.HOST_ID: 'node2'})
        original = dict(port, **{portbindings.HOST_ID: 'node1'})
        self.plugin.registry_port_updated(None, None, None,
                                          self._payload(port, original))
        self.assertEqual(
            [mock.call(self.ctx, 'd1', remove=mock.ANY),
             mock.call(self.ctx, 'd1', add=mock.ANY)],
            self.agent_rpc.routes_updated.call_args_list)
        self.assertEqual('node1', self.agent_rpc.routes_updated.call_args_list[
            0][1]['remove'][0]['host'])

    def test_a_deleted_port_is_withdrawn(self):
        self.plugin.registry_port_deleted(None, None, None,
                                          self._payload(_port()))
        self.assertIn('remove', self.agent_rpc.routes_updated.call_args[1])

    def test_a_domain_without_an_allocation_is_skipped(self):
        self.srv6_db.get_function_id.return_value = None
        self.plugin._notify_port(self.ctx, _port())
        self.agent_rpc.routes_updated.assert_not_called()

    def test_a_failure_is_contained(self):
        self.domain_db.get_domain_ids_for_network.side_effect = \
            RuntimeError('boom')
        self.plugin.registry_port_deleted(None, None, None,
                                          self._payload(_port()))
