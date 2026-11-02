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

"""PLAN-UNRESERVED-STEERING step 4: TE paths from one source VM, auto paths.

Plugin units with the database mocked (test_te's base): what a create
accepts and refuses, the payload key the head-end agent reads
(segments_by_source_port), the status, the auto-path guard on PUT, and what
a VM's migration or deletion does to the paths.
"""

from unittest import mock

from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib import exceptions as n_exc

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_plugin
from networking_srv6_agent.tests.unit.services import test_te

VM, DST, OTHER = 'vm-port', 'dst-port', 'other-port'


def _port(port_id, host, network='net1', ips=('10.0.0.9',)):
    return test_plugin._port(id=port_id, network_id=network,
                             fixed_ips=[{'ip_address': ip} for ip in ips],
                             **{portbindings.HOST_ID: host})


class SourcePortTestCase(test_te.TePluginTestCaseBase):

    def setUp(self):
        super().setUp()
        self.ports = {VM: _port(VM, 'node1', ips=('10.0.0.9', 'fd00::9')),
                      DST: _port(DST, 'node2', ips=('10.0.0.5',)),
                      OTHER: _port(OTHER, 'node3', network='net9')}

        def get_port(context, port_id, fields=None):
            if port_id not in self.ports:
                raise n_exc.PortNotFound(port_id=port_id)
            return self.ports[port_id]
        self.core.get_port.side_effect = get_port


class TestCreate(SourcePortTestCase):

    def setUp(self):
        super().setUp()
        self.domain_db.get_domain.return_value = test_plugin._domain(
            id='d1', project_id='owner', networks=['net1'])
        self.te_db.create_te_path.side_effect = \
            lambda ctx, domain_id, body: test_te._db_path(
                domain_id=domain_id, project_id=body['project_id'],
                destination_host=body.get('destination_host'),
                destination_port_id=body.get('destination_port_id'),
                source_port_id=body.get('source_port_id'),
                origin=body.get('origin'), via_hosts=body['via_hosts'])
        push = mock.patch.object(self.plugin, '_push_domain')
        self.push = push.start()
        self.addCleanup(push.stop)

    def _create(self, **body):
        return self.plugin.create_srv6_te_path(self.ctx, {'srv6_te_path': dict(
            {'domain_id': 'd1', 'project_id': 'admin',
             'destination_host': None, 'destination_port_id': None,
             'source_port_id': VM, 'via_hosts': []}, **body)})

    def _refused(self, words, **body):
        e = self.assertRaises(plugin.te_ext.Srv6TePathInvalid, self._create,
                              **body)
        self.assertIn(words, str(e))
        self.te_db.create_te_path.assert_not_called()

    def test_a_vm_path_is_stored_as_the_users(self):
        path = self._create(destination_port_id=DST, via_hosts=['node3'])
        body = self.te_db.create_te_path.call_args[0][2]
        self.assertEqual((VM, 'user', None),
                         (body['source_port_id'], body['origin'],
                          body['source_host']))
        # resolved from the VM's binding (node1): node3, then node2's decap
        self.assertEqual('ACTIVE', path['status'])
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:2:7::'],
                         path['segments'])
        self.push.assert_called_once()

    def test_not_with_a_source_host(self):
        self._refused('exclude each other', destination_host='node2',
                      source_host='node3')

    def test_not_with_bandwidth(self):
        self._refused('does not reserve', destination_host='node2',
                      bandwidth=10)

    def test_not_to_itself(self):
        self._refused('its own destination', destination_port_id=VM)

    def test_a_vm_that_does_not_exist(self):
        self._refused('does not exist', destination_host='node2',
                      source_port_id='gone')

    def test_a_vm_of_another_network(self):
        self._refused('not attached', destination_host='node2',
                      source_port_id=OTHER)

    def test_an_unbound_vm(self):
        self.ports[VM][portbindings.HOST_ID] = ''
        self._refused('not bound', destination_host='node2')

    def test_a_vm_on_the_destination_node(self):
        self.ports[VM][portbindings.HOST_ID] = 'node2'
        self._refused('never encapsulates', destination_port_id=DST)

    def test_a_vm_on_a_node_without_a_locator(self):
        self.ports[VM][portbindings.HOST_ID] = 'node9'
        self._refused('no registered SRv6 locator', destination_host='node2')


class TestAutoPathGuard(SourcePortTestCase):

    def test_an_auto_path_cannot_be_re_steered(self):
        self.te_db.get_te_path.return_value = test_te._db_path(
            origin=te_def.ORIGIN_AUTO, source_port_id=VM,
            destination_port_id=DST)
        e = self.assertRaises(
            plugin.te_ext.Srv6TePathInvalid,
            self.plugin.update_srv6_te_path, self.ctx, 'tp1',
            {'srv6_te_path': {'via_hosts': ['node3']}})
        self.assertIn('origin auto', str(e))
        self.te_db.update_te_path.assert_not_called()

    def test_a_users_vm_path_can(self):
        self.te_db.get_te_path.return_value = test_te._db_path(
            origin=te_def.ORIGIN_USER, source_port_id=VM)
        self.te_db.update_te_path.return_value = test_te._db_path(
            source_port_id=VM, destination_port_id=DST, via_hosts=['node3'])
        with mock.patch.object(self.plugin, '_push_domain_by_id'):
            self.plugin.update_srv6_te_path(
                self.ctx, 'tp1', {'srv6_te_path': {'via_hosts': ['node3']}})
        self.te_db.update_te_path.assert_called_once()


