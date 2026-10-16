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

"""P6: traffic-engineering paths.

The REST cases drive /v2.0/srv6_te_paths through neutron's own harness on
the P4 base class: the policy from the entry point, a real database, and
the plugin's validation, answered as a whole. The unit cases after them pin
what REST cannot see -- payload precedence, the DEGRADED fallback, the
incremental path and segment resolution -- against mocks.
"""

import datetime
from unittest import mock

from neutron_lib.api.definitions import portbindings
from neutron_lib import exceptions as n_exc
from oslo_db import exception as db_exc
from oslo_utils import uuidutils

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.common import config
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_plugin


TE = te_def.COLLECTION_NAME
LOC1 = 'fc00:0:1::/48'
LOC2 = 'fc00:0:2::/48'
LOC3 = 'fc00:0:3::/48'


# ----------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------
class TeRestTestCase(test_plugin.Srv6RestTestCase):

    def setUp(self):
        super().setUp()
        now = datetime.datetime.now(datetime.timezone.utc)
        for host, locator in (('node1', LOC1), ('node2', LOC2)):
            srv6_db.set_host_locator(self.admin_ctx, host, locator, now)
        self.domain = self._domain()
        self.fid = self._show_domain(self.domain['id'],
                                     as_admin=True)['sid_function']
        self.net = self._network()
        self._assoc(self.domain['id'], self.net['id'])
        self.agent_rpc.reset_mock()

    # --- helpers ---------------------------------------------------------
    def _decap(self, node=2):
        """The domain's decap SID on a node: the function id is HEX."""
        return 'fc00:0:%d:%x::' % (node, self.fid)

    def _path(self, expected=201, as_admin=True, project_id=None, **attrs):
        body = dict({'domain_id': self.domain['id']}, **attrs)
        req = self.new_create_request(TE, {'srv6_te_path': body},
                                      as_admin=as_admin,
                                      project_id=project_id)
        res = self._check(req.get_response(self.ext_api), expected)
        return res['srv6_te_path'] if expected == 201 else res

    def _host_path(self, expected=201, **attrs):
        return self._path(expected, **dict({'destination_host': 'node2',
                                            'via_hosts': ['node1']},
                                           **attrs))

    def _update(self, path_id, expected=200, as_admin=True, **attrs):
        req = self.new_update_request(TE, {'srv6_te_path': attrs}, path_id,
                                      as_admin=as_admin)
        res = self._check(req.get_response(self.ext_api), expected)
        return res['srv6_te_path'] if expected == 200 else res

    def _show(self, path_id, expected=200, **kwargs):
        req = self.new_show_request(TE, path_id, **kwargs)
        res = self._check(req.get_response(self.ext_api), expected)
        return res['srv6_te_path'] if expected == 200 else res

    def _list(self, **kwargs):
        req = self.new_list_request(TE, **kwargs)
        return self._check(req.get_response(self.ext_api), 200)[TE]

    def _delete_path(self, path_id, expected=204, as_admin=True):
        req = self.new_delete_request(TE, path_id, as_admin=as_admin)
        self._check(req.get_response(self.ext_api), expected)

    def _vm_port(self, network=None):
        return self._make_port(self.fmt,
                               (network or self.net)['id'])['port']

    def _message(self, res):
        return res['NeutronError']['message']


