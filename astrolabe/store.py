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

"""Where recorded entries live: the operator's own Django session.

No model, no table, no migration. The log is per-session, so it disappears
when the operator logs out, and one admin never sees another's.

Entries are kept JSON-serialisable on purpose: Django's default session
serialiser is JSON. They are also kept small, and the REST calls are stored
structured rather than as rendered ``curl`` text, because how much room there
is depends entirely on a setting Astrolabe does not control.

``SESSION_ENGINE`` decides where a session lives. On the cache backend, which
is what most deployments run and what Horizon's own settings file suggests,
the cap below is a formality. On ``signed_cookies`` the whole session travels
in one cookie, and that is not an exotic choice: it is what Horizon's shipped
``local_settings.py.example`` offers for ``tox -e runserver``, so it is what a
plugin is most likely to be developed against.

There the budget is roughly 4KB, before signing and base64 inflate it, for
*everything* in the session rather than for this log. Django does not check:
it signs the cookie and sets it, and a browser silently drops a Set-Cookie
that is too big. The operator is not told their command log was truncated,
they are logged out.

So the cap is on entries, but the thing to watch is bytes. A one-call entry
runs a few hundred of them. One that took several calls grows with what was
selected -- creating a host aggregate with thirty hosts comes to about 6.5KB
on its own, with hostnames of ordinary length, which is more than the whole
cookie budget. Lowering the cap to 1 would not help. If you run
``signed_cookies``, keep the cap low and know that a single large selection
can still be too much.
"""

SESSION_KEY = "astrolabe.log"

#: Entries kept per session, newest first. Override with
#: ``ASTROLABE_MAX_ENTRIES`` in ``local_settings.py``; lower it on a
#: ``signed_cookies`` deployment, and see the module docstring for why that
#: is necessary without being sufficient.
DEFAULT_MAX_ENTRIES = 50


def max_entries():
    from django.conf import settings
    return getattr(settings, "ASTROLABE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)


def load(session):
    """Recorded entries, newest first."""
    entries = session.get(SESSION_KEY)
    return list(entries) if isinstance(entries, list) else []


def add(session, entry, limit=None):
    """Prepend an entry, discarding the oldest beyond the cap."""
    if limit is None:
        limit = max_entries()
    entries = [entry] + load(session)
    # Assignment is what marks the session dirty, so Django saves it. Never
    # mutate the stored list in place: SessionBase only notices __setitem__.
    session[SESSION_KEY] = entries[:limit]


def clear(session):
    # SessionBase.pop sets ``modified`` itself when the key was there.
    session.pop(SESSION_KEY, None)
