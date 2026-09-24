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
its own self-check. ``rules.validate()`` imports the Horizon form each rule
targets and confirms the field names still exist, so a rename in a future
Horizon release surfaces as a logged warning rather than a silently incomplete
command. The check runs once, on the first submission a rule matches, and its
failures are logged rather than raised.

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

Panels chosen because they are unambiguously admin-scoped (no policy or
project-scoping subtleties) and have flat, well-understood forms:

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
Delete, on any of the above    ``openstack <resource> delete``
============================== ====================================

Deletes are handled by one generic rule that decodes Horizon's
``<table>__<action>__<id>`` action encoding, so row actions and multi-select
batch deletes both work.

Roles are the exception to "it just works". Horizon ships the roles panel as
its AngularJS variant by default (``ANGULAR_FEATURES['roles_panel']``), and
that variant POSTs to ``/api/*`` rather than submitting a Django form, so
**neither creates nor deletes are recorded there** — in that mode the Django
create view is not even routed. The rules are present and correct, and start
recording as soon as the panel is the Django one, which is also what happens
when the Angular variant is eventually removed upstream.

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

     python -c "import astrolabe; print('ok', astrolabe.__file__)"

   You should see ``ok`` and a path ending in
   ``.../site-packages/astrolabe/__init__.py``. If it errors, the wrong
   ``pip``/``python`` was used — fix that before going on.

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
endpoint it maps to, and the fields it carries. Six field kinds cover
everything so far:

``opt(field, flag, api, cast, omit_when, absent_when)``
    A value carried by a flag, ``--ram 2048``. ``omit_when`` drops the flag for
    a given value but keeps it in the REST body, which is how ``--swap 0``
    stays off the command line while ``swap: 0`` still reaches the API.
    ``absent_when`` names a sentinel meaning "not supplied" and drops the field
    from both — Horizon's flavor form uses ``auto`` that way, and novaclient
    turns ``auto`` into an omitted id.

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
    A positional argument. Always rendered last, as the CLI expects.

``redacted(field, flag)``
    Stands in for a field Astrolabe refuses to read. The secret filter drops
    password-like names before the interpreter runs, so the rule emits a
    prompting flag instead of a value — ``--password-prompt`` rather than a
    silently absent password. The flag is unconditional, because a dropped
    field and an empty one look identical from here, so use it only for fields
    the form requires. Nothing reaches the REST body. The field name is still
    recorded, so ``validate()`` keeps checking it exists.

Set ``form`` to the Horizon class the rule targets, as
``"module:ClassName"``. That is what lets ``validate()`` check the rule against
the real form. You do not need to look the field names up by hand::

    >>> from astrolabe import rules
    >>> rules.validate()     # [] when every field still exists
    []
    >>> rules.uncovered()    # form fields no rule mentions
    {'network-create': ['with_subnet']}

If the declarative vocabulary cannot express a new panel, extend both
``rules.py`` and ``translate.py``'s ``_apply_form`` together, and add a case to
``tests/test_rules.py``.

Keep new rules admin-scoped for now. Project-scoped panels bring policy and
project-id questions that v1 deliberately avoids.

Tests
=====

Dependency-free: no npm, no jsdom, no pytest required::

    python3 tests/test_rules.py

Four layers, in increasing order of what has to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The interpreter turns them into the expected commands, and the session store
   behaves. Needs nothing.
3. The middleware records the right things, and only for the right people.
   Needs Django, but not Horizon.
4. The rules still match the Horizon forms they target. Needs a Horizon
   checkout, and is skipped with a note when one is not importable.

To include layer 4, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py

That layer includes a test that deliberately stages a renamed field and asserts
``validate()`` reports it, so the safety net cannot pass vacuously.

CI (``.github/workflows/ci.yml``) runs flake8, then the suite twice on each of
Python 3.9 through 3.13 — once on a bare interpreter, which is what keeps the
"needs nothing" claim above honest, and again with Django installed to pick up
layer 3. It also builds a wheel and checks the template and the enabled file
are inside it. **Layer 4 is not in CI**: it needs a Horizon checkout and would
pin one Horizon version, so a green run says nothing about whether the rules
still match upstream. Run it locally after a Horizon upgrade.

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
  are not recorded — creates or deletes. Of the panels Astrolabe knows about,
  ``roles_panel`` defaults to Angular and ``flavors_panel`` does not; see the
  note under *What v1 covers* for how to switch a panel back.
* **Create user is deliberately incomplete in two places.** Astrolabe never
  reads the password, so the command ends up with ``--password-prompt`` and the
  ``curl`` body has no password in it at all — the CLI form asks you at run
  time, the REST form you must fill in yourself. Horizon also assigns the
  primary role in a second API call, which one command cannot express, so the
  recorded ``openstack user create`` leaves the new user unroled. Add the
  matching ``openstack role add`` yourself.
* **Create router does not record Enable SNAT.** Horizon sends it only when
  a gateway network was chosen too, nested beside the network id, and a rule
  cannot make one field depend on another. Unticking it is invisible here, so
  add ``--disable-snat`` yourself if you meant it.
* **Create host aggregate records only the first step.** The workflow's
  second step adds hosts, through a separate action class and one API call
  per host, which a rule describing one form and one command cannot express.
  The recorded command creates an empty aggregate; add the matching
  ``openstack aggregate add host`` calls yourself.
* Rendered commands reproduce what was submitted. They are a starting point for
  a script, not a tested one — read them before you run them.
* The ``curl`` equivalents target the real service APIs, not Horizon's proxy, so
  they need a token and endpoints from your own shell.

License
=======

Apache License 2.0. See ``LICENSE``.
