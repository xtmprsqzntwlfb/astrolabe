=========
astrolabe
=========

An OpenStack Horizon plugin that shows the **CLI and REST equivalents of what
you just did in the dashboard**. Perform an admin action — create a flavor,
delete a network — and Astrolabe records the equivalent ``openstack`` command
and ``curl`` request, ready to copy into a script or download as one.

It exists to shorten the gap between clicking through Horizon and automating
the same work.

Design goals
============

* **No backend.** No models, no migrations, no API endpoints, no daemons, no
  extra runtime dependencies. Recorded entries live in the operator's own
  Django session.
* **Pure Python.** Not one line of JavaScript, and no ``<script>`` tag. The
  recorder is a middleware and the display is a Django template. Horizon is
  moving toward removing AngularJS altogether, and Astrolabe has nothing to
  remove.
* **Encapsulated.** It patches nothing in Horizon and touches no existing
  dashboard. It adds one dashboard of its own, with one panel.
* **Admin only.** Gated twice, server-side: the dashboard carries the same
  admin permissions Horizon's own Admin dashboard uses, and the middleware
  checks ``is_superuser`` before it records anything. A non-admin's actions are
  never read, let alone stored.
* **Read-only, and offline.** Astrolabe calls no OpenStack API. It reads form
  values the operator has already submitted, and renders text.

How it works
============

Django Horizon submits its modal forms and its table row/batch actions as
ordinary POSTs. One middleware therefore observes every create and delete an
admin performs, together with the exact values they entered.

This is the same vantage point ``horizon.middleware.OperationLogMiddleware``
uses — in-tree, enabled by a setting, and shipped in Horizon's default
``MIDDLEWARE`` — and Astrolabe deliberately follows its shape: fetch the
response first, then read ``request.POST``. That ordering matters. Reading POST
before the view runs consumes the request body and breaks Horizon's file-upload
forms.

Each submission is matched against a rule table. **A submission that matches no
rule is ignored and never stored** — Astrolabe only ever retains fields for the
handful of forms it explicitly understands::

    POST /admin/flavors/create/
            │
            ▼
    middleware.py ──▶ translate.py ──▶ session  ──▶ panel
                          ▲
                       rules.py

The rules live in Python, in ``astrolabe/rules.py``, which is pure data plus
its own self-check. ``rules.validate()`` confirms everything a rule depends
on upstream, because those things drift independently: the form class and its
field names, the URL the rule matches on (reversed from the Horizon URL name
the rule records, since the name is the stable handle and the path is what
moves), where an edit rule's pattern finds the resource id in that path, and
the table name deletes arrive under. A rule can be perfectly correct about
its fields and still never fire. All of it surfaces as a logged warning rather
than a silently incomplete — or silently absent — command. The check runs
once, on the first submission a rule matches, and its failures are logged
rather than raised.

``astrolabe/translate.py`` holds the interpreter that applies the rules. It
knows nothing about any particular panel.

Recording outcomes
------------------

Because it runs after the view, the middleware sees whether Horizon actually
accepted the action, which is something a browser-side recorder cannot know.
Two signals:

* **Status code.** Horizon redirects on success and re-renders the modal with
  errors on failure. That covers both invalid input and an API call the service
  refused, because ``ModalFormView.form_valid`` falls through to
  ``form_invalid`` when ``form.handle`` raises.
* **Queued messages.** Table actions redirect either way, so for those the
  status code says nothing and Horizon's error messages are the only evidence.
  Reading them is non-destructive — ``BaseStorage.update`` stores
  ``_queued_messages`` without clearing it — so the operator still sees the
  message regardless of where Astrolabe sits relative to Django's
  ``MessageMiddleware``. That attribute is private, hence the guard and the
  fallback to the status code alone.

Rejected actions are still recorded, marked in the panel and commented in the
downloaded script. Knowing what you tried is usually worth as much as knowing
what worked.

What is never read
------------------

