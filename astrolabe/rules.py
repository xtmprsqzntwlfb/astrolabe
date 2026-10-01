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

import re

from importlib import import_module

# Rendered commands never carry a real endpoint or a real token: these panels
# get screenshotted into tickets. Commands render against shell variables
# instead, which the panel footer documents.
COMPUTE = "$OS_COMPUTE_API"
VOLUME = "$OS_VOLUME_API"
NETWORK = "$OS_NETWORK_API"
IDENTITY = "$OS_IDENTITY_API"

# Import prefixes, only to keep the table list below readable.
ADMIN = "openstack_dashboard.dashboards.admin."
IDENT = "openstack_dashboard.dashboards.identity."


# --------------------------------------------------------------- field kinds
#
# Six kinds cover every rule below. Each describes one form field: how it
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


def target(group="id"):
    """The resource being changed, read out of the URL rather than the form.

    An edit form does not submit the id of the thing it is editing; Horizon
    puts it in the path. Every panel names that capture group differently --
    ``id`` on flavors, ``network_id`` on networks, ``tenant_id`` on projects
    -- but Astrolabe matches with a pattern of its own rather than Horizon's,
    so a rule writes ``(?P<id>[^/]+)`` whatever upstream calls it and this
    reads it back.

    It trails the command like any other positional, and reaches the REST
    call through the ``{id}`` in the rule's endpoint. Nothing goes in the
    body: an id identifies the resource rather than describing it, and
    Neutron would reject it as an attribute. It carries no ``api`` key at
    all, for the same reason :func:`redacted` does not.
    """
    return {"kind": "target", "field": group}


#: Kinds whose ``field`` names a URL capture group rather than a form field.
#: validate() must not go looking for these in ``base_fields``.
FROM_URL = frozenset(["target"])


