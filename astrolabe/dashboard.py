# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A self-contained dashboard, so the plugin doesn't touch existing ones."""

from django.utils.translation import gettext_lazy as _
from openstack_auth import utils

import horizon


class Astrolabe(horizon.Dashboard):
    name = _("Astrolabe")
    slug = "astrolabe"
    panels = ("commands",)
    default_panel = "commands"
    # Admin-only, using the same permissions the in-tree Admin dashboard uses.
    # These derive from the deployment's admin roles, the same source as the
    # ``is_superuser`` check the middleware makes before recording anything.
    permissions = (tuple(utils.get_admin_permissions()),)


horizon.register(Astrolabe)
