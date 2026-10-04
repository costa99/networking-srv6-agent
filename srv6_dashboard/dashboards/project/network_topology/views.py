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

"""Horizon's topology page and JSON, plus the SRv6 domains (FEATURE D24).

Both views subclass Horizon's: the page adds the script that draws the
links, the JSON adds the domains to draw. Everything else is Horizon's.
"""

import json
import logging

from django import http
from django.urls import reverse
from openstack_dashboard.dashboards.project.network_topology import (
    views as core_views)

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.project.network_topology import topology

LOG = logging.getLogger(__name__)


class NetworkTopologyView(core_views.NetworkTopologyView):
    # Under the SRv6 panel's templates: see panel.py.
    template_name = 'project/srv6/network_topology.html'


class JSONView(core_views.JSONView):
    """GET json: Horizon's answer, plus srv6_domains.

    One more request, domain_list for the current project: the domains
    this project's networks are in, even for an admin, as on the SRv6
    Domains panel (D14).
    """

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        data = json.loads(response.content)
        data['srv6_domains'] = self._srv6_domains(
            request, [n['id'] for n in data['networks']])
        return http.HttpResponse(json.dumps(data, ensure_ascii=False),
                                 content_type=response['Content-Type'])

    def _srv6_domains(self, request, network_ids):
        try:
            domains = srv6_api.domain_list(
                request, project_id=request.user.project_id)
        except Exception as e:
            # Horizon's JSON view drops a failed read the same way: the
            # topology still draws, without the domain links. This view
            # is polled every 10 s, so no error message either.
            LOG.warning("SRv6 domains for the topology failed: %s", e)
            return []
        return topology.srv6_domains(
            domains, network_ids,
            lambda did: reverse('horizon:project:srv6:detail', args=[did]))
