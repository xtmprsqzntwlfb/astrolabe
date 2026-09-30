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

#: The one place the version is written. setup.cfg reads it from here with
#: ``attr:``, which setuptools resolves by parsing this file rather than
#: importing it — so this module must stay free of imports, as the rest of
#: the plugin's "needs nothing at import time" design already requires.
#:
#: Not derived from git tags: pbr would do that, but it is the one OpenStack
#: convention this repository does without, and a version that only exists in
#: a tag is a version an operator cannot read off a checkout.
__version__ = "0.1.0"