class TestPayload(SourcePortTestCase):

    def _attach(self, by_source_port, *routes):
        self.te_db.get_te_paths_for_domain.return_value = {
            'by_port': {}, 'by_host': {}, 'by_source': {},
            'by_source_port': by_source_port}
        routes = [dict(r) for r in routes]
        self.plugin._attach_segments(self.ctx, 'd1', routes,
                                     dict(self.locators))
        return routes

    def test_the_entry_names_the_node_network_prefixes_and_segments(self):
        [r] = self._attach({VM: {'by_port': {DST: ['node3']},
                                 'by_host': {}}},
                           test_te._route(DST))
        self.assertEqual({VM: {'host': 'node1', 'network_id': 'net1',
                               'prefixes': ['10.0.0.9/32', 'fd00::9/128'],
                               'segments': ['fc00:0:3:fff0::']}},
                         r['segments_by_source_port'])
        # the other ranks are untouched: no source-VM entry leaks into them
        self.assertNotIn('segments', r)
        self.assertNotIn('segments_by_source', r)

    def test_port_path_over_host_path_and_an_explicit_direct(self):
        maps = {VM: {'by_port': {DST: []}, 'by_host': {'node2': ['node3']}}}
        to_dst, to_host = self._attach(
            maps, test_te._route(DST),
            test_te._route('another', prefix='10.0.0.6/32'))
        self.assertEqual([], to_dst['segments_by_source_port'][VM][
            'segments'])
        self.assertEqual(['fc00:0:3:fff0::'],
                         to_host['segments_by_source_port'][VM]['segments'])

    def test_a_route_without_a_vm_path_gets_no_key(self):
        [r] = self._attach({VM: {'by_port': {}, 'by_host': {'node3': []}}},
                           test_te._route(DST))
        self.assertNotIn('segments_by_source_port', r)

    def test_a_route_to_the_vms_own_node_gets_no_entry(self):
        [r] = self._attach({VM: {'by_port': {}, 'by_host': {'node1': []}}},
                           test_te._route('local', host='node1'))
        self.assertNotIn('segments_by_source_port', r)

    def test_an_unbound_or_gone_vm_gets_no_entry(self):
        self.ports[VM][portbindings.HOST_ID] = ''
        maps = {VM: {'by_port': {DST: ['node3']}, 'by_host': {}},
                'gone': {'by_port': {DST: ['node3']}, 'by_host': {}}}
        [r] = self._attach(maps, test_te._route(DST))
        self.assertNotIn('segments_by_source_port', r)

    def test_only_vm_paths_still_steer(self):
        # the early return for a domain with no path must not skip them
        [r] = self._attach({VM: {'by_port': {DST: ['node3']},
                                 'by_host': {}}}, test_te._route(DST))
        self.assertIn('segments_by_source_port', r)


class TestResolve(SourcePortTestCase):

    def _resolve(self, **path):
        return self.plugin._resolve_te_path(
            self.ctx, test_te._db_path(**path), dict(self.locators), 7)

    def test_the_source_is_the_vms_binding_now(self):
        path = self._resolve(source_port_id=VM, destination_port_id=DST,
                             via_hosts=['node1', 'node3'])
        # node1 is the VM's node: its own leading SID is dropped, as its
        # agent drops it
        self.assertEqual(['fc00:0:3:fff0::', 'fc00:0:2:7::'],
                         path['segments'])
        # moved to node3: node1 is a waypoint again, node3 is not leading
        self.ports[VM][portbindings.HOST_ID] = 'node3'
        path = self._resolve(source_port_id=VM, destination_port_id=DST,
                             via_hosts=['node1', 'node3'])
        self.assertEqual(['fc00:0:1:fff0::', 'fc00:0:3:fff0::',
                          'fc00:0:2:7::'], path['segments'])

    def test_an_unbound_vm_is_degraded(self):
        self.ports[VM][portbindings.HOST_ID] = ''
        path = self._resolve(source_port_id=VM, destination_host='node2')
        self.assertEqual(te_def.STATUS_DEGRADED, path['status'])

    def test_a_vm_moved_onto_the_destination_is_degraded(self):
        self.ports[VM][portbindings.HOST_ID] = 'node2'
        path = self._resolve(source_port_id=VM, destination_port_id=DST)
        self.assertEqual(te_def.STATUS_DEGRADED, path['status'])


