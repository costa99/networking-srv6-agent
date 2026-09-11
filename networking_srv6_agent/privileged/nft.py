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

"""The one privileged operation the SRv6 edge filter needs: load a ruleset.

A module of its own because seg6.ip_route_cmd runs only `ip`. The dataplane
renders the ruleset (MIGRATION-PLAN.md 8.7); this only hands it to nft.

On stdin, never on the command line: `nft -f -` parses the whole script as
one transaction, so the `add table` / `delete table` / `table {...}` idiom
replaces the table atomically -- the kernel never sees a half-applied filter
-- and nothing in it passes through a shell.
"""

from oslo_concurrency import processutils

from networking_srv6_agent import privileged


@privileged.default.entrypoint
def apply_ruleset(text):
    """Load `text` with `nft -f -`. Raises on any failure, missing nft too.

    The caller decides what a failure means; the edge filter treats it as
    "not active" and keeps End-SID rules disabled.
    """
    processutils.execute('nft', '-f', '-', process_input=text,
                         check_exit_code=True)
