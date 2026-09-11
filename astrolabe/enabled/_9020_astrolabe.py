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

"""Horizon plugin registration for Astrolabe.

Copy (or symlink) this file into your Horizon deployment's
``openstack_dashboard/local/enabled/`` directory, and ensure ``astrolabe`` is
importable (pip install the package).

Astrolabe adds no dashboard and no panel. It contributes exactly two things to
Horizon: one JavaScript file appended to the global bundle, and one template
rendered into the extensible header.
"""

# Add our app so Django can find its templates and static files.
ADD_INSTALLED_APPS = ["astrolabe"]

# The recorder itself. Horizon appends this to the JS bundle served on every
# page, but it does nothing until the admin-only header marker shows up.
ADD_JS_FILES = ["astrolabe/js/astrolabe.js"]

# Renders the marker (and the toggle) into the top navigation bar. The template
# gates on ``request.user.is_superuser``, which openstack_auth derives from the
# user's roles in the current scope.
ADD_HEADER_SECTIONS = ["astrolabe.views.AstrolabeHeader"]