class TestVmMovesAndGoes(SourcePortTestCase):

    def setUp(self):
        super().setUp()
        push = mock.patch.object(self.plugin, '_push_domain_by_id')
        self.push = push.start()
        self.addCleanup(push.stop)
        self.admin = self.ctx.elevated.return_value
        self.te_db.delete_moved_reservations.return_value = []

    def _moved(self, old, new):
        before = dict(self.ports[VM], status='ACTIVE',
                      **{portbindings.HOST_ID: old})
        after = dict(before, **{portbindings.HOST_ID: new})
        self.plugin.registry_port_updated(
            None, None, None, payload=mock.Mock(
                context=self.ctx, latest_state=after, states=[before]))

    def test_a_move_deletes_its_auto_paths_and_pushes_their_domains(self):
        self.te_db.delete_auto_paths_of_port.return_value = [
            test_te._db_path(domain_id='d1', origin='auto',
                             source_port_id=VM, destination_port_id=DST)]
        self.te_db.get_domain_ids_for_source_port.return_value = ['d2']
        with mock.patch.object(plugin.LOG, 'warning') as warn:
            self._moved('node1', 'node3')
        self.te_db.delete_auto_paths_of_port.assert_called_once_with(
            self.admin, VM)
        self.assertEqual([mock.call(self.admin, 'd1'),
                          mock.call(self.admin, 'd2')],
                         self.push.call_args_list)
        self.assertTrue(any('automatic path' in c[0][0]
                            for c in warn.call_args_list))

    def test_no_move_no_path_work(self):
        self._moved('node1', 'node1')
        self.te_db.delete_auto_paths_of_port.assert_not_called()

    def test_a_database_error_does_not_break_the_port_update(self):
        self.te_db.delete_auto_paths_of_port.side_effect = RuntimeError
        with mock.patch.object(plugin.LOG, 'exception') as log:
            self._moved('node1', 'node3')
        self.assertTrue(log.called)
        self.push.assert_not_called()

    def test_a_deleted_vm_has_its_domains_pushed_after_the_delete(self):
        self.te_db.get_domain_ids_for_source_port.return_value = ['d1']
        self.plugin.registry_port_deleting(
            None, events.BEFORE_DELETE, None,
            payload=mock.Mock(context=self.ctx, resource_id=VM))
        self.push.assert_not_called()           # the rows are not gone yet
        self.plugin.registry_port_deleted(
            None, events.AFTER_DELETE, None,
            payload=mock.Mock(context=self.ctx,
                              latest_state=self.ports[VM]))
        self.push.assert_called_once_with(self.admin, 'd1')
        self.assertEqual({}, self.plugin._deleting_source_ports)

    def test_a_deleted_port_without_vm_paths_pushes_nothing(self):
        self.plugin.registry_port_deleting(
            None, events.BEFORE_DELETE, None,
            payload=mock.Mock(context=self.ctx, resource_id=DST))
        self.plugin.registry_port_deleted(
            None, events.AFTER_DELETE, None,
            payload=mock.Mock(context=self.ctx,
                              latest_state=self.ports[DST]))
        self.push.assert_not_called()


class TestRest(test_te.TeRestTestCase):
    """Over REST, with the real database and policy."""

    def setUp(self):
        super().setUp()
        self.vm = self._vm_port()
        self.dst = self._vm_port()
        core = self.srv6._core_plugin()
        real = core.get_port
        self.bound = {self.vm['id']: 'node1', self.dst['id']: 'node2'}

        def get_port(context, port_id, fields=None):
            port = real(context, port_id)
            return dict(port, **{portbindings.HOST_ID:
                                 self.bound.get(port_id, '')})
        patcher = mock.patch.object(core, 'get_port', side_effect=get_port)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_created_shown_with_origin_user(self):
        path = self._path(destination_port_id=self.dst['id'],
                          source_port_id=self.vm['id'], via_hosts=['node1'])
        self.assertEqual((self.vm['id'], 'user', None),
                         (path['source_port_id'], path['origin'],
                          path['source_host']))
        self.assertEqual(path['source_port_id'],
                         self._show(path['id'], as_admin=True)[
                             'source_port_id'])

    def test_two_vms_and_an_any_source_path_to_one_port(self):
        other = self._vm_port()
        self.bound[other['id']] = 'node1'
        self._path(destination_port_id=self.dst['id'])
        self._path(destination_port_id=self.dst['id'],
                   source_port_id=self.vm['id'])
        self._path(destination_port_id=self.dst['id'],
                   source_port_id=other['id'])
        res = self._path(expected=409, destination_port_id=self.dst['id'],
                         source_port_id=self.vm['id'])
        self.assertIn('from port %s' % self.vm['id'], self._message(res))

    def test_origin_cannot_be_posted(self):
        self._path(expected=400, destination_host='node2', origin='auto')

    def test_the_source_port_cannot_be_changed(self):
        path = self._path(destination_host='node2',
                          source_port_id=self.vm['id'])
        self._update(path['id'], expected=400, source_port_id=None)

    def test_a_tenant_cannot_name_a_source_port(self):
        self._path(expected=403, as_admin=False, destination_host='node2',
                   source_port_id=self.vm['id'])

    def test_unbound_is_refused(self):
        del self.bound[self.vm['id']]
        res = self._path(expected=400, destination_host='node2',
                         source_port_id=self.vm['id'])
        self.assertIn('not bound', self._message(res))
