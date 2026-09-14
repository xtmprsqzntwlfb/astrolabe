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

"""The interpreter: a submitted form in, a CLI command and REST call out.

This module contains no knowledge of any particular Horizon panel. What to
translate, and how, comes from :mod:`astrolabe.rules`; everything here is the
generic machinery that applies those rules.

Nothing at module level imports Django or Horizon, so the whole translation
path stays importable and testable on its own.
"""

import json
import re

from astrolabe import rules

# Field names whose values are never read, let alone stored.
#
# Long, unambiguous words match anywhere in the name - that is what catches
# Django's run-together "csrfmiddlewaretoken". Short words match only on
# boundaries, so "passthrough" and "keystone_url" are not dropped by accident.
SECRET_ANYWHERE = re.compile(r"password|passwd|secret|token|credential", re.I)
SECRET_WORD = re.compile(r"(^|_)(pass|key|auth)(_|$)", re.I)

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_DELETE_ACTION = re.compile(r"^[a-z0-9_]+__delete(__|$)")

# Compiled once; the rule table does not change at runtime.
_patterns = {}


def is_secret(name):
    """Whether a field name looks like it carries a credential."""
    # Split camelCase into underscore-separated words first, so boundary
    # matching also catches names like Nova's "adminPass".
    normalized = _CAMEL.sub(r"\1_\2", str(name))
    return bool(SECRET_ANYWHERE.search(normalized) or
                SECRET_WORD.search(normalized))


def shq(value):
    """Quote a value for a POSIX shell, leaving safe words bare."""
    text = "" if value is None else str(value)
    if _SAFE_WORD.match(text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def read_fields(post):
    """Turn a submitted ``QueryDict`` into a plain dict, dropping secrets.

    Repeated fields (multi-selects, batch-action checkboxes) come back as
    lists; everything else as a string.
    """
    fields = {}
    items = post.lists() if hasattr(post, "lists") else post.items()
    for name, values in items:
        if is_secret(name):
            continue
        if not isinstance(values, (list, tuple)):
            values = [values]
        fields[name] = values[0] if len(values) == 1 else list(values)
    return fields


# ---------------------------------------------------------------- primitives


def _blank(value):
    return value is None or value == "" or value == []


def _scalar(value):
    """Collapse an accidentally repeated field to its first value."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _checked(value):
    """Django renders unchecked booleans by omitting them entirely."""
    return _scalar(value) in ("on", "true", "True")


def _cast(value, how):
    if how != "int":
        return str(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_list(value):
    if _blank(value):
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# ---------------------------------------------------------------- the rules


def _apply_form(spec, fields):
    """Apply one form rule to a submission.

    Positionals are collected and appended last regardless of where they
    appear in the rule, because that is where the openstack CLI wants them.
    """
    parts = list(spec["command"])
    trailing = []
    body = {}
    subject = ""

    for field in spec["fields"]:
        raw = fields.get(field["field"])
        kind = field["kind"]

        if kind == "value":
            raw = _scalar(raw)
            if _blank(raw):
                continue
            absent = field.get("absentWhen")
            if absent is not None and str(raw) == str(absent):
                continue
            omit = field["omitWhen"]
            if omit is None or str(raw) != str(omit):
                parts += [field["flag"], shq(raw)]
            value = _cast(raw, field["cast"])
            if value is not None:
                body[field["api"]] = value

        elif kind == "flag":
            on = _checked(raw)
            if on and field["on"]:
                parts.append(field["on"])
            elif not on and field["off"]:
                parts.append(field["off"])
            body[field["api"]] = on

        elif kind == "multi":
            values = [str(one) for one in _as_list(raw) if not _blank(one)]
            for one in values:
                parts += [field["flag"], shq(one)]
            if values:
                body[field["api"]] = values

        elif kind == "positional":
            raw = _scalar(raw)
            if _blank(raw):
                continue
            trailing.append(shq(raw))
            body[field["api"]] = str(raw)
            subject = str(raw)

    return {
        "title": "%s %s" % (spec["title"], subject) if subject
                 else spec["title"],
        "cli": " ".join(parts + trailing),
        "calls": [{
            "method": spec["method"],
            "url": spec["endpoint"],
            "body": {spec["envelope"]: body},
        }],
    }


def _apply_delete(fields):
    """Apply the table-delete rule, if the action names a known table."""
    bits = str(_scalar(fields.get("action")) or "").split("__")
    table = rules.TABLES.get(bits[0])
    if not table:
        return None
    # A row action carries the id in the third segment; a batch action puts
    # the selected ids in repeated object_ids fields.
    if len(bits) > 2:
        ids = ["__".join(bits[2:])]
    else:
        ids = [str(one) for one in _as_list(fields.get("object_ids"))]
    ids = [one for one in ids if one]
    if not ids:
        return None

    noun = table["noun"]
    return {
        "title": "Delete %s%s (%d)" % (
            noun, "s" if len(ids) > 1 else "", len(ids)),
        "cli": "openstack %s delete %s" % (
            noun, " ".join(shq(one) for one in ids)),
        "calls": [
            {"method": "DELETE", "url": "%s/%s" % (table["path"], one),
             "body": None}
            for one in ids
        ],
    }


def _pattern(form):
    compiled = _patterns.get(form["id"])
    if compiled is None or compiled.pattern != form["url"]:
        compiled = _patterns[form["id"]] = re.compile(form["url"])
    return compiled


def translate(url, fields):
    """Translate a submission, or return None if no rule claims it."""
    for form in rules.FORMS:
        if _pattern(form).search(url):
            return _apply_form(form, fields)
    action = _scalar(fields.get("action"))
    if isinstance(action, str) and _DELETE_ACTION.match(action):
        return _apply_delete(fields)
    return None


# ------------------------------------------------------------------ output


def curl_for(call):
    """Render one REST call as a copyable ``curl`` invocation."""
    parts = [
        'curl -sS -X %s "%s"' % (call["method"], call["url"]),
        '-H "X-Auth-Token: $OS_TOKEN"',
    ]
    if call.get("body"):
        parts.append('-H "Content-Type: application/json"')
        body = json.dumps(call["body"]).replace("'", "'\\''")
        parts.append("-d '%s'" % body)
    return " \\\n  ".join(parts)


SCRIPT_PREAMBLE = """\
#!/bin/sh
# Recorded by Astrolabe from the OpenStack dashboard.
#
# Review before running. Astrolabe records what you submitted; it does not
# know your service catalog, so set these first:
#
#   export OS_TOKEN=$(openstack token issue -f value -c id)
#
set -eu"""


def script_for(entries):
    """Render recorded entries as a shell script, oldest action first."""
    lines = [SCRIPT_PREAMBLE]
    for entry in reversed(entries):
        lines.append("")
        lines.append("# %s" % entry["title"])
        if not entry.get("ok", True):
            lines.append("# NOTE: this action was rejected by the dashboard.")
        lines.append(entry["cli"])
    lines.append("")
    return "\n".join(lines)