Secret-looking field names are dropped before anything is read. ``password``,
``secret``, ``token`` and ``credential`` match anywhere in the name (this is
what catches Django's run-together ``csrfmiddlewaretoken``); ``pass``, ``key``
and ``auth`` match only on word boundaries, so ``passthrough`` and
``keystone_url`` survive. Names are split on camelCase first, so Nova's
``adminPass`` is caught too.

Nothing is sent anywhere. Entries live in the operator's session, capped, and
disappear at logout.

A note on ``/api/*``
--------------------

An earlier sketch of this plugin recorded Horizon's ``/api/nova/...`` REST proxy
traffic. That turns out to be incompatible with a Django-only design: every
caller of those endpoints lives in
``openstack_dashboard/static/app/core/openstack-service-api/``, which *is* the
AngularJS service layer. On a de-Angularized Horizon there is no ``/api/*``
traffic to record. Form submissions are the Django-native equivalent, and they
carry the operator's actual input rather than a proxy's reshaping of it.

What v1 covers
==============

Panels chosen for flat, well-understood forms over admin-managed resources.
Most of them exist only in the admin or identity dashboards; routers are the
one that does not, and *Which dashboard a panel lives in* below says how that
is handled:

============================== ====================================
Action                         Rendered as
============================== ====================================
Create flavor                  ``openstack flavor create``
Create volume type             ``openstack volume type create``
Create network (admin)         ``openstack network create``
Create project                 ``openstack project create``
Create domain                  ``openstack domain create``
Create group                   ``openstack group create``
Create role                    ``openstack role create``
Create user                    ``openstack user create``
Create host aggregate          ``openstack aggregate create``
Create router                  ``openstack router create``
Edit volume type               ``openstack volume type set``
Edit network (admin)           ``openstack network set``
Edit project                   ``openstack project set``
Edit domain                    ``openstack domain set``
Edit group                     ``openstack group set``
Edit role                      ``openstack role set``
Edit user                      ``openstack user set``
Edit host aggregate            ``openstack aggregate set``
Edit router                    ``openstack router set``
Delete, on any of the above    ``openstack <resource> delete``
============================== ====================================

Every create has a matching edit except flavors, which Horizon does not let you
edit: its "Edit Flavor" button changes which projects may use the flavor, and
nothing about the flavor itself. See Known limits.

Deletes are handled by one generic rule that decodes Horizon's
``<table>__<action>__<id>`` action encoding, so row actions and multi-select
batch deletes both work.

Two of these take more than one call, and come out as more than one command
under a single heading, because they were a single thing the operator did.
Creating a host aggregate with three hosts selected is an ``aggregate create``
followed by three ``aggregate add host``; creating a user with a primary
project and a role is a ``user create`` followed by a ``role add``.


Roles are the exception to "it just works". Horizon ships the roles panel as
its AngularJS variant by default (``ANGULAR_FEATURES['roles_panel']``), and
that variant POSTs to ``/api/*`` rather than submitting a Django form, so
**nothing is recorded there** — in that mode the Django create and edit views
are not even routed. The rules are present and correct, and start recording as
soon as the panel is the Django one, which is also what happens when the
Angular variant is eventually removed upstream.

To opt in now, drop a file in ``local_settings.d`` and restart::

    cat > ../horizon/openstack_dashboard/local/local_settings.d/_11_toggle_angular_features.py <<'EOF'
    ANGULAR_FEATURES.update({'roles_panel': False})
    EOF

That switches the panel to the legacy Django UI, which looks different from the
Angular one. It is a deployment choice, not something Astrolabe requires — the
plugin can only observe form submissions, so it records exactly the panels that
are not Angular. ``images_panel`` defaults to Angular for the same reason.

Each entry renders two things: the ``openstack`` command, and the equivalent
REST call as ``curl``.

Which dashboard a panel lives in
--------------------------------

Astrolabe records by *who is asking*, not by where they are: the middleware
checks the operator holds an admin role in the current scope and then looks at
the submission, wherever in Horizon it came from. That has a consequence worth
being explicit about, because the two halves behave differently.

Deletes are dashboard-independent already. The generic rule keys off the table
name in the ``action`` field, and a table keeps its name across dashboards, so
deleting a router from the project side is recorded exactly as deleting one
from the admin side. Every table name Astrolabe maps refers to the same
resource wherever Horizon uses it; the handful of other places the names turn
up are ``LinkAction`` classes, which are ordinary links and never POST a
delete.

Creates and edits are matched by URL, one rule at a time, so each rule has to
say which dashboards it covers. Most of them need only one: flavors, volume
types and host aggregates exist solely in the admin dashboard, and projects,
domains, groups, roles and users solely in identity. Routers are the exception
and both router rules cover both dashboards, because the admin forms subclass
the project ones and add nothing but a project selector on the create side and
a redirect on the edit side.

Networks are the case where that shortcut does not hold, on both sides. The
admin create panel is a plain form; the project one is a multi-step workflow
that creates a subnet alongside the network, under different field names, and
it would be a separate rule producing more than one command. The two edit
panels are separate classes too, and there the mismatch is worse than a gap:
see Known limits. Neither project-side panel is covered, so an admin working
on a network from the project dashboard gets nothing.

Endpoints and the token
-----------------------

Rendered commands carry no real endpoint and no real token — these panels get
screenshotted into tickets. They reference shell variables you set once::

    export OS_TOKEN=$(openstack token issue -f value -c id)
    export OS_COMPUTE_API=https://your-cloud/compute/v2.1
    export OS_VOLUME_API=https://your-cloud/volume/v3/$OS_PROJECT_ID
    export OS_NETWORK_API=https://your-cloud:9696/v2.0
    export OS_IDENTITY_API=https://your-cloud:5000/v3

The ``openstack`` commands themselves need none of these; they use your usual
``clouds.yaml`` or ``OS_*`` credentials. The variables are only for the
``curl`` equivalents.

Installation
============

These steps assume a **source checkout of Horizon** (e.g. devstack) with the
``horizon`` and ``astrolabe`` trees side by side::

    parent/
    ├── horizon/      # the Horizon source tree (has manage.py)
    └── astrolabe/    # this repo

A Horizon plugin is *not* copied into the ``horizon`` tree. ``pip install``
puts the ``astrolabe`` package on Horizon's Python path; two small drop-in
files wire it in, and Horizon imports the rest from the installed package.
Nothing in the ``horizon`` tree gets edited.

Every command must use **the Python environment that runs your Horizon**. If
your checkout uses a virtualenv, activate it first; otherwise use whatever
``python``/``pip`` you launch Horizon with.

Run these from the parent directory that holds both trees.

1. **Install the plugin into Horizon's Python environment**::

     cd astrolabe
     pip install .

   Then confirm it imported into the right place::

     python -c "import astrolabe; print(astrolabe.__version__, astrolabe.__file__)"

   You should see a version and a path ending in
   ``.../site-packages/astrolabe/__init__.py``. If it errors, the wrong
   ``pip``/``python`` was used — fix that before going on. The version is
   written in ``astrolabe/__init__.py`` and ``setup.cfg`` reads it from there
   with ``attr:``, so the package metadata and the running code cannot
   disagree.

   (Use ``pip install -e .`` instead if you want edits in ``astrolabe`` to take
   effect on restart without reinstalling.)

2. **Register the panel** — copy the enabled file into Horizon's local enabled
   directory::

     cp astrolabe/enabled/_9020_astrolabe.py \
        ../horizon/openstack_dashboard/local/enabled/

   (The ``_9020_`` prefix controls load order; leave it unless it collides with
   an existing file.)

3. **Register the recorder.** Horizon's plugin loader has hooks for apps,
   panels, JavaScript and header sections, but none for middleware, so this
   takes a second drop-in file::

     cat > ../horizon/openstack_dashboard/local/local_settings.d/_9020_astrolabe.py <<'EOF'
     MIDDLEWARE = list(MIDDLEWARE) + ['astrolabe.middleware.AstrolabeMiddleware']
     EOF

   It has to be that directory rather than ``local_settings.py``. Snippets in
   ``local_settings.d`` are ``exec``'d in the settings namespace, so
   ``MIDDLEWARE`` is in scope to extend; ``local_settings.py`` is imported as
   its own module, where the same line would raise ``NameError``.

   Appending is correct. Astrolabe reads the response and Django runs the
   response phase in reverse order, so the end of the list is where it belongs.

   Without this step the panel installs and works, and stays permanently empty.

4. **Restart Horizon.**

   * Dev server: stop it and re-run ``python manage.py runserver`` from the
     ``horizon`` dir.
   * Apache/mod_wsgi (packaged installs)::

       sudo systemctl restart httpd          # RHEL/CentOS/Fedora
       # or
       sudo systemctl restart apache2        # Ubuntu/Debian

   No ``collectstatic`` or ``compress`` step: Astrolabe ships no static assets.

5. **Verify.** Log in as an admin-capable user. An **Astrolabe** dashboard
   appears in the sidebar. Go to *Admin > Compute > Flavors*, create a flavor,
   then open *Astrolabe > Commands* — the equivalent ``openstack flavor create``
   command and ``curl`` request are waiting there.

   If the dashboard does not appear, check that your user actually holds an
   admin role in the current scope. If it appears but stays empty, step 3 did
   not take: ``python manage.py diffsettings | grep -i middleware`` should
   mention ``astrolabe``.

Settings
--------

Both are optional, and go in ``local_settings.py`` like any other Horizon
setting.

``ASTROLABE_ENABLED``
    Default ``True``. Set it to ``False`` to switch the recorder off without
    uninstalling; the middleware then raises ``MiddlewareNotUsed`` at startup
    and costs nothing per request. The panel stays visible and empty.

``ASTROLABE_MAX_ENTRIES``
    Default ``50``. Entries kept per session. Horizon's default session backend
    is the cache, where 50 is nothing. Lower it if you have switched
    ``SESSION_ENGINE`` to ``django.contrib.sessions.backends.signed_cookies``,
    which puts the whole session in a ~4KB cookie.

Local dev setup (tox runserver against a devstack VM)
=====================================================

A common dev layout: OpenStack runs on a **devstack VM**, and you run a **local
Horizon checkout** on your workstation, pointed at the VM's Keystone via
``OPENSTACK_HOST`` in ``local_settings.py``. Attaching Astrolabe this way
touches only your local checkout — the VM is never modified. Astrolabe makes no
API calls of its own at all, so it adds no traffic to the VM beyond the
requests your own clicks already send.

This checkout is launched with ``tox -e runserver``, so the Python environment
Horizon actually uses is the tox venv at ``horizon/.tox/runserver`` — that is
where Astrolabe must be installed (not your system or user ``pip``). Paths
below assume ``horizon`` and ``astrolabe`` side by side.

1. **Install into the runserver venv** (editable, so your edits apply on the
   next restart). Use the **absolute path** to your ``astrolabe`` checkout so
   the install doesn't depend on your current directory — an editable install
   records the path you give it::

     horizon/.tox/runserver/bin/pip install -e /full/path/to/astrolabe

2. **Verify it imports with settings loaded**, and check the rule table against
   the Horizon you are about to run it on. A bare ``python -c "import
   astrolabe"`` proves very little — ``rules.validate()`` is the interesting
   check, and it imports Horizon's form classes, which need Django settings. Go
   through ``manage.py`` instead::

     horizon/.tox/runserver/bin/python horizon/manage.py shell \
       -c "from astrolabe import rules; print(rules.validate() or 'ok')"

   ``ok`` means every rule still lines up with the forms in this checkout.
   Anything else is a list of rules to fix before they silently stop matching.

3. **Register the panel**::

     cp astrolabe/astrolabe/enabled/_9020_astrolabe.py \
        horizon/openstack_dashboard/local/enabled/

4. **Register the recorder**::

     cat > horizon/openstack_dashboard/local/local_settings.d/_9020_astrolabe.py <<'EOF'
     MIDDLEWARE = list(MIDDLEWARE) + ['astrolabe.middleware.AstrolabeMiddleware']
     EOF

   Put ``ASTROLABE_ENABLED`` and ``ASTROLABE_MAX_ENTRIES``, if you want them, in
   ``local_settings.py`` — **not** in shell environment variables. tox only
   forwards allowlisted vars into the venv (``passenv``), so an exported
   ``ASTROLABE_MAX_ENTRIES`` would not reach the running server anyway, and
   Astrolabe reads settings, not the environment.

5. **Run it** from the ``horizon`` dir::

     tox -e runserver

   No ``collectstatic`` or ``compress`` step at any point: Astrolabe ships no
   static assets.

Notes:

* **Recreating the venv wipes the Astrolabe install.** ``tox -re runserver``
  always rebuilds it, but a plain ``tox -e runserver`` will too whenever tox
  decides the environment is stale. The symptom is
  ``ModuleNotFoundError: No module named 'astrolabe'`` at startup; the fix is to
  re-run step 1. To check::

    horizon/.tox/runserver/bin/pip show astrolabe

  Steps 3 and 4 survive this — those two files live in the ``horizon`` tree, not
  in the venv. Only the ``pip install`` is lost.

* **Restarting the dev server can empty the panel.** The log lives in your
  Horizon session, so it lasts exactly as long as the session backend does. With
  Horizon's default ``SESSION_ENGINE`` over a local-memory cache the session
  dies with the process — every autoreload logs you out and clears the log.
  Point ``CACHES`` at a running memcached if you want a log that survives your
  own edits.

* To see anything at all you need to be logged in with an **admin role in the
  current scope** on the devstack VM, and to perform one of the actions in *What
  v1 covers*. A demo-only login gets no Astrolabe dashboard, by design.

Uninstalling
============

::

    rm ../horizon/openstack_dashboard/local/enabled/_9020_astrolabe.py
    rm ../horizon/openstack_dashboard/local/local_settings.d/_9020_astrolabe.py
    pip uninstall astrolabe

Then restart Horizon.

Extending the rule table
========================

Adding a panel means adding one entry to ``FORMS`` in ``astrolabe/rules.py``.
Nothing else changes. A rule names the URL it matches, the command and
endpoint it maps to, and the fields it carries. Eight field kinds cover
everything so far:

``opt(field, flag, api, cast, omit_when, absent_when, clearable)``
    A value carried by a flag, ``--ram 2048``. ``omit_when`` drops the flag for
    a given value but keeps it in the REST body, which is how ``--swap 0``
    stays off the command line while ``swap: 0`` still reaches the API.
    ``absent_when`` names a sentinel meaning "not supplied" and drops the field
    from both — Horizon's flavor form uses ``auto`` that way, and novaclient
    turns ``auto`` into an omitted id. ``clearable`` says an empty box means
    "remove this", which is only ever true on an edit: a create and an edit
    submit the same empty string and mean opposite things by it. Set it only
    where emptying the field is something the API actually does, and only on
    a rule that carries a ``target``.

``boolean(field, api, on, off)``
    A checkbox. ``on`` is the flag emitted when ticked, ``off`` when not; either
    may be omitted. That covers both ``--share`` (emitted when ticked) and
    ``--disable`` (emitted when *not* ticked).

``repeated(field, flag, api)``
    A multi-select, emitted as the flag once per value.

``choice(field, api, choices)``
    A select where each option carries its own flag and its own API value.
    ``choices`` maps the submitted value to ``(flag, api_value)``; a submitted
    value the map does not mention emits nothing and writes nothing. That is
    how Horizon's "Use Server Default" options behave — the router form sends
    ``distributed`` only once centralized or distributed has been picked — so
    leaving the sentinel out of the map is all a rule has to say.

``arg(field, api)``
    A positional argument. Always rendered last, as the CLI expects. Pass
    ``api=None`` for one that names some *other* resource rather than
    describing this one — the aggregate a host is being added to — so it goes
    on the command line and stays out of the REST body.

``item(api)``
    The value a repeating step is currently on; see ``per`` below. One
    ``aggregate add host`` per selected host, and this is whichever host that
    is. Like ``target`` it names no form field, because the field it comes
    from holds the list rather than the value.

``target(group)``
    The resource an edit form is editing, read out of the URL rather than
    the submission. An edit form sends what the resource should become; the
    path says which one. It trails the command like any positional and
    reaches the REST call through the ``{id}`` in the rule's endpoint, never
    through the body — an id identifies a resource rather than describing
    it, and Neutron rejects it as an attribute.

``redacted(field, flag)``
    Stands in for a field Astrolabe refuses to read. The secret filter drops
    password-like names before the interpreter runs, so the rule emits a
    prompting flag instead of a value — ``--password-prompt`` rather than a
    silently absent password. The flag is unconditional, because a dropped
    field and an empty one look identical from here, so use it only for fields
    the form requires. Nothing reaches the REST body. The field name is still
    recorded, so ``validate()`` keeps checking it exists.

When one saved form makes more than one API call, a rule carries ``then``:
follow-up steps. A step is a rule minus the parts that place it — no ``url``,
no ``routes``, no ``title`` — so the same interpreter reads it, and nearly
every check that applies to a rule applies to a step too. Three keys are its
own: ``per`` names a multi-select and runs the step once per selected value,
which ``item()`` stands for; ``when`` names fields that must all carry a value,
for a call Horizon itself only makes sometimes; and ``form`` names the action
class the step's own fields come from, where that differs from the rule's. A
workflow posts every step at once, so either class's fields may turn up in the
submission and ``validate()`` accepts both.

A follow-up call needs the id of the thing the first call created, and
Astrolabe never sees a response, so it cannot know it. The rule declares
``creates``, a placeholder that ``{new}`` in a step's endpoint resolves to, and
the panel footer explains it. **Only the REST half needs this.** The commands
address the new resource by the name the operator typed, which is what keeps
them runnable exactly as rendered — ``openstack aggregate add host gpu-nodes
compute-1`` needs no id at all.

A repeating step has one thing nothing else does: its multi-select is built in
the action's ``__init__`` from the action's own slug, so the name never reaches
``base_fields`` and the ordinary field check cannot see it. ``validate()`` asks
Horizon's own ``get_member_field_name`` instead, so a change to that naming
scheme is reported rather than silently dropping every follow-up call.

Alongside the fields, a rule records where it lives. ``routes`` lists the
Horizon URL names the rule serves — usually one, two for routers — and
``validate()`` reverses each and checks the rule's pattern still matches the
path that comes back. Patterns end in ``/?$`` because several of these paths
have no trailing slash (``/identity/create``,
``/admin/volume_types/create_type``); the test is what keeps that from being
tidied away. A ``TABLES`` entry names the ``DataTable`` class its key came
from, so the same check covers deletes.

An edit rule carries a resource id in its path. Horizon names that capture
group differently on every panel — ``id`` on flavors, ``network_id`` on
networks, ``tenant_id`` on projects — but a rule matches with a pattern of
its own, so it always writes ``(?P<id>[^/]+)`` and spells the endpoint
``.../networks/{id}``. ``validate()`` reverses such a route with a stand-in
id and then checks the rule's group caught *that*, because a pattern can
match the path while its group lands on the wrong segment, and a command
built against the wrong resource is worse than one that never fires.

Checkboxes change meaning on an edit, and a rule has to follow. A create form
lets an unticked box mean "leave the default alone", so the rule names only
the side worth saying — ``boolean("shared", "shared", on="--share")``. An edit
form builds its request from every field each time, so an unticked box there
is a decision, and the rule names both sides. One side on an edit rule records
the operator turning something on and stays silent when they turn it off,
which reads as though they never touched it.

Set ``form`` to the Horizon class the rule targets, as
``"module:ClassName"``. That is what lets ``validate()`` check the rule against
the real form. You do not need to look the field names up by hand::

    >>> from astrolabe import rules
    >>> rules.validate()     # [] when every field still exists
    []
    >>> rules.uncovered()['router-update']    # fields no rule mentions
    ['ha']

``validate()`` needs Horizon importable. Its counterpart ``verify_cli()``
needs ``python-openstackclient`` instead, and checks the other direction: that
every flag the rule can emit is one the command still takes. Run it after
adding a rule rather than checking flags by hand::

    >>> rules.verify_cli()   # [] when every command and flag still exists
    []

If the declarative vocabulary cannot express a new panel, extend both
``rules.py`` and ``translate.py``'s ``_apply_form`` together, and add a case to
``tests/test_rules.py``.

A rule covers whatever dashboards its form serves, which is usually one. Give
a rule a second dashboard only when the same form class is behind both, as it
is for routers; a panel that merely creates the same kind of resource is a
different form and wants a rule of its own. Where a project-side form leaves
the owning project implicit, say so in Known limits rather than guessing at a
``--project`` value: Astrolabe records what was submitted, and the project was
not.

Tests
=====

Dependency-free: no npm, no jsdom, no pytest required::

    python3 tests/test_rules.py

Five layers, in increasing order of what has to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The interpreter turns them into the expected commands, and the session store
   behaves. Needs nothing.
3. The middleware records the right things and only for the right people, and
   the panel shows back what it recorded. Needs Django, but not Horizon.
4. The rules still match the Horizon forms they target. Needs a Horizon
   checkout, and is skipped with a note when one is not importable.
5. The commands the rules render still exist, with the flags they use. Needs
   ``python-openstackclient``, and is skipped with a note when it is absent.

A rule straddles two projects, and the last two layers watch one upstream
each. They fail differently, which is why both are worth having. Horizon drift
stops a rule firing: the panel moves, or a field is renamed, and the recording
quietly stops happening. CLI drift leaves the rule firing perfectly — right
URL, right fields, right table — and rendering a command that no longer works.
Nothing about the dashboard looks wrong; the operator finds out when they
paste it into a shell.

The panel half of layer 3 renders the real template over a real
``translate()`` result, because the middleware and the panel are the two ends
of one entry shape and testing them apart lets both be right while
disagreeing. Two things belong to Horizon rather than to Astrolabe — the view
base class and ``base.html`` — and both are stood in for when no Horizon is
importable, so the panel is covered on every CI interpreter rather than only
in the weekly job. When a real Horizon *is* on the path, the real base class
is used, so the substitution cannot be what hides a change in it.

To include layer 4, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py

That layer includes a test that deliberately stages a renamed field and asserts
``validate()`` reports it, so the safety net cannot pass vacuously.

Layer 5 wants an interpreter with the CLI on it, which Horizon's does not
have::

    python3 -m venv /tmp/osc
    /tmp/osc/bin/pip install python-openstackclient
    /tmp/osc/bin/python tests/test_rules.py

It loads each command through its openstackclient entry point, builds the
command's argparse parser — which needs no cloud, no config and no network —
and checks every flag the rule can emit is one the parser accepts, and that
the command still takes the positional each rule ends with. Commands
registered once per API version are checked against the newest, because
Horizon talks Keystone v3 and Cinder v3 and the v2 parsers are missing flags
the rules legitimately use.

CI (``.github/workflows/ci.yml``) runs flake8, then the suite twice on each of
Python 3.9 through 3.13 — once on a bare interpreter, which is what keeps the
"needs nothing" claim above honest, and again with Django installed to pick up
layer 3. A separate job installs ``python-openstackclient`` for layer 5. It
also builds a wheel and checks the template and the enabled file are inside
it. **Layer 4 is not part of that run**: it needs a Horizon checkout, and
pinning one Horizon version to every pull request would say nothing useful
about upstream.

Layer 5 is on every pull request rather than weekly, because the CLI is a
release off PyPI rather than a moving checkout. Installing the current one
answers a question worth asking per-change — do these commands work for an
operator installing the CLI today — and it catches a flag typed wrong in a new
rule before it merges.

Layer 4 has a workflow of its own instead. ``upstream.yml`` runs weekly
against Horizon **master**, installing Horizon under OpenStack's
upper-constraints and printing ``validate()`` and ``uncovered()`` before
running the suite. It answers a different question from ``ci.yml`` — not "is
Astrolabe self-consistent" but "do the rules still describe the Horizon that
exists today" — so it is scheduled rather than attached to pull requests, and
a failure there means go and look at upstream rather than at the last commit.
It is early warning: ``validate()`` runs inside the plugin too, but by then
somebody has upgraded and lost a recording.

Layer 4 stages each drift it claims to catch and asserts it is reported — a
renamed field, a moved panel, a URL name that stopped reversing, an id
captured from the wrong path segment, a renamed table, a renamed field on a
follow-up step, and a membership multi-select that changed name — so none of
the seven can pass vacuously. Layer 5 does the same for its four: a withdrawn
flag, a renamed command, a command that stopped taking a positional, and a
follow-up step naming a command that does not exist. It also asserts it
resolved a plausible number of commands and flags, because an entry point
layout it did not expect would find nothing to check and look exactly like a
clean run.

Known limits
============

* Only the actions listed above are recognised. Everything else is
  silently ignored, by design.
* There is no copy button, because there is no JavaScript. Select the text, or
  use **Download as shell script** for the whole log at once.
* The panel shows what you have already done; it does not appear beside the
  form you are filling in. Do your work, then go and collect the commands.
* Panels switched to their AngularJS variants via ``ANGULAR_FEATURES`` POST
  JSON to Horizon's ``/api/*`` proxy instead of submitting a Django form, and
  are not recorded at all. Of the panels Astrolabe knows about,
  ``roles_panel`` defaults to Angular and ``flavors_panel`` does not; see the
  note under *What v1 covers* for how to switch a panel back.
* **Clearing a field is recorded where the CLI can say it, and not
  otherwise.** Emptying a description on any edit panel comes out as
  ``--description ''``, and so does an emptied email on *Edit User*. Three
  fields deliberately stay quiet: a cleared **Primary Project**, because
  openstackclient has no ``user unset`` and no ``--no-project``, and
  ``--project ''`` would go looking for a project named ``""``; an emptied
  **Availability Zone** on *Edit Host Aggregate*, because Nova refuses to
  clear one once it is set; and names, which every form but the router's
  requires anyway. For those, the recorded command leaves the old value
  alone. A command that is short beats one that fails.
* **Create user never records the password.** The command ends up with
  ``--password-prompt`` and the ``curl`` body has no password in it at all —
  the CLI form asks you at run time, the REST form you must fill in yourself.
  The primary role *is* recorded, as a second ``openstack role add``.
* **Edit user names fields Horizon would have left out.** Horizon drops the
  primary project and the description from its request unless you actually
  changed them; Astrolabe sees the submitted form and not which boxes were
  touched, so it renders both whenever they hold a value. The command sets
  them to what they already are, which is longer than it needs to be rather
  than wrong. Neither the password nor the domain is recorded: the password is
  changed on a panel of its own, and Keystone does not let a user move domain.
* **Network create is recorded from the admin panel only.** The project
  panel is a different form — a workflow that also creates a subnet — and
  needs a rule of its own. See "Which dashboard a panel lives in".
* **Network edit is recorded from the admin panel only too**, and for a
  different reason. The project form is a separate class carrying name,
  admin state and shared, but no **External Network** box. On an edit
  Horizon sends every one of those keys each time, so the rule emits both
  sides of each checkbox — and a project-side submission, which never had an
  external box to untick, would come out as ``--internal`` and claim the
  operator turned external routing off. Missing a panel beats describing one
  wrongly, so the rule stays pinned to ``/admin/``.
* **A router created from the project dashboard records no owning project.**
  Only the admin form offers a project selector. From the project side Horizon
  uses whatever scope you are in, and there is no field to record, so the
  command comes out as a bare ``openstack router create <name>`` and creates
  the router in whichever project your shell is scoped to at the time. Every
  other recorded command names what it acts on; this one does not. Add
  ``--project`` yourself if you will run it elsewhere.
* **Create router does not record Enable SNAT.** Horizon sends it only when
  a gateway network was chosen too, nested beside the network id, and a rule
  cannot make one field depend on another. Unticking it is invisible here, so
  add ``--disable-snat`` yourself if you meant it.
* **Editing a membership list is not recorded, anywhere.** This is the one
  thing follow-up steps do not solve, and the reason is not the number of
  calls. Changing the members of a project or a domain, the hosts of an
  existing aggregate, or the projects a flavor is available to, means
  *diffing* what you selected against what was there before — one call per
  addition and one per removal. Astrolabe sees the selection and never the
  starting point, so it cannot tell an addition from a value that was already
  set, and it cannot see a removal at all. Adding hosts while *creating* an
  aggregate is recorded precisely because there is nothing to diff against:
  the aggregate did not exist a moment ago.

  In practice that means: *Edit Project* and *Edit Domain* record their first
  step only, the name, description and enabled state, and not the role changes
  made in the same dialog; *Manage Hosts* on an existing aggregate records
  nothing; and *Edit Flavor*, which despite the name edits only the list of
  projects that may use the flavor, records nothing either.
* Rendered commands reproduce what was submitted. They are a starting point for
  a script, not a tested one — read them before you run them.
* The ``curl`` equivalents target the real service APIs, not Horizon's proxy, so
  they need a token and endpoints from your own shell.

License
=======

Apache License 2.0. See ``LICENSE``.
