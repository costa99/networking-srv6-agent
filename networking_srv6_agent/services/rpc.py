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

The kwargs are part of the contract, not only the method names. The
agent's handlers end in **kwargs, so a server sending `vpn=` to a
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

    # Used by the server plugin to push changes out to every SRv6 agent.
    # The plugin calls one of the three public methods below whenever the
    # database changes; each one turns into a broadcast message.

    def __init__(self, topic=topics.AGENT):
        self.topic = topic
        # Build the mailbox name the agents subscribe to. With the defaults
        # this is something like "q-agent-notifier-srv6-update".
        self.topic_srv6_update = topics.get_topic_name(
            topic, constants.TOPIC_SRV6, topics.UPDATE)
        # The RPC client is the object that actually puts messages on
        # RabbitMQ. version='1.0' is the API version both sides agree on.
        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def _fanout(self, context, method, **kwargs):
        # Shared helper: send "method(**kwargs)" to every agent at once.
        # `method` is the name of the function that will run on the agent.
        LOG.debug("SRv6 fanout %(method)s on %(topic)s: %(kwargs)s",
                  {'method': method, 'topic': self.topic_srv6_update,
                   'kwargs': kwargs})
        # prepare() picks the mailbox; fanout=True means "all agents",
        # not just one of them.
        cctxt = self.client.prepare(topic=self.topic_srv6_update, fanout=True)
        # cast() = send without waiting for a reply. Every message also
        # carries the payload version, so an agent built for a different
        # message format can recognise it and ignore the message.
        cctxt.cast(context, method,
                   payload_version=constants.PAYLOAD_VERSION, **kwargs)

    def domain_updated(self, context, domain):
        """Full desired state of one domain, to every agent.

        Sent on any change that alters which networks or subnets a domain
        covers. Agents treat it as authoritative and converge to it, so a
        lost message costs at most one resync interval rather than leaving
        permanently divergent state.
        """
        # "Here is the complete picture of this domain; make your node
        # match it." Because it is the whole picture, not a diff, sending
        # it twice is harmless.
        self._fanout(context, constants.DOMAIN_UPDATED, domain=domain)

    def routes_updated(self, context, domain_id, add=None, remove=None):
        """Incremental per-port routes.

        Separate from domain_updated because port churn is the common case,
        and rebuilding a whole domain's payload for one instance would put
        the entire route set on the wire every time a VM boots.
        """
        # "Only these routes changed": a small message sent when a VM port
        # appears or disappears. Missing lists become empty lists so the
        # agent can always loop over `add` and `remove` safely.
        self._fanout(context, constants.ROUTES_UPDATED, domain_id=domain_id,
                     add=add or [], remove=remove or [])

    def domain_deleted(self, context, domain_id, function_id, vrf_table):
        """The domain is gone; tear down its VRF and SID.

        Carries function_id and vrf_table because after the delete the agent
        cannot look them up -- the row is gone -- and it needs both to name
        the kernel objects it must remove.
        """
        # "This domain no longer exists; clean it up on your node." The
        # extra fields tell the agent exactly which kernel objects to delete.
        self._fanout(context, constants.DOMAIN_DELETED, domain_id=domain_id,
                     function_id=function_id, vrf_table=vrf_table)


class Srv6ServerRpcCallback:
    """Server-side receiver of the agent's sync_state call."""

    # The other direction: the server listens here for questions coming
    # from agents. oslo.messaging runs the method whose name matches the
    # incoming message.
    target = oslo_messaging.Target(version='1.0')

    def __init__(self, plugin):
        # Keep a reference to the service plugin, which owns the database
        # and does the real work.
        self.plugin = plugin

    def sync_state(self, rpc_context, **kwargs):
        """Register the caller's locator and return every domain's state.

        The agent's only upward call, and deliberately three things at once:
        it tells the server where this node is, it gets the node everything
        it needs to converge, and -- because a new locator changes what
        *other* nodes must encapsulate to -- it triggers a re-broadcast.
        """
        # Who is asking: the compute node's hostname and its SRv6 locator
        # (the IPv6 prefix that identifies this node on the SRv6 fabric).
        host = kwargs.get('host')
        locator = kwargs.get('locator')
        # Which message format the agent speaks. Older agents may not send
        # it, so assume version 1.
        version = kwargs.get('payload_version', 1)
        # If the agent and server disagree on the format, return nothing
        # rather than send data the agent could misread.
        if version != constants.PAYLOAD_VERSION:
            LOG.warning("SRv6 agent on %(host)s speaks payload version "
                        "%(got)s, server speaks %(want)s; refusing sync",
                        {'host': host, 'got': version,
                         'want': constants.PAYLOAD_VERSION})
            return []
        LOG.info("SRv6 sync_state from %(host)s (locator %(loc)s)",
                 {'host': host, 'loc': locator})
        # Hand off to the plugin: it saves the locator, builds the list of
        # domains, and returns it. That list is the reply the agent gets.
        # What the agent's kernel and iproute2 can do (encap-red,
        # next-csid). Older agents do not send it: None, "unknown".
        capabilities = kwargs.get('capabilities')
        return self.plugin.handle_sync_state(rpc_context, host, locator,
                                             capabilities=capabilities)
