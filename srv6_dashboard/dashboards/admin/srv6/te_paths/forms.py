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

"""Create a TE path, and change its waypoints (FEATURE §5.2, D6, D7, D18).

Hosts are never typed: every host field offers the hosts that have a
registered locator (GET /v2.0/srv6_locators), the same set the plugin
resolves names against. Waypoints are MAX_TE_VIA_HOSTS ordered choice
fields -- the server's own limit -- because a waypoint list is ordered and
a multi-select is not.

The source host is optional: empty means every ingress, as the API's None
(FEATURE-TE-SOURCE-HOST).

Each clean() rule names the server check it mirrors. The server stays the
authority: whatever passes here is still validated there, and its refusal
is shown as is.
"""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from horizon import exceptions
from horizon import forms
from horizon import messages

from networking_srv6_agent.common import constants
from srv6_dashboard.api import srv6 as srv6_api

INDEX_URL = 'horizon:admin:srv6:index'
TE_TAB = '?tab=srv6__te_paths'


def _via_field(n):
    return 'via_host_%d' % n


class ViaHostsMixin:
    """MAX_TE_VIA_HOSTS ordered waypoint fields, and their rules."""

    def _add_via_fields(self, hosts, initial=()):
        choices = [('', _("-"))] + [(h, h) for h in hosts]
        for n in range(1, constants.MAX_TE_VIA_HOSTS + 1):
            self.fields[_via_field(n)] = forms.ThemableChoiceField(
                label=_("Waypoint %d") % n, required=False, choices=choices,
                initial=initial[n - 1] if n <= len(initial) else '')

    def _clean_via_hosts(self, cleaned):
        values = [cleaned.get(_via_field(n)) or ''
                  for n in range(1, constants.MAX_TE_VIA_HOSTS + 1)]
        # No gaps: "waypoint 1 empty, waypoint 2 set" has no order to
        # mean. The server would just see a shorter list.
        last = max((i for i, v in enumerate(values) if v), default=-1)
        if '' in values[:last + 1]:
            raise forms.ValidationError(
                _("Fill the waypoints in order, without gaps."))
        via = values[:last + 1]
        # Mirrors the API's list_of_unique_strings validator on via_hosts
        # (api/definitions/srv6_te.py): a host twice is a 400 there.
        if len(set(via)) != len(via):
            raise forms.ValidationError(
                _("A host can be a waypoint only once."))
        return via


