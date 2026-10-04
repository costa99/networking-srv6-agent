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

"""Horizon's network topology URLs, with the page and its JSON swapped.

Built from Horizon's own list, so the other URLs (the balloons' iframes,
create network/router) stay Horizon's, under the same names.
"""

from django.urls import re_path
from openstack_dashboard.dashboards.project.network_topology import (
    urls as core_urls)

from srv6_dashboard.dashboards.project.network_topology import views

_OURS = {
    'index': views.NetworkTopologyView.as_view(),
    'json': views.JSONView.as_view(),
}

urlpatterns = [
    re_path(p.pattern.regex.pattern, _OURS[p.name], name=p.name)
    if p.name in _OURS else p
    for p in core_urls.urlpatterns
]