class TestTePathApi(TeRestTestCase):

    def test_a_host_path_resolves_to_end_sids_then_the_decap_sid(self):
        path = self._host_path()
        self.assertEqual(['fc00:0:1:fff0::', self._decap()],
                         path['segments'])
        self.assertEqual('ACTIVE', path['status'])
        self.assertEqual('node2', path['destination_host'])
        self.assertIsNone(path['destination_port_id'])
        self.assertEqual(['node1'], path['via_hosts'])

    def test_creating_pushes_the_domain(self):
        self._host_path()
        self.assertEqual(1, self.agent_rpc.domain_updated.call_count)
        self.assertEqual(
            self.domain['id'],
            self.agent_rpc.domain_updated.call_args[0][1]['id'])

    def test_via_the_destination_itself_is_accepted(self):
        # The only depth-2 SRH two nodes can produce (G2).
        path = self._host_path(via_hosts=['node2'])
        self.assertEqual(['fc00:0:2:fff0::', self._decap()],
                         path['segments'])

    def test_an_empty_via_list_is_explicitly_direct(self):
        path = self._host_path(via_hosts=[])
        self.assertEqual([self._decap()], path['segments'])
        self.assertEqual('ACTIVE', path['status'])

    def test_the_path_belongs_to_the_domains_project(self):
        path = self._host_path(project_id='admin-project')
        self.assertEqual(self._project_id, path['project_id'])

    def test_a_port_path_on_a_domain_network(self):
        port = self._vm_port()
        path = self._path(destination_port_id=port['id'],
                          via_hosts=['node2'])
        self.assertEqual(port['id'], path['destination_port_id'])
        self.assertIsNone(path['destination_host'])
        # The harness binds no host, so the destination is unknown: the
        # route (there is none yet) would go direct.
        self.assertEqual('DEGRADED', path['status'])
        self.assertEqual([], path['segments'])

    def test_update_replaces_the_vias_and_pushes(self):
        path = self._host_path()
        self.agent_rpc.reset_mock()
        updated = self._update(path['id'], via_hosts=['node2'])
        self.assertEqual(['node2'], updated['via_hosts'])
        self.assertEqual(['fc00:0:2:fff0::', self._decap()],
                         updated['segments'])
        self.assertEqual(1, self.agent_rpc.domain_updated.call_count)

    def test_update_to_empty_un_steers(self):
        path = self._host_path()
        self.assertEqual([self._decap()],
                         self._update(path['id'], via_hosts=[])['segments'])
        self.assertEqual([], self._show(path['id'],
                                        as_admin=True)['via_hosts'])

    def test_the_selectors_cannot_be_changed(self):
        path = self._host_path()
        self._update(path['id'], expected=400, destination_host='node1')
        self._update(path['id'], expected=400,
                     destination_port_id=uuidutils.generate_uuid())

    def test_delete_pushes(self):
        path = self._host_path()
        self.agent_rpc.reset_mock()
        self._delete_path(path['id'])
        self.assertEqual(1, self.agent_rpc.domain_updated.call_count)
        self._show(path['id'], expected=404, as_admin=True)

    def test_deleting_a_missing_path_is_not_found(self):
        self._delete_path(uuidutils.generate_uuid(), expected=404)

    def test_list_and_filter(self):
        mine = self._host_path()
        other = self._domain(name='blue')
        theirs = self._path(domain_id=other['id'], destination_host='node2')
        self.assertEqual(sorted([mine['id'], theirs['id']]),
                         sorted(p['id'] for p in self._list(as_admin=True)))
        self.assertEqual(
            [theirs['id']],
            [p['id'] for p in self._list(
                as_admin=True, params='domain_id=%s' % other['id'])])

    def test_listed_paths_are_resolved_too(self):
        self._host_path()
        self.assertEqual(['fc00:0:1:fff0::', self._decap()],
                         self._list(as_admin=True)[0]['segments'])

    def test_a_domain_delete_cascades_its_paths(self):
        # By the domain's owner: the admin's paths go with it, and the
        # server logs that.
        self._host_path()
        req = self.new_delete_request('srv6_domains', self.domain['id'])
        self._check(req.get_response(self.ext_api), 204)
        self.assertEqual([], self._list(as_admin=True))

    def test_a_port_delete_cascades_its_path(self):
        port = self._vm_port()
        self._path(destination_port_id=port['id'])
        self._delete('ports', port['id'])
        self.assertEqual([], self._list(as_admin=True))


class TestTePathValidation(TeRestTestCase):
    """A 400, never a silent `segments: []`."""

    def test_an_unknown_domain_is_not_found(self):
        self._host_path(expected=404, domain_id=uuidutils.generate_uuid())

    def test_both_selectors_are_refused(self):
        port = self._vm_port()
        res = self._host_path(expected=400, destination_port_id=port['id'])
        self.assertIn('exactly one', self._message(res))

    def test_neither_selector_is_refused(self):
        self._path(expected=400, via_hosts=['node1'])

    def test_an_unregistered_destination_host(self):
        res = self._host_path(expected=400, destination_host='node9')
        self.assertIn('node9', self._message(res))

    def test_an_unregistered_via_host(self):
        # A typo here would otherwise mean "not steered", silently.
        res = self._host_path(expected=400, via_hosts=['node1', 'nod2'])
        self.assertIn('nod2', self._message(res))

    def test_more_than_six_via_hosts(self):
        res = self._host_path(expected=400,
                              via_hosts=['n%d' % i for i in range(7)])
        self.assertIn('at most 6', self._message(res))

    def test_duplicate_via_hosts(self):
        self._host_path(expected=400, via_hosts=['node1', 'node1'])

    def test_a_port_outside_the_domain(self):
        elsewhere = self._network()
        port = self._vm_port(elsewhere)
        res = self._path(expected=400, destination_port_id=port['id'])
        self.assertIn('not attached', self._message(res))

    def test_a_port_that_does_not_exist(self):
        self._path(expected=400,
                   destination_port_id=uuidutils.generate_uuid())

    def test_the_same_host_twice_is_a_conflict(self):
        self._host_path()
        self._host_path(expected=409, via_hosts=[])

    def test_the_same_port_twice_is_a_conflict(self):
        port = self._vm_port()
        self._path(destination_port_id=port['id'])
        self._path(expected=409, destination_port_id=port['id'])

    def test_a_host_path_and_a_port_path_coexist(self):
        port = self._vm_port()
        self._host_path()
        self._path(destination_port_id=port['id'])

    def test_an_update_to_an_unregistered_via_host(self):
        path = self._host_path()
        self._update(path['id'], expected=400, via_hosts=['node9'])
        self.assertEqual(['node1'],
                         self._show(path['id'], as_admin=True)['via_hosts'])

    def test_nothing_is_pushed_on_a_refusal(self):
        self._host_path(expected=400, via_hosts=['node9'])
        self.agent_rpc.domain_updated.assert_not_called()