class CreateTePathForm(ViaHostsMixin, forms.SelfHandlingForm):
    domain_id = forms.ThemableChoiceField(label=_("Domain"))
    destination_type = forms.ThemableChoiceField(
        label=_("Destination"),
        choices=[('host', _("A host: every port of the domain on it")),
                 ('port', _("One port (one VM)"))],
        widget=forms.ThemableSelectWidget(attrs={
            'class': 'switchable', 'data-slug': 'desttype'}))
    destination_host = forms.ThemableChoiceField(
        label=_("Destination host"), required=False,
        widget=forms.ThemableSelectWidget(attrs={
            'class': 'switched', 'data-switch-on': 'desttype',
            'data-desttype-host': _("Destination host"),
            'data-required-when-shown': 'true'}))
    destination_port_id = forms.ThemableChoiceField(
        label=_("Destination port"), required=False,
        widget=forms.ThemableSelectWidget(attrs={
            'class': 'switched', 'data-switch-on': 'desttype',
            'data-desttype-port': _("Destination port"),
            'data-required-when-shown': 'true'}))
    source_host = forms.ThemableChoiceField(
        label=_("Source host"), required=False,
        help_text=_("Steer only the traffic that enters the domain at this "
                    "host. Such a path outranks every path without one."))

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        try:
            domains = srv6_api.domain_list(request)
            hosts = sorted(loc.host for loc in srv6_api.locator_list(request))
            ports = self._domain_ports(request, domains)
        except Exception:
            exceptions.handle(request, _('Unable to retrieve the domains, '
                                         'hosts or ports.'),
                              redirect=reverse(INDEX_URL) + TE_TAB)
        self.fields['domain_id'].choices = [('', _("Select a domain"))] + [
            (d.id, d.name_or_id) for d in
            sorted(domains, key=lambda d: d.name_or_id)]
        self.fields['destination_host'].choices = (
            [('', _("Select a host"))] + [(h, h) for h in hosts])
        self.fields['source_host'].choices = (
            [('', _("Any node (every ingress)"))] + [(h, h) for h in hosts])
        # port id -> network id, for clean(): see _domain_ports.
        self._port_network = {p.id: p.network_id for p, __ in ports}
        self._domain_networks = {d.id: set(d.networks) for d in domains}
        self.fields['destination_port_id'].choices = (
            [('', _("Select a port"))] + [(p.id, label) for p, label in ports])
        self._add_via_fields(hosts)

    @staticmethod
    def _domain_ports(request, domains):
        """VM ports on the networks of any domain, with readable labels.

        One request for all of them (network_id repeated). Only compute
        ports: steering toward a DHCP or router port is accepted by the
        server but means nothing. Each label names the domain, so the
        admin sees which domain a port can be steered in.
        """
        net_domain = {nid: d for d in domains for nid in d.networks}
        if not net_domain:
            return []
        ports = srv6_api.port_list(request, network_id=list(net_domain))
        out = []
        for p in ports:
            domain = net_domain.get(p.network_id)
            if (domain is None or
                    not (p.device_owner or '').startswith('compute:')):
                continue
            ips = ', '.join(ip['ip_address'] for ip in p.fixed_ips or [])
            label = '%s (%s) - domain %s' % (p.name_or_id, ips or '-',
                                             domain.name_or_id)
            out.append((p, label))
        return sorted(out, key=lambda c: c[1])

    def clean(self):
        cleaned = super().clean()
        dtype = cleaned.get('destination_type')
        # Exactly one destination (services/plugin.py
        # create_srv6_te_path: "exactly one of destination_host and
        # destination_port_id must be set"). The switch shows one field;
        # the other is dropped here whatever the browser sent.
        if dtype == 'host':
            cleaned['destination_port_id'] = None
            if not cleaned.get('destination_host'):
                self.add_error('destination_host',
                               _("Select the destination host."))
        elif dtype == 'port':
            cleaned['destination_host'] = None
            port_id = cleaned.get('destination_port_id')
            if not port_id:
                self.add_error('destination_port_id',
                               _("Select the destination port."))
            elif (self._port_network.get(port_id) not in
                    self._domain_networks.get(cleaned.get('domain_id'),
                                              ())):
                # Mirrors _validate_destination_port: the port must be on
                # a network attached to the domain.
                self.add_error('destination_port_id',
                               _("This port is not on a network of the "
                                 "selected domain."))
        # Mirrors _validate_source_host: a node never encapsulates the
        # traffic toward its own VMs, so a path from the destination host
        # would steer nothing. (A port bound on the source is the server's
        # check: the form does not read bindings.)
        source = cleaned.get('source_host')
        if (dtype == 'host' and source and
                source == cleaned.get('destination_host')):
            self.add_error('source_host',
                           _("The source cannot be the destination host."))
        cleaned['via_hosts'] = self._clean_via_hosts(cleaned)
        return cleaned

    def handle(self, request, data):
        attrs = {'domain_id': data['domain_id']}
        # As the CLI does: a source only when one is chosen.
        if data.get('source_host'):
            attrs['source_host'] = data['source_host']
        if data['destination_host']:
            attrs['destination_host'] = data['destination_host']
        else:
            attrs['destination_port_id'] = data['destination_port_id']
        # As the CLI does: waypoints only when there are some, so the two
        # clients send the same body.
        if data['via_hosts']:
            attrs['via_hosts'] = data['via_hosts']
        try:
            path = srv6_api.te_path_create(request, **attrs)
        except Exception:
            exceptions.handle(request, _('Unable to create the TE path.'),
                              redirect=reverse(INDEX_URL) + TE_TAB)
            return False
        messages.success(request, _('Created TE path %s.') % path.id)
        return path


class UpdateTePathForm(ViaHostsMixin, forms.SelfHandlingForm):
    path_id = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        try:
            hosts = sorted(loc.host for loc in srv6_api.locator_list(request))
        except Exception:
            exceptions.handle(request, _('Unable to retrieve the hosts.'),
                              redirect=reverse(INDEX_URL) + TE_TAB)
        # A current waypoint that lost its locator (why a path goes
        # DEGRADED) stays selectable, so opening the form does not
        # silently drop it.
        current = list(self.initial.get('via_hosts') or [])
        hosts += [h for h in current if h not in hosts]
        self._add_via_fields(hosts, current)

    def clean(self):
        cleaned = super().clean()
        cleaned['via_hosts'] = self._clean_via_hosts(cleaned)
        return cleaned

    def handle(self, request, data):
        try:
            # The whole list, replaced; [] makes the path direct again.
            path = srv6_api.te_path_update(request, data['path_id'],
                                           data['via_hosts'])
        except Exception:
            exceptions.handle(request, _('Unable to update the TE path.'),
                              redirect=reverse(INDEX_URL) + TE_TAB)
            return False
        messages.success(request, _('Updated TE path %s.') % path.id)
        return path
