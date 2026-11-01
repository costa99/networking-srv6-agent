# Reproduction of review finding 1 (ponytail-review of 0e282a3, 2026-11-01).
#
# A bandwidth reservation to a PORT is accounted on
#     source + pinned via_hosts + the port's CURRENT host
# so after a live migration the accounted route is not the route the traffic
# takes, and the next admission over-commits a link.
#
# Fabric (host links 100 Mbit/s so one extra 60 Mbit/s flow overflows them):
#
#   node1 - la = s1,s2 = lb - node2   (node1 link 1000; every other link 100)
#                  s1,s2 = lc - node3
#
# 1. reserve 60 Mbit/s node1 -> port P, P on node2:  hops [la, s1, lb]
# 2. P live-migrates to node3 (under lc). The path now steers
#    la, s1, lb, <node3 decap>: after lb the packet is routed by the
#    underlay, lb -> spine -> lc -> node3, so lc -> node3 carries 60 Mbit/s.
# 3. reserve 60 Mbit/s node1 -> node3 (another domain): it would put another
#    60 on lc -> node3 (capacity 100), so admission must answer 409.
#
# Expected: step 3 is refused.   Observed on 0e282a3: 201.
# d17948b refused step 1 (400); since reserved_host (PLAN-RESERVATION-
# FOLLOWS-INSTANCE step 2) ports are allowed again and the books end at the
# host the hops were computed for. The control still shows the 409 for a host.

from unittest import mock

from neutron_lib.api.definitions import portbindings

from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.services import plugin
from networking_srv6_agent.tests.unit.services import test_te

LOC3 = 'fc00:0:3::/48'
FABRIC = {'la': 'fc00:0:a::/48', 'lb': 'fc00:0:b::/48',
          'lc': 'fc00:0:d::/48',
          's1': 'fc00:0:c1::/48', 's2': 'fc00:0:c2::/48'}
CAPACITY = {}
for _a, _b, _c in (('node1', 'la', 1000), ('node2', 'lb', 100),
                   ('node3', 'lc', 100),
                   ('la', 's1', 100), ('la', 's2', 100),
                   ('s1', 'lb', 100), ('s2', 'lb', 100),
                   ('s1', 'lc', 100), ('s2', 'lc', 100)):
    CAPACITY[(_a, _b)] = CAPACITY[(_b, _a)] = _c


class TestReservationToAPortAfterMigration(test_te.TeRestTestCase):

    def setUp(self):
        super().setUp()
        import datetime
        srv6_db.set_host_locator(self.admin_ctx, 'node3', LOC3,
                                 datetime.datetime.now(
                                     datetime.timezone.utc))
        self.srv6.fabric_locators = dict(FABRIC)
        self.srv6.capacity = dict(CAPACITY)
        # The harness binds no host: bind the port through a switch.
        self.port_host = {'host': 'node2'}
        core = self.srv6._core_plugin()
        real = core.get_port

        def get_port(context, port_id, fields=None):
            return dict(real(context, port_id),
                        **{portbindings.HOST_ID: self.port_host['host']})
        patcher = mock.patch.object(core, 'get_port', side_effect=get_port)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_control_the_same_load_on_a_host_path_is_refused(self):
        # Same second flow, but the first reservation was made to node3 by
        # host name: the links are accounted right, and 409 comes back.
        self._path(destination_host='node3', source_host='node1',
                   bandwidth=60)
        res = self._path(domain_id=self._domain()['id'],
                         destination_host='node3', source_host='node1',
                         bandwidth=60, expected=409)
        self.assertIn('Mbit/s', self._message(res))

    def test_after_a_migration_the_books_end_at_the_reserved_host(self):
        # The fix (reserved_host): the books keep the host the hops were
        # computed for. Before, they read the port's new host and charged
        # lb-node3, a link that does not exist.
        port = self._vm_port()
        first = self._path(destination_port_id=port['id'],
                           source_host='node1', bandwidth=60)
        self.assertEqual(['la', 's1', 'lb'], first['via_hosts'])
        self.port_host['host'] = 'node3'              # live migration
        with mock.patch.object(plugin.pce, 'compute',
                               wraps=plugin.pce.compute) as compute:
            self._path(domain_id=self._domain()['id'],
                       destination_host='node3', source_host='node1',
                       bandwidth=10)
        used = compute.call_args[0][1]
        self.assertEqual(60, used[('lb', 'node2')])
        self.assertNotIn(('lb', 'node3'), used)