class TestTePathTenantView(TeRestTestCase):
    """G5: host names and underlay SIDs are not the tenant's to see."""

    def setUp(self):
        super().setUp()
        self.path = self._host_path()

    def test_a_tenant_lists_nothing(self):
        self.assertEqual([], self._list())

    def test_a_tenant_cannot_show_it(self):
        self._show(self.path['id'], expected=404)

    def test_a_tenant_cannot_create_one(self):
        self._path(expected=403, as_admin=False, destination_host='node1')

    def test_a_tenant_cannot_update_or_delete_one(self):
        self._update(self.path['id'], expected=404, as_admin=False,
                     via_hosts=[])
        self._delete_path(self.path['id'], expected=404, as_admin=False)
        self.assertEqual(['node1'],
                         self._show(self.path['id'],
                                    as_admin=True)['via_hosts'])


class TestTeSourceApi(TeRestTestCase):
    """source_host over REST: stored, shown, validated, unique per source.

    Listing is left to the unit cases: the list endpoint answers 500 in
    this harness (a known, pre-existing failure of every REST list test).
    """

    def setUp(self):
        super().setUp()
        srv6_db.set_host_locator(self.admin_ctx, 'node3', LOC3,
                                 datetime.datetime.now(datetime.timezone.utc))

    def test_a_path_without_a_source_shows_none(self):
        self.assertIsNone(self._host_path()['source_host'])

    def test_a_source_path_shows_its_source(self):
        path = self._host_path(source_host='node3', via_hosts=['node1'])
        self.assertEqual('node3', path['source_host'])
        self.assertEqual('node3', self._show(path['id'],
                                             as_admin=True)['source_host'])

    def test_its_own_leading_sid_is_not_shown(self):
        # node1 is the ingress and its first waypoint: its agent drops the
        # SID, so the list the API shows drops it too.
        path = self._host_path(source_host='node1',
                               via_hosts=['node1', 'node3'])
        self.assertEqual(['fc00:0:3:fff0::', self._decap()],
                         path['segments'])
        self.assertEqual(path['segments'], path['encoded_segments'])

    def test_a_source_path_next_to_an_any_source_path(self):
        self._host_path()
        self._host_path(source_host='node1', via_hosts=['node3'])
        self._host_path(source_host='node3', via_hosts=[])

    def test_the_same_source_twice_is_a_conflict(self):
        self._host_path(source_host='node1', via_hosts=['node3'])
        res = self._host_path(expected=409, source_host='node1')
        self.assertIn('from node1', self._message(res))

    def test_an_empty_source_is_any_source(self):
        path = self._host_path(source_host='')
        self.assertIsNone(path['source_host'])
        self._host_path(expected=409)

    def test_an_unregistered_source(self):
        res = self._host_path(expected=400, source_host='node9')
        self.assertIn('node9', self._message(res))

    def test_the_destination_as_its_own_source(self):
        res = self._host_path(expected=400, source_host='node2')
        self.assertIn('never encapsulates', self._message(res))

    def test_the_source_cannot_be_changed(self):
        path = self._host_path(source_host='node1', via_hosts=['node3'])
        self._update(path['id'], expected=400, source_host='node3')

    def test_a_tenant_cannot_name_a_source(self):
        self._path(expected=403, as_admin=False, destination_host='node2',
                   source_host='node1')


# ----------------------------------------------------------------------
# unit
# ----------------------------------------------------------------------
def _route(port_id, host='node2', prefix='10.0.0.5/32'):
    return {'prefix': prefix, 'host': host, 'port_id': port_id,
            'network_id': 'net1'}


def _db_path(**kwargs):
    path = {'id': 'tp1', 'project_id': 'p1', 'domain_id': 'd1',
            'destination_host': None, 'destination_port_id': None,
            'source_host': None, 'via_hosts': [], 'segments': [],
            'status': None}
    path.update(kwargs)
    return path


class TePluginTestCaseBase(test_plugin.PluginUnitTestCaseBase):

    locators = {'node1': LOC1, 'node2': LOC2, 'node3': LOC3}

    def setUp(self):
        super().setUp()
        self.srv6_db.get_host_locators.return_value = dict(self.locators)
        self.srv6_db.get_function_id.return_value = 7

    def _paths(self, by_port=None, by_host=None, by_source=None):
        """The te_db maps; by_source is {source: (by_port, by_host)}."""
        self.te_db.get_te_paths_for_domain.return_value = {
            'by_port': by_port or {}, 'by_host': by_host or {},
            'by_source': {source: {'by_port': ports or {},
                                   'by_host': hosts or {}}
                          for source, (ports, hosts)
                          in (by_source or {}).items()}}


