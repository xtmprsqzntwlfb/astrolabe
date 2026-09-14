/**
 * Astrolabe - show the CLI and REST equivalents of Horizon actions.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may
 * not use this file except in compliance with the License. You may obtain
 * a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 * How this works
 * --------------
 * Django Horizon submits its modal forms and its table row/batch actions as
 * ordinary form submissions (horizon.modals.js delegates `.modal form` submits
 * through $.ajax; table actions post the table's own form). A single capturing
 * `submit` listener therefore sees every create and delete the operator
 * performs, along with the exact field values they entered.
 *
 * This file contains no knowledge of any particular Horizon panel. What to
 * translate, and how, comes from astrolabe/rules.py, which the header template
 * embeds as JSON. Everything below is the generic interpreter for those rules
 * plus the drawer that displays the results.
 *
 * A submission matching no rule is ignored and never stored. Nothing here
 * calls OpenStack: the recorder reads what the operator already submitted and
 * renders equivalent text.
 */

(function () {
  'use strict';

  var STORE_KEY = 'astrolabe.log';
  var MAX_ENTRIES = 50;

  // Field names whose values are never read, let alone stored.
  //
  // Long, unambiguous words match anywhere in the name - that is what catches
  // Django's run-together "csrfmiddlewaretoken". Short words match only on
  // boundaries, so "passthrough" and "keystone_url" are not dropped by
  // accident.
  var SECRET_ANYWHERE = /password|passwd|secret|token|credential/i;
  var SECRET_WORD = /(^|_)(pass|key|auth)(_|$)/i;

  function isSecret(name) {
    // Split camelCase into underscore-separated words first, so boundary
    // matching also catches names like Nova's "adminPass".
    var normalized = String(name).replace(/([a-z0-9])([A-Z])/g, '$1_$2');
    return SECRET_ANYWHERE.test(normalized) || SECRET_WORD.test(normalized);
  }

  // ---------------------------------------------------------------- helpers

  /** Quote a value for a POSIX shell, leaving safe words bare. */
  function shq(value) {
    var s = value === null || value === undefined ? '' : String(value);
    if (s !== '' && /^[A-Za-z0-9_@%+=:,.\/-]+$/.test(s)) {
      return s;
    }
    return "'" + s.replace(/'/g, "'\\''") + "'";
  }

  function blank(value) {
    return value === null || value === undefined || value === '';
  }

  /** Django renders unchecked booleans by omitting them entirely. */
  function checked(value) {
    return value === 'on' || value === 'true' || value === 'True';
  }

  function cast(value, how) {
    if (how !== 'int') {
      return String(value);
    }
    var n = Number(value);
    return isNaN(n) ? null : n;
  }

  function asList(value) {
    if (blank(value)) {
      return [];
    }
    return Array.isArray(value) ? value : [value];
  }

  // ------------------------------------------------------------ interpreter

  /**
   * Apply one form rule to a submission.
   *
   * Positionals are collected and appended last regardless of where they
   * appear in the rule, because that is where the openstack CLI wants them.
   */
  function applyForm(spec, fields) {
    var parts = spec.command.slice();
    var trailing = [];
    var body = {};
    var subject = '';

    spec.fields.forEach(function (field) {
      var raw = fields[field.field];

      if (field.kind === 'value') {
        if (blank(raw)) {
          return;
        }
        if (field.omitWhen === null || String(raw) !== String(field.omitWhen)) {
          parts.push(field.flag, shq(raw));
        }
        var value = cast(raw, field.cast);
        if (value !== null) {
          body[field.api] = value;
        }
        return;
      }

      if (field.kind === 'flag') {
        var on = checked(raw);
        if (on && field.on) {
          parts.push(field.on);
        } else if (!on && field.off) {
          parts.push(field.off);
        }
        body[field.api] = on;
        return;
      }

      if (field.kind === 'multi') {
        var values = asList(raw);
        values.forEach(function (one) {
          parts.push(field.flag, shq(one));
        });
        if (values.length) {
          body[field.api] = values;
        }
        return;
      }

      if (field.kind === 'positional') {
        if (blank(raw)) {
          return;
        }
        trailing.push(shq(raw));
        body[field.api] = String(raw);
        subject = String(raw);
      }
    });

    var wrapped = {};
    wrapped[spec.envelope] = body;

    return {
      title: subject ? spec.title + ' ' + subject : spec.title,
      cli: parts.concat(trailing).join(' '),
      calls: [{
        method: spec.method,
        url: spec.endpoint,
        body: wrapped
      }]
    };
  }

  /** Apply the generic table-delete rule, if the action names a known table. */
  function applyDelete(tables, fields) {
    var bits = String(fields.action).split('__');
    var table = tables[bits[0]];
    if (!table) {
      return null;
    }
    // A row action carries the id in the third segment; a batch action puts
    // the selected ids in repeated object_ids fields.
    var ids = bits.length > 2 ? [bits.slice(2).join('__')]
      : asList(fields.object_ids);
    if (!ids.length) {
      return null;
    }
    return {
      title: 'Delete ' + table.noun + (ids.length > 1 ? 's' : '') +
        ' (' + ids.length + ')',
      cli: 'openstack ' + table.noun + ' delete ' + ids.map(shq).join(' '),
      calls: ids.map(function (id) {
        return { method: 'DELETE', url: table.path + '/' + id, body: null };
      })
    };
  }

  /** Translate a submission, or return null if no rule claims it. */
  function translate(spec, url, fields) {
    for (var i = 0; i < spec.forms.length; i++) {
      if (spec.forms[i].pattern.test(url)) {
        return applyForm(spec.forms[i], fields);
      }
    }
    if (typeof fields.action === 'string' &&
        /^[a-z0-9_]+__delete(__|$)/.test(fields.action)) {
      return applyDelete(spec.tables, fields);
    }
    return null;
  }

  /** Read the embedded rule set, compiling each url pattern once. */
  function loadSpec() {
    var node = document.getElementById('astrolabe-rules');
    if (!node) {
      return null;
    }
    var spec = JSON.parse(node.textContent);
    spec.forms.forEach(function (form) {
      form.pattern = new RegExp(form.url);
    });
    return spec;
  }

  // ------------------------------------------------------------- rendering

  function curlFor(call) {
    var parts = [
      'curl -sS -X ' + call.method + ' "' + call.url + '"',
      '-H "X-Auth-Token: $OS_TOKEN"'
    ];
    if (call.body) {
      parts.push('-H "Content-Type: application/json"');
      parts.push("-d '" + JSON.stringify(call.body).replace(/'/g, "'\\''") + "'");
    }
    return parts.join(' \\\n  ');
  }

  // --------------------------------------------------------------- storage

  function load() {
    try {
      return JSON.parse(window.sessionStorage.getItem(STORE_KEY)) || [];
    } catch (e) {
      return [];
    }
  }

  function save(entries) {
    try {
      window.sessionStorage.setItem(
        STORE_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
    } catch (e) {
      // Storage disabled or over quota. Recording is best-effort and must
      // never interfere with the action the operator is performing.
    }
  }

  // --------------------------------------------------------------- capture

  function readFields(form, submitter) {
    var data;
    try {
      // The two-argument form folds in the clicked button's name/value, which
      // is how Horizon table actions carry "action".
      data = new window.FormData(form, submitter);
    } catch (e) {
      data = new window.FormData(form);
    }
    var fields = {};
    data.forEach(function (value, name) {
      if (isSecret(name)) {
        return;
      }
      if (typeof window.File !== 'undefined' && value instanceof window.File) {
        value = '<file:' + value.name + '>';
      }
      if (Object.prototype.hasOwnProperty.call(fields, name)) {
        fields[name] = asList(fields[name]).concat([value]);
      } else {
        fields[name] = value;
      }
    });
    // Older browsers ignore the second FormData argument.
    if (submitter && submitter.name && !(submitter.name in fields) &&
        !isSecret(submitter.name)) {
      fields[submitter.name] = submitter.value;
    }
    return fields;
  }

  function record(spec, form, submitter) {
    var fields = readFields(form, submitter);
    var url = form.getAttribute('action') || window.location.pathname;
    var built = translate(spec, url, fields);
    if (!built) {
      return;
    }
    var entries = load();
    entries.unshift({
      at: Date.now(),
      title: built.title,
      cli: built.cli,
      calls: built.calls || []
    });
    save(entries);
  }

  // ------------------------------------------------------------------- UI

  var STYLE = [
    '.astrolabe-toggle{cursor:pointer;white-space:nowrap}',
    '.astrolabe-count{display:inline-block;min-width:1.5em;margin-left:.4em;',
    'padding:0 .4em;border-radius:.8em;background:#337ab7;color:#fff;',
    'font-size:.75em;text-align:center}',
    '.astrolabe-drawer{position:fixed;top:0;right:0;bottom:0;width:41rem;',
    'max-width:100vw;z-index:1100;background:#fff;color:#333;',
    'box-shadow:-2px 0 8px rgba(0,0,0,.3);display:flex;flex-direction:column;',
    'font-size:13px}',
    '.astrolabe-drawer[hidden]{display:none}',
    '.astrolabe-head{display:flex;align-items:center;gap:.5rem;',
    'padding:.75rem 1rem;border-bottom:1px solid #ddd}',
    '.astrolabe-head h3{margin:0;font-size:15px;flex:1}',
    '.astrolabe-body{flex:1;overflow:auto;padding:.5rem 1rem}',
    '.astrolabe-foot{padding:.6rem 1rem;border-top:1px solid #ddd;',
    'color:#666;font-size:11px;line-height:1.5}',
    '.astrolabe-entry{border-bottom:1px solid #eee;padding:.75rem 0}',
    '.astrolabe-entry h4{margin:0 0 .35rem;font-size:13px;font-weight:600}',
    '.astrolabe-when{color:#888;font-weight:400;font-size:11px;',
    'margin-left:.5rem}',
    '.astrolabe-block{position:relative;margin:.35rem 0}',
    '.astrolabe-block pre{margin:0;padding:.5rem 4.5rem .5rem .6rem;',
    'background:#f5f5f5;border:1px solid #e3e3e3;border-radius:3px;',
    'white-space:pre-wrap;word-break:break-all;font-size:12px}',
    '.astrolabe-copy{position:absolute;top:.3rem;right:.3rem}',
    '.astrolabe-empty{color:#888;padding:1rem 0}'
  ].join('');

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  /** A copyable code block. Values are set as text, never as markup. */
  function block(label, text) {
    var wrap = el('div', 'astrolabe-block');
    var pre = el('pre', null, text);
    pre.setAttribute('aria-label', label);
    var copy = el('button', 'btn btn-default btn-xs astrolabe-copy', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', function () {
      var done = function () {
        copy.textContent = 'Copied';
        window.setTimeout(function () { copy.textContent = 'Copy'; }, 1200);
      };
      if (window.navigator.clipboard) {
        window.navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        // http:// origins get no async clipboard; fall back to a selection.
        var area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); done(); } catch (e) { /* noop */ }
        document.body.removeChild(area);
      }
    });
    wrap.appendChild(pre);
    wrap.appendChild(copy);
    return wrap;
  }

  function build(toggle, spec) {
    var style = el('style');
    style.textContent = STYLE;
    document.head.appendChild(style);

    var drawer = el('div', 'astrolabe-drawer');
    drawer.hidden = true;
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-label', 'Astrolabe');

    var head = el('div', 'astrolabe-head');
    head.appendChild(el('h3', null, 'Astrolabe'));
    var clear = el('button', 'btn btn-default btn-xs', 'Clear');
    clear.type = 'button';
    var close = el('button', 'btn btn-default btn-xs', 'Close');
    close.type = 'button';
    head.appendChild(clear);
    head.appendChild(close);

    var body = el('div', 'astrolabe-body');

    var foot = el('div', 'astrolabe-foot', null);
    foot.appendChild(document.createTextNode(
      'Commands are rendered against $OS_TOKEN, $OS_COMPUTE_API (e.g. ' +
      'https://host/compute/v2.1), $OS_VOLUME_API (e.g. ' +
      'https://host/volume/v3/$OS_PROJECT_ID) and $OS_NETWORK_API (e.g. ' +
      'https://host:9696/v2.0). Your token is never written into a command. ' +
      'Recorded in this browser tab only.'));

    drawer.appendChild(head);
    drawer.appendChild(body);
    drawer.appendChild(foot);
    document.body.appendChild(drawer);

    var count = document.getElementById('astrolabe-count');

    function badge(n) {
      if (count) {
        count.textContent = String(n);
        count.hidden = n === 0;
      }
    }

    function render() {
      var entries = load();
      badge(entries.length);

      body.textContent = '';
      if (!entries.length) {
        body.appendChild(el('div', 'astrolabe-empty',
          'No actions recorded yet. Create or delete a flavor, volume type ' +
          'or network and it will show up here.'));
        return;
      }

      entries.forEach(function (entry) {
        var item = el('div', 'astrolabe-entry');
        var title = el('h4', null, entry.title);
        title.appendChild(el('span', 'astrolabe-when',
          new Date(entry.at).toLocaleTimeString()));
        item.appendChild(title);
        item.appendChild(block('CLI', entry.cli));
        (entry.calls || []).forEach(function (call) {
          item.appendChild(block('REST', curlFor(call)));
        });
        body.appendChild(item);
      });
    }

    toggle.addEventListener('click', function (evt) {
      evt.preventDefault();
      // Horizon binds its own click handler to header sections to reorder
      // them; don't let a toggle click trigger that.
      evt.stopPropagation();
      if (drawer.hidden) {
        render();
        drawer.hidden = false;
      } else {
        drawer.hidden = true;
      }
    });
    toggle.addEventListener('keydown', function (evt) {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        toggle.click();
      }
    });
    close.addEventListener('click', function () { drawer.hidden = true; });
    clear.addEventListener('click', function () { save([]); render(); });

    document.addEventListener('keydown', function (evt) {
      if (evt.key === 'Escape' && !drawer.hidden) {
        drawer.hidden = true;
      }
    });

    // Capture phase, so the entry is stored before horizon.modals.js hands the
    // form to $.ajax and before a full-page action submit unloads us.
    document.addEventListener('submit', function (evt) {
      var form = evt.target;
      if (!form || form.tagName !== 'FORM') {
        return;
      }
      try {
        record(spec, form, evt.submitter);
        if (drawer.hidden) {
          badge(load().length);
        } else {
          render();
        }
      } catch (e) {
        // Never let a recording bug block the operator's action.
        if (window.console && window.console.warn) {
          window.console.warn('astrolabe: could not record submission', e);
        }
      }
    }, true);

    render();
  }

  // Exposed for the dependency-free harness in tests/, and so an operator can
  // inspect what Astrolabe parsed from the browser console. Not an interface
  // anything else should depend on.
  window.astrolabe = {
    loadSpec: loadSpec,
    translate: translate,
    curlFor: curlFor,
    readFields: readFields
  };

  // The header section arrives by AJAX after page load (see
  // horizon.extensible_header.js), and only for admins. Wait for it: no
  // marker means no drawer, no rules and no recording.
  function whenReady(callback) {
    var found = document.getElementById('astrolabe-toggle');
    if (found) {
      callback(found);
      return;
    }
    var observer = new window.MutationObserver(function () {
      var node = document.getElementById('astrolabe-toggle');
      if (node) {
        observer.disconnect();
        callback(node);
      }
    });
    observer.observe(document.documentElement, {
      childList: true, subtree: true
    });
  }

  var started = false;
  whenReady(function (toggle) {
    if (started) {
      return;
    }
    started = true;
    var spec = loadSpec();
    if (!spec) {
      return;
    }
    build(toggle, spec);
  });
}());
