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
navigation bar (see ``openstack_dashboard.views.ExtensibleHeaderView``). This
one emits two things, both only when the logged-in user holds an admin role: a
marker element that activates the bundled JavaScript, and the rule set from
:mod:`astrolabe.rules` as embedded JSON.

It makes no API calls, touches no database, and registers no URLs.
"""

import logging

from django.views import generic

from astrolabe import rules

LOG = logging.getLogger(__name__)

_validated = False


def _validate_once():
    """Warn if a Horizon upgrade has moved a field out from under a rule.

    Deferred to first render rather than import time: the Horizon dashboard
    modules a rule targets are not reliably importable while Django is still
    assembling the app registry. Failures here are logged, never raised — a
    header section that blows up costs the operator their navigation bar.
    """
    global _validated
    if _validated:
        return
    _validated = True
    try:
        for problem in rules.validate():
            LOG.warning("astrolabe: %s", problem)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break UI
        LOG.warning("astrolabe: could not validate rules (%s)", exc)


class AstrolabeHeader(generic.TemplateView):
    template_name = 'astrolabe/_header.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        _validate_once()
        context['rules'] = rules.as_dict()
        return context
