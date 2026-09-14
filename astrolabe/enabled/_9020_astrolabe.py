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

"""Horizon plugin registration for the Astrolabe dashboard.

Copy (or symlink) this file into your Horizon deployment's
``openstack_dashboard/local/enabled/`` directory, and make sure ``astrolabe``
is importable (pip install the package). Horizon auto-discovers ``dashboard.py``
and ``panel.py`` in the added app.

This file registers the *panel*. The recorder that fills it is a middleware,
and Horizon's plugin loader has no hook for adding middleware, so that takes a
second drop-in file, ``local/local_settings.d/_9020_astrolabe.py``::

    MIDDLEWARE = list(MIDDLEWARE) + ['astrolabe.middleware.AstrolabeMiddleware']

It has to be that directory rather than ``local_settings.py``: snippets there
are exec'd in the settings namespace, so ``MIDDLEWARE`` is in scope to extend,
whereas ``local_settings.py`` is imported as its own module and would raise
NameError. See the README for the full sequence.
"""

# The slug of the dashboard this file configures.
DASHBOARD = "astrolabe"

# Not the default landing dashboard.
DEFAULT = False

# Add our app so Horizon imports its dashboard.py / panel.py.
ADD_INSTALLED_APPS = ["astrolabe"]
