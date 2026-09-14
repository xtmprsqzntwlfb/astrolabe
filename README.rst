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

Three admin panels, chosen because they are unambiguously admin-scoped (no
policy or project-scoping subtleties) and have flat, well-understood forms:

============================== ====================================
Action                         Rendered as
============================== ====================================
Create flavor                  ``openstack flavor create``
Create volume type             ``openstack volume type create``
Create network (admin)         ``openstack network create``
Delete, on any of those three  ``openstack <resource> delete``
============================== ====================================

Deletes are handled by one generic rule that decodes Horizon's
``<table>__<action>__<id>`` action encoding, so row actions and multi-select
batch deletes both work.

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
endpoint it maps to, and the fields it carries. Four field kinds cover
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

``arg(field, api)``
    A positional argument. Always rendered last, as the CLI expects.

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

Known limits
============

* Only the four actions listed above are recognised. Everything else is
  silently ignored, by design.
* There is no copy button, because there is no JavaScript. Select the text, or
  use **Download as shell script** for the whole log at once.
* The panel shows what you have already done; it does not appear beside the
  form you are filling in. Do your work, then go and collect the commands.
* Panels switched to their AngularJS variants via ``ANGULAR_FEATURES`` (flavors
  has one) POST JSON to Horizon's ``/api/*`` proxy instead of submitting a
  Django form, and are not recorded.
* Rendered commands reproduce what was submitted. They are a starting point for
  a script, not a tested one — read them before you run them.
* The ``curl`` equivalents target the real service APIs, not Horizon's proxy, so
  they need a token and endpoints from your own shell.

License
=======

Apache License 2.0. See ``LICENSE``.
