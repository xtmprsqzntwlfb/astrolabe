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

"""What Astrolabe knows how to translate.

This is the file you edit to teach Astrolabe a new panel. Each entry maps a
Horizon form to the ``openstack`` command and REST call it corresponds to. The
browser never contains any of this knowledge: the header view serialises these
rules to JSON, the template embeds them, and a generic interpreter in
``astrolabe.js`` applies them.

Keeping the rules here rather than in JavaScript buys one thing JavaScript
cannot have — :func:`validate` imports the Horizon form each rule targets and
checks the field names still exist, so a rename in a future Horizon release
surfaces as a warning instead of a silently incomplete command.

Nothing at module level imports Django or Horizon, so this module stays
importable (and testable) on its own.
"""

from importlib import import_module

# Service endpoints are not knowable from the browser, so commands render
# against these shell variables. The drawer footer documents them.
COMPUTE = "$OS_COMPUTE_API"
VOLUME = "$OS_VOLUME_API"
NETWORK = "$OS_NETWORK_API"


# --------------------------------------------------------------- field kinds
#
# Four kinds cover every rule below. Each describes one form field: how it
# reaches the command line, and how it reaches the REST body.


def opt(field, flag, api=None, cast="str", omit_when=None):
    """A value carried by a flag: ``--ram 2048``.

    ``omit_when`` drops the flag for a given value but keeps it in the REST
    body, which is how ``--swap 0`` stays off the command line while ``swap:
    0`` remains meaningful to the API.
    """
    return {
        "kind": "value", "field": field, "flag": flag,
        "api": api or field, "cast": cast, "omitWhen": omit_when,
    }


def boolean(field, api, on=None, off=None):
    """A checkbox.

    ``on`` is the flag emitted when it is ticked, ``off`` when it is not.
    Either may be None. Django omits unticked checkboxes from the submission
    entirely, so absence means False.
    """
    return {
        "kind": "flag", "field": field, "api": api, "on": on, "off": off,
    }


def repeated(field, flag, api):
    """A multi-select, emitted as the flag once per value."""
    return {"kind": "multi", "field": field, "flag": flag, "api": api}


def arg(field, api="name"):
    """A positional argument. Always rendered last, as the CLI expects."""
    return {"kind": "positional", "field": field, "api": api}


# --------------------------------------------------------------------- rules
#
# ``form`` names the Horizon class this rule was written against, as
# "module:ClassName". It is used only by validate().

FORMS = [
    {
        "id": "flavor-create",
        "title": "Create flavor",
        "url": r"/admin/flavors/create/?$",
        "form": "openstack_dashboard.dashboards.admin.flavors.workflows"
                ":CreateFlavorInfoAction",
        "method": "POST",
        "endpoint": COMPUTE + "/flavors",
        "envelope": "flavor",
        "command": ["openstack", "flavor", "create"],
        "fields": [
            opt("flavor_id", "--id", api="id"),
            opt("vcpus", "--vcpus", cast="int"),
            opt("memory_mb", "--ram", api="ram", cast="int"),
            opt("disk_gb", "--disk", api="disk", cast="int"),
            opt("eph_gb", "--ephemeral", api="OS-FLV-EXT-DATA:ephemeral",
                cast="int", omit_when="0"),
            opt("swap_mb", "--swap", api="swap", cast="int", omit_when="0"),
            arg("name"),
        ],
    },
    {
        "id": "volume-type-create",
        "title": "Create volume type",
        "url": r"/admin/volume_types/create_type/?$",
        "form": "openstack_dashboard.dashboards.admin.volume_types.forms"
                ":CreateVolumeType",
        "method": "POST",
        "endpoint": VOLUME + "/types",
        "envelope": "volume_type",
        "command": ["openstack", "volume", "type", "create"],
        "fields": [
            opt("vol_type_description", "--description", api="description"),
            # The form ships this ticked; only the private case needs a flag.
            boolean("is_public", "os-volume-type-access:is_public",
                    off="--private"),
            arg("name"),
        ],
    },
    {
        "id": "network-create",
        "title": "Create network",
        "url": r"/admin/networks/create/?$",
        "form": "openstack_dashboard.dashboards.admin.networks.forms"
                ":CreateNetwork",
        "method": "POST",
        "endpoint": NETWORK + "/networks",
        "envelope": "network",
        "command": ["openstack", "network", "create"],
        "fields": [
            opt("tenant_id", "--project"),
            opt("network_type", "--provider-network-type",
                api="provider:network_type"),
            opt("physical_network", "--provider-physical-network",
                api="provider:physical_network"),
            opt("segmentation_id", "--provider-segment",
                api="provider:segmentation_id", cast="int"),
            opt("mtu", "--mtu", cast="int"),
            boolean("shared", "shared", on="--share"),
            boolean("external", "router:external", on="--external"),
            # Defaults to up; absence means the operator unticked it.
            boolean("admin_state", "admin_state_up", off="--disable"),
            repeated("az_hints", "--availability-zone-hint",
                     "availability_zone_hints"),
            arg("name"),
        ],
    },
]

# Horizon encodes table actions as "<table>__<action>[__<id>]" in a field named
# "action". Only tables listed here are recognised; deletes on anything else
# are ignored. Row actions and multi-select batch deletes both work.
TABLES = {
    "flavors": {"noun": "flavor", "path": COMPUTE + "/flavors"},
    "volume_types": {"noun": "volume type", "path": VOLUME + "/types"},
    "networks": {"noun": "network", "path": NETWORK + "/networks"},
}


def as_dict():
    """The rule set, in the shape the JavaScript interpreter consumes."""
    return {"forms": FORMS, "tables": TABLES}


# ---------------------------------------------------------------- validation


def _load_form(target):
    module_name, class_name = target.split(":")
    return getattr(import_module(module_name), class_name)


def validate():
    """Check every rule still matches the Horizon form it targets.

    Returns a list of human-readable problems, empty when all is well. Imports
    Horizon lazily, so this module remains usable without a dashboard present.
    """
    problems = []
    for form in FORMS:
        target = form.get("form")
        if not target:
            continue
        try:
            form_class = _load_form(target)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problems.append(
                "%s: cannot import %s (%s)" % (form["id"], target, exc))
            continue
        known = set(getattr(form_class, "base_fields", {}))
        if not known:
            problems.append(
                "%s: %s exposes no base_fields" % (form["id"], target))
            continue
        for field in form["fields"]:
            if field["field"] not in known:
                problems.append(
                    "%s: field %r is no longer on %s"
                    % (form["id"], field["field"], target))
    return problems


def uncovered():
    """Form fields no rule mentions, keyed by rule id.

    Informational rather than a problem: plenty of fields are deliberately
    unmapped (Horizon's ``with_subnet`` branches into a second form, for
    instance). Useful when reviewing a rule after a Horizon upgrade.
    """
    report = {}
    for form in FORMS:
        target = form.get("form")
        if not target:
            continue
        try:
            form_class = _load_form(target)
        except Exception:  # noqa: BLE001 - validate() reports this properly
            continue
        mapped = {field["field"] for field in form["fields"]}
        missing = sorted(set(getattr(form_class, "base_fields", {})) - mapped)
        if missing:
            report[form["id"]] = missing
    return report
