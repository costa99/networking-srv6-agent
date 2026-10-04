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

"""Horizon plugin for networking-srv6-agent.

A package of its own, beside networking_srv6_agent, so that neither side
imports the other's world: this one never imports neutron, the server never
imports Django. The only thing it takes from networking_srv6_agent is the
neutron-free part: cli.resources (the SDK classes) and common.constants.

Design reference: private-docs/features/FEATURE-HORIZON-DASHBOARD.md.
"""
