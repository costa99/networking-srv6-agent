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
        net = ipaddress.IPv6Network(locator, strict=True) #checks that the host bits are zero for a valid locator
    except ValueError as e:
        raise InvalidLocator(_("%s is not a valid IPv6 prefix: %s")
                             % (locator, e))
    if net.prefixlen + constants.FUNCTION_BITS > 128: #checks that the function field fits in the remaining bits of the address
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


def encap_overhead(segment_count=1, reduced=False):
    """Bytes seg6 encapsulation adds for a pushed list of this length.

    `segment_count` counts what is PUSHED: full SIDs, or in usid mode C-SID
    containers. H.Encaps writes the first SID twice, in the outer DA and in
    the SRH; H.Encaps.Red (`reduced`) leaves it out of the SRH, and a list
    of one then pushes no SRH at all.
    """
    if not reduced:
        return (constants.SRH_FIXED_OVERHEAD +
                constants.SRH_PER_SEGMENT * segment_count)
    if segment_count <= 1:
        return constants.IPV6_HEADER
    return (constants.IPV6_HEADER + constants.SRH_HEADER +
            constants.SRH_PER_SEGMENT * (segment_count - 1))


# ----------------------------------------------------------------------
# microSID (RFC 9800 NEXT-C-SID, F3216)
# ----------------------------------------------------------------------
def _usid_network(locator):
    net, prefixlen = parse_locator(locator)
    if prefixlen != constants.USID_LOCATOR_PREFIXLEN:
        raise InvalidLocator(
            _("locator %(loc)s is a /%(len)d; microSID (F3216) needs a "
              "/%(want)d: a %(b)d-bit block plus a %(c)d-bit node C-SID")
            % {'loc': locator, 'len': prefixlen,
               'want': constants.USID_LOCATOR_PREFIXLEN,
               'b': constants.USID_BLOCK_BITS, 'c': constants.USID_CSID_BITS})
    return net


def usid_node_id(locator):
    """The node C-SID of a /48 locator: fc00:0:3::/48 -> 3."""
    net = _usid_network(locator)
    return (int(net.network_address) >>
            (128 - constants.USID_LOCATOR_PREFIXLEN)) & \
        ((1 << constants.USID_CSID_BITS) - 1)


def usid_block(locator):
    """The Locator-Block a /48 locator sits in: fc00:0:3::/48 -> fc00::/32."""
    net = _usid_network(locator)
    return str(net.supernet(new_prefix=constants.USID_BLOCK_BITS))


def check_usid_locator(locator):
    """Raise InvalidLocator unless the locator is usable for microSID.

    A /48, and a node C-SID inside the Global ID Block: 0 would terminate
    the container it appeared in, and a LIB value would collide with the
    domain functions every node holds under its own uN.
    """
    node = usid_node_id(locator)
    if not constants.USID_GIB_MIN <= node <= constants.USID_GIB_MAX:
        raise InvalidLocator(
            _("locator %(loc)s has node C-SID %(node)#06x, outside the "
              "Global ID Block %(lo)#06x-%(hi)#06x")
            % {'loc': locator, 'node': node, 'lo': constants.USID_GIB_MIN,
               'hi': constants.USID_GIB_MAX})


def node_sid(locator):
    """A node's uN SID: its /48 locator's own address, fc00:0:3::.

    The SID a TE path names a waypoint by in usid mode. The uN route that
    answers it is the whole /48, because the DA it must match carries the
    rest of the path in its low bits.
    """
    return str(_usid_network(locator).network_address)


def lib_prefix(locator):
    """The node's Local ID Block, fc00:0:3:e000::/52."""
    net = _usid_network(locator)
    shift = 128 - constants.USID_LIB_PREFIXLEN
    lib = int(net.network_address) | \
        ((constants.USID_LIB_MIN >> (constants.USID_CSID_BITS - 4)) << shift)
    return '%s/%d' % (ipaddress.IPv6Address(lib), constants.USID_LIB_PREFIXLEN)


