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

"""The panel: read what the middleware recorded, render it, hand it back.

These views make no API calls and reach nothing but the operator's own
session. Access is already gated twice over — the dashboard carries the admin
permissions, and the middleware only ever writes to an admin's session — so
there is nothing here for a non-admin to read even if they reached it.
"""

import datetime

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from horizon import views

from astrolabe import store
from astrolabe import translate

#: Shown in the panel footer and in the downloaded script's preamble.
ENVIRONMENT = (
    ("$OS_TOKEN", "openstack token issue -f value -c id"),
    ("$OS_COMPUTE_API", "https://your-cloud/compute/v2.1"),
    ("$OS_VOLUME_API", "https://your-cloud/volume/v3/$OS_PROJECT_ID"),
    ("$OS_NETWORK_API", "https://your-cloud:9696/v2.0"),
)


def _decorate(entry):
    """Add the display-only fields the template wants.

    The session holds structured REST calls rather than rendered ``curl``
    text, so the rendering happens here, once per view.
    """
    shown = dict(entry)
    shown["when"] = datetime.datetime.fromtimestamp(entry.get("at") or 0)
    shown["curls"] = [translate.curl_for(call)
                      for call in entry.get("calls") or []]
    return shown


class IndexView(views.HorizonTemplateView):
    template_name = "astrolabe/index.html"
    page_title = _("Commands")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entries = store.load(self.request.session)
        context["entries"] = [_decorate(entry) for entry in entries]
        context["environment"] = ENVIRONMENT
        context["limit"] = store.max_entries()
        return context


def script(request):
    """The whole log as a downloadable shell script."""
    entries = store.load(request.session)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(translate.script_for(entries),
                            content_type="text/x-shellscript; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="astrolabe-%s.sh"' % stamp)
    return response


@require_POST
def clear(request):
    store.clear(request.session)
    return redirect(reverse("horizon:astrolabe:commands:index"))
