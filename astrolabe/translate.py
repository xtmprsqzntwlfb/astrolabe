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

import functools
import json
import re

from urllib.parse import quote

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


def _put(body, path, value):
    """Set a body key, building nested dicts for a dotted path.

    Neutron nests a router's gateway under ``external_gateway_info``, so a
    rule spells that key "external_gateway_info.network_id" rather than
    needing a field kind of its own. Undotted keys, which is nearly all of
    them, take the same path and land at the top level.
    """
    keys = path.split(".")
    for key in keys[:-1]:
        body = body.setdefault(key, {})
    body[keys[-1]] = value


def _endpoint(spec, captured, fields=None, item=None, creates=None):
    """The REST path, with everything the rule can substitute filled in.

    An edit rule's endpoint carries the resource id -- "/networks/{id}" --
    because the id says which resource is being changed rather than what it
    should become, so it belongs in the path and not in the body. A follow-up
    step can also name submitted fields, and ``{new}`` for the resource the
    first call creates. Create rules substitute nothing and their endpoints
    pass through untouched.

    Values are percent-encoded. They come out of a form and end up inside a
    double-quoted URL in a curl command the operator may well run, so a space
    or a quote in one would otherwise break the command apart. ``{new}`` is
    the exception: it is a placeholder standing in for an id Astrolabe never
    sees, and encoding the dollar sign would stop it reading as one.
    """
    url = spec["endpoint"]
    if creates:
        url = url.replace("{new}", creates)
    values = dict(captured)
    for name, value in (fields or {}).items():
        if isinstance(value, str):
            values[name] = value
    if item is not None:
        values["item"] = item
    for name, value in values.items():
        url = url.replace("{%s}" % name, quote(str(value), safe=""))
    return url


def _apply_value(field, raw, parts, body):
    """An ``opt``: a value behind a flag, with the rule's omissions applied.

    Lifted out of _apply_form, which Horizon's flake8 complexity limit will
    not hold otherwise. The two branchiest kinds live on their own.

    Absent and empty are told apart here, and nowhere else: a field the form
    never carried is skipped, while one the operator emptied is a clear if
    the rule says so. _blank collapses the two, so this does not use it.
    """
    raw = _scalar(raw)
    if raw is None:
        return
    if raw == "":
        # Only a rule that opted in acts on this: the same empty string is a
        # deletion on an edit form and nothing at all on a create. shq
        # renders it as '', which is what the CLI wants. See rules.opt().
        if field["clearable"]:
            parts += [field["flag"], shq(raw)]
            _put(body, field["api"], "")
        return
    absent = field["absentWhen"]
    if absent is not None and str(raw) == str(absent):
        return
    omit = field["omitWhen"]
    if omit is None or str(raw) != str(omit):
        parts += [field["flag"], shq(raw)]
    value = _cast(raw, field["cast"])
    if value is not None:
        _put(body, field["api"], value)


def _apply_boolean(field, raw, parts, body):
    """A checkbox: whichever side of it the rule names."""
    on = _checked(raw)
    if on and field["on"]:
        parts.append(field["on"])
    elif not on and field["off"]:
        parts.append(field["off"])
    _put(body, field["api"], on)


def _render(spec, fields, captured, item=None):
    """One command line and one REST body, from one spec.

    A spec is a rule or one of its follow-up steps: the same shape, read the
    same way. Returns the command as a list of parts, the body, and whatever
    the command names, which the rule uses for an entry's title.

    Positionals are collected and appended last regardless of where they
    appear in the spec, because that is where the openstack CLI wants them.
    Among themselves they keep the order they were written in, which is the
    only thing a two-positional command like ``aggregate add host`` has to
    go on.
    """
    parts = list(spec["command"])
    trailing = []
    body = {}
    subject = ""

    for field in spec["fields"]:
        raw = fields.get(field["field"])
        kind = field["kind"]

        if kind == "value":
            _apply_value(field, raw, parts, body)

        elif kind == "flag":
            _apply_boolean(field, raw, parts, body)

        elif kind == "choice":
            # A select where each option is its own flag, and anything not
            # listed means "leave it to the server": Horizon's router form
            # offers centralized/distributed/server_default and sends the
            # "distributed" key only for the first two. Absence from the map
            # is the sentinel, so a rule does not have to name it.
            picked = field["choices"].get(str(_scalar(raw)))
            if picked is not None:
                if picked["flag"]:
                    parts.append(picked["flag"])
                _put(body, field["api"], picked["value"])

        elif kind == "multi":
            values = [str(one) for one in _as_list(raw) if not _blank(one)]
            for one in values:
                parts += [field["flag"], shq(one)]
            if values:
                _put(body, field["api"], values)

        elif kind == "redacted":
            # read_fields dropped the value before it reached us, so there is
            # nothing to render and nothing to put in the body. The flag makes
            # the command ask for it instead. See rules.redacted().
            parts.append(field["flag"])

        elif kind == "positional":
            raw = _scalar(raw)
            if _blank(raw):
                continue
            trailing.append(shq(raw))
            # api=None means this positional names some other resource
            # rather than describing this one. See rules.arg().
            if field["api"]:
                _put(body, field["api"], str(raw))
            subject = str(raw)

        elif kind == "target":
            # Not from the submission: see rules.target(). It trails the
            # command and reaches the REST call through _endpoint, never
            # through the body.
            found = captured.get(field["field"])
            if _blank(found):
                continue
            trailing.append(shq(found))
            subject = str(found)

        elif kind == "item":
            # Whichever value of the step's ``per`` list this pass is on.
            if _blank(item):
                continue
            trailing.append(shq(item))
            if field["api"]:
                _put(body, field["api"], str(item))

    return parts + trailing, body, subject


