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

"""The topology page and the JSON it draws (FEATURE §5.2, D8, D22).

The page is a shell; the browser fetches data/ and draws it. Both URLs
are under the Admin dashboard, so both are admin-only.
"""

import logging

from django.http import JsonResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django import views as django_views
from horizon import views

from srv6_dashboard.api import srv6 as srv6_api
from srv6_dashboard.dashboards.admin.srv6_topology import topology

LOG = logging.getLogger(__name__)


class IndexView(views.HorizonTemplateView):
    template_name = 'admin/srv6_topology/index.html'
    page_title = _("SRv6 Topology")


class JSONView(django_views.View):
    """GET data/: four API reads, one JSON answer.

    locator_list, domain_list, port_list (only when some domain has
    networks: the VM ports, for where each domain runs and where a port
    path ends) and te_path_list.
    """

    def get(self, request, *args, **kwargs):
        try:
            locators = srv6_api.locator_list(request)
            domains = srv6_api.domain_list(request)
            networks = [nid for d in domains for nid in d.networks]
            ports = (srv6_api.port_list(request, network_id=networks)
                     if networks else [])
            paths = srv6_api.te_path_list(request)
        except Exception as e:
            # Not exceptions.handle(): it redirects or re-raises, which
            # means nothing to a script. The page shows this message
            # instead (an admin's page: Neutron's text is safe to show).
            LOG.warning("SRv6 topology data failed: %s", e)
            return JsonResponse({'error': str(e)}, status=502)
        return JsonResponse(topology.build(
            locators, domains, ports, paths,
            lambda pid: reverse('horizon:admin:srv6:te_path_detail',
                                args=[pid])))
