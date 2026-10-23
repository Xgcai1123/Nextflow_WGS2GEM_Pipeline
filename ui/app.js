/* NextFlow-WGS2GEM — local single-page UI.
 *
 * Native JavaScript (no framework, no build step, no external resources).
 * Contract: docs/ui.md. The front end never infers run state: every state
 * change comes from GET /api/state or the SSE `state` event.
 */
(function () {
  'use strict';

  /* ------------------------------------------------------------- constants */

  var STATUS = {
    idle: { label: 'Idle', cls: 'badge-idle' },
    running: { label: 'Running', cls: 'badge-running' },
    done: { label: 'Done', cls: 'badge-done' },
    failed: { label: 'Failed', cls: 'badge-failed' },
    cancelled: { label: 'Cancelled', cls: 'badge-cancelled' },
    interrupted: { label: 'Interrupted', cls: 'badge-interrupted' }
  };

  var MODE_LABEL = { resume: 'Resume (-resume)', fresh: 'Fresh run' };

  /* Pipeline order (12 processes). The backend reports `steps[].process`; the
   * order is display-only so a run's ladder always has the same shape. */
  var PROCESS_ORDER = [
    'FASTQC_RAW', 'TRIMMOMATIC', 'FASTQC_TRIMMED', 'SPADES', 'QUAST',
    'ASSEMBLY_REF', 'BUSCO', 'BAKTA', 'CARVEME', 'GROWTH_CHECK', 'MEMOTE', 'MULTIQC'
  ];

  /* One aggregate task each, not one per sample. */
  var AGGREGATE = { GROWTH_CHECK: true, MULTIQC: true };

  /* Tasks each process submits per sample: two FASTQC runs per read pair. */
  var PER_SAMPLE_EXPECTED = {
    FASTQC_RAW: 2, TRIMMOMATIC: 1, FASTQC_TRIMMED: 2, SPADES: 1, QUAST: 1,
    ASSEMBLY_REF: 1, BUSCO: 1, BAKTA: 1, CARVEME: 1, MEMOTE: 1
  };

  var DRAFT_KEY = 'wgs2gem.ui.draft.v1';
  var MAX_LOG_LINES = 20000;
  var LOG_ERR_RE = /ERROR|Caused by|FAIL/;
  var SAMPLE_RE = /^[A-Za-z0-9._-]+$/;

  /* Refresh fallbacks. SSE is the primary channel, but the UI must keep telling
   * the truth when that single long connection stalls or dies: poll the state
   * endpoint while the tab is visible, re-fetch once after a terminal
   * transition (report/exit_code can lag the SSE snapshot), and reconnect the
   * stream when it has been silent for too long. */
  var TERMINAL_STATES = { done: true, failed: true, cancelled: true, interrupted: true };
  var STATE_POLL_MS = 5000;
  var TERMINAL_REFETCH_MS = 600;
  var SSE_WATCHDOG_MS = 45000;

  var TOKEN = '';
  try {
    TOKEN = new URLSearchParams(window.location.search).get('token') || '';
  } catch (e) {
    TOKEN = '';
  }

  /* ---------------------------------------------------------------- helpers */

  function $(id) { return document.getElementById(id); }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.class) node.className = opts.class;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (key) {
        var value = opts.attrs[key];
        if (value !== null && value !== undefined && value !== false) {
          node.setAttribute(key, String(value));
        }
      });
    }
    if (opts.props) {
      Object.keys(opts.props).forEach(function (key) { node[key] = opts.props[key]; });
    }
    if (opts.on) {
      Object.keys(opts.on).forEach(function (ev) { node.addEventListener(ev, opts.on[ev]); });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        if (typeof child === 'string') node.appendChild(document.createTextNode(child));
        else if (child.nodeType) node.appendChild(child);
      });
    }
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function withToken(url) {
    if (!TOKEN) return url;
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  }

  function dirname(path) {
    if (!path) return null;
    var trimmed = String(path);
    var cut = trimmed.lastIndexOf('/');
    if (cut < 0) return null;
    if (cut === 0) return '/';
    return trimmed.slice(0, cut);
  }

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return '—';
    var value = Number(bytes);
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var idx = 0;
    while (value >= 1024 && idx < units.length - 1) { value /= 1024; idx += 1; }
    return (idx === 0 ? String(Math.round(value)) : value.toFixed(value >= 100 ? 0 : 1)) + ' ' + units[idx];
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
    var total = Math.max(0, Math.round(Number(seconds)));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    var pad = function (n) { return n < 10 ? '0' + n : String(n); };
    return (h > 0 ? h + ':' : '') + pad(m) + ':' + pad(s);
  }

  function fmtClock(epoch) {
    if (!epoch) return '—';
    try {
      return new Date(epoch * 1000).toLocaleString('en-US', { hour12: false });
    } catch (e) {
      return new Date(epoch * 1000).toLocaleString();
    }
  }

  function num(value) {
    if (value === null || value === undefined || value === '') return '—';
    return String(value);
  }

  function utf8Len(str) {
    if (!str) return 0;
    if (window.TextEncoder) return new TextEncoder().encode(str).length;
    /* Fallback for old engines: count UTF-8 bytes manually. */
    var bytes = 0;
    for (var i = 0; i < str.length; i += 1) {
      var code = str.charCodeAt(i);
      if (code < 0x80) bytes += 1;
      else if (code < 0x800) bytes += 2;
      else if (code >= 0xD800 && code <= 0xDBFF) { bytes += 4; i += 1; }
      else bytes += 3;
    }
    return bytes;
  }

  var dom = {};

  var app = {
    server: null,
    run: null,
    preflight: null,
    draft: null,
    startBusy: false,
    preflightBusy: false,
    logOffset: 0,
    logLines: [],
    logPartial: '',
    logRendered: 0,
    logHasContent: false,
    logFilter: '',
    logAutoScroll: true,
    filterTimer: null,
    lastRunId: undefined,
    sse: null,
    sseRetry: 0,
    reconnectTimer: null,
    elapsedTimer: null,
    draftSaveTimer: null,
    reportFrameUrl: null,
    modal: null,
    authFailed: false,
    lastStateAt: 0,
    pollTimer: null,
    stateFetch: null,
    terminalRefreshTimer: null,
    watchdogTimer: null
  };

  /* ---------------------------------------------------------------- draft */

  function newSample() {
    return { name: '', fq1: '', fq2: '', species: '', strain: '', reference: '' };
  }

  function normalizeSample(raw) {
    var sample = newSample();
    if (!raw || typeof raw !== 'object') return sample;
    Object.keys(sample).forEach(function (key) {
      if (typeof raw[key] === 'string') sample[key] = raw[key];
    });
    return sample;
  }

  function defaultDraft() {
    return {
      samples: [newSample()],
      mode: 'resume',
      modeTouched: false,
      clean: false,
      profile: 'standard',
      report_tag: ''
    };
  }

  function loadDraft() {
    var raw = null;
    try { raw = window.localStorage.getItem(DRAFT_KEY); } catch (e) { raw = null; }
    if (!raw) return null;
    var parsed = null;
    try { parsed = JSON.parse(raw); } catch (e) { return null; }
    if (!parsed || typeof parsed !== 'object') return null;
    var draft = defaultDraft();
    if (Array.isArray(parsed.samples)) draft.samples = parsed.samples.map(normalizeSample);
    if (!draft.samples.length) draft.samples = [newSample()];
    if (parsed.mode === 'fresh' || parsed.mode === 'resume') draft.mode = parsed.mode;
    draft.modeTouched = parsed.modeTouched === true;
    draft.clean = parsed.clean === true;
    if (parsed.profile === 'lowmem') draft.profile = 'lowmem';
    if (typeof parsed.report_tag === 'string') draft.report_tag = parsed.report_tag;
    return draft;
  }

  function saveDraftSoon() {
    if (app.draftSaveTimer) window.clearTimeout(app.draftSaveTimer);
    app.draftSaveTimer = window.setTimeout(saveDraftNow, 220);
  }

  function saveDraftNow() {
    app.draftSaveTimer = null;
    try { window.localStorage.setItem(DRAFT_KEY, JSON.stringify(app.draft)); } catch (e) { /* private mode */ }
  }

  /* ------------------------------------------------------------------ API */

  function showAuthOverlay() {
    if (app.authFailed) return;
    app.authFailed = true;
    dom.authOverlay.hidden = false;
    setConn(false);
    if (app.sse) { try { app.sse.close(); } catch (e) { /* noop */ } app.sse = null; }
    if (app.elapsedTimer) { window.clearInterval(app.elapsedTimer); app.elapsedTimer = null; }
  }

  function api(path, options) {
    options = options || {};
    var headers = { Accept: 'application/json' };
    if (TOKEN) headers['X-UI-Token'] = TOKEN;
    var init = {
      method: options.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      cache: 'no-store'
    };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    return window.fetch(path, init).then(function (response) {
      return response.text().then(function (raw) {
        var data = null;
        if (raw) {
          try { data = JSON.parse(raw); } catch (e) { data = null; }
        }
        if (response.status === 403) showAuthOverlay();
        if (!response.ok) {
          var message = (data && (data.message || data.error)) || ('HTTP ' + response.status);
          throw { status: response.status, body: data, message: message };
        }
        return data;
      });
    }, function () {
      throw { status: 0, body: null, message: 'Cannot reach the local server (this page may not be served by bin/pipeline-ui)' };
    });
  }

  /* ---------------------------------------------------------------- status */

  function projectPath() {
    var server = app.server || {};
    var candidates = [server.project, server.project_dir, server.project_root, server.root, server.cwd, server.workdir];
    for (var i = 0; i < candidates.length; i += 1) {
      if (typeof candidates[i] === 'string' && candidates[i]) return candidates[i];
    }
    return null;
  }

  function nextflowVersion() {
    var checks = app.preflight && app.preflight.checks;
    if (!checks) return null;
    for (var i = 0; i < checks.length; i += 1) {
      if (checks[i] && checks[i].id === 'nextflow' && checks[i].detail) {
        var first = String(checks[i].detail).split(/[\s(]/)[0];
        if (first && first !== '—') return first;
      }
    }
    return null;
  }

  function renderStatus() {
    var state = app.run && app.run.state ? app.run.state : 'idle';
    var info = STATUS[state] || STATUS.idle;
    dom.statusBadge.textContent = info.label;
    dom.statusBadge.className = 'badge ' + info.cls;
    dom.statusBadge.setAttribute('data-state', state);

    var project = projectPath();
    dom.projectPath.textContent = project || '—';
    dom.projectPath.title = project || 'The backend did not report the project root in the /api/state server field';

    var server = app.server || {};
    var port = server.port;
    var version = server.version || server.app_version || nextflowVersion();
    dom.serverMeta.textContent = 'Port ' + num(port) + ' · '
      + (version ? 'Nextflow ' + String(version) : 'version —');

    var host = server.host || server.bind || '';
    var loopback = ['127.0.0.1', 'localhost', '::1', '[::1]'].indexOf(String(host)) >= 0;
    var lan = server.non_loopback === true || server.lan === true || (host && !loopback);
    if (lan) {
      dom.lanBanner.hidden = false;
      dom.lanBanner.textContent = 'Warning: the server is listening on a non-loopback address (' + host + '); other machines on the same LAN may be able to open this page. '
        + 'This UI is meant for a single local user; start it on 127.0.0.1.';
    } else {
      dom.lanBanner.hidden = true;
      dom.lanBanner.textContent = '';
    }
  }

  function setConn(connected) {
    dom.connBadge.textContent = connected ? 'Log stream connected' : 'Log stream disconnected';
    dom.connBadge.className = 'conn ' + (connected ? 'conn-on' : 'conn-off');
  }

  /* --------------------------------------------------------------- samples */

  function sampleField(label, field, value, placeholder) {
    var input = el('input', {
      attrs: {
        type: 'text',
        'data-field': field,
        autocomplete: 'off',
        spellcheck: 'false',
        placeholder: placeholder || ''
      }
    });
    input.value = value || '';
    return el('label', { class: 'field' }, [
      el('span', { class: 'field-label', text: label }),
      input
    ]);
  }

  function sampleFieldWithBrowse(label, field, value, placeholder) {
    var input = el('input', {
      attrs: {
        type: 'text',
        'data-field': field,
        autocomplete: 'off',
        spellcheck: 'false',
        placeholder: placeholder || ''
      }
    });
    input.value = value || '';
    var browse = el('button', {
      class: 'btn btn-small',
      attrs: { type: 'button', 'data-act': 'browse', 'data-field': field, 'aria-label': 'Browse ' + label },
      text: 'Browse'
    });
    return el('label', { class: 'field' }, [
      el('span', { class: 'field-label', text: label }),
      el('div', { class: 'input-row' }, [input, browse])
    ]);
  }

  function buildSampleCard(sample, index) {
    var number = index + 1;
    var card = el('div', {
      class: 'sample-card',
      attrs: { 'data-idx': String(index), 'aria-label': 'Sample ' + number }
    });
    card.appendChild(el('div', { class: 'sample-card-head' }, [
      el('span', { class: 'sample-idx', text: 'Sample ' + number }),
      el('button', {
        class: 'btn-remove',
        attrs: {
          type: 'button', 'data-act': 'remove', 'data-idx': String(index),
          title: 'Remove sample ' + number, 'aria-label': 'Remove sample ' + number
        },
        text: '✕'
      })
    ]));
    card.appendChild(sampleField('Sample name', 'name', sample.name, 'e.g. SRR1770413'));
    card.appendChild(sampleFieldWithBrowse('R1', 'fq1', sample.fq1, 'Server absolute path, e.g. /…/x_1.fastq.gz'));
    card.appendChild(sampleFieldWithBrowse('R2', 'fq2', sample.fq2, 'Server absolute path'));
    card.appendChild(el('div', { class: 'sample-grid-2' }, [
      sampleField('Species', 'species', sample.species, 'e.g. Escherichia coli'),
      sampleField('Strain', 'strain', sample.strain, 'e.g. K-12')
    ]));
    card.appendChild(sampleFieldWithBrowse('Reference', 'reference', sample.reference, 'Optional: absolute FASTA path'));
    card.appendChild(el('p', { class: 'field-error sample-error', props: { hidden: true } }));
    return card;
  }

  function renderSamples() {
    clear(dom.sampleList);
    app.draft.samples.forEach(function (sample, index) {
      dom.sampleList.appendChild(buildSampleCard(sample, index));
    });
    dom.sampleCount.textContent = String(app.draft.samples.length);
    dom.sampleEmptyHint.hidden = app.draft.samples.length > 0;
    applyLock();
  }

  function cardAt(index) {
    return dom.sampleList.querySelector('.sample-card[data-idx="' + index + '"]');
  }

  function addSample() {
    app.draft.samples.push(newSample());
    saveDraftSoon();
    renderSamples();
    var last = app.draft.samples.length - 1;
    var card = cardAt(last);
    if (card) {
      var input = card.querySelector('input[data-field="name"]');
      if (input) { input.focus(); }
      card.scrollIntoView({ block: 'nearest' });
    }
  }

  function removeSample(index) {
    if (index < 0 || index >= app.draft.samples.length) return;
    app.draft.samples.splice(index, 1);
    if (!app.draft.samples.length) app.draft.samples.push(newSample());
    saveDraftSoon();
    renderSamples();
  }

  function applyLock() {
    var locked = !!(app.run && app.run.state === 'running');
    if (dom.runOptions) dom.runOptions.disabled = locked;
    var controls = dom.sampleList.querySelectorAll('input, button');
    for (var i = 0; i < controls.length; i += 1) controls[i].disabled = locked;
    dom.btnAddSample.disabled = locked;
    dom.sampleList.classList.toggle('is-locked', locked);
    dom.btnRun.disabled = locked || app.startBusy || app.draft.samples.length === 0;
    dom.btnRun.textContent = app.startBusy ? 'Starting…' : 'Start run';
    dom.btnCancel.disabled = !locked;
    dom.btnCancel.title = locked ? 'Cancel the current run (asks for confirmation)' : 'Only a run started by this UI can be cancelled';
  }

  /* --------------------------------------------------------------- options */

  function setInputValue(node, value) {
    if (!node || document.activeElement === node) return;
    if (node.value !== value) node.value = value;
  }

  function renderOptions() {
    var radios = document.querySelectorAll('input[name="mode"]');
    for (var i = 0; i < radios.length; i += 1) radios[i].checked = radios[i].value === app.draft.mode;
    dom.cleanCheck.checked = app.draft.clean === true;
    setInputValue(dom.profileSelect, app.draft.profile);
    setInputValue(dom.reportTag, app.draft.report_tag || '');
    renderCleanPlan();
  }

  function renderCleanPlan() {
    var show = app.draft.clean === true;
    dom.cleanPlan.hidden = !show;
    if (!show) return;
    var existing = app.preflight && app.preflight.existing;
    var plan = existing && existing.clean_plan;
    if (plan && plan.text) {
      dom.cleanPlanText.textContent = plan.text;
    } else {
      dom.cleanPlanText.textContent = '(No preflight result yet, or the backend returned no plan. Click “Preflight” '
        + 'in the top bar to show the deletion list; a run will still execute bin/pipeline-clean outputs --yes and ask for confirmation.)';
    }
  }

  function applyDefaultMode(preflight) {
    if (!preflight || app.draft.modeTouched) return;
    var existing = preflight.existing || {};
    var hasHistory = !!(existing.report || (existing.results_bytes && existing.results_bytes > 0));
    var mode = hasHistory ? 'resume' : 'fresh';
    if (app.draft.mode !== mode) {
      app.draft.mode = mode;
      saveDraftSoon();
      renderOptions();
    }
  }

  /* ------------------------------------------------------------- preflight */

  function setPreflightBusy(busy) {
    app.preflightBusy = busy;
    dom.btnPreflight.disabled = busy;
    dom.btnPreflight.textContent = busy ? 'Checking…' : 'Preflight';
    dom.btnPreflight2.disabled = busy;
    dom.btnPreflight2.textContent = busy ? 'Checking…' : 'Re-run preflight';
  }

  function loadPreflight(initial) {
    if (app.preflightBusy) return Promise.resolve();
    setPreflightBusy(true);
    return api('/api/preflight').then(function (data) {
      app.preflight = data || null;
      if (initial) applyDefaultMode(app.preflight);
      renderPreflight();
      renderCleanPlan();
      renderResults();
      renderStatus();
      return data;
    }).catch(function (err) {
      if (err.status === 403) return;
      toast('Preflight failed: ' + (err.message || 'unknown error'), 'error');
      dom.preflightSummary.textContent = 'Preflight failed';
    }).then(function () {
      setPreflightBusy(false);
    });
  }

  function renderPreflight() {
    var data = app.preflight;
    clear(dom.preflightList);
    if (!data || !Array.isArray(data.checks)) {
      dom.preflightList.appendChild(el('li', { class: 'check-item status-unknown' }, [
        el('span', { class: 'check-icon', text: '·' }),
        el('span', { class: 'check-label', text: 'Not checked yet' }),
        el('span', { class: 'check-detail', text: 'Click “Preflight” to check the local environment (about 1-3 s)' })
      ]));
      dom.preflightSummary.textContent = 'Not checked yet';
      dom.preflightFoot.textContent = 'Preflight is advisory only: a passing preflight does not guarantee the pipeline will succeed.';
      return;
    }

    data.checks.forEach(function (check) {
      var status = check.status === 'ok' || check.status === 'warn' || check.status === 'fail' ? check.status : 'unknown';
      var icon = status === 'ok' ? '✓' : status === 'warn' ? '⚠' : status === 'fail' ? '✗' : '·';
      var item = el('li', { class: 'check-item status-' + status }, [
        el('span', { class: 'check-icon', attrs: { 'aria-hidden': 'true' }, text: icon }),
        el('span', { class: 'check-label', text: check.label || check.id || '—' }),
        el('span', { class: 'check-detail', text: check.detail || '' })
      ]);
      if (check.hint) {
        item.appendChild(el('span', { class: 'check-hint', text: check.hint }));
      }
      dom.preflightList.appendChild(item);
    });

    var summary = data.summary || {};
    var failures = summary.failures || 0;
    var warnings = summary.warnings || 0;
    var summaryText;
    if (failures > 0) summaryText = '✗ ' + failures + ' failed · ⚠ ' + warnings + ' warning(s)';
    else if (warnings > 0) summaryText = '✓ Ready to run · ⚠ ' + warnings + ' warning(s)';
    else summaryText = '✓ All checks passed';
    dom.preflightSummary.textContent = summaryText;

    var config = data.config || {};
    var foot = [];
    if (config.source === 'fallback') {
      foot.push('Config parsing failed; built-in default paths are used (source=fallback), so report discovery may be inaccurate.');
    } else if (config.source) {
      foot.push('Config source: ' + config.source + '。');
    }
    var disk = data.disk || {};
    if (disk.free_gb !== undefined && disk.free_gb !== null) {
      foot.push('Free disk ' + disk.free_gb + ' GiB (requires ≥' + num(disk.required_gb) + ' GiB).');
    }
    foot.push('Preflight is advisory only: a passing preflight does not guarantee the pipeline will succeed.');
    dom.preflightFoot.textContent = foot.join(' ');
  }

  /* ---------------------------------------------------------------- ladder */

  function runSamplesForLadder() {
    if (app.run && Array.isArray(app.run.samples) && app.run.samples.length) return app.run.samples.slice();
    var names = app.draft.samples.map(function (sample, index) {
      var name = (sample.name || '').trim();
      return name || ('Sample ' + (index + 1));
    });
    return names;
  }

  function stepMap() {
    var map = {};
    var list = app.run && Array.isArray(app.run.steps) ? app.run.steps : [];
    list.forEach(function (step) {
      if (!step || !step.process) return;
      var key = String(step.process).toUpperCase();
      if (!map[key]) map[key] = step;
    });
    return map;
  }

  function renderLadder() {
    var samples = runSamplesForLadder();
    var map = stepMap();
    var head = dom.ladderHead;
    var body = dom.ladderBody;
    clear(head);
    clear(body);
    if (!samples.length) samples = ['—'];

    var headRow = el('tr', null, [el('th', { class: 'col-step', attrs: { scope: 'col' }, text: 'process' })]);
    samples.forEach(function (name) {
      headRow.appendChild(el('th', { class: 'col-sample', attrs: { scope: 'col', title: name }, text: name }));
    });
    headRow.appendChild(el('th', { class: 'col-count', attrs: { scope: 'col' }, text: 'Done/Expected' }));
    head.appendChild(headRow);

    var seen = {};
    PROCESS_ORDER.forEach(function (processName) { seen[processName] = true; });
    var extra = Object.keys(map).filter(function (key) { return !seen[key]; }).sort();
    var rows = PROCESS_ORDER.concat(extra);

    rows.forEach(function (processName) {
      var step = map[processName];
      var row = el('tr');
      var nameCell = el('td', null, [
        el('span', {
          class: 'step-name' + (AGGREGATE[processName] ? ' is-agg' : ''),
          text: processName
        })
      ]);
      if (step && step.failed > 0) nameCell.appendChild(el('span', { class: 'step-bad', text: '✗' + step.failed }));
      if (step && step.running > 0) nameCell.appendChild(el('span', { class: 'step-run', text: '▶' + step.running }));
      row.appendChild(nameCell);

      if (AGGREGATE[processName]) {
        var aggregateText = '—';
        var aggregateClass = 'cell-agg cell-na';
        if (step) {
          var expected = step.expected;
          var completed = step.completed || 0;
          aggregateText = completed + '/' + num(expected);
          if (expected && completed >= expected) aggregateClass = 'cell-agg cell-done';
          else if (completed > 0) aggregateClass = 'cell-agg cell-part';
          else aggregateClass = 'cell-agg cell-empty';
          if (step.failed > 0) aggregateText = '✗ ' + aggregateText;
        }
        row.appendChild(el('td', {
          class: aggregateClass,
          attrs: { colspan: String(samples.length), title: processName + ' is a cohort-wide aggregate task, not one task per sample' },
          text: aggregateText
        }));
      } else {
        var bySample = (step && step.by_sample) || {};
        var perSampleExpected = PER_SAMPLE_EXPECTED[processName] || null;
        var optionalDone = processName === 'ASSEMBLY_REF' && step
          && typeof step.expected === 'number' && typeof step.completed === 'number'
          && step.expected < samples.length && step.completed >= step.expected;
        samples.forEach(function (sampleName) {
          var count = Object.prototype.hasOwnProperty.call(bySample, sampleName) ? bySample[sampleName] : null;
          var cellClass = 'cell cell-na';
          var cellText = '—';
          var title = processName + ' / ' + sampleName;
          if (optionalDone && !count) {
            title += ': no reference for this sample (ASSEMBLY_REF is optional)';
          } else if (count !== null && count !== undefined) {
            if (perSampleExpected && count >= perSampleExpected) {
              cellClass = 'cell cell-done';
              cellText = '✓';
            } else if (count > 0) {
              cellClass = 'cell cell-part';
              cellText = count + '/' + num(perSampleExpected);
            } else if (step && step.failed > 0) {
              cellClass = 'cell cell-fail';
              cellText = '✗';
            } else {
              cellClass = 'cell cell-empty';
              cellText = '·';
            }
            title += ': ' + count + ' task(s) completed' + (perSampleExpected ? ' (expected ' + perSampleExpected + ')' : '');
          } else {
            title += ': no progress data yet';
          }
          row.appendChild(el('td', { class: cellClass, attrs: { title: title }, text: cellText }));
        });
      }

      var countText = step ? String(step.completed || 0) + '/' + num(step.expected) : '—';
      var countCell = el('td', { class: 'row-count', text: countText });
      if (step && step.failed > 0) countCell.appendChild(el('span', { class: 'rc-bad', text: ' ✗' + step.failed }));
      row.appendChild(countCell);
      body.appendChild(row);
    });

    var expected = app.run && app.run.tasks ? app.run.tasks.expected : null;
    dom.ladderHint.textContent = '12 processes · 11 tasks per sample · +1 per reference'
      + (expected !== null && expected !== undefined ? ' · ' + expected + ' tasks expected this run' : '');
    dom.ladderHint.title = 'Expected tasks = 11×N + references + 2: FASTQC_RAW×2 and FASTQC_TRIMMED×2, '
      + 'one each of TRIMMOMATIC/SPADES/QUAST/BUSCO/BAKTA/CARVEME/MEMOTE, one ASSEMBLY_REF per reference, '
      + 'and one each of GROWTH_CHECK and MULTIQC (aggregate tasks).';
  }

  /* --------------------------------------------------------------- counters */

  function countValue(value) {
    return value === null || value === undefined ? '—' : String(value);
  }

  function syncRunButtons() {
    applyLock();
  }

  /* --------------------------------------------------------------- progress */

  /* One glanceable model for the overall progress bar: percentage from the
   * task counters, the stage that is running right now, and a terminal colour.
   * The 12-stage ladder and the four counters stay as the detailed view. */
  function progressModel() {
    var model = {
      state: 'idle',
      pct: 0,
      caption: 'No run yet',
      stage: 'Waiting for the engine…',
      cls: 'is-idle'
    };
    var run = app.run;
    if (!run) return model;

    var state = run.state || 'idle';
    var tasks = run.tasks || {};
    var completed = Number(tasks.completed) || 0;
    var expected = Number(tasks.expected) || 0;
    var submitted = Number(tasks.submitted) || 0;
    var failed = Number(tasks.failed) || 0;
    var running = Math.max(0, submitted - completed - failed);
    var pct = expected > 0 ? Math.round((completed / expected) * 100) : 0;
    pct = Math.max(0, Math.min(100, pct));
    if (state === 'done') pct = 100;

    var runningSteps = [];
    var failedStep = null;
    (Array.isArray(run.steps) ? run.steps : []).forEach(function (step) {
      if (!step || !step.process) return;
      if (!failedStep && step.failed > 0) failedStep = step.process;
      if (step.running > 0) runningSteps.push(step.process);
    });

    var parts = [completed + ' / ' + expected + ' tasks', pct + '%'];
    if (running > 0) parts.push(running + ' running');
    if (failed > 0) parts.push(failed + ' failed');
    if (state === 'done') parts.push('complete');
    else if (state === 'failed') parts.push(failedStep ? 'failed at ' + failedStep : 'failed');
    else if (state === 'cancelled') parts.push('cancelled');
    else if (state === 'interrupted') parts.push('interrupted');

    model.state = state;
    model.pct = pct;
    model.caption = parts.join(' · ');
    if (state === 'running') model.cls = 'is-running';
    else if (TERMINAL_STATES[state]) model.cls = 'is-' + state;
    else model.cls = 'is-idle';

    if (state === 'done') model.stage = 'Finished';
    else if (state === 'failed') model.stage = failedStep ? 'Failed at ' + failedStep : 'Failed';
    else if (state === 'cancelled' || state === 'interrupted') model.stage = 'Stopped';
    else if (runningSteps.length) model.stage = runningSteps.join(' · ');
    else model.stage = 'Waiting for the engine…';
    return model;
  }

  function renderProgress() {
    if (!dom.overallProgress || !dom.progressFill) return;
    var model = progressModel();
    dom.overallProgress.setAttribute('aria-valuenow', String(model.pct));
    dom.overallProgress.setAttribute('aria-valuetext', model.caption);
    dom.overallProgress.className = 'progress ' + model.cls;
    dom.progressFill.style.width = model.pct + '%';
    dom.progressPct.textContent = app.run ? model.pct + '%' : '—';
    dom.progressCaption.textContent = model.caption;
    dom.currentStage.textContent = model.stage;
  }

  function runMetaText() {
    var run = app.run;
    if (!run) return 'No run yet';
    var parts = [];
    var info = STATUS[run.state] || STATUS.idle;
    parts.push(info.label);
    if (run.id) parts.push('#' + run.id);
    if (run.mode) parts.push(MODE_LABEL[run.mode] || run.mode);
    if (run.state === 'running' && run.started_at) {
      parts.push('elapsed ' + fmtDuration(Date.now() / 1000 - Number(run.started_at)));
    } else if (run.elapsed_seconds !== null && run.elapsed_seconds !== undefined) {
      parts.push('took ' + fmtDuration(run.elapsed_seconds));
    }
    if (run.exit_code !== null && run.exit_code !== undefined) parts.push('exit ' + run.exit_code);
    if (run.cancel_requested) parts.push('cancel requested');
    return parts.join(' · ');
  }

  function renderRunPanel() {
    var run = app.run;
    if (!run) {
      dom.runMeta.textContent = runMetaText();
      dom.countSubmitted.textContent = '—';
      dom.countCompleted.textContent = '—';
      dom.countFailed.textContent = '—';
      dom.countExpected.textContent = '—';
      dom.actionHint.textContent = app.preflight ? 'Fill in the samples, then start the run.' : 'Tip: run the preflight check first.';
    } else {
      dom.runMeta.textContent = runMetaText();

      var tasks = run.tasks || {};
      dom.countSubmitted.textContent = countValue(tasks.submitted);
      dom.countCompleted.textContent = countValue(tasks.completed);
      dom.countFailed.textContent = countValue(tasks.failed);
      dom.countExpected.textContent = countValue(tasks.expected);

      if (run.state === 'running') dom.actionHint.textContent = 'Running: the form is locked and the log streams live; cancelling asks for confirmation.';
      else if (run.state === 'done') dom.actionHint.textContent = 'Done: load the report lazily or download artifacts on the right.';
      else if (run.state === 'failed') dom.actionHint.textContent = 'Run failed: see the failure panel above for step / sample / exit code / stderr tail.';
      else if (run.state === 'cancelled') dom.actionHint.textContent = 'Cancelled: work/ is kept; switch to resume and start again.';
      else if (run.state === 'interrupted') dom.actionHint.textContent = 'Interrupted: the process is gone; use “Retry with -resume” to continue.';
      else dom.actionHint.textContent = '';
    }
    syncRunButtons();
    renderLadder();
    renderProgress();
  }

  function tickSecond() {
    if (app.run && app.run.state === 'running') dom.runMeta.textContent = runMetaText();
    renderStateUpdated();
  }

  /* ---------------------------------------------------------------- notice */

  function renderNotice() {
    var box = dom.noticePanel;
    clear(box);
    var run = app.run;
    var show = false;
    if (run && run.state === 'cancelled') {
      box.className = 'notice notice-warn';
      box.appendChild(el('strong', { text: 'Cancelled' }));
      box.appendChild(el('p', {
        text: 'The process group was terminated; work/ and results/ are kept. Switch to “Resume” and run again to reuse completed tasks.'
      }));
      show = true;
    }
    box.hidden = !show;
  }

  /* --------------------------------------------------------------- failure */

  function retryWithResume() {
    app.draft.mode = 'resume';
    app.draft.modeTouched = true;
    saveDraftSoon();
    renderOptions();
    startRun();
  }

  function renderFailure() {
    var box = dom.failurePanel;
    clear(box);
    var run = app.run;
    var state = run && run.state;
    if (state !== 'failed' && state !== 'interrupted') {
      box.hidden = true;
      return;
    }
    var failed = run.failed || {};
    var reason = failed.reason || (state === 'interrupted' ? 'interrupted' : 'task_failed');
    var title;
    var message;
    if (state === 'interrupted') {
      title = 'Run interrupted';
      message = run.message || failed.message
        || 'The previous run ended after the UI exited or was killed; the process is gone. Use “Retry with -resume” to converge.';
    } else if (reason === 'report_missing') {
      title = 'Run finished, but no report was found';
      message = 'The process exited with code 0, but report discovery failed (no match from the log report line, the configured directory, or the results/report glob). '
        + 'Per the contract this is failed, not done, so no green check is shown.';
    } else if (reason === 'clean_failed') {
      title = 'Clean failed; nextflow was not started';
      message = failed.message || 'bin/pipeline-clean outputs --yes exited non-zero, so the pipeline was not started.';
    } else {
      title = 'Run failed';
      message = failed.message || 'The pipeline failed with a non-zero exit code.';
    }

    box.appendChild(el('div', { class: 'panel-head' }, [
      el('h2', { text: '✗ ' + title }),
      el('span', { class: 'badge ' + (STATUS[state] ? STATUS[state].cls : 'badge-failed'), text: STATUS[state] ? STATUS[state].label : state })
    ]));
    box.appendChild(el('p', { class: 'failure-message', text: message }));

    var kv = el('dl', { class: 'failure-kv' });
    function addRow(label, valueNode) {
      kv.appendChild(el('dt', { text: label }));
      var dd = el('dd');
      if (typeof valueNode === 'string') dd.textContent = valueNode;
      else if (valueNode) dd.appendChild(valueNode);
      kv.appendChild(dd);
    }
    addRow('Step', failed.process ? el('code', { text: failed.process }) : '—');
    addRow('Sample', failed.tag ? el('code', { text: failed.tag }) : '—');
    var exitVal = failed.exit_code === null || failed.exit_code === undefined ? '—' : String(failed.exit_code);
    addRow('exit code', el('span', { class: 'bad-num', text: exitVal }));
    addRow('Reason', reason);
    addRow('Work directory', failed.work_dir ? el('code', { text: failed.work_dir }) : '—');
    box.appendChild(kv);

    if (failed.stderr_tail) {
      var details = el('details', { class: 'stderr-details', props: { open: true } }, [
        el('summary', { text: 'stderr tail (last 40 lines)' }),
        el('pre', { text: failed.stderr_tail })
      ]);
      box.appendChild(details);
    }

    var actions = el('div', { class: 'failure-actions' }, [
      el('button', {
        class: 'btn btn-primary',
        attrs: { type: 'button' },
        text: 'Retry with -resume',
        on: { click: retryWithResume }
      })
    ]);
    if (failed.work_dir) {
      actions.appendChild(el('button', {
        class: 'btn',
        attrs: { type: 'button' },
        text: 'Copy work directory',
        on: {
          click: function () { copyText(failed.work_dir); }
        }
      }));
    }
    box.appendChild(actions);
    box.hidden = false;
  }

  /* --------------------------------------------------------------- results */

  function currentReport() {
    if (app.run && app.run.report && app.run.report.path) {
      return { report: app.run.report, source: 'run' };
    }
    var existing = app.preflight && app.preflight.existing;
    if (existing && existing.report && existing.report.path) {
      return { report: existing.report, source: 'history' };
    }
    return { report: null, source: null };
  }

  function setAnchorEnabled(anchor, enabled) {
    anchor.setAttribute('aria-disabled', enabled ? 'false' : 'true');
    anchor.classList.toggle('is-disabled', !enabled);
  }

  function renderResults() {
    var current = currentReport();
    var report = current.report;
    var run = app.run;
    var hasResults = false;
    if (report) hasResults = true;
    else if (app.preflight && app.preflight.existing && app.preflight.existing.results_bytes) hasResults = true;

    /* ---- report panel ---- */
    var stateText = 'No run yet';
    if (run && run.state === 'running') stateText = report ? 'Running (previous report)' : 'Running (loadable when done)';
    else if (run && run.state === 'done') stateText = report ? 'Done' : 'Done, but the report is missing';
    else if (run && run.state === 'failed') stateText = report ? 'Failed (previous report kept)' : 'Failed; no report produced';
    else if (run && run.state === 'cancelled') stateText = 'Cancelled';
    else if (run && run.state === 'interrupted') stateText = 'Interrupted';
    else if (report) stateText = 'Report from the previous run';
    dom.reportState.textContent = stateText;

    dom.reportPath.textContent = report && report.path ? report.path : '—';
    dom.reportPath.title = report && report.path ? report.path : '';
    dom.reportSize.textContent = report && report.size !== undefined ? fmtBytes(report.size) : '—';
    dom.reportFound.textContent = report && report.found_at ? fmtClock(report.found_at) : '—';

    var canLoad = !!report;
    dom.btnLoadReport.disabled = !canLoad;
    dom.btnLoadReport.textContent = app.reportFrameUrl ? 'Reload report (~15 MB)' : 'Load report (~15 MB)';
    dom.btnDownloadReport.disabled = !canLoad;
    setAnchorEnabled(dom.linkOpenReport, canLoad);
    setAnchorEnabled(dom.linkDownloadResults, hasResults || canLoad);

    var reportUrl = withToken(report && report.url ? report.url : '/api/report');
    dom.linkOpenReport.href = reportUrl;
    dom.btnDownloadReport.setAttribute('data-url', withToken('/api/download?what=report'));
    dom.linkDownloadResults.href = withToken('/api/download?what=results');

    if (!report) {
      dom.reportHint.textContent = 'The report is a self-contained single-file MultiQC page (~15 MB). To save initial bandwidth the iframe loads only after a click.';
    } else if (current.source === 'history') {
      dom.reportHint.textContent = 'The current run has no report; this is the leftover report from the previous run (existing.report from /api/preflight).';
    } else {
      dom.reportHint.textContent = 'The report is a self-contained single-file MultiQC page. Once loaded you can interact with it in the iframe below, or open it in a new tab.';
    }

    if (app.reportFrameUrl && (!report || app.reportFrameUrl !== reportUrl)) {
      unloadReportFrame();
    }

    /* ---- artifacts ---- */
    dom.artifactRunId.textContent = run && run.id ? run.id : '—';
    dom.artifactLog.textContent = run && run.log ? run.log : '—';
    dom.artifactSheet.textContent = run && run.sheet ? run.sheet : '—';

    var nfPaths = nextflowArtifactPaths();
    dom.artifactNfReport.textContent = nfPaths.report || '—';
    dom.artifactNfTrace.textContent = nfPaths.trace || '—';

    var logLink = dom.btnLogDownload;
    if (run && run.log) {
      logLink.href = withToken('/api/download?what=log');
      logLink.classList.remove('is-disabled');
      logLink.removeAttribute('aria-disabled');
    } else {
      logLink.href = '#';
      logLink.classList.add('is-disabled');
      logLink.setAttribute('aria-disabled', 'true');
    }
  }

  function nextflowArtifactPaths() {
    var report = app.run && app.run.report;
    var base = null;
    if (report && report.path) base = dirname(report.path);
    else if (app.preflight && app.preflight.config && app.preflight.config.multiqc_dir) {
      base = app.preflight.config.multiqc_dir;
    }
    if (!base) return { report: null, trace: null };
    var root = base.replace(/\/+$/, '');
    return { report: root + '/nextflow/report.html', trace: root + '/nextflow/trace.tsv' };
  }

  function loadReportFrame() {
    var current = currentReport();
    if (!current.report) return;
    var url = withToken(current.report.url || '/api/report');
    app.reportFrameUrl = url;
    dom.reportFrame.src = url;
    dom.reportFrameWrap.hidden = false;
    dom.btnLoadReport.textContent = 'Reload report (~15 MB)';
  }

  function unloadReportFrame() {
    app.reportFrameUrl = null;
    dom.reportFrame.removeAttribute('src');
    dom.reportFrameWrap.hidden = true;
    dom.btnLoadReport.textContent = 'Load report (~15 MB)';
  }

  function triggerDownload(url) {
    if (!url) return;
    var anchor = document.createElement('a');
    anchor.href = url;
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  }

  /* ------------------------------------------------------------------- log */

  function logMatches(line) {
    if (!app.logFilter) return true;
    return line.toLowerCase().indexOf(app.logFilter.toLowerCase()) >= 0;
  }

  function ensureLogPlaceholderGone() {
    if (app.logHasContent) return;
    app.logHasContent = true;
    clear(dom.log);
    app.logRendered = 0;
  }

  function appendRenderedLines(lines) {
    if (!lines.length) return;
    var fragment = document.createDocumentFragment();
    lines.forEach(function (line) {
      var cls = 'log-line' + (LOG_ERR_RE.test(line) ? ' log-err' : '');
      fragment.appendChild(el('span', { class: cls, text: line }));
    });
    dom.log.appendChild(fragment);
    app.logRendered += lines.length;
    if (app.logAutoScroll) dom.log.scrollTop = dom.log.scrollHeight;
  }

  function renderLogFiltered() {
    ensureLogPlaceholderGone();
    clear(dom.log);
    app.logRendered = 0;
    var matches = app.logLines.filter(logMatches);
    appendRenderedLines(matches);
    updateLogMeta();
  }

  function scheduleFilterRender() {
    if (app.filterTimer) window.clearTimeout(app.filterTimer);
    app.filterTimer = window.setTimeout(function () {
      app.filterTimer = null;
      renderLogFiltered();
    }, 120);
  }

  function appendLogText(text) {
    if (!text) return;
    if (!app.logHasContent) app.logHasContent = true;
    var combined = app.logPartial + text;
    var parts = combined.split('\n');
    app.logPartial = parts.pop();
    if (!parts.length) return;

    if (!app.logHasContent || dom.log.querySelector('.log-placeholder')) {
      clear(dom.log);
      app.logRendered = 0;
    }
    parts.forEach(function (line) { app.logLines.push(line); });

    var overflow = app.logLines.length - MAX_LOG_LINES;
    if (overflow > 0) {
      app.logLines.splice(0, overflow);
      if (!app.logFilter) {
        var removeCount = Math.min(overflow, app.logRendered);
        for (var i = 0; i < removeCount; i += 1) {
          if (dom.log.firstChild) dom.log.removeChild(dom.log.firstChild);
          app.logRendered -= 1;
        }
      } else {
        renderLogFiltered();
        return;
      }
    }

    if (app.logFilter) {
      var matched = parts.filter(logMatches);
      appendRenderedLines(matched);
    } else {
      appendRenderedLines(parts);
    }
    updateLogMeta();
  }

  function ingestLogChunk(data) {
    if (!data || typeof data !== 'object') return;
    var text = typeof data.text === 'string' ? data.text : '';
    if (typeof data.from === 'number' && !isNaN(data.from)) {
      app.logOffset = Math.max(app.logOffset, data.from);
    }
    if (text) {
      app.logOffset += utf8Len(text);
      appendLogText(text);
    }
    updateLogMeta();
  }

  function updateLogMeta() {
    var total = app.logLines.length;
    var shown = app.logFilter ? app.logLines.filter(logMatches).length : total;
    dom.logMeta.textContent = app.run
      ? total + ' lines · ' + fmtBytes(app.logOffset) + ' read'
      : (total ? total + ' lines' : 'Waiting for a run');
    dom.logCount.textContent = app.logFilter
      ? shown + ' / ' + total + ' lines match'
      : total + ' lines (cap ' + MAX_LOG_LINES + ')';
  }

  function resetLogStream() {
    app.logOffset = 0;
    app.logLines = [];
    app.logPartial = '';
    app.logRendered = 0;
    app.logHasContent = false;
    clear(dom.log);
    dom.log.appendChild(el('span', { class: 'log-placeholder', text: 'Waiting for the run log…' }));
    updateLogMeta();
  }

  function copyText(text) {
    if (!text || text === '—') {
      toast('Nothing to copy', 'error');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast('Copied', 'ok');
      }, function () {
        fallbackCopy(text);
      });
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(area);
    toast(ok ? 'Copied' : 'Copy failed, please select the text manually', ok ? 'ok' : 'error');
  }

  function copyLog() {
    var lines = app.logFilter ? app.logLines.filter(logMatches) : app.logLines.slice();
    if (app.logPartial && logMatches(app.logPartial)) lines.push(app.logPartial);
    copyText(lines.join('\n'));
  }

  /* --------------------------------------------------------------- SSE/state */

  function handleState(payload, source) {
    if (!payload || typeof payload !== 'object') return;
    if (payload.server) app.server = payload.server;

    var run = payload.run || null;
    var newId = run && run.id ? run.id : null;
    if (app.lastRunId !== undefined && newId !== app.lastRunId) {
      /* A different run_id owns a different run.log: restart the log stream
       * from byte 0 so the beginning of the new file is not skipped. */
      resetLogStream();
      unloadReportFrame();
      if (app.sse) connectSSE();
    }
    var previousState = app.run && app.run.state ? app.run.state : null;
    app.lastRunId = newId;
    app.run = run;
    app.lastStateAt = Date.now();

    if (run && run.mode && !app.draft.modeTouched) {
      app.draft.mode = run.mode;
      saveDraftSoon();
    }

    /* The SSE snapshot can arrive before report discovery and exit_code are
     * recorded, so pull the authoritative state once more after a terminal
     * transition (source is informational only). */
    if (run && TERMINAL_STATES[run.state] && previousState !== run.state) {
      scheduleTerminalRefetch();
    }

    renderStatus();
    renderOptions();
    renderRunPanel();
    renderNotice();
    renderFailure();
    renderResults();
    updateLogMeta();
    renderStateUpdated();
  }

  /* ------------------------------------------------------ state fallbacks */

  /* Polling is the second, independent channel: SSE alone must never be the
   * reason the Run panel goes stale once a long run ends. */
  function pollState() {
    if (app.authFailed) return;
    if (document.visibilityState === 'hidden') return;
    refreshState().catch(function () { /* the next poll or the SSE retries */ });
  }

  function startStatePolling() {
    if (app.pollTimer) return;
    app.pollTimer = window.setInterval(pollState, STATE_POLL_MS);
  }

  function stopStatePolling() {
    if (!app.pollTimer) return;
    window.clearInterval(app.pollTimer);
    app.pollTimer = null;
  }

  function scheduleTerminalRefetch() {
    if (app.terminalRefreshTimer) window.clearTimeout(app.terminalRefreshTimer);
    app.terminalRefreshTimer = window.setTimeout(function () {
      app.terminalRefreshTimer = null;
      refreshState().catch(function () { /* best effort */ });
    }, TERMINAL_REFETCH_MS);
  }

  function renderStateUpdated() {
    if (!dom.stateUpdated) return;
    if (!app.lastStateAt) {
      dom.stateUpdated.textContent = 'state updated —';
      return;
    }
    var seconds = Math.max(0, Math.floor((Date.now() - app.lastStateAt) / 1000));
    dom.stateUpdated.textContent = 'state updated ' + seconds + 's ago';
  }

  function refreshState() {
    if (app.stateFetch) return app.stateFetch;
    app.stateFetch = api('/api/state').then(function (data) {
      app.stateFetch = null;
      app.server = data && data.server ? data.server : app.server;
      handleState({ server: app.server, run: data ? data.run : null }, 'fetch');
      return data;
    }, function (err) {
      app.stateFetch = null;
      throw err;
    });
    return app.stateFetch;
  }

  /* ------------------------------------------------------------- SSE stream */

  /* EventSource heartbeats are comment frames, which are invisible to JS, so
   * the watchdog resets on real events and treats prolonged silence as a dead
   * stream: close it and let the reconnect path refresh the state. */
  function bumpSseWatchdog() {
    if (app.watchdogTimer) window.clearTimeout(app.watchdogTimer);
    app.watchdogTimer = window.setTimeout(function () {
      app.watchdogTimer = null;
      onWatchdogTimeout();
    }, SSE_WATCHDOG_MS);
  }

  function clearSseWatchdog() {
    if (!app.watchdogTimer) return;
    window.clearTimeout(app.watchdogTimer);
    app.watchdogTimer = null;
  }

  function onWatchdogTimeout() {
    if (app.authFailed) return;
    setConn(false);
    if (app.sse) {
      try { app.sse.close(); } catch (e) { /* noop */ }
      app.sse = null;
    }
    app.sseRetry = 0;
    connectSSE();
  }

  function connectSSE() {
    if (app.authFailed) return;
    if (app.sse) {
      try { app.sse.close(); } catch (e) { /* noop */ }
      app.sse = null;
    }
    var url = '/api/events?from=' + app.logOffset;
    url = withToken(url);
    var source;
    try {
      source = new EventSource(url);
    } catch (e) {
      setConn(false);
      scheduleReconnect();
      return;
    }
    app.sse = source;
    bumpSseWatchdog();
    source.addEventListener('open', function () {
      app.sseRetry = 0;
      setConn(true);
      bumpSseWatchdog();
      /* A (re)connected stream may have missed a terminal transition. */
      refreshState().catch(function () { /* the poll timer retries */ });
    });
    source.addEventListener('state', function (event) {
      bumpSseWatchdog();
      var data = null;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      handleState(data, 'sse');
    });
    source.addEventListener('log', function (event) {
      bumpSseWatchdog();
      var data = null;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      ingestLogChunk(data);
    });
    source.addEventListener('error', function () {
      setConn(false);
      clearSseWatchdog();
      try { source.close(); } catch (e) { /* noop */ }
      if (app.sse === source) app.sse = null;
      if (!app.authFailed) scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    if (app.reconnectTimer) window.clearTimeout(app.reconnectTimer);
    var delay = Math.min(15000, Math.round(800 * Math.pow(1.7, app.sseRetry)));
    app.sseRetry = Math.min(app.sseRetry + 1, 8);
    app.reconnectTimer = window.setTimeout(function () {
      app.reconnectTimer = null;
      if (app.authFailed) return;
      connectSSE();
      refreshState().catch(function () { /* reconnect loop already handles it */ });
    }, delay);
  }

  /* ------------------------------------------------------------------ modal */

  function openModal(options) {
    var previousFocus = document.activeElement;
    var titleId = 'modal-title-' + Date.now();
    var backdrop = el('div', { class: 'modal-backdrop' });
    var modal = el('div', {
      class: 'modal' + (options.small ? ' modal-sm' : ''),
      attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId }
    });

    var head = el('div', { class: 'modal-head' }, [
      el('div', null, [
        el('h2', { attrs: { id: titleId }, text: options.title || 'Dialog' }),
        options.subtitle ? el('p', { text: options.subtitle }) : null
      ]),
      el('button', {
        class: 'btn-remove',
        attrs: { type: 'button', 'aria-label': 'Close dialog' },
        text: '✕',
        on: { click: function () { close(null); } }
      })
    ]);

    var body = el('div', { class: 'modal-body' }, [options.body || null]);
    var foot = el('div', { class: 'modal-foot' });
    (options.buttons || []).forEach(function (button) {
      foot.appendChild(el('button', {
        class: 'btn' + (button.primary ? ' btn-primary' : '') + (button.danger ? ' btn-danger' : ''),
        attrs: { type: 'button' },
        text: button.label,
        on: {
          click: function () {
            close(button.value !== undefined ? button.value : true);
          }
        }
      }));
    });

    modal.appendChild(head);
    modal.appendChild(body);
    modal.appendChild(foot);
    backdrop.appendChild(modal);
    document.getElementById('modal-root').appendChild(backdrop);
    document.body.classList.add('modal-open');

    var onKey = function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close(null);
        return;
      }
      if (event.key !== 'Tab') return;
      var focusables = modal.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])');
      if (!focusables.length) return;
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);

    function close(value) {
      document.removeEventListener('keydown', onKey, true);
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      document.body.classList.remove('modal-open');
      app.modal = null;
      if (previousFocus && previousFocus.focus) {
        try { previousFocus.focus(); } catch (e) { /* noop */ }
      }
      if (options.onClose) options.onClose(value);
    }

    app.modal = { close: close, element: modal };

    var focusTarget = body.querySelector('input, button, select');
    if (!focusTarget) focusTarget = foot.querySelector('button');
    window.setTimeout(function () {
      if (focusTarget && focusTarget.focus) focusTarget.focus();
    }, 0);

    return { close: close, element: modal };
  }

  function confirmDialog(options) {
    return new Promise(function (resolve) {
      var body = el('div');
      if (options.message) {
        body.appendChild(el('p', {
          class: 'modal-note' + (options.danger ? ' modal-note-warn' : ''),
          text: options.message
        }));
      }
      if (options.preText) {
        body.appendChild(el('pre', { class: 'modal-pre', text: options.preText }));
      }
      openModal({
        title: options.title || 'Please confirm',
        small: true,
        body: body,
        buttons: [
          { label: 'Cancel', value: false },
          { label: options.confirmLabel || 'Confirm', value: true, primary: true, danger: options.danger }
        ],
        onClose: function (value) { resolve(value === true); }
      });
    });
  }

  /* --------------------------------------------------------- directory browser */

  function browserStartPath(field, currentValue) {
    var config = (app.preflight && app.preflight.config) || {};
    if (currentValue) {
      var dir = dirname(currentValue);
      if (dir) return dir;
    }
    if (field === 'reference') return config.ref_dir || '/';
    return config.raw_dir || '/';
  }

  function openFsBrowser(options) {
    var state = { path: options.startPath || '/', data: null, manual: false };
    var body = el('div');
    body.appendChild(el('p', {
      class: 'modal-note',
      text: 'Files are not uploaded through the browser: this only lists server directories and fills the chosen absolute path into the form. Nothing is uploaded or copied.'
    }));

    var pathInput = el('input', {
      attrs: { type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Absolute path of a server directory or file' }
    });
    pathInput.value = state.path;
    var goButton = el('button', {
      class: 'btn',
      attrs: { type: 'button' },
      text: 'Go',
      on: { click: function () { load(pathInput.value.trim()); } }
    });
    pathInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        load(pathInput.value.trim());
      }
    });
    body.appendChild(el('div', { class: 'fs-path-row' }, [pathInput, goButton]));

    var shortcuts = el('div', { class: 'fs-shortcuts' });
    body.appendChild(shortcuts);

    var message = el('p', { class: 'fs-hint' });
    body.appendChild(message);
    var list = el('ul', { class: 'fs-list' });
    body.appendChild(list);

    var useButton = el('button', {
      class: 'btn btn-primary',
      attrs: { type: 'button' },
      text: 'Use this path',
      on: {
        click: function () {
          var value = (pathInput.value || '').trim();
          if (!value) { message.textContent = 'Enter an absolute path.'; message.className = 'fs-hint bad'; return; }
          finish(value);
        }
      }
    });

    function finish(path) {
      if (options.onPick) options.onPick(path);
      modal.close(true);
    }

    function setBusy(busy) {
      goButton.disabled = busy;
      goButton.textContent = busy ? 'Loading…' : 'Go';
    }

    function renderList(data) {
      clear(list);
      clear(shortcuts);
      (data.shortcuts || []).forEach(function (shortcut) {
        shortcuts.appendChild(el('button', {
          class: 'btn btn-small',
          attrs: { type: 'button' },
          text: shortcut.name,
          on: { click: function () { load(shortcut.path); } }
        }));
      });

      if (data.parent) {
        list.appendChild(el('li', null, [el('button', {
          class: 'fs-item fs-dir',
          attrs: { type: 'button' },
          text: '↵ Parent directory',
          on: { click: function () { load(data.parent); } }
        })]));
      }

      (data.dirs || []).forEach(function (dir) {
        list.appendChild(el('li', null, [el('button', {
          class: 'fs-item fs-dir',
          attrs: { type: 'button', title: dir.path },
          on: { click: function () { load(dir.path); } },
          text: null
        }, [
          el('span', { class: 'fs-name', text: dir.name + '/' }),
          el('span', { class: 'fs-meta', text: 'dir' })
        ])]));
      });

      var files = data.files || [];
      files.forEach(function (file) {
        var kind = file.kind || 'other';
        var chip = el('span', { class: 'kind-chip' + (kind === 'fasta' ? ' kind-fasta' : ''), text: kind });
        list.appendChild(el('li', null, [el('button', {
          class: 'fs-item',
          attrs: { type: 'button', title: file.path },
          on: { click: function () { finish(file.path); } }
        }, [
          el('span', { class: 'fs-name', text: file.name }),
          el('span', { class: 'fs-meta' }, [chip, el('span', { text: fmtBytes(file.size) })])
        ])]));
      });

      if (!data.dirs.length && !files.length) {
        list.appendChild(el('li', { class: 'fs-empty', text: 'No subdirectories or sequence files to show here.' }));
      }
    }

    function load(path) {
      if (!path) { path = '/'; }
      setBusy(true);
      message.textContent = '';
      message.className = 'fs-hint';
      api('/api/fs?path=' + encodeURIComponent(path)).then(function (data) {
        state.data = data;
        state.manual = false;
        var resolved = data && data.path ? data.path : path;
        pathInput.value = resolved;
        renderList(data || {});
        setBusy(false);
      }).catch(function (err) {
        setBusy(false);
        state.manual = true;
        message.textContent = 'Cannot open as a directory: ' + (err.message || 'unknown error')
          + '. If this is an absolute file path, click “Use this path” directly.';
        message.className = 'fs-hint bad';
        clear(list);
      });
    }

    var modal = openModal({
      title: 'Select a server file',
      subtitle: options.subtitle || 'Starting points: data/raw for FASTQ, data/reference for reference FASTA',
      body: body,
      buttons: [
        { label: 'Cancel', value: false }
      ],
      onClose: function () { /* nothing to clean up */ }
    });
    /* Replace the footer with [Cancel] [Use this path]. */
    var foot = modal.element.querySelector('.modal-foot');
    clear(foot);
    foot.appendChild(el('button', {
      class: 'btn',
      attrs: { type: 'button' },
      text: 'Cancel',
      on: { click: function () { modal.close(false); } }
    }));
    foot.appendChild(useButton);

    load(state.path);
  }

  /* -------------------------------------------------------------- validation */

  function pushError(errors, index, field, message) {
    errors.push({ index: index, field: field, message: message });
  }

  function validateDraft() {
    var errors = [];
    var seen = {};
    app.draft.samples.forEach(function (sample, index) {
      var name = (sample.name || '').trim();
      var fq1 = (sample.fq1 || '').trim();
      var fq2 = (sample.fq2 || '').trim();
      var reference = (sample.reference || '').trim();
      if (!name) pushError(errors, index, 'name', 'Sample name is required');
      else if (name === '.' || name === '..') pushError(errors, index, 'name', 'Sample name cannot be . or ..');
      else if (!SAMPLE_RE.test(name)) pushError(errors, index, 'name', 'Sample name may only contain letters, digits, dot, underscore and hyphen');
      else if (seen[name]) pushError(errors, index, 'name', 'Duplicate sample name: ' + name);
      else seen[name] = true;

      if (!fq1) pushError(errors, index, 'fq1', 'R1 path is required');
      else if (fq1.charAt(0) !== '/') pushError(errors, index, 'fq1', 'R1 must be an absolute path');
      if (!fq2) pushError(errors, index, 'fq2', 'R2 path is required');
      else if (fq2.charAt(0) !== '/') pushError(errors, index, 'fq2', 'R2 must be an absolute path');
      else if (fq1 && fq2 === fq1) pushError(errors, index, 'fq2', 'R2 must differ from R1');
      if (reference && reference.charAt(0) !== '/') pushError(errors, index, 'reference', 'Reference must be an absolute path');
      ['species', 'strain'].forEach(function (field) {
        if (/[\t\r\n]/.test(sample[field] || '')) pushError(errors, index, field, field + ' must not contain tabs or newlines');
      });
    });
    return errors;
  }

  function clearSampleErrors() {
    var cards = dom.sampleList.querySelectorAll('.sample-card');
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var inputs = card.querySelectorAll('input');
      for (var j = 0; j < inputs.length; j += 1) inputs[j].classList.remove('input-invalid');
      var errorBox = card.querySelector('.sample-error');
      if (errorBox) {
        clear(errorBox);
        errorBox.hidden = true;
      }
    }
  }

  function renderErrors(errors) {
    clearSampleErrors();
    var byIndex = {};
    errors.forEach(function (error) {
      var index = error.index;
      if (index === null || index === undefined || index < 0) {
        toast(error.message, 'error');
        return;
      }
      if (!byIndex[index]) byIndex[index] = [];
      byIndex[index].push(error);
      var card = cardAt(index);
      if (!card) return;
      var input = card.querySelector('input[data-field="' + error.field + '"]');
      if (input) input.classList.add('input-invalid');
    });
    Object.keys(byIndex).forEach(function (index) {
      var card = cardAt(Number(index));
      if (!card) return;
      var box = card.querySelector('.sample-error');
      if (!box) return;
      clear(box);
      byIndex[index].forEach(function (error) {
        box.appendChild(el('span', { class: 'field-error-line', text: error.message }));
      });
      box.hidden = false;
    });
  }

  function renderServerErrors(rawErrors) {
    var errors = Array.isArray(rawErrors) ? rawErrors : [];
    var mapped = [];
    errors.forEach(function (error) {
      var index = null;
      if (typeof error.row === 'number' && error.row >= 2) index = error.row - 2;
      mapped.push({
        index: index,
        field: error.field || 'name',
        message: error.message || ('Validation failed: ' + (error.code || 'invalid'))
      });
    });
    renderErrors(mapped);
    var first = mapped[0];
    if (first) toast('Sample sheet validation failed: ' + first.message, 'error');
  }

  /* ---------------------------------------------------------------- actions */

  function buildPayload() {
    return {
      samples: app.draft.samples.map(function (sample) {
        return {
          name: (sample.name || '').trim(),
          fq1: (sample.fq1 || '').trim(),
          fq2: (sample.fq2 || '').trim(),
          species: (sample.species || '').trim(),
          strain: (sample.strain || '').trim(),
          reference: (sample.reference || '').trim()
        };
      }),
      mode: app.draft.mode === 'fresh' ? 'fresh' : 'resume',
      clean: app.draft.clean === true,
      profile: app.draft.profile === 'lowmem' ? 'lowmem' : 'standard',
      report_tag: (app.draft.report_tag || '').trim()
    };
  }

  function startRun() {
    if (app.startBusy) return;
    if (app.run && app.run.state === 'running') { toast('A run is already in progress', 'error'); return; }

    var errors = validateDraft();
    if (errors.length) {
      renderErrors(errors);
      toast('Fix the sample rows first (' + errors.length + ' issue(s))', 'error');
      return;
    }
    clearSampleErrors();

    var proceed = function () {
      app.startBusy = true;
      syncRunButtons();
      api('/api/run', { method: 'POST', body: buildPayload() }).then(function (data) {
        toast('Run submitted' + (data && data.run_id ? ': ' + data.run_id : ''), 'ok');
        return refreshState();
      }).catch(function (err) {
        var detailErrors = null;
        if (err.body) {
          if (Array.isArray(err.body.errors)) detailErrors = err.body.errors;
          else if (err.body.detail && Array.isArray(err.body.detail.errors)) detailErrors = err.body.detail.errors;
        }
        if (err.status === 400 && detailErrors) {
          renderServerErrors(detailErrors);
        } else if (err.status === 409) {
          toast('Cannot start: ' + (err.message || 'a run or an external nextflow process is already active'), 'error');
          refreshState().catch(function () { /* nothing else to do */ });
        } else if (err.status === 403) {
          /* overlay already shown */
        } else {
          toast('Failed to start: ' + (err.message || 'unknown error'), 'error');
        }
      }).then(function () {
        app.startBusy = false;
        syncRunButtons();
      });
    };

    if (app.draft.clean) {
      var existing = app.preflight && app.preflight.existing;
      var plan = existing && existing.clean_plan;
      var run = function () {
        confirmDialog({
          title: 'Confirm cleaning outputs and starting',
          message: 'This first runs bin/pipeline-clean outputs --yes to delete the nextflow outputs listed below (irreversible), then starts the pipeline. '
            + 'data/, data/reference/ and repository files are not on the allow-list.',
          preText: plan && plan.text ? plan.text : '(The backend returned no clean plan; bin/pipeline-clean outputs --yes will still run.)',
          confirmLabel: 'Clean and run',
          danger: true
        }).then(function (ok) { if (ok) proceed(); });
      };
      if (app.preflight) run();
      else loadPreflight(false).then(run);
    } else {
      proceed();
    }
  }

  function cancelRun() {
    confirmDialog({
      title: 'Cancel run',
      message: 'SIGTERM is sent to the run process group; after 10 s without exit it gets SIGKILL. work/ and results/ are kept; '
        + 'afterwards you can switch to “Resume” and start again.',
      confirmLabel: 'Cancel run',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      api('/api/cancel', { method: 'POST' }).then(function () {
        toast('Cancel requested; waiting for the process to exit…', 'ok');
        refreshState().catch(function () { /* state event will follow */ });
      }).catch(function (err) {
        if (err.status === 409) toast('No active run started by this UI (an external nextflow process is never killed)', 'error');
        else toast('Cancel failed: ' + (err.message || 'unknown error'), 'error');
      });
    });
  }

  /* --------------------------------------------------------------- binding */

  function deriveSampleName(path) {
    if (!path) return '';
    var name = String(path).split('/').pop() || '';
    name = name.replace(/\.(fastq|fq)(\.gz)?$/i, '').replace(/\.gz$/i, '');
    name = name.replace(/[._-](R?1)$/i, '');
    return name;
  }

  function onSampleInput(event) {
    var input = event.target.closest ? event.target.closest('input[data-field]') : null;
    if (!input) return;
    var card = input.closest('.sample-card');
    if (!card) return;
    var index = Number(card.getAttribute('data-idx'));
    var field = input.getAttribute('data-field');
    if (!app.draft.samples[index]) return;
    app.draft.samples[index][field] = input.value;
    input.classList.remove('input-invalid');
    var errorBox = card.querySelector('.sample-error');
    if (errorBox) { clear(errorBox); errorBox.hidden = true; }
    saveDraftSoon();
  }

  function onSampleChange(event) {
    var input = event.target.closest ? event.target.closest('input[data-field]') : null;
    if (!input) return;
    var card = input.closest('.sample-card');
    if (!card) return;
    var index = Number(card.getAttribute('data-idx'));
    var field = input.getAttribute('data-field');
    var sample = app.draft.samples[index];
    if (!sample) return;
    if (field === 'fq1' && !(sample.name || '').trim()) {
      var derived = deriveSampleName(input.value.trim());
      if (derived) {
        sample.name = derived;
        var nameInput = card.querySelector('input[data-field="name"]');
        if (nameInput) nameInput.value = derived;
        saveDraftSoon();
      }
    }
  }

  function onSampleClick(event) {
    var target = event.target.closest ? event.target.closest('button[data-act]') : null;
    if (!target) return;
    var action = target.getAttribute('data-act');
    var card = target.closest('.sample-card');
    var index = Number(card ? card.getAttribute('data-idx') : -1);
    if (action === 'remove') {
      removeSample(index);
      return;
    }
    if (action === 'browse') {
      var field = target.getAttribute('data-field');
      var sample = app.draft.samples[index];
      if (!sample) return;
      var currentValue = field === 'name' ? '' : (sample[field] || '');
      openFsBrowser({
        startPath: browserStartPath(field, currentValue),
        subtitle: field === 'reference'
          ? 'Starting at data/reference; the reference is an optional FASTA'
          : 'Starting at data/raw; pick the FASTQ files (R1/R2)',
        onPick: function (path) {
          var cardNow = cardAt(index);
          if (!cardNow) return;
          var input = cardNow.querySelector('input[data-field="' + field + '"]');
          app.draft.samples[index][field] = path;
          if (input) {
            input.value = path;
            input.classList.remove('input-invalid');
          }
          if (field === 'fq1' && !(app.draft.samples[index].name || '').trim()) {
            var derived = deriveSampleName(path);
            if (derived) {
              app.draft.samples[index].name = derived;
              var nameInput = cardNow.querySelector('input[data-field="name"]');
              if (nameInput) nameInput.value = derived;
            }
          }
          saveDraftSoon();
        }
      });
    }
  }

  function bindEvents() {
    dom.btnAddSample.addEventListener('click', addSample);
    dom.sampleList.addEventListener('input', onSampleInput);
    dom.sampleList.addEventListener('change', onSampleChange);
    dom.sampleList.addEventListener('click', onSampleClick);

    document.querySelectorAll('input[name="mode"]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        if (!this.checked) return;
        app.draft.mode = this.value;
        app.draft.modeTouched = true;
        saveDraftSoon();
      });
    });

    dom.cleanCheck.addEventListener('change', function () {
      app.draft.clean = this.checked;
      saveDraftSoon();
      renderCleanPlan();
    });

    dom.profileSelect.addEventListener('change', function () {
      app.draft.profile = this.value;
      saveDraftSoon();
    });
    dom.reportTag.addEventListener('input', function () {
      app.draft.report_tag = this.value;
      saveDraftSoon();
    });

    dom.btnPreflight.addEventListener('click', function () { loadPreflight(false); });
    dom.btnPreflight2.addEventListener('click', function () { loadPreflight(false); });
    dom.btnRun.addEventListener('click', startRun);
    dom.btnCancel.addEventListener('click', cancelRun);

    dom.autoScroll.addEventListener('change', function () {
      app.logAutoScroll = this.checked;
      if (app.logAutoScroll) dom.log.scrollTop = dom.log.scrollHeight;
    });

    dom.logFilter.addEventListener('input', function () {
      app.logFilter = this.value.trim();
      scheduleFilterRender();
    });

    dom.btnLogCopy.addEventListener('click', copyLog);
    dom.btnLogDownload.addEventListener('click', function (event) {
      if (this.classList.contains('is-disabled') || this.getAttribute('aria-disabled') === 'true') {
        event.preventDefault();
        toast('There is no run log to download yet', 'error');
      }
    });

    dom.btnLoadReport.addEventListener('click', loadReportFrame);
    dom.btnUnloadReport.addEventListener('click', unloadReportFrame);
    dom.btnDownloadReport.addEventListener('click', function () {
      triggerDownload(this.getAttribute('data-url'));
    });
    dom.linkOpenReport.addEventListener('click', function (event) {
      if (this.getAttribute('aria-disabled') === 'true') {
        event.preventDefault();
        toast('There is no report to open yet', 'error');
      }
    });
    dom.linkDownloadResults.addEventListener('click', function (event) {
      if (this.getAttribute('aria-disabled') === 'true') {
        event.preventDefault();
        toast('There is nothing in results/ to download yet', 'error');
      }
    });

    document.addEventListener('click', function (event) {
      var button = event.target.closest ? event.target.closest('[data-copy]') : null;
      if (!button) return;
      var target = document.getElementById(button.getAttribute('data-copy'));
      copyText(target ? target.textContent.trim() : '');
    });

    /* Refresh fallbacks: pause polling while hidden, resume and refresh at
     * once when the tab becomes visible or the window regains focus. */
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') {
        stopStatePolling();
        return;
      }
      startStatePolling();
      if (app.authFailed) return;
      if (!app.sse) connectSSE();
      refreshState().catch(function () { /* handled by the reconnect loop */ });
    });

    window.addEventListener('focus', function () {
      if (document.visibilityState === 'hidden' || app.authFailed) return;
      refreshState().catch(function () { /* the poll timer retries */ });
    });

    window.addEventListener('beforeunload', function () {
      stopStatePolling();
      clearSseWatchdog();
      if (app.sse) { try { app.sse.close(); } catch (e) { /* noop */ } }
    });
  }

  /* ----------------------------------------------------------------- init */

  function cacheDom() {
    dom.statusBadge = $('status-badge');
    dom.connBadge = $('conn-badge');
    dom.projectPath = $('project-path');
    dom.serverMeta = $('server-meta');
    dom.btnPreflight = $('btn-preflight');
    dom.lanBanner = $('lan-banner');
    dom.sampleCount = $('sample-count');
    dom.btnAddSample = $('btn-add-sample');
    dom.sampleList = $('sample-list');
    dom.sampleEmptyHint = $('sample-empty-hint');
    dom.runOptions = $('run-options');
    dom.cleanCheck = $('clean-check');
    dom.cleanPlan = $('clean-plan');
    dom.cleanPlanText = $('clean-plan-text');
    dom.profileSelect = $('profile-select');
    dom.reportTag = $('report-tag');
    dom.failurePanel = $('failure-panel');
    dom.noticePanel = $('notice-panel');
    dom.preflightSummary = $('preflight-summary');
    dom.btnPreflight2 = $('btn-preflight-2');
    dom.preflightList = $('preflight-list');
    dom.preflightFoot = $('preflight-foot');
    dom.runMeta = $('run-meta');
    dom.stateUpdated = $('state-updated');
    dom.overallProgress = $('overall-progress');
    dom.progressFill = dom.overallProgress ? dom.overallProgress.querySelector('.progress-fill') : null;
    dom.progressPct = $('progress-pct');
    dom.progressCaption = $('progress-caption');
    dom.currentStage = $('current-stage');
    dom.btnRun = $('btn-run');
    dom.btnCancel = $('btn-cancel');
    dom.actionHint = $('action-hint');
    dom.countSubmitted = $('count-submitted');
    dom.countCompleted = $('count-completed');
    dom.countFailed = $('count-failed');
    dom.countExpected = $('count-expected');
    dom.ladderHead = $('ladder-head');
    dom.ladderBody = $('ladder-body');
    dom.ladderHint = $('ladder-hint');
    dom.logMeta = $('log-meta');
    dom.btnLogCopy = $('btn-log-copy');
    dom.btnLogDownload = $('btn-log-download');
    dom.autoScroll = $('auto-scroll');
    dom.logFilter = $('log-filter');
    dom.logCount = $('log-count');
    dom.log = $('log');
    dom.reportState = $('report-state');
    dom.reportPath = $('report-path');
    dom.reportSize = $('report-size');
    dom.reportFound = $('report-found');
    dom.btnLoadReport = $('btn-load-report');
    dom.linkOpenReport = $('link-open-report');
    dom.btnDownloadReport = $('btn-download-report');
    dom.linkDownloadResults = $('link-download-results');
    dom.reportHint = $('report-hint');
    dom.reportFrameWrap = $('report-frame-wrap');
    dom.reportFrame = $('report-frame');
    dom.btnUnloadReport = $('btn-unload-report');
    dom.artifactRunId = $('artifact-run-id');
    dom.artifactLog = $('artifact-log');
    dom.artifactSheet = $('artifact-sheet');
    dom.artifactNfReport = $('artifact-nf-report');
    dom.artifactNfTrace = $('artifact-nf-trace');
    dom.toastRegion = $('toast-region');
    dom.authOverlay = $('auth-overlay');
  }

  function init() {
    cacheDom();
    app.draft = loadDraft() || defaultDraft();

    bindEvents();
    renderSamples();
    renderOptions();
    renderStatus();
    renderLadder();
    renderRunPanel();
    renderResults();
    updateLogMeta();
    renderStateUpdated();

    app.elapsedTimer = window.setInterval(tickSecond, 1000);

    /* Independent of SSE: poll the state endpoint while the tab is visible. */
    startStatePolling();

    refreshState().then(function () {
      /* The token has served its purpose once the cookie is set; keep the
       * address bar clean so the secret is not in screenshots or history. */
      if (TOKEN) {
        try { window.history.replaceState(null, '', window.location.pathname + window.location.hash); } catch (e) { /* noop */ }
      }
    }).catch(function (err) {
      if (err.status === 403) return;
      toast(err.message || 'Cannot read run state', 'error');
    }).then(function () {
      connectSSE();
      loadPreflight(true);
    });

    if (dom.logFilter) dom.logFilter.value = '';
    dom.autoScroll.checked = app.logAutoScroll;
    updateLogMeta();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
