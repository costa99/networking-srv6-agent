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

"""RPC between the SRv6 service plugin and the compute-node agents.

Plain dict payloads over classic Neutron RPC, modelled on l2pop rather than
on the OVO push framework: l2pop is an exact in-tree template for "server
fans out state, agent applies it", and this package must not depend on
networking-bagpipe.

Directions:
  server -> agents   fanout casts: domain_updated, routes_updated,
                     domain_deleted
  agent  -> server   call:         sync_state(host, locator) -> [domain, ...]

The kwargs are part of the contract, not only the method names (P4-PLAN.md
4.4). The agent's handlers end in **kwargs, so a server sending `vpn=` to a
handler expecting `domain=` would see the payload land in **kwargs and the
handler return without a word. P5's contract test binds every cast below to
the agent's named parameters.

Payload shape. Each route is {prefix, host, port_id} and may carry an
optional `segments` list of transit SIDs (filled by P6). PAYLOAD_VERSION is
an equality check on both sides, so adding an optional key the agent already
reads is the way to extend it: bumping the version would make every older
agent ignore every update.
"""

from neutron_lib.agent import topics
from neutron_lib import rpc as n_rpc
from oslo_log import log as logging
import oslo_messaging

from networking_srv6_agent.common import constants


LOG = logging.getLogger(__name__)


class Srv6AgentNotifyAPI:
    """Server-side sender. One fanout topic, three methods."""

    def __init__(self, topic=topics.AGENT):
        self.topic = topic
        self.topic_srv6_update = topics.get_topic_name(
            topic, constants.TOPIC_SRV6, topics.UPDATE)
        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def _fanout(self, context, method, **kwargs):
        LOG.debug("SRv6 fanout %(method)s on %(topic)s: %(kwargs)s",
                  {'method': method, 'topic': self.topic_srv6_update,
                   'kwargs': kwargs})
        cctxt = self.client.prepare(topic=self.topic_srv6_update, fanout=True)
        cctxt.cast(context, method,
                   payload_version=constants.PAYLOAD_VERSION, **kwargs)

    def domain_updated(self, context, domain):
        """Full desired state of one domain, to every agent.

        Sent on any change that alters which networks or subnets a domain
        covers. Agents treat it as authoritative and converge to it, so a
        lost message costs at most one resync interval rather than leaving
        permanently divergent state.
        """
        self._fanout(context, constants.DOMAIN_UPDATED, domain=domain)

    def routes_updated(self, context, domain_id, add=None, remove=None):
        """Incremental per-port routes.

        Separate from domain_updated because port churn is the common case,
        and rebuilding a whole domain's payload for one instance would put
        the entire route set on the wire every time a VM boots.
        """
        self._fanout(context, constants.ROUTES_UPDATED, domain_id=domain_id,
                     add=add or [], remove=remove or [])

    def domain_deleted(self, context, domain_id, function_id, vrf_table):
        """The domain is gone; tear down its VRF and SID.

        Carries function_id and vrf_table because after the delete the agent
        cannot look them up -- the row is gone -- and it needs both to name
        the kernel objects it must remove.
        """
        self._fanout(context, constants.DOMAIN_DELETED, domain_id=domain_id,
                     function_id=function_id, vrf_table=vrf_table)


class Srv6ServerRpcCallback:
    """Server-side receiver of the agent's sync_state call."""

    target = oslo_messaging.Target(version='1.0')

    def __init__(self, plugin):
        self.plugin = plugin

    def sync_state(self, rpc_context, **kwargs):
        """Register the caller's locator and return every domain's state.

        The agent's only upward call, and deliberately three things at once:
        it tells the server where this node is, it gets the node everything
        it needs to converge, and -- because a new locator changes what
        *other* nodes must encapsulate to -- it triggers a re-broadcast.
        """
        host = kwargs.get('host')
        locator = kwargs.get('locator')
        version = kwargs.get('payload_version', 1)
        if version != constants.PAYLOAD_VERSION:
            LOG.warning("SRv6 agent on %(host)s speaks payload version "
                        "%(got)s, server speaks %(want)s; refusing sync",
                        {'host': host, 'got': version,
                         'want': constants.PAYLOAD_VERSION})
            return []
        LOG.info("SRv6 sync_state from %(host)s (locator %(loc)s)",
                 {'host': host, 'loc': locator})
        return self.plugin.handle_sync_state(rpc_context, host, locator)
