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

"""The SDK resources against the server's API definitions.

The resources cannot import api/definitions (client machines have no
neutron_lib); this is where the two are held together.
"""

from openstack import fields
import testtools

from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def
from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.cli import resources

_PAGING = {'limit': 'limit', 'marker': 'marker'}


class TestResources(testtools.TestCase):

    def _check(self, res, definition_map, key, keys, base_path, allow,
               query):
        self.assertEqual(key, res.resource_key)
        self.assertEqual(keys, res.resources_key)
        self.assertEqual(base_path, res.base_path)
        for verb in ('create', 'fetch', 'commit', 'delete', 'list'):
            self.assertEqual(verb in allow, getattr(res, 'allow_' + verb),
                             verb)
        mapping = dict(_PAGING)
        mapping.update({q: q for q in query})
        self.assertEqual(mapping, res._query_mapping._mapping)
        # Every attribute the server returns has a typed property.
        body = set(res._get_mapping(fields.Body))
        self.assertLessEqual(set(definition_map), body | {'parent'})
        # And every filter asked for is one the server declares.
        filters = {k for k, v in definition_map.items()
                   if isinstance(v, dict) and v.get('is_filter')}
        self.assertLessEqual(set(query), filters)

    def test_domain(self):
        self._check(resources.Srv6Domain,
                    srv6_def.RESOURCE_ATTRIBUTE_MAP[srv6_def.COLLECTION_NAME],
                    srv6_def.RESOURCE_NAME, srv6_def.COLLECTION_NAME,
                    '/srv6_domains',
                    ('create', 'fetch', 'commit', 'delete', 'list'),
                    ('name', 'project_id'))

    def test_network_association(self):
        sub = srv6_def.SUB_RESOURCE_ATTRIBUTE_MAP[
            srv6_def.NET_ASSOC_COLLECTION_NAME]
        self._check(resources.Srv6NetworkAssociation, sub['parameters'],
                    srv6_def.NET_ASSOC_RESOURCE_NAME,
                    srv6_def.NET_ASSOC_COLLECTION_NAME,
                    '/srv6_domains/%(domain_id)s/network_associations',
                    ('create', 'fetch', 'delete', 'list'),
                    ('network_id', 'project_id'))
        self.assertEqual(srv6_def.COLLECTION_NAME,
                         sub['parent']['collection_name'])

    def test_locator(self):
        self._check(resources.Srv6Locator,
                    loc_def.RESOURCE_ATTRIBUTE_MAP[loc_def.COLLECTION_NAME],
                    loc_def.RESOURCE_NAME, loc_def.COLLECTION_NAME,
                    '/srv6_locators', ('fetch', 'list'), ('host',))

    def test_te_path(self):
        self._check(resources.Srv6TePath,
                    te_def.RESOURCE_ATTRIBUTE_MAP[te_def.COLLECTION_NAME],
                    te_def.RESOURCE_NAME, te_def.COLLECTION_NAME,
                    '/srv6_te_paths',
                    ('create', 'fetch', 'commit', 'delete', 'list'),
                    ('domain_id', 'destination_host',
                     'destination_port_id', 'project_id'))

    def test_update_sends_only_the_change(self):
        path = resources.Srv6TePath.new(id='abc', via_hosts=[])
        path._body._dirty.discard('id')    # as Resource.commit does
        request = path._prepare_request(prepend_key=True)
        self.assertEqual('srv6_te_paths/abc', request.url)
        self.assertEqual({'srv6_te_path': {'via_hosts': []}}, request.body)