def is_lib_function_id(function_id):
    return constants.USID_LIB_MIN <= function_id <= constants.USID_LIB_MAX


def _csids_of(address, block_bits, csid_bits):
    """The C-SIDs a SID contributes: its non-zero fields after the block.

    fc00:0:1:: -> [0x1] (a uN), fc00:0:3:e005:: -> [0x3, 0xe005] (a uDT).
    Trailing zero fields are padding; a zero BEFORE a non-zero field would
    end the container early (0 is the end-of-container marker), and bits
    below the last whole field cannot be carried at all -- both are
    refused rather than silently changed.
    """
    width = 128 - block_bits
    per = width // csid_bits
    tail_bits = width - per * csid_bits
    value = int(address) & ((1 << width) - 1)
    if value & ((1 << tail_bits) - 1):
        raise InvalidLocator(_("%s has bits below the last C-SID field")
                             % address)
    fields = [(value >> (width - csid_bits * (i + 1))) & ((1 << csid_bits) - 1)
              for i in range(per)]
    while fields and fields[-1] == 0:
        fields.pop()
    if not fields:
        raise InvalidLocator(_("%s carries no C-SID after its block")
                             % address)
    if 0 in fields:
        raise InvalidLocator(_("%s has an empty C-SID before its last one")
                             % address)
    return fields


def pack_next_csid(sids, block_bits=constants.USID_BLOCK_BITS,
                   csid_bits=constants.USID_CSID_BITS):
    """Compress a segment list into NEXT-C-SID containers (RFC 9800 s6.2).

    :param sids: SIDs in traversal order, e.g. the uN SIDs of the via hosts
                 followed by the destination's decap SID:
                 ['fc00:0:1::', 'fc00:0:3:e005::']
    :returns: the containers to push, in traversal order:
              ['fc00:0:1:3:e005::']
    :raises InvalidLocator: when the SIDs do not share one block, or one of
                            them is not a C-SID sequence

    Each container is the block followed by up to (128 - block) / csid
    C-SIDs -- six for F3216 -- zero-padded (s6.3: "the remaining least
    significant bits ... MUST be set to 0").

    A SID's C-SIDs are never split across two containers (USID-PLAN.md D9):
    when the decap SID's node + function pair does not fit in what is left,
    the container is closed early. Split, the function would open the next
    container alone as block:function::, which only a pure-LIB route on
    every node could answer. Kept whole, the decap route stays the one
    block:node:function::/64 entry. It costs nothing up to four waypoints.
    """
    block_mask = ((1 << block_bits) - 1) << (128 - block_bits)
    per = (128 - block_bits) // csid_bits
    block = None
    groups = []
    for text in sids:
        try:
            address = ipaddress.IPv6Address(text)
        except ValueError as e:
            raise InvalidLocator(_("%s is not a SID: %s") % (text, e))
        this_block = int(address) & block_mask
        if block is None:
            block = this_block
        elif this_block != block:
            raise InvalidLocator(
                _("%(sid)s is not in the same %(bits)d-bit block as %(first)s"
                  "; SIDs from different blocks cannot share a container")
                % {'sid': text, 'bits': block_bits, 'first': sids[0]})
        groups.append(_csids_of(address, block_bits, csid_bits))

    containers = []
    current = []
    for csids in groups:
        if len(current) + len(csids) > per:
            containers.append(current)
            current = []
        current.extend(csids)
    if current:
        containers.append(current)

    result = []
    for csids in containers:
        value = block
        for i, csid in enumerate(csids):
            value |= csid << (128 - block_bits - csid_bits * (i + 1))
        result.append(str(ipaddress.IPv6Address(value)))
    return result


def is_topology_function_id(function_id):
    """True for ids reserved for per-node SIDs (End, End.X)."""
    return (constants.MIN_TOPOLOGY_FUNCTION_ID <= function_id <=
            constants.MAX_TOPOLOGY_FUNCTION_ID)
