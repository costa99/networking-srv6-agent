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

"""SID arithmetic.

Deliberately pure: no config, no DB, no logging. Both the server plugin and
the agent derive addresses through this module, so a disagreement between
them is impossible by construction rather than by convention.

A SID is  <locator><function><argument>.  With a /48 locator the function
field occupies the next 16 bits:

    locator          fc00:0:1::/48
    function id      7
    SID              fc00:0:1:7::

Note the function field is *hexadecimal* in the address and decimal in the
VRF table number. Function id 17 is SID ``fc00:0:1:11::`` and table 10017 --
these are the same domain. Reading ``fc00:0:1:11::`` as "domain 11" is a
mistake the plan warns about, and is why format_sid is the only place the
conversion happens.
"""

import ipaddress

from networking_srv6_agent._i18n import _
from networking_srv6_agent.common import constants


class InvalidLocator(Exception):
    pass


def parse_locator(locator):
    """Split a locator string into (network, prefixlen).

    :param locator: e.g. 'fc00:0:1::/48'
    :returns: (ipaddress.IPv6Network, int)
    :raises InvalidLocator: on anything that is not a usable IPv6 locator
    """
    try:
        net = ipaddress.IPv6Network(locator, strict=True)
    except ValueError as e:
        raise InvalidLocator(_("%s is not a valid IPv6 prefix: %s")
                             % (locator, e))
    if net.prefixlen + constants.FUNCTION_BITS > 128:
        raise InvalidLocator(
            _("locator %s is too long: a %d-bit function field does not "
              "fit")
            % (locator, constants.FUNCTION_BITS))
    return net, net.prefixlen


def sid_for(locator, function_id):
    """Build the SID for a function id inside a locator.

    :param locator: locator string, e.g. 'fc00:0:1::/48'
    :param function_id: int
    :returns: ipaddress.IPv6Address
    """
    net, prefixlen = parse_locator(locator)
    shift = 128 - prefixlen - constants.FUNCTION_BITS
    if function_id < 0 or function_id >= (1 << constants.FUNCTION_BITS):
        raise InvalidLocator(_("function id %s does not fit in %d bits")
                             % (function_id, constants.FUNCTION_BITS))
    return ipaddress.IPv6Address(int(net.network_address) |
                                 (function_id << shift))


def format_sid(locator, function_id):
    """The SID as the string the dataplane wants, e.g. 'fc00:0:1:7::'."""
    return str(sid_for(locator, function_id))


def vrf_table(function_id, base):
    """The routing table number for a domain. Derived, never allocated."""
    return base + function_id


def vrf_name(function_id):
    """The VRF device name, bounded by IFNAMSIZ."""
    return '%s%d' % (constants.VRF_PREFIX, function_id)


def gateway_port_name(function_id, local_vlan):
    """The OVS internal port that owns a network's gateway IP in the VRF."""
    return '%s%d-%s' % (constants.GW_PORT_PREFIX, function_id, local_vlan)


def encap_overhead(segment_count=1):
    """Bytes seg6 encapsulation adds for a segment list of this depth."""
    return (constants.SRH_FIXED_OVERHEAD +
            constants.SRH_PER_SEGMENT * segment_count)


def is_topology_function_id(function_id):
    """True for ids reserved for per-node SIDs (End, End.X)."""
    return (constants.MIN_TOPOLOGY_FUNCTION_ID <= function_id <=
            constants.MAX_TOPOLOGY_FUNCTION_ID)