class TestPrecedence(TePluginTestCaseBase):
    """Port path > host path > direct."""

    def _attach(self, *routes):
        return self.plugin._attach_segments(self.ctx, 'd1', list(routes),
                                            self.locators)

    def test_a_port_path_beats_the_host_path(self):
        self._paths(by_port={'p1': ['node3']}, by_host={'node2': ['node1']})
        routes = self._attach(_route('p1'), _route('p2', prefix='10.0.0.6/32'))
        self.assertEqual(['fc00:0:3:fff0::'], routes[0]['segments'])
        # G3: the neighbour follows the host path.
        self.assertEqual(['fc00:0:1:fff0::'], routes[1]['segments'])

    def test_a_host_path_leaves_other_hosts_alone(self):
        self._paths(by_host={'node2': ['node1']})
        route = self._attach(_route('p1', host='node3'))[0]
        self.assertNotIn('segments', route)

    def test_an_empty_port_path_is_direct_even_under_a_host_path(self):
        self._paths(by_port={'p1': []}, by_host={'node2': ['node1']})
        self.assertNotIn('segments', self._attach(_route('p1'))[0])

    def test_no_path_leaves_the_p5_route_untouched(self):
        route = _route('p1')
        self.assertEqual([dict(route)], self._attach(route))

    def test_transit_sids_only_in_traversal_order(self):
        # The agent appends the decap SID itself.
        self._paths(by_port={'p1': ['node3', 'node1']})
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:1:fff0::'],
                         self._attach(_route('p1'))[0]['segments'])

    def test_a_degraded_port_path_goes_direct_not_to_the_host_path(self):
        self._paths(by_port={'p1': ['node9']}, by_host={'node2': ['node1']})
        with mock.patch.object(plugin.LOG, 'warning') as warning:
            route = self._attach(_route('p1'))[0]
        self.assertNotIn('segments', route)
        self.assertTrue(warning.called)

    def test_a_degraded_host_path_goes_direct(self):
        self._paths(by_host={'node2': ['node3', 'node9']})
        self.assertNotIn('segments', self._attach(_route('p1'))[0])


E1, E2, E3 = 'fc00:0:1:fff0::', 'fc00:0:2:fff0::', 'fc00:0:3:fff0::'