def _call(spec, body, captured, fields, item=None, creates=None):
    """One REST call. A spec with no envelope sends no body at all."""
    envelope = spec.get("envelope")
    return {
        "method": spec["method"],
        "url": _endpoint(spec, captured, fields, item, creates),
        "body": {envelope: body} if envelope else None,
    }


def _follow_up(step, fields, captured, creates):
    """Every command a follow-up step contributes, which may be none.

    ``when`` is for the calls Horizon itself only makes sometimes: a user is
    given a role only if a project and a role were both chosen. ``per`` is
    for the ones it makes repeatedly, once per selected value.
    """
    for name in step.get("when") or []:
        if _blank(fields.get(name)):
            return []
    per = step.get("per")
    items = _as_list(fields.get(per)) if per else [None]
    produced = []
    for one in items:
        if per and _blank(one):
            continue
        parts, body, _subject = _render(step, fields, captured, item=one)
        produced.append((" ".join(parts),
                         _call(step, body, captured, fields, one, creates)))
    return produced


def _apply_form(spec, fields, captured=None):
    """Apply one form rule to a submission.

    ``captured`` holds the named groups from the rule's URL pattern, which is
    where an edit form's resource id comes from: the form submits what the
    resource should become, and the path says which one.

    One saved form is one entry, however many calls it took. Saving the
    aggregate workflow with three hosts selected is four commands under one
    heading, because that is one thing the operator did.
    """
    captured = captured or {}
    creates = spec.get("creates")
    parts, body, subject = _render(spec, fields, captured)
    commands = [" ".join(parts)]
    calls = [_call(spec, body, captured, fields, creates=creates)]
    for step in spec.get("then") or []:
        for command, call in _follow_up(step, fields, captured, creates):
            commands.append(command)
            calls.append(call)
    return {
        "title": "%s %s" % (spec["title"], subject) if subject
                 else spec["title"],
        "commands": commands,
        "calls": calls,
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
        # One command, however many ids: the CLI takes them all at once,
        # while the API wants a request each.
        "commands": ["openstack %s delete %s" % (
            noun, " ".join(shq(one) for one in ids))],
        "calls": [
            {"method": "DELETE", "url": "%s/%s" % (table["path"], one),
             "body": None}
            for one in ids
        ],
    }


@functools.lru_cache(maxsize=None)
def _pattern(expression):
    """Compile a rule's URL pattern, once per distinct expression."""
    return re.compile(expression)


def translate(url, fields):
    """Translate a submission, or return None if no rule claims it."""
    for form in rules.FORMS:
        match = _pattern(form["url"]).search(url)
        if match:
            return _apply_form(form, fields, match.groupdict())
    action = _scalar(fields.get("action"))
    if isinstance(action, str) and _DELETE_ACTION.match(action):
        return _apply_delete(fields)
    return None


# ------------------------------------------------------------------ output


def commands_of(entry):
    """The commands a recorded entry renders.

    Entries used to hold a single ``cli`` string, before a saved form could
    stand for more than one command. Sessions outlive a restart on the cache
    backend, so an upgrade finds entries in that older shape and must render
    them rather than drop them.
    """
    found = entry.get("commands")
    if found:
        return list(found)
    one = entry.get("cli")
    return [one] if one else []


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
        # An action that took several calls writes several lines, in the
        # order the dashboard made them: the aggregate exists before a host
        # is added to it.
        lines.extend(commands_of(entry))
    lines.append("")
    return "\n".join(lines)
