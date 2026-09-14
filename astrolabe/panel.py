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

"""The single panel, registered on the Astrolabe dashboard."""

from django.utils.translation import gettext_lazy as _

import horizon

from astrolabe.dashboard import Astrolabe


class Commands(horizon.Panel):
    name = _("Commands")
    slug = "commands"
    # URLs for this panel live in astrolabe/panel_urls.py. It must NOT be
    # named astrolabe/urls.py: the dashboard package is "astrolabe", and
    # Horizon would then pick that module up as the *dashboard's* own default
    # URLs too, sharing (and mutating) the same urlpatterns list and creating
    # a self-referential include -> RecursionError at startup.
    urls = "astrolabe.panel_urls"


Astrolabe.register(Commands)
