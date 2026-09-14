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

Entries are kept JSON-serialisable on purpose. Django's default session
serialiser is JSON, and Horizon's default session backend is the cache, but
``signed_cookies`` is a supported alternative that puts the whole session in a
~4KB cookie. Hence the cap, and hence storing the REST calls in structured form
rather than as rendered ``curl`` text.
"""

SESSION_KEY = "astrolabe.log"

#: Entries kept per session, newest first. Override with ``ASTROLABE_MAX_
#: ENTRIES`` in ``local_settings.py``; lower it on a ``signed_cookies``
#: deployment.
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