class TestSourcePrecedence(TePluginTestCaseBase):
    """(port, src) > (host, src) > (port, any) > (host, any) > direct.

    The routes go to every node. `segments` is what every ingress pushes;
    `segments_by_source` holds the answer of each source whose paths
    apply, and that node's agent uses it instead -- even when it is [].
    """

    def _attach(self, *routes):
        return self.plugin._attach_segments(self.ctx, 'd1', list(routes),
                                            self.locators)

    def test_only_source_paths_still_steer(self):
        # A domain whose only path names a source: no early return.
        self._paths(by_source={'node1': (None, {'node2': ['node3']})})
        route = self._attach(_route('p1'))[0]
        self.assertNotIn('segments', route)
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_a_source_port_path_beats_a_source_host_path(self):
        self._paths(by_source={'node1': ({'p1': []}, {'node2': ['node3']})})
        steered, neighbour = self._attach(
            _route('p1'), _route('p2', prefix='10.0.0.6/32'))
        self.assertEqual({'node1': []}, steered['segments_by_source'])
        self.assertEqual({'node1': [E3]}, neighbour['segments_by_source'])

    def test_a_source_host_path_beats_an_any_source_port_path(self):
        # U1: the source rank wins, at that ingress only.
        self._paths(by_port={'p1': ['node1']},
                    by_source={'node1': (None, {'node2': ['node3']})})
        route = self._attach(_route('p1'))[0]
        self.assertEqual([E1], route['segments'])
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_an_explicitly_direct_source_path_is_sent_empty(self):
        # Left out, node1 would fall back to `segments`.
        self._paths(by_host={'node2': ['node3']},
                    by_source={'node1': (None, {'node2': []})})
        route = self._attach(_route('p1'))[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node1': []}, route['segments_by_source'])

    def test_a_degraded_source_path_is_direct_for_its_source(self):
        # Not "fall back to the any-source path" (U1).
        self._paths(by_host={'node2': ['node3']},
                    by_source={'node1': (None, {'node2': ['node9']})})
        with mock.patch.object(plugin.LOG, 'warning') as warning:
            route = self._attach(_route('p1'))[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node1': []}, route['segments_by_source'])
        self.assertTrue(warning.called)

    def test_a_source_without_a_locator_is_direct(self):
        self._paths(by_source={'node9': (None, {'node2': ['node3']})})
        with mock.patch.object(plugin.LOG, 'warning'):
            route = self._attach(_route('p1'))[0]
        self.assertEqual({'node9': []}, route['segments_by_source'])

    def test_a_direct_any_source_port_path_keeps_the_source_entry(self):
        # The any-source answer is "nothing"; the loop must still reach
        # the source pass.
        self._paths(by_port={'p1': []},
                    by_source={'node1': (None, {'node2': ['node3']})})
        route = self._attach(_route('p1'))[0]
        self.assertNotIn('segments', route)
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_no_applicable_source_path_adds_no_key(self):
        # node1's path goes to node3; this route is on node2.
        self._paths(by_host={'node2': ['node3']},
                    by_source={'node1': (None, {'node3': ['node2']})})
        route = self._attach(_route('p1'))[0]
        self.assertEqual([E3], route['segments'])
        self.assertNotIn('segments_by_source', route)

    def test_the_routes_own_host_gets_no_entry(self):
        # A port path whose VM has moved onto its source: that node never
        # encapsulates traffic toward its own VMs.
        self._paths(by_source={'node2': ({'p1': ['node3']}, None)})
        self.assertNotIn('segments_by_source',
                         self._attach(_route('p1'))[0])

    def test_each_source_gets_its_own_entry(self):
        self._paths(by_host={'node2': ['node1']},
                    by_source={'node1': (None, {'node2': ['node3']}),
                               'node3': (None, {'node2': []})})
        route = self._attach(_route('p1'))[0]
        self.assertEqual([E1], route['segments'])
        self.assertEqual({'node1': [E3], 'node3': []},
                         route['segments_by_source'])

    def test_a_source_entry_keeps_the_sources_own_sid(self):
        # The server sends the list as resolved; the source's agent drops
        # its own leading SID, as it does for `segments`.
        self._paths(by_source={'node1': (None, {'node2': ['node1',
                                                          'node3']})})
        self.assertEqual({'node1': [E1, E3]},
                         self._attach(_route('p1'))[0]['segments_by_source'])


class TestPayloadCarriesTe(TePluginTestCaseBase):

    def setUp(self):
        super().setUp()
        self.core.get_network.return_value = {'subnets': [], 'mtu': 1450}
        self.steered = test_plugin._port(**{portbindings.HOST_ID: 'node2'})
        self.neighbour = test_plugin._port(
            fixed_ips=[{'ip_address': '10.0.0.6'}],
            **{portbindings.HOST_ID: 'node2'})
        self.core.get_ports.return_value = [self.steered, self.neighbour]
        self._paths(by_port={self.steered['id']: ['node2']})

    def _routes(self):
        return self.plugin._build_domain_payload(
            self.ctx, test_plugin._domain(networks=['net1']))['routes']

    def test_the_steered_vm_gets_segments_and_its_neighbour_none(self):
        steered, neighbour = self._routes()
        self.assertEqual(['fc00:0:2:fff0::'], steered['segments'])
        self.assertNotIn('segments', neighbour)

    def test_every_route_carries_its_network(self):
        self.assertEqual(['net1', 'net1'],
                         [r['network_id'] for r in self._routes()])

    def test_a_source_path_reaches_the_payload(self):
        self._paths(by_source={'node1': ({self.steered['id']: ['node3']},
                                         None)})
        steered, neighbour = self._routes()
        self.assertEqual({'node1': [E3]}, steered['segments_by_source'])
        self.assertNotIn('segments_by_source', neighbour)

    def test_paths_are_read_once_per_payload(self):
        self._routes()
        self.te_db.get_te_paths_for_domain.assert_called_once_with(
            self.ctx.elevated.return_value, mock.ANY)


class TestIncrementalPathCarriesTe(TePluginTestCaseBase):
    """_notify_port attaches segments too.

    Without them a steered VM's route arrives at depth 1 and stays so until
    the next full sync.
    """

    def setUp(self):
        super().setUp()
        self.domain_db.get_domain_ids_for_network.return_value = ['d1']
        self.port = test_plugin._port(**{portbindings.HOST_ID: 'node2'})

    def _added(self, call=0):
        return self.agent_rpc.routes_updated.call_args_list[call][1]['add']

    def test_a_steered_port_arrives_steered(self):
        self._paths(by_port={self.port['id']: ['node2']})
        self.plugin._notify_port(self.ctx, self.port)
        self.assertEqual([{'prefix': '10.0.0.5/32', 'host': 'node2',
                           'port_id': self.port['id'], 'network_id': 'net1',
                           'segments': ['fc00:0:2:fff0::']}], self._added())

    def test_a_source_path_arrives_with_the_port(self):
        self._paths(by_source={'node1': ({self.port['id']: ['node3']},
                                         None)})
        self.plugin._notify_port(self.ctx, self.port)
        self.assertEqual({'node1': [E3]},
                         self._added()[0]['segments_by_source'])

    def test_segments_are_per_domain(self):
        self.domain_db.get_domain_ids_for_network.return_value = ['d1', 'd2']
        self.te_db.get_te_paths_for_domain.side_effect = \
            lambda ctx, domain_id: {
                'by_port': ({self.port['id']: ['node1']}
                            if domain_id == 'd1' else {}),
                'by_host': {}}
        self.plugin._notify_port(self.ctx, self.port)
        self.assertEqual(['fc00:0:1:fff0::'], self._added(0)[0]['segments'])
        self.assertNotIn('segments', self._added(1)[0])

    def test_a_withdrawal_needs_no_paths(self):
        self.plugin._notify_port(self.ctx, self.port, removed=True)
        self.te_db.get_te_paths_for_domain.assert_not_called()
        removed = self.agent_rpc.routes_updated.call_args[1]['remove']
        self.assertNotIn('segments', removed[0])

    def test_a_migration_is_steered_at_its_new_host(self):
        # A host path follows the VM, and resolution runs at build time.
        self._paths(by_host={'node3': ['node1']})
        moved = dict(self.port, **{portbindings.HOST_ID: 'node3'})
        self.plugin.registry_port_updated(
            None, None, None,
            mock.Mock(context=self.ctx, latest_state=moved,
                      states=[self.port]))
        self.assertEqual(['fc00:0:1:fff0::'], self._added(1)[0]['segments'])


class TestResolveTePath(TePluginTestCaseBase):

    def _resolve(self, path, function_id=7):
        return self.plugin._resolve_te_path(self.ctx, path, self.locators,
                                            function_id)

    def test_shape_and_order(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node3', 'node1']))
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:1:fff0::',
                          'fc00:0:2:7::'], path['segments'])
        self.assertEqual('ACTIVE', path['status'])

    def test_the_function_id_is_hex_in_the_sid(self):
        path = self._resolve(_db_path(destination_host='node2'),
                             function_id=17)
        self.assertEqual(['fc00:0:2:11::'], path['segments'])

    def test_a_port_path_resolves_through_the_binding(self):
        self.core.get_port.return_value = {portbindings.HOST_ID: 'node2'}
        path = self._resolve(_db_path(destination_port_id='p1',
                                      via_hosts=['node2']))
        self.assertEqual(['fc00:0:2:fff0::', 'fc00:0:2:7::'],
                         path['segments'])
        self.core.get_port.assert_called_once_with(
            self.ctx.elevated.return_value, 'p1')

    def test_an_unbound_port_is_degraded(self):
        self.core.get_port.return_value = {portbindings.HOST_ID: ''}
        path = self._resolve(_db_path(destination_port_id='p1'))
        self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                            path['status']))

    def test_a_vanished_port_is_degraded(self):
        self.core.get_port.side_effect = n_exc.PortNotFound(port_id='p1')
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_port_id='p1'))['status'])

    def test_a_lost_waypoint_is_degraded(self):
        # G6: the via host's locator row is gone.
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node9']))
        self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                            path['status']))

    def test_a_destination_without_a_locator_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node9'))['status'])

    def test_a_domain_without_a_function_id_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node2'), function_id=None)['status'])

    def test_a_source_path_drops_its_own_leading_sid(self):
        # The source is the only ingress, and its agent drops the SID.
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node1',
                                      via_hosts=['node1', 'node3']))
        self.assertEqual([E3, 'fc00:0:2:7::'], path['segments'])
        self.assertEqual(path['segments'], path['encoded_segments'])
        self.assertEqual('ACTIVE', path['status'])

    def test_only_leading_source_sids_are_dropped(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node1',
                                      via_hosts=['node3', 'node1']))
        self.assertEqual([E3, E1, 'fc00:0:2:7::'], path['segments'])

    def test_an_any_source_path_keeps_every_sid(self):
        # Any node may be the ingress; each strips its own SID itself.
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node1', 'node3']))
        self.assertEqual([E1, E3, 'fc00:0:2:7::'], path['segments'])

    def test_a_source_without_a_locator_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node2', source_host='node9'))[
                'status'])

    def test_a_port_moved_onto_its_source_is_degraded(self):
        self.core.get_port.return_value = {portbindings.HOST_ID: 'node1'}
        path = self._resolve(_db_path(destination_port_id='p1',
                                      source_host='node1',
                                      via_hosts=['node3']))
        self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                            path['status']))

    def test_the_db_dict_is_not_mutated(self):
        original = _db_path(destination_host='node2')
        self._resolve(original)
        self.assertEqual(_db_path(destination_host='node2'), original)


