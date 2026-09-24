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
Horizon form to the ``openstack`` command and REST call it corresponds to.
This module is pure data plus its own self-check; :mod:`astrolabe.translate`
holds the generic interpreter that applies these rules, and knows nothing
about any particular panel.

Because the rules live in Python, :func:`validate` can import the Horizon form
each rule targets and check the field names still exist, so a rename in a
future Horizon release surfaces as a warning instead of a silently incomplete
command.

Nothing at module level imports Django or Horizon, so this module stays
importable (and testable) on its own.
"""

from importlib import import_module

# Rendered commands never carry a real endpoint or a real token: these panels
# get screenshotted into tickets. Commands render against shell variables
# instead, which the panel footer documents.
COMPUTE = "$OS_COMPUTE_API"
VOLUME = "$OS_VOLUME_API"
NETWORK = "$OS_NETWORK_API"
IDENTITY = "$OS_IDENTITY_API"


# --------------------------------------------------------------- field kinds
#
# Four kinds cover every rule below. Each describes one form field: how it
# reaches the command line, and how it reaches the REST body.


def opt(field, flag, api=None, cast="str", omit_when=None, absent_when=None):
    """A value carried by a flag: ``--ram 2048``.

    ``omit_when`` drops the flag for a given value but keeps it in the REST
    body, which is how ``--swap 0`` stays off the command line while ``swap:
    0`` remains meaningful to the API.

    ``absent_when`` names a sentinel the operator can type that means "not
    supplied", and drops the field from the command *and* the body. Horizon's
    flavor form uses ``auto`` this way.
    """
    return {
        "kind": "value", "field": field, "flag": flag,
        "api": api or field, "cast": cast, "omitWhen": omit_when,
        "absentWhen": absent_when,
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


def redacted(field, flag):
    """Stands in for a field Astrolabe deliberately never reads.

    ``translate.read_fields`` drops password-like names before the interpreter
    sees them, so a rule cannot know what was typed — only that the form has
    such a field. Emitting a prompting flag is the honest rendering: the
    command asks for the value when it runs, rather than quietly creating a
    user nobody can log in as.

    The flag is unconditional, because absence proves nothing here: a dropped
    field and an empty one look identical from this side. Use it only for
    fields the form requires.

    Nothing reaches the REST body — there is no value to put there, and a
    placeholder inside the single-quoted ``curl -d`` payload would not expand.
    The field name is kept so :func:`validate` goes on checking it exists.
    """
    return {"kind": "redacted", "field": field, "flag": flag}


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
            # Horizon defaults this to "auto" and novaclient turns "auto"
            # into an omitted id, so it must not reach the command or body.
            opt("flavor_id", "--id", api="id", absent_when="auto"),
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
    {
        "id": "aggregate-create",
        "title": "Create host aggregate",
        "url": r"/admin/aggregates/create/?$",
        "form": "openstack_dashboard.dashboards.admin.aggregates.workflows"
                ":SetAggregateInfoAction",
        "method": "POST",
        "endpoint": COMPUTE + "/os-aggregates",
        "envelope": "aggregate",
        "command": ["openstack", "aggregate", "create"],
        # The workflow's second step adds hosts, through a separate action
        # class and a separate API call per host. That is beyond a rule, which
        # describes one form and one command; the recorded command creates an
        # empty aggregate. See Known limits.
        "fields": [
            opt("availability_zone", "--zone"),
            arg("name"),
        ],
    },
    {
        "id": "project-create",
        "title": "Create project",
        # Projects are the identity dashboard's default panel, so the panel
        # slug does not appear in the path.
        "url": r"/identity/create/?$",
        "form": "openstack_dashboard.dashboards.identity.projects.workflows"
                ":CreateProjectInfoAction",
        "method": "POST",
        "endpoint": IDENTITY + "/projects",
        "envelope": "project",
        "command": ["openstack", "project", "create"],
        "fields": [
            # The form shows domain_name and submits domain_id; only the id
            # means anything to the API, and --domain accepts either.
            opt("domain_id", "--domain"),
            opt("description", "--description"),
            # Ships ticked. Only the unticked case needs saying.
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
    },
    {
        "id": "domain-create",
        "title": "Create domain",
        "url": r"/identity/domains/create/?$",
        "form": "openstack_dashboard.dashboards.identity.domains.workflows"
                ":CreateDomainInfoAction",
        "method": "POST",
        "endpoint": IDENTITY + "/domains",
        "envelope": "domain",
        "command": ["openstack", "domain", "create"],
        "fields": [
            opt("description", "--description"),
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
    },
    {
        "id": "group-create",
        "title": "Create group",
        "url": r"/identity/groups/create/?$",
        "form": "openstack_dashboard.dashboards.identity.groups.forms"
                ":CreateGroupForm",
        "method": "POST",
        "endpoint": IDENTITY + "/groups",
        "envelope": "group",
        "command": ["openstack", "group", "create"],
        "fields": [
            opt("description", "--description"),
            arg("name"),
        ],
    },
    {
        "id": "user-create",
        "title": "Create user",
        "url": r"/identity/users/create/?$",
        "form": "openstack_dashboard.dashboards.identity.users.forms"
                ":CreateUserForm",
        "method": "POST",
        "endpoint": IDENTITY + "/users",
        "envelope": "user",
        "command": ["openstack", "user", "create"],
        # Four form fields are deliberately unmapped, and uncovered() lists
        # them: confirm_password (never read), domain_name (display only,
        # domain_id is submitted beside it), role_id (Horizon assigns the role
        # in a second API call, which one command cannot express) and
        # lock_password, which is a checkbox rather than a credential but
        # whose name matches the secret filter, so read_fields drops it before
        # a rule could see it. Narrowing that filter to let one boolean
        # through is a bad trade: the cost of getting it wrong is a leaked
        # password, and the gain is --enable-lock-password.
        "fields": [
            opt("domain_id", "--domain"),
            opt("project", "--project", api="default_project_id"),
            opt("email", "--email"),
            opt("description", "--description"),
            redacted("password", "--password-prompt"),
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
    },
    {
        "id": "role-create",
        "title": "Create role",
        # Only reachable when ANGULAR_FEATURES['roles_panel'] is False. It
        # defaults to True, in which case the panel POSTs to /api/* instead
        # and this rule simply never matches. It is here because the flag can
        # be turned off today, and because the Angular panel is on its way out.
        "url": r"/identity/roles/create/?$",
        "form": "openstack_dashboard.dashboards.identity.roles.forms"
                ":CreateRoleForm",
        "method": "POST",
        "endpoint": IDENTITY + "/roles",
        "envelope": "role",
        "command": ["openstack", "role", "create"],
        "fields": [
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
    "host_aggregates": {"noun": "aggregate",
                        "path": COMPUTE + "/os-aggregates"},
    # Horizon still calls the project table "tenants", the pre-Keystone-v3
    # name. The CLI noun and the API path are both "project".
    "tenants": {"noun": "project", "path": IDENTITY + "/projects"},
    "domains": {"noun": "domain", "path": IDENTITY + "/domains"},
    "groups": {"noun": "group", "path": IDENTITY + "/groups"},
    "roles": {"noun": "role", "path": IDENTITY + "/roles"},
    "users": {"noun": "user", "path": IDENTITY + "/users"},
}


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
