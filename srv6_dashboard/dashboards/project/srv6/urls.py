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

from django.urls import re_path

from srv6_dashboard.dashboards.project.srv6 import views

DOMAIN = r'^(?P<domain_id>[^/]+)/%s$'

urlpatterns = [
    re_path(r'^$', views.IndexView.as_view(), name='index'),
    re_path(r'^create/$', views.CreateView.as_view(), name='create'),
    re_path(DOMAIN % 'detail/', views.DetailView.as_view(), name='detail'),
    re_path(DOMAIN % 'update/', views.UpdateView.as_view(), name='update'),
    re_path(DOMAIN % 'associate/', views.AssociateNetworkView.as_view(),
            name='associate'),
]