class TestCreate(TePluginTestCaseBase):

    def setUp(self):
        super().setUp()
        self.domain_db.get_domain.return_value = test_plugin._domain(
            id='d1', project_id='owner', networks=['net1'])
        self.te_db.create_te_path.side_effect = \
            lambda ctx, domain_id, body: _db_path(
                domain_id=domain_id, project_id=body['project_id'],
                destination_host=body.get('destination_host'),
                destination_port_id=body.get('destination_port_id'),
                source_host=body.get('source_host'),
                via_hosts=body['via_hosts'])
        push = mock.patch.object(self.plugin, '_push_domain')
        self.push = push.start()
        self.addCleanup(push.stop)

    def _create(self, **body):
        return self.plugin.create_srv6_te_path(self.ctx, {'srv6_te_path': dict(
            {'domain_id': 'd1', 'project_id': 'admin',
             'destination_host': None, 'destination_port_id': None,
             'via_hosts': []}, **body)})

    def _refused(self, exc=None, **body):
        self.assertRaises(exc or plugin.te_ext.Srv6TePathInvalid,
                          self._create, **body)
        self.push.assert_not_called()

    def test_the_project_is_forced_to_the_domains(self):
        self.assertEqual('owner',
                         self._create(destination_host='node2')['project_id'])

    def test_the_domain_is_pushed_after_the_write(self):
        calls = mock.Mock()
        calls.attach_mock(self.te_db.create_te_path, 'create')
        calls.attach_mock(self.push, 'push')
        self._create(destination_host='node2')
        self.assertEqual(['create', 'push'],
                         [c[0] for c in calls.mock_calls])

    def test_the_answer_is_resolved(self):
        path = self._create(destination_host='node2', via_hosts=['node1'])
        self.assertEqual(['fc00:0:1:fff0::', 'fc00:0:2:7::'],
                         path['segments'])

    def test_a_port_on_a_domain_network(self):
        self.core.get_port.return_value = {'network_id': 'net1',
                                           portbindings.HOST_ID: 'node2'}
        self.assertEqual('ACTIVE',
                         self._create(destination_port_id='p1')['status'])

    def test_a_port_on_another_network(self):
        self.core.get_port.return_value = {'network_id': 'net9'}
        self._refused(destination_port_id='p1')

    def test_a_missing_domain(self):
        self.domain_db.get_domain.return_value = None
        self._refused(plugin.srv6_ext.Srv6DomainNotFound,
                      destination_host='node2')
        self.te_db.create_te_path.assert_not_called()

    def test_six_via_hosts_is_the_limit(self):
        many = {'n%d' % i: 'fc00:0:%x::/48' % (i + 10) for i in range(7)}
        many['node2'] = LOC2
        self.srv6_db.get_host_locators.return_value = many
        self._create(destination_host='node2',
                     via_hosts=['n%d' % i for i in range(6)])
        self.push.reset_mock()
        self._refused(destination_host='node2',
                      via_hosts=['n%d' % i for i in range(7)])

    def test_a_duplicate_is_a_conflict(self):
        self.te_db.create_te_path.side_effect = db_exc.DBDuplicateEntry()
        self._refused(plugin.te_ext.Srv6TePathExists,
                      destination_host='node2')

    def test_a_port_deleted_under_the_request_is_a_400(self):
        self.core.get_port.return_value = {'network_id': 'net1'}
        self.te_db.create_te_path.side_effect = db_exc.DBReferenceError(
            'srv6_te_paths', 'fk', 'destination_port_id', 'ports')
        self._refused(destination_port_id='p1')

    def test_an_empty_source_is_stored_as_any_source(self):
        self.assertIsNone(self._create(destination_host='node2',
                                       source_host='')['source_host'])
        self.assertIsNone(
            self.te_db.create_te_path.call_args[0][2]['source_host'])

    def test_a_source_is_stored(self):
        path = self._create(destination_host='node2', source_host='node1',
                            via_hosts=['node3'])
        self.assertEqual('node1', path['source_host'])

    def test_an_unregistered_source(self):
        self._refused(destination_host='node2', source_host='node9')

    def test_the_destination_host_as_its_own_source(self):
        self._refused(destination_host='node2', source_host='node2')

    def test_a_port_bound_on_the_source(self):
        self.core.get_port.return_value = {'id': 'p1', 'network_id': 'net1',
                                           portbindings.HOST_ID: 'node1'}
        self._refused(destination_port_id='p1', source_host='node1')

    def test_a_port_bound_elsewhere_may_name_a_source(self):
        self.core.get_port.return_value = {'id': 'p1', 'network_id': 'net1',
                                           portbindings.HOST_ID: 'node2'}
        self.assertEqual('ACTIVE', self._create(
            destination_port_id='p1', source_host='node1',
            via_hosts=['node3'])['status'])

    def test_a_duplicate_names_the_source(self):
        self.te_db.create_te_path.side_effect = db_exc.DBDuplicateEntry()
        exc = self.assertRaises(plugin.te_ext.Srv6TePathExists,
                                self._create, destination_host='node2',
                                source_host='node1')
        self.assertIn('host node2 from node1', str(exc))


