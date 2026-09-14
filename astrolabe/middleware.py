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

"""Observes admin form submissions and records their CLI equivalents.

Horizon submits its modal forms and its table row/batch actions as ordinary
POSTs, so one middleware sees every create and delete an operator performs,
with the exact values they entered. This is the same vantage point
``horizon.middleware.OperationLogMiddleware`` uses, and this middleware follows
its shape deliberately:

* the response is fetched *first*, and ``request.POST`` read afterwards.
  Reading POST before the view runs consumes the request body and breaks
  Horizon's file-upload forms;
* looking at the response also reveals whether the action was accepted, which
  is something a browser-side recorder cannot see;
* secret-looking fields are dropped before anything is read;
* a submission matching no rule is ignored and never stored.

It is deliberately cheap for everyone it does not concern: non-POST requests,
non-admins and unmatched paths all fall out within a few comparisons, before
any parsing happens.
"""

import logging
import time

from django.conf import settings
from django.contrib.messages import constants as message_levels
from django.core.exceptions import MiddlewareNotUsed

from astrolabe import rules
from astrolabe import store
from astrolabe import translate

LOG = logging.getLogger(__name__)


class AstrolabeMiddleware(object):
    """Record the CLI and REST equivalents of admin actions."""

    def __init__(self, get_response):
        if not getattr(settings, "ASTROLABE_ENABLED", True):
            raise MiddlewareNotUsed
        self.get_response = get_response
        self._validated = False

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception as exc:  # noqa: BLE001 - see below
            # Recording is an observer. It must never turn a successful
            # operator action into an error page.
            LOG.warning("astrolabe: could not record %s (%s)",
                        request.path, exc)
        return response

    # ------------------------------------------------------------ recording

    def _record(self, request, response):
        if request.method != "POST":
            return
        if not self._is_admin(request):
            return

        built = translate.translate(
            request.path, translate.read_fields(request.POST))
        if built is None:
            return

        self._validate_once()
        built["at"] = time.time()
        built["ok"] = _succeeded(request, response)
        store.add(request.session, built)

    @staticmethod
    def _is_admin(request):
        """Admin-only, evaluated server-side and failing closed.

        ``is_superuser`` on openstack_auth's user compares the operator's roles
        in the current scope against the deployment's admin roles, so this
        follows a project switch rather than a login-time snapshot.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if not getattr(user, "is_superuser", False):
            return False
        return getattr(request, "session", None) is not None

    def _validate_once(self):
        """Warn if a Horizon upgrade has moved a field out from under a rule.

        Deferred to the first matched submission rather than done at import or
        startup: the dashboard modules a rule targets are not reliably
        importable while Django is still assembling the app registry.
        """
        if self._validated:
            return
        self._validated = True
        try:
            for problem in rules.validate():
                LOG.warning("astrolabe: %s", problem)
        except Exception as exc:  # noqa: BLE001 - diagnostics, not behaviour
            LOG.warning("astrolabe: could not validate rules (%s)", exc)


def _succeeded(request, response):
    """Whether Horizon accepted the submission.

    Two signals, cheapest first. Horizon redirects on success and re-renders
    the form with errors on failure, so a non-redirect means the action did not
    happen — that covers both invalid input and an API call the service
    refused, since ``ModalFormView.form_valid`` falls through to
    ``form_invalid`` when ``form.handle`` raises.

    Table actions redirect either way, so for those the status code says
    nothing and the queued messages are the only evidence. Reading them is
    non-destructive: ``BaseStorage.update`` stores ``_queued_messages`` without
    clearing it, so the operator still sees the message regardless of where
    this middleware sits relative to Django's ``MessageMiddleware``. The
    attribute is private, hence the guard and the fallback.
    """
    try:
        queued = request._messages._queued_messages
        for message in queued:
            if message.level >= message_levels.ERROR:
                return False
    except AttributeError:
        pass
    return 300 <= response.status_code < 400
