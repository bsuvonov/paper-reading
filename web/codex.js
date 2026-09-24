'use strict';
(() => {
  const cx = {open: false, sessionView: false, options: null, draft: {conferences: [], projects: [], papers: []}, scope: null,
    sessions: [], selected: null, terminal: null, fit: null, cursor: 0, generation: null,
    connection: 0, controller: null, input: '', sending: false, connecting: false,
    paperRows: [], paperRequest: 0, query: 0, running: false};
  const emptyScope = () => ({conferences: [], projects: [], papers: []});
  const projectName = id => cx.options?.projects.find(p => p.id === id)?.name || id;
  function error(message = '') {
    $('codex-error').textContent = message;
    $('codex-error').hidden = !message;
  }
  async function request(path, method = 'GET', body, signal) {
    if (!state.token) await refreshLibrary();
    return api('/api/codex' + path, {method, signal, headers: {'Content-Type': 'application/json',
      'X-Library-Token': state.token}, body: body === undefined ? undefined : JSON.stringify(body)});
  }
  function toggle(open = !cx.open) {
    cx.open = open;
    document.documentElement.dataset.mode = open ? 'codex' : 'papers';
    $('codex-sidebar').hidden = !open;
    $('toggle-codex').setAttribute('aria-expanded', String(open));
    $('toggle-codex').setAttribute('aria-label', open ? 'Close Codex' : 'Open Codex');
    $('codex-current-paper').disabled = !state.selected;
    if (open) {
      divider.hidden = false;
      loadOptions().catch(e => error(e.message));
      requestAnimationFrame(fitTerminal);
    } else {
      divider.hidden = $('sidebar').hidden;
    }
  }
  const libraryToggle = $('toggle-sidebar').onclick;
  $('toggle-sidebar').onclick = () => {
    if (cx.open) { toggle(false); setSidebarHidden(false); }
    else libraryToggle();
  };
  const searchToggle = $('toggle-search').onclick;
  $('toggle-search').onclick = () => { if (cx.open) toggle(false); searchToggle(); };
  const refresh = $('refresh').onclick;
  $('refresh').onclick = () => cx.open ? refreshSessions().catch(e => error(e.message)) : refresh();
  function showSession(show) {
    cx.sessionView = show && Boolean(cx.selected);
    $('codex-browser').hidden = cx.sessionView;
    $('codex-main').hidden = !cx.sessionView;
    $('codex-current-paper').hidden = cx.sessionView;
    $('codex-back').hidden = !cx.selected;
    $('codex-back').textContent = cx.sessionView ? 'Sessions' : 'Back to session';
    if (cx.sessionView) requestAnimationFrame(fitTerminal);
  };
  $('toggle-codex').onclick = () => toggle();
  $('codex-back').onclick = () => {
    if (cx.sessionView) { showSession(false); refreshSessions().catch(e => error(e.message)); }
    else connect(cx.selected).catch(e => error(e.message));
  };

  async function loadOptions() {
    if (cx.options) {
      if (JSON.stringify(cx.options.projects) !== JSON.stringify(state.projects)) {
        cx.options.projects = state.projects;
        const source = $('codex-paper-source').value;
        const project = $('codex-project').value;
        $('codex-paper-source').replaceChildren(...state.projects.map(p => new Option(p.name, p.id)));
        $('codex-project').replaceChildren(...state.projects.filter(p => p.kind === 'local').map(p => new Option(p.name, p.id)));
        if (state.projects.some(p => p.id === source)) $('codex-paper-source').value = source;
        if (state.projects.some(p => p.id === project)) $('codex-project').value = project;
        renderDraft();
        if ($('codex-scope-kind').value === 'paper') await loadPapers();
      }
      return;
    }
    const data = await request('/options');
    cx.options = data;
    if (data.error) error(data.error);
    $('codex-conferences').replaceChildren();
    for (const project of data.projects.filter(p => p.kind === 'conference')) {
      const label = element('label', 'codex-check');
      const input = element('input'); input.type = 'checkbox'; input.value = project.id;
      input.checked = project.id === state.projectId;
      input.onchange = renderYears;
      label.append(input, document.createTextNode(project.name));
      $('codex-conferences').append(label);
    }
    for (const project of data.projects) {
      $('codex-paper-source').append(new Option(project.name, project.id));
      if (project.kind === 'local') $('codex-project').append(new Option(project.name, project.id));
    }
    $('codex-paper-source').value = state.projectId;
    if (state.project?.kind === 'local') $('codex-project').value = state.projectId;
    for (const model of data.models || []) $('codex-new-model').append(new Option(model.displayName || model.model || model.id, model.model || model.id));
    renderYears();
    let saved;
    try { saved = JSON.parse(localStorage.getItem('paper-codex-scope')); } catch { /* use current library */ }
    cx.draft = saved || (state.project?.kind === 'local' ? {conferences: [], projects: [state.projectId], papers: []} :
      {conferences: [{id: state.projectId, years: $('year').value ? [Number($('year').value)] : null}], projects: [], papers: []});
    renderDraft();
    await applyScope();
    const previous = localStorage.getItem('paper-codex-session');
    const session = cx.sessions.find(s => s.id === previous);
    if (session) {
      cx.selected = session;
      $('codex-session-title').textContent = session.title;
      $('codex-rename').hidden = false;
      $('codex-reconnect').hidden = false;
      showSession(false);
      renderSessions();
      if (session.running) await connect(session);
    }
  }
  function selectedConferences() {
    return [...$('codex-conferences').querySelectorAll('input:checked')].map(input => input.value);
  }
  function renderYears() {
    const old = new Set([...$('codex-years').querySelectorAll('input:checked')].map(input => Number(input.value)));
    const ids = selectedConferences();
    const years = [...new Set(cx.options.projects.filter(p => ids.includes(p.id)).flatMap(p => p.years))].sort((a, b) => b - a);
    $('codex-years').replaceChildren();
    for (const year of years) {
      const label = element('label', 'codex-check');
      const input = element('input'); input.type = 'checkbox'; input.value = year; input.checked = old.has(year);
      label.append(input, document.createTextNode(String(year))); $('codex-years').append(label);
    }
    const count = cx.options.projects.filter(p => p.kind === 'conference').length;
    $('codex-all-conferences').checked = ids.length === count;
    $('codex-all-conferences').indeterminate = ids.length > 0 && ids.length < count;
  }
  $('codex-all-conferences').onchange = () => {
    $('codex-conferences').querySelectorAll('input').forEach(input => input.checked = $('codex-all-conferences').checked);
    renderYears();
  };
  $('codex-all-years').onchange = () => $('codex-years').hidden = $('codex-all-years').checked;
  $('codex-scope-kind').onchange = () => {
    const kind = $('codex-scope-kind').value;
    $('codex-conference-fields').hidden = kind !== 'conferences';
    $('codex-project-fields').hidden = kind !== 'project';
    $('codex-paper-fields').hidden = kind !== 'paper';
    if (kind === 'paper') loadPapers().catch(e => error(e.message));
  };
  async function loadPapers() {
    const requestId = ++cx.paperRequest;
    const id = $('codex-paper-source').value;
    const data = await api('/api/library?project=' + encodeURIComponent(id));
    if (requestId !== cx.paperRequest) return;
    cx.paperRows = data.papers;
    $('codex-paper-year').replaceChildren(new Option('All years', ''));
    for (const year of data.years.slice().reverse()) $('codex-paper-year').append(new Option(String(year), String(year)));
    $('codex-paper-year').hidden = data.project.kind === 'local';
    renderPaperOptions();
  }
  function renderPaperOptions() {
    const query = $('codex-paper-search').value.trim().toLowerCase();
    const year = $('codex-paper-year').value;
    $('codex-paper').replaceChildren();
    for (const paper of cx.paperRows.filter(p => (!year || p.year === year) && (!query || p.title.toLowerCase().includes(query)))) {
      $('codex-paper').append(new Option(`${paper.year === 'local' ? '' : paper.year + ' · '}${paper.title}`, paper.id));
    }
  }
  $('codex-paper-source').onchange = () => loadPapers().catch(e => error(e.message));
  $('codex-paper-year').onchange = renderPaperOptions;
  $('codex-paper-search').oninput = renderPaperOptions;
  function renderDraft() {
    $('codex-scope-parts').replaceChildren();
    const append = (label, remove) => {
      const row = element('div', 'codex-scope-part');
      const button = element('button', '', '×'); button.type = 'button'; button.setAttribute('aria-label', 'Remove ' + label);
      button.onclick = () => { remove(); renderDraft(); };
      row.append(element('span', '', label), button); $('codex-scope-parts').append(row);
    };
    cx.draft.conferences.forEach((item, i) => append(`${projectName(item.id)} · ${item.years?.join(', ') || 'All years'}`, () => cx.draft.conferences.splice(i, 1)));
    cx.draft.projects.forEach((id, i) => append(projectName(id), () => cx.draft.projects.splice(i, 1)));
    cx.draft.papers.forEach((item, i) => {
      const paper = [...cx.paperRows, ...state.papers].find(p => p.id === item.paper_id && p.project_id === item.project_id);
      append(`${projectName(item.project_id)} · ${paper?.title || item.title || 'Selected paper'}`, () => cx.draft.papers.splice(i, 1));
    });
    $('codex-apply-scope').disabled = !Object.values(cx.draft).some(items => items.length);
  }
  function addSelection() {
    const kind = $('codex-scope-kind').value;
    if (kind === 'conferences') {
      const ids = selectedConferences();
      const years = $('codex-all-years').checked ? null : [...$('codex-years').querySelectorAll('input:checked')].map(i => Number(i.value));
      if (!ids.length || (years && !years.length)) throw new Error('Select conferences and at least one year, or all years.');
      let added = 0;
      for (const id of ids) {
        const allowed = cx.options.projects.find(p => p.id === id).years;
        const chosen = years === null ? null : years.filter(year => allowed.includes(year));
        if (chosen?.length === 0) continue;
        const existing = cx.draft.conferences.find(p => p.id === id);
        if (existing) existing.years = existing.years === null || chosen === null ? null : [...new Set([...existing.years, ...chosen])].sort();
        else cx.draft.conferences.push({id, years: chosen});
        added++;
      }
      if (!added) throw new Error('No selected conference has proceedings for those years.');
    } else if (kind === 'project') {
      const id = $('codex-project').value;
      if (!id) throw new Error('Create a local project in the paper library first.');
      if (!cx.draft.projects.includes(id)) cx.draft.projects.push(id);
    } else {
      const id = $('codex-paper').value;
      if (!id) throw new Error('Choose a paper from a fetched paper list.');
      const project = $('codex-paper-source').value;
      if (!cx.draft.papers.some(p => p.paper_id === id && p.project_id === project)) {
        cx.draft.papers.push({project_id: project, paper_id: id, title: cx.paperRows.find(p => p.id === id)?.title});
      }
    }
    renderDraft();
  }
  $('codex-add-scope').onclick = () => { try { error(); addSelection(); } catch (e) { error(e.message); } };
  $('codex-clear-scope').onclick = () => { cx.draft = emptyScope(); renderDraft(); };
  async function applyScope() {
    error();
    const result = await request('/scope', 'POST', {scope: cx.draft});
    if (cx.selected && cx.selected.scope_id !== result.scope_id) {
      ++cx.connection; cx.controller?.abort(); cx.input = ''; cx.running = false; cx.selected = null;
      $('codex-session-title').textContent = 'Choose a session'; $('codex-terminal-status').textContent = '';
      for (const id of ['codex-terminal', 'codex-command-bar', 'codex-rename', 'codex-reconnect', 'codex-stop']) $(id).hidden = true;
      $('codex-welcome').hidden = false;
      showSession(false);
    }
    cx.scope = result.scope;
    cx.draft = JSON.parse(JSON.stringify(result.scope));
    localStorage.setItem('paper-codex-scope', JSON.stringify(cx.scope));
    $('codex-scope-summary').textContent = result.label;
    $('codex-scope-summary').title = result.label;
    $('codex-scope-info').textContent = `${result.downloaded} local / ${result.total} listed papers` +
      (result.missing_catalogs.length ? ` · ${result.missing_catalogs.length} year lists available through Codex’s paper helper` : '');
    $('codex-new').disabled = false;
    $('codex-scope-editor').open = false;
    renderDraft();
    await refreshSessions();
  }
  $('codex-apply-scope').onclick = () => applyScope().catch(e => error(e.message));
  $('codex-current-paper').onclick = async () => {
    if (!state.selected) return;
    cx.draft = {conferences: [], projects: [], papers: [{project_id: state.projectId, paper_id: state.selected}]};
    try { await applyScope(); } catch (e) { error(e.message); }
  };
  async function refreshSessions() {
    if (!cx.scope) return;
    const query = ++cx.query;
    const result = await request('/sessions/query', 'POST', {scope: cx.scope});
    if (query !== cx.query) return;
    cx.sessions = result.sessions;
    renderSessions();
  }
  function renderSessions() {
    $('codex-sessions').replaceChildren();
    for (const session of cx.sessions) {
      const button = element('button', 'codex-session' + (session.id === cx.selected?.id ? ' selected' : ''));
      button.setAttribute('aria-pressed', String(session.id === cx.selected?.id));
      button.append(element('strong', '', session.title), element('small', '',
        `${new Date(session.created_at * 1000).toLocaleString()}${session.running ? ' · Running' : ''}`));
      button.onclick = () => connect(session).catch(e => error(e.message));
      $('codex-sessions').append(button);
    }
    if (!cx.sessions.length) $('codex-sessions').append(element('p', 'codex-help', 'No sessions for this scope yet. Create one to start reading with Codex.'));
  }
  $('codex-new').onclick = () => { $('codex-new-form').hidden = false; $('codex-new-title').focus(); };
  $('codex-cancel-new').onclick = () => $('codex-new-form').hidden = true;
  $('codex-new-form').onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      const result = await request('/sessions', 'POST', {scope: cx.scope, title: $('codex-new-title').value, model: $('codex-new-model').value});
      $('codex-new-form').hidden = true; $('codex-new-title').value = '';
      await refreshSessions();
      await connect(result.session);
    } catch (e) { error(e.message); }
    finally { button.disabled = false; }
  };

  function initTerminal() {
    if (cx.terminal) return;
    cx.terminal = new Terminal({fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 13,
      lineHeight: 1.15, cursorBlink: true, scrollback: 10000, screenReaderMode: true, allowProposedApi: false});
    cx.fit = new FitAddon.FitAddon(); cx.terminal.loadAddon(cx.fit);
    cx.terminal.open($('codex-terminal'));
    cx.terminal.onData(data => {
      if (!cx.running || cx.connecting) return;
      cx.input += data;
      sendInput();
    });
    new ResizeObserver(() => fitTerminal()).observe($('codex-terminal'));
    new MutationObserver(() => terminalTheme()).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    terminalTheme();
  }
  function terminalTheme() {
    if (!cx.terminal) return;
    const dark = document.documentElement.dataset.theme === 'dark';
    cx.terminal.options.theme = {background: dark ? '#111111' : '#f7f8f6', foreground: dark ? '#e5e5e5' : '#202b30',
      cursor: dark ? '#e5e5e5' : '#202b30', selectionBackground: dark ? '#555555' : '#bbcfc3'};
  }
  let resizeTimer;
  function fitTerminal() {
    if (!cx.open || !cx.terminal || $('codex-terminal').hidden || !$('codex-terminal').clientWidth) return;
    cx.fit.fit();
    if (cx.running && !cx.connecting) {
      clearTimeout(resizeTimer);
      const id = cx.selected.id;
      resizeTimer = setTimeout(() => request(`/sessions/${id}/resize`, 'POST', {
        cols: Math.max(20, cx.terminal.cols), rows: Math.max(5, cx.terminal.rows)}).catch(e => error(e.message)), 100);
    }
  }
  async function connect(session) {
    if (cx.connecting) return;
    if (cx.selected?.id === session.id && cx.running) {
      showSession(true); fitTerminal(); cx.terminal.focus(); return;
    }
    cx.connecting = true;
    error();
    const connection = ++cx.connection;
    cx.controller?.abort(); cx.input = ''; cx.running = false;
    cx.selected = session; cx.cursor = 0; cx.generation = null;
    $('codex-session-title').textContent = session.title;
    $('codex-terminal-status').textContent = 'Connecting…';
    $('codex-reconnect').hidden = true; $('codex-stop').hidden = true;
    $('codex-rename').hidden = false;
    $('codex-welcome').hidden = true; $('codex-terminal').hidden = false; $('codex-command-bar').hidden = false;
    showSession(true);
    initTerminal(); cx.terminal.reset(); fitTerminal(); renderSessions();
    localStorage.setItem('paper-codex-session', session.id);
    try {
      const result = await request(`/sessions/${session.id}/start`, 'POST', {
        cols: Math.max(20, cx.terminal.cols), rows: Math.max(5, cx.terminal.rows)});
      if (connection !== cx.connection) return;
      cx.selected = result.session; cx.running = true;
      $('codex-terminal-status').textContent = 'Connected'; $('codex-stop').hidden = false;
      cx.connecting = false;
      if (cx.open && cx.sessionView) cx.terminal.focus();
      readOutput(connection, session.id);
      await refreshSessions();
    } catch (e) {
      $('codex-terminal-status').textContent = 'Disconnected'; $('codex-reconnect').hidden = false;
      throw e;
    } finally { cx.connecting = false; }
  }
  async function readOutput(connection, id) {
    while (connection === cx.connection) {
      cx.controller = new AbortController();
      try {
        const result = await request(`/sessions/${id}/output?cursor=${cx.cursor}`, 'GET', undefined, cx.controller.signal);
        if (connection !== cx.connection) return;
        if (cx.generation && cx.generation !== result.generation) { cx.cursor = 0; cx.generation = result.generation; cx.terminal.reset(); continue; }
        if (result.reset) cx.terminal.reset();
        cx.generation = result.generation; cx.cursor = result.cursor;
        if (result.data) {
          const data = Uint8Array.from(atob(result.data), c => c.charCodeAt(0));
          await new Promise(resolve => cx.terminal.write(data, resolve));
        }
        if (!result.running) {
          cx.running = false;
          $('codex-terminal-status').textContent = result.exit_code ? `Exited (${result.exit_code})` : 'Stopped';
          $('codex-stop').hidden = true; $('codex-reconnect').hidden = false;
          await refreshSessions(); return;
        }
      } catch (e) {
        if (e.name === 'AbortError' || connection !== cx.connection) return;
        cx.running = false;
        $('codex-terminal-status').textContent = 'Disconnected';
        $('codex-stop').hidden = true; $('codex-reconnect').hidden = false;
        error(e.message); return;
      }
    }
  }
  async function sendInput() {
    if (cx.sending) return;
    cx.sending = true;
    const connection = cx.connection;
    const id = cx.selected?.id;
    try {
      while (cx.input && connection === cx.connection && cx.running) {
        const data = cx.input.slice(0, 8000); cx.input = cx.input.slice(data.length);
        await request(`/sessions/${id}/input`, 'POST', {data});
      }
    } catch (e) {
      cx.input = '';
      error(e.message);
    }
    finally { cx.sending = false; if (cx.input && cx.running) sendInput(); }
  }
  $('codex-reconnect').onclick = () => connect(cx.selected).catch(e => error(e.message));
  $('codex-stop').onclick = async () => {
    try { await request(`/sessions/${cx.selected.id}/stop`, 'POST', {}); } catch (e) { error(e.message); }
  };
  $('codex-copy').onclick = async () => {
    try { await navigator.clipboard.writeText(cx.terminal.getSelection()); } catch { error('Use your browser’s copy shortcut to copy the selected terminal text.'); }
  };
  $('codex-rename').onclick = () => {
    $('codex-rename-title').value = cx.selected.title;
    $('codex-rename-dialog').showModal(); $('codex-rename-title').focus();
  };
  $('codex-rename-form').onsubmit = async event => {
    event.preventDefault();
    try {
      const result = await request(`/sessions/${cx.selected.id}`, 'PATCH', {title: $('codex-rename-title').value});
      cx.selected = result.session; $('codex-session-title').textContent = cx.selected.title;
      $('codex-rename-dialog').close(); await refreshSessions();
    } catch (e) { error(e.message); }
  };
  window.paperCodex = {toggle, get terminal() { return cx.terminal; }};
})();