class TestListBySource(TePluginTestCaseBase):
    """The filter goes to the DB as given; [''] lists any-source paths.

    Tested here, not over REST: neutron keeps an empty filter value only
    with its empty-string-filtering extension, which ML2 loads and the
    REST harness's core plugin does not.
    """

    def test_the_filter_reaches_the_db(self):
        self.te_db.get_all_te_paths.return_value = []
        for value in (['node1'], ['']):
            self.plugin.get_srv6_te_paths(self.ctx,
                                          filters={'source_host': value})
            self.assertEqual(
                value,
                self.te_db.get_all_te_paths.call_args[0][1]['source_host'])


class TestDeadWaypoint(TePluginTestCaseBase):
    """A path through a node whose agent is down goes direct.

    The feature is FEATURE-TE-FAILOVER. Direct, not "skip the dead hop" and
    not "fall to the host path": the same answer as an unknown locator
    (decision 9). The registry keeps the dead
    node's locator -- its VMs' routes and decap SIDs must stay on every
    other node -- so the death is a separate input, dead_hosts.
    """

    def _agents(self, *dead, hosts=('node1', 'node2', 'node3')):
        self.core.get_agents.return_value = [
            {'host': host, 'alive': host not in dead} for host in hosts]

    def _attach(self, *routes):
        return self.plugin._attach_segments(self.ctx, 'd1', list(routes),
                                            self.locators)

    # --- which hosts are dead --------------------------------------------
    def test_only_the_open_vswitch_agents_are_asked(self):
        self._agents('node1')
        self.assertEqual(frozenset({'node1'}),
                         self.plugin._dead_hosts(self.ctx))
        self.core.get_agents.assert_called_once_with(
            self.ctx, filters={'agent_type': ['Open vSwitch agent']})

    def test_a_host_with_no_agent_is_alive(self):
        self._agents('node2', hosts=('node2',))
        self.assertEqual(frozenset({'node2'}),
                         self.plugin._dead_hosts(self.ctx))

    def test_the_check_off_asks_nobody(self):
        self.config(group=config.SRV6_SERVER_GROUP, te_liveness_interval=0)
        self._agents('node1')
        self.assertEqual(frozenset(), self.plugin._dead_hosts(self.ctx))
        self.assertFalse(self.core.get_agents.called)

    def test_an_unanswerable_query_leaves_every_host_alive(self):
        self.core.get_agents.side_effect = RuntimeError('db gone')
        self.assertEqual(frozenset(), self.plugin._dead_hosts(self.ctx))

    def test_a_core_plugin_without_agents_leaves_every_host_alive(self):
        del self.core.get_agents
        self.assertEqual(frozenset(), self.plugin._dead_hosts(self.ctx))

    # --- the routes ---------------------------------------------------------
    def test_a_host_path_through_a_dead_node_goes_direct(self):
        self._paths(by_host={'node2': ['node1']})
        self._agents('node1')
        with mock.patch.object(plugin.LOG, 'warning') as warning:
            route = self._attach(_route('p1'))[0]
        self.assertNotIn('segments', route)
        self.assertTrue(warning.called)

    def test_the_same_path_is_steered_while_the_node_lives(self):
        self._paths(by_host={'node2': ['node1']})
        self._agents()
        self.assertEqual([E1], self._attach(_route('p1'))[0]['segments'])

    def test_a_dead_node_off_the_path_changes_nothing(self):
        self._paths(by_host={'node2': ['node1']})
        self._agents('node3')
        self.assertEqual([E1], self._attach(_route('p1'))[0]['segments'])

    def test_one_dead_hop_sends_the_whole_path_direct(self):
        # Not a shorter list: that is the partial steering decision 9 refuses.
        self._paths(by_host={'node2': ['node3', 'node1']})
        self._agents('node1')
        self.assertNotIn('segments', self._attach(_route('p1'))[0])

    def test_a_dead_port_path_goes_direct_not_to_the_host_path(self):
        self._paths(by_port={'p1': ['node1']}, by_host={'node2': ['node3']})
        self._agents('node1')
        route = self._attach(_route('p1'), _route('p2', prefix='10.0.0.6/32'))
        self.assertNotIn('segments', route[0])
        self.assertEqual([E3], route[1]['segments'])

    def test_a_dead_node_off_a_source_path_leaves_it(self):
        self._paths(by_source={'node1': (None, {'node2': ['node3']})})
        self._agents('node2')
        route = self._attach(_route('p1'))[0]
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_a_source_path_through_a_dead_node_is_direct_for_its_source(self):
        self._paths(by_host={'node2': ['node3']},
                    by_source={'node1': (None, {'node2': ['node1']})})
        self._agents('node1')
        route = self._attach(_route('p1'))[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node1': []}, route['segments_by_source'])

    def test_a_dead_destination_does_not_unsteer_its_path(self):
        # Its VMs are gone with it; nothing to reroute.
        self._paths(by_host={'node2': ['node3']})
        self._agents('node2')
        self.assertEqual([E3], self._attach(_route('p1'))[0]['segments'])

    def test_a_domain_with_no_path_costs_no_agent_query(self):
        self._agents('node1')
        self._attach(_route('p1'))
        self.assertFalse(self.core.get_agents.called)

    def test_the_caller_can_hand_in_the_dead_set(self):
        self._paths(by_host={'node2': ['node1']})
        route = self.plugin._attach_segments(
            self.ctx, 'd1', [_route('p1')], self.locators,
            dead_hosts=frozenset({'node1'}))[0]
        self.assertNotIn('segments', route)
        self.assertFalse(self.core.get_agents.called)

    def test_the_payload_keeps_the_dead_nodes_locator(self):
        self.core.get_network.return_value = {'subnets': [], 'mtu': 1450}
        port = test_plugin._port(**{portbindings.HOST_ID: 'node2'})
        self.core.get_ports.return_value = [port]
        self._paths(by_port={port['id']: ['node1']})
        self._agents('node1')
        payload = self.plugin._build_domain_payload(
            self.ctx, test_plugin._domain(networks=['net1']))
        self.assertEqual(self.locators, payload['locators'])
        self.assertNotIn('segments', payload['routes'][0])

    # --- what the API reports -----------------------------------------------
    def _path(self, **kwargs):
        return _db_path(destination_host='node2', via_hosts=['node1'],
                        **kwargs)

    def test_the_path_reports_degraded(self):
        path = self.plugin._resolve_te_path(
            self.ctx, self._path(), self.locators, 7, frozenset({'node1'}))
        self.assertEqual('DEGRADED', path['status'])
        self.assertEqual([], path['segments'])
        self.assertEqual([], path['encoded_segments'])

    def test_the_path_reports_active_once_the_node_is_back(self):
        path = self.plugin._resolve_te_path(
            self.ctx, self._path(), self.locators, 7, frozenset())
        self.assertEqual('ACTIVE', path['status'])

    def test_a_dead_destination_or_source_does_not_degrade_the_path(self):
        path = self.plugin._resolve_te_path(
            self.ctx, self._path(source_host='node3'), self.locators, 7,
            frozenset({'node2', 'node3'}))
        self.assertEqual('ACTIVE', path['status'])

    def test_listing_resolves_against_one_liveness_query(self):
        self.te_db.get_all_te_paths.return_value = [
            self._path(id='a'), self._path(id='b')]
        self._agents('node1')
        paths = self.plugin.get_srv6_te_paths(self.ctx)
        self.assertEqual(['DEGRADED', 'DEGRADED'],
                         [p['status'] for p in paths])
        self.assertEqual(1, self.core.get_agents.call_count)


class TestLivenessWorker(TePluginTestCaseBase):

    def test_the_monitor_runs_as_one_periodic_worker(self):
        with mock.patch.object(plugin.neutron_worker,
                               'PeriodicWorker') as worker:
            workers = self.plugin.get_workers()
        self.assertEqual([worker.return_value], workers)
        func, interval, delay = worker.call_args[0]
        self.assertEqual(15, interval)
        self.assertEqual('check', func.__name__)
        self.assertGreaterEqual(delay, 1)

    def test_no_worker_when_the_check_is_off(self):
        self.config(group=config.SRV6_SERVER_GROUP, te_liveness_interval=0)
        self.assertEqual([], self.plugin.get_workers())