def choice(field, api, choices):
    """A select where each option carries its own flag and its own API value.

    ``choices`` maps the submitted value to ``(flag, api_value)``. A submitted
    value the map does not mention emits nothing and writes nothing, which is
    how Horizon's "Use Server Default" options behave: the router form sends
    ``distributed`` only once the operator has picked centralized or
    distributed. Leaving the sentinel out of the map says that, without the
    rule having to name it.

    Pass None as the flag for an option the CLI expresses by saying nothing.
    """
    return {
        "kind": "choice", "field": field, "api": api,
        "choices": {
            value: {"flag": flag, "value": api_value}
            for value, (flag, api_value) in choices.items()
        },
    }


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
        "routes": ["horizon:admin:flavors:create"],
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
        "routes": ["horizon:admin:volume_types:create_type"],
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
        "routes": ["horizon:admin:networks:create"],
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
        "id": "network-update",
        "title": "Update network",
        # Admin only, unlike the router rule, which covers both dashboards.
        # The project form is a separate class carrying name, admin_state and
        # shared but not external. On a create an absent checkbox is just a
        # default, but on an update Horizon sends all four keys every time, so
        # the rule has to emit both sides of each -- and then a project-side
        # submission, which never had an "external" box to untick, would
        # render --internal and claim the operator turned external routing
        # off. Missing a panel beats describing one wrongly. See Known limits.
        "url": r"/admin/networks/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.admin.networks.forms"
                ":UpdateNetwork",
        "routes": ["horizon:admin:networks:update"],
        "method": "PUT",
        "endpoint": NETWORK + "/networks/{id}",
        "envelope": "network",
        "command": ["openstack", "network", "set"],
        # Every boolean here names both sides. UpdateNetwork.handle builds its
        # params unconditionally from all four fields, so an unticked box on
        # an edit is a decision -- "make this not shared" -- in a way the same
        # unticked box on a create is not.
        "fields": [
            opt("name", "--name"),
            boolean("admin_state", "admin_state_up",
                    on="--enable", off="--disable"),
            boolean("shared", "shared", on="--share", off="--no-share"),
            boolean("external", "router:external",
                    on="--external", off="--internal"),
            target(),
        ],
    },
    {
        "id": "aggregate-create",
        "title": "Create host aggregate",
        "url": r"/admin/aggregates/create/?$",
        "form": "openstack_dashboard.dashboards.admin.aggregates.workflows"
                ":SetAggregateInfoAction",
        "routes": ["horizon:admin:aggregates:create"],
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
        "id": "router-create",
        "title": "Create router",
        # Both dashboards, because the admin form subclasses the project one
        # and adds nothing but tenant_id. A submission from the project side
        # simply does not carry that field, and an absent value is already
        # skipped. Validating against the admin class therefore covers both:
        # its base_fields are a superset. The cost is that a project-side
        # command names no project, because none was submitted — Horizon uses
        # whatever scope the operator is in. See Known limits.
        "url": r"/(admin|project)/routers/create/?$",
        "form": "openstack_dashboard.dashboards.admin.routers.forms"
                ":CreateForm",
        "routes": [
            "horizon:admin:routers:create",
            "horizon:project:routers:create",
        ],
        "method": "POST",
        "endpoint": NETWORK + "/routers",
        "envelope": "router",
        "command": ["openstack", "router", "create"],
        # enable_snat is deliberately unmapped. Horizon sends it only when a
        # gateway network was also chosen, nested beside network_id, and a
        # rule cannot make one field depend on another. Unticking it is
        # therefore invisible here. See Known limits.
        "fields": [
            opt("tenant_id", "--project"),
            # Defaults to up; absence means the operator unticked it.
            boolean("admin_state_up", "admin_state_up", off="--disable"),
            opt("external_network", "--external-gateway",
                api="external_gateway_info.network_id"),
            # Both of these offer "server_default", which Horizon reads as
            # "send no key at all"; choice() expresses that by omission.
            choice("mode", "distributed", {
                "centralized": ("--centralized", False),
                "distributed": ("--distributed", True),
            }),
            choice("ha", "ha", {
                "enabled": ("--ha", True),
                "disabled": ("--no-ha", False),
            }),
            repeated("az_hints", "--availability-zone-hint",
                     "availability_zone_hints"),
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
        "routes": ["horizon:identity:projects:create"],
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
        "routes": ["horizon:identity:domains:create"],
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
        "routes": ["horizon:identity:groups:create"],
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
        "routes": ["horizon:identity:users:create"],
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
        "routes": ["horizon:identity:roles:create"],
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
#
# The key is the name Horizon gives the table, which is what arrives in the
# action field. "table" names the DataTable class it came from, so validate()
# can confirm the name is still that class's, the way "form" lets it confirm a
# rule's fields. Deletes are dashboard-independent -- a table keeps its name
# wherever it is used -- so one class is enough even where two dashboards
# render the same table.
TABLES = {
    "flavors": {
        "noun": "flavor", "path": COMPUTE + "/flavors",
        "table": ADMIN + "flavors.tables:FlavorsTable",
    },
    "volume_types": {
        "noun": "volume type", "path": VOLUME + "/types",
        "table": ADMIN + "volume_types.tables:VolumeTypesTable",
    },
    "networks": {
        "noun": "network", "path": NETWORK + "/networks",
        "table": ADMIN + "networks.tables:NetworksTable",
    },
    "routers": {
        "noun": "router", "path": NETWORK + "/routers",
        "table": ADMIN + "routers.tables:RoutersTable",
    },
    "host_aggregates": {
        "noun": "aggregate", "path": COMPUTE + "/os-aggregates",
        "table": ADMIN + "aggregates.tables:HostAggregatesTable",
    },
    # Horizon still calls the project table "tenants", the pre-Keystone-v3
    # name. The CLI noun and the API path are both "project".
    "tenants": {
        "noun": "project", "path": IDENTITY + "/projects",
        "table": IDENT + "projects.tables:TenantsTable",
    },
    "domains": {
        "noun": "domain", "path": IDENTITY + "/domains",
        "table": IDENT + "domains.tables:DomainsTable",
    },
    "groups": {
        "noun": "group", "path": IDENTITY + "/groups",
        "table": IDENT + "groups.tables:GroupsTable",
    },
    "roles": {
        "noun": "role", "path": IDENTITY + "/roles",
        "table": IDENT + "roles.tables:RolesTable",
    },
    "users": {
        "noun": "user", "path": IDENTITY + "/users",
        "table": IDENT + "users.tables:UsersTable",
    },
}


# ---------------------------------------------------------------- validation


def _load_form(target):
    module_name, class_name = target.split(":")
    return getattr(import_module(module_name), class_name)


#: Stood into an edit panel's URL when reversing it. Two of them because
#: Horizon's id patterns are usually ``[^/]+`` but occasionally numeric, and
#: a placeholder the pattern rejects would look exactly like a moved panel.
_STAND_INS = ("0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9", "1")


def _reverse_route(name, wants_id):
    """The path a URL name resolves to, or None if it no longer resolves."""
    from django.urls import NoReverseMatch
    from django.urls import reverse

    attempts = [(stand_in,) for stand_in in _STAND_INS] if wants_id else [()]
    for args in attempts:
        try:
            return reverse(name, args=args), (args[0] if args else None)
        except NoReverseMatch:
            continue
    return None, None


def _check_routes(form, problems):
    """Confirm a rule's url pattern still reaches the panel it names.

    Checking fields is not enough. If Horizon moves a panel's path, every
    field on the form is still exactly where the rule says, and the rule
    simply stops matching -- no warning, no failure, nothing recorded. This is
    the half of the safety net that notices.

    Reversing the URL name rather than hard-coding a path is the point: the
    name is Horizon's stable handle, the path is the thing allowed to move.

    An edit panel's URL takes the resource id, so it is reversed with a
    stand-in, and the rule's capture group is then checked to have caught that
    stand-in and not some other segment. A pattern that matches the path while
    capturing the wrong part of it builds a command against the wrong
    resource, which is a worse outcome than not matching at all.
    """
    wants_id = "(?P<id>" in form["url"]
    for name in form["routes"]:
        path, stand_in = _reverse_route(name, wants_id)
        if path is None:
            problems.append(
                "%s: %s no longer reverses; the rule cannot fire"
                % (form["id"], name))
            continue
        match = re.search(form["url"], path)
        if not match:
            problems.append(
                "%s: %s is now %s, which %r does not match"
                % (form["id"], name, path, form["url"]))
        elif stand_in is not None and match.groupdict().get("id") != stand_in:
            problems.append(
                "%s: %s is now %s, where %r captures %r as the id rather "
                "than the resource" % (form["id"], name, path, form["url"],
                                       match.groupdict().get("id")))


def _check_tables(problems):
    """Confirm each delete rule's table still goes by the name we match on."""
    for key, table in TABLES.items():
        target = table["table"]
        try:
            table_class = _load_form(target)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problems.append(
                "table %s: cannot import %s (%s)" % (key, target, exc))
            continue
        actual = getattr(getattr(table_class, "_meta", None), "name", None)
        if actual != key:
            problems.append(
                "table %s: %s now calls itself %r, so deletes there stop "
                "being recorded" % (key, target, actual))


def validate():
    """Check every rule still describes the Horizon it targets.

    Three things can drift independently, so all three are checked: the form
    class and its field names, the URL the rule matches on, and the table name
    deletes arrive under. A rule can be perfectly correct about its fields and
    still never fire.

    Returns a list of human-readable problems, empty when all is well. Imports
    Horizon lazily, so this module remains usable without a dashboard present.
    """
    problems = []
    _check_tables(problems)
    for form in FORMS:
        _check_routes(form, problems)
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
            if field["kind"] in FROM_URL:
                continue
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
        mapped = {field["field"] for field in form["fields"]
                  if field["kind"] not in FROM_URL}
        missing = sorted(set(getattr(form_class, "base_fields", {})) - mapped)
        if missing:
            report[form["id"]] = missing
    return report
