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

"""The plugin's only view: an extensible-header section.

Horizon renders every registered ``ADD_HEADER_SECTIONS`` view into the top
navigation bar (see ``openstack_dashboard.views.ExtensibleHeaderView``). All
this one does is emit a marker element when the logged-in user holds an admin
role; the bundled JavaScript stays inert until that element exists, which is
how Astrolabe is limited to admins.

It makes no API calls, touches no database, and registers no URLs.
"""

from django.views import generic


class AstrolabeHeader(generic.TemplateView):
    template_name = 'astrolabe/_header.html'
