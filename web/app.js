'use strict';
const $ = id => document.getElementById(id);
const state = {papers: [], selected: null, detail: null, tab: 'papers', token: '', job: null, request: 0, completed: ''};
state.submittingDownloads = new Set();
state.projectId = localStorage.getItem('paper-project') || 'osdi';
state.projects = [];
state.project = null;
state.libraryRequest = 0;
state.yearStatus = {};
state.pendingCatalog = null;
state.startingCatalog = false;
state.catalogFailure = null;
let dismissedJob = localStorage.getItem('osdi-dismissed-job');
const labels = {catalog: 'Finding papers', download_year: 'Downloading papers', download: 'Downloading paper', outline: 'Generating outline', prepare: 'Preparing reader', import_url: 'Fetching paper', copy_paper: 'Saving paper to project'};
let toastTimer;
function notify(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').hidden = true, 10000);
}
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({error: `Request failed (${response.status})`}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function taskButton(text, action, className = 'primary', paperId = state.selected) {
  const button = element('button', className, text);
  button.dataset.task = 'true';
  button.dataset.action = action;
  button.dataset.paperId = paperId;
  button.dataset.projectId = state.projectId;
  button.dataset.label = text;
  updateTaskButton(button);
  button.onclick = () => startJob({action, paper_id: paperId, project_id: button.dataset.projectId});
  return button;
}
function downloadTask(paperId, projectId = state.projectId) {
  const jobs = [...(state.job?.status === 'running' ? [state.job] : []), ...(state.job?.queue || [])];
  return jobs.find(job => job.action === 'download' && job.paper_id === paperId && job.project_id === projectId);
}
function updateTaskButton(button) {
  if (button.dataset.action !== 'download') {
    button.disabled = state.job?.status === 'running';
    return;
  }
  const task = downloadTask(button.dataset.paperId, button.dataset.projectId);
  const submitting = state.submittingDownloads.has(`${button.dataset.projectId}/${button.dataset.paperId}`);
  button.disabled = Boolean(task || submitting);
  button.textContent = task?.status === 'running' ? 'Downloading…' : task ? `Queued · ${task.position}` : submitting ? 'Adding…' : button.dataset.label;
}
function filteredPapers() {
  const query = $('search').value.toLowerCase().trim();
  return state.papers.filter(p => (!query || p.title.toLowerCase().includes(query)) &&
    (!$('year').value || p.year === $('year').value) &&
    ($('availability').value === 'all' ||
      $('availability').value === 'downloaded' && p.downloaded ||
      $('availability').value === 'missing' && !p.downloaded ||
      $('availability').value === 'outlines' && p.section_count > 0));
}
function renderList() {
  const papers = filteredPapers();
  const list = $('paper-list');
  list.replaceChildren();
  const lookingUp = catalogLookupActive();
  const failure = state.catalogFailure?.key === catalogKey() ? state.catalogFailure.message : '';
  if (papers.length && lookingUp) list.append(element('p', 'catalog-status', 'Loading the complete paper list…'));
  $('paper-count').textContent = papers.length;
  for (const paper of papers) {
    const button = element('button', 'paper-card' + (state.selected === paper.id ? ' selected' : ''));
    button.setAttribute('aria-pressed', String(state.selected === paper.id));
    const meta = element('span', 'paper-meta');
    meta.append(element('span', 'year-tag', paper.project_kind === 'local' ? 'Local paper' : `${paper.project_name} ${paper.year}`));
    const task = downloadTask(paper.id);
    const status = task?.status === 'running' ? 'Downloading…' : task ? `Queued · ${task.position}` : paper.downloaded ? 'Downloaded' : 'Not downloaded';
    meta.append(element('span', 'badge' + (paper.downloaded ? ' ready' : ''), status));
    if (paper.section_count) meta.append(element('span', 'badge' + (paper.outline_status === 'needs_review' ? ' review' : ''), 'Outline'));
    button.append(meta, element('span', 'paper-title', paper.title));
    button.onclick = () => selectPaper(paper.id);
    list.append(button);
  }
  if (!papers.length) {
    const empty = element('div', 'panel-empty');
    const title = lookingUp ? 'Loading paper list…' : failure ? 'Could not load the paper list' : 'No papers here yet';
    const message = lookingUp ? 'Fetching paper titles from the conference proceedings. Papers are downloaded only when you request them.' : failure || (state.project?.kind === 'local' ? 'Add a paper by URL, upload a PDF, or save one from a conference library.' : 'Choose a year to load its paper list, or try a different filter.');
    empty.append(element('h3', '', title), element('p', '', message));
    if (failure) {
      const retry = element('button', 'quiet', 'Retry');
      retry.onclick = () => queueYearCatalog(true);
      empty.append(retry);
    }
    list.append(empty);
  }
  renderYearAction();
}
function renderYearAction() {
  const year = $('year').value;
  const complete = state.yearStatus[year]?.status === 'downloaded';
  $('download-year').textContent = complete ? `${year} downloaded` : year ? `Download ${year}` : 'Download year';
  $('download-year').disabled = !year || complete || state.job?.status === 'running';
}
function yearLabel(year, info) {
  if (info?.status === 'downloaded') return `${year} · ✓ Downloaded`;
  const count = info?.total ? `${info.downloaded}/${info.total}` : info?.downloaded;
  if (info?.status === 'in_progress') return `${year} · ${count} in progress`;
  if (info?.downloaded) return `${year} · ${count} downloaded`;
  return String(year);
}
function switchTab(tab) {
  state.tab = tab;
  for (const name of ['papers', 'outline']) {
    const active = tab === name;
    $(name + '-tab').setAttribute('aria-selected', String(active));
    $(name + '-tab').tabIndex = active ? 0 : -1;
  }
  $('paper-list').hidden = tab !== 'papers';
  $('outline-panel').hidden = tab !== 'outline';
  if (tab === 'outline') renderOutline();
}
function renderOutline() {
  const panel = $('outline-panel');
  panel.replaceChildren();
  const paper = state.detail;
  if (!paper) {
    panel.append(element('div', 'panel-empty', 'Choose a paper in the Papers tab to explore its outline.'));
    return;
  }
  const header = element('div', 'outline-header');
  if (paper.downloaded) header.append(taskButton(paper.sections.length ? 'Regenerate outline' : 'Generate outline', 'outline', 'quiet'));
  else header.append(taskButton('Download paper', 'download'));
  if (state.project?.kind === 'local') {
    const remove = element('button', 'quiet danger', 'Remove paper');
    remove.dataset.task = 'true';
    remove.disabled = state.job?.status === 'running';
    remove.onclick = removeSelectedPaper;
    header.append(remove);
  }
  if (paper.warnings.length) {
    const notes = element('details', 'outline-note');
    notes.append(element('summary', '', 'Review suggested'));
    for (const warning of paper.warnings) notes.append(element('p', '', warning));
    header.append(notes);
  }
  panel.append(header);
  if (!paper.sections.length) {
    panel.append(element('div', 'panel-empty', paper.downloaded ? 'Generate an outline to explore sections and subsections. Older scanned papers may take a few minutes.' : 'Download this paper first, then generate its outline.'));
  }
  for (const section of paper.sections) {
    const button = element('button', 'section-link');
    const canJump = Boolean(paper.viewer_url && (section.page || section.url));
    button.style.paddingLeft = `${10 + (Math.min(section.level, 5) - 1) * 15}px`;
    button.setAttribute('aria-disabled', String(!canJump));
    button.append(element('span', 'section-number', section.number), element('span', 'section-name', section.title));
    if (section.page) button.append(element('span', 'page-number', `p. ${section.page}`));
    button.onclick = () => {
      if (!canJump) return;
      panel.querySelectorAll('.active').forEach(el => el.classList.remove('active'));
      button.classList.add('active');
      showDocument(paper, section);
    };
    panel.append(button);
  }
}
function showDocument(paper, section = null) {
  const container = $('viewer-container');
  if (paper.viewer_url) {
    let url = section?.url || paper.viewer_url;
    if (paper.viewer_type === 'pdf') url += '#page=' + (section?.page || 1) + '&view=FitH';
    // A new frame makes PDF page navigation work across browser PDF viewers.
    const frame = element('iframe');
    frame.title = paper.title;
    if (paper.viewer_type !== 'pdf') frame.setAttribute('sandbox', '');
    frame.src = url;
    container.replaceChildren(frame);
    return;
  }
  const empty = element('div', 'empty-state');
  empty.append(element('div', 'book-icon', '▤'));
  if (paper.downloaded) {
    empty.append(element('h2', '', 'Ready to prepare for reading'), element('p', '', 'This older paper needs a one-time conversion before it can open in your browser.'), taskButton('Open paper in reader', 'prepare'));
  } else {
    empty.append(element('h2', '', 'Add this paper to your library'), element('p', '', 'Download the full paper to read it here and explore its structure.'), taskButton('Download paper', 'download'));
    if (paper.error) {
      const error = element('details', 'outline-note');
      error.append(element('summary', '', 'Previous download failed'), element('p', '', paper.error));
      empty.append(error);
    }
  }
  container.replaceChildren(empty);
}
function renderReader(previous) {
  const paper = state.detail;
  if (!paper) return;
  if (!previous || previous.id !== paper.id || previous.viewer_url !== paper.viewer_url || previous.downloaded !== paper.downloaded) showDocument(paper);
}
async function selectPaper(id, {persist = true} = {}) {
  const requestId = ++state.request;
  const projectId = state.projectId;
  state.selected = id;
  renderList();
  try {
    const detail = await api(`/api/papers/${id}?project=${encodeURIComponent(projectId)}`);
    if (requestId !== state.request || projectId !== state.projectId) return;
    const previous = state.detail;
    state.detail = detail;
    if (persist) localStorage.setItem('paper-selected-' + projectId, id);
    $('save-to-project').hidden = !detail.downloaded;
    renderReader(previous);
    renderOutline();
  } catch (error) { notify(error.message); }
}
async function refreshLibrary() {
  const requestId = ++state.libraryRequest;
  const projectId = state.projectId;
  const data = await api('/api/library?project=' + encodeURIComponent(projectId));
  if (requestId !== state.libraryRequest || projectId !== state.projectId) return;
  state.papers = data.papers;
  state.token = data.token;
  state.project = data.project;
  state.projects = data.projects;
  state.yearStatus = data.year_status || {};
  renderProjects();
  const year = $('year').value;
  $('year').replaceChildren(new Option('All years', ''));
  for (const value of data.years.slice().reverse()) $('year').append(new Option(yearLabel(value, state.yearStatus[String(value)]), String(value)));
  $('year').value = data.years.map(String).includes(year) ? year : '';
  renderList();
  renderJob(data.job);
  if (state.selected && state.papers.some(p => p.id === state.selected)) await selectPaper(state.selected);
}
function renderJob(job) {
  const signature = value => JSON.stringify([value?.id, value?.status, value?.queue?.map(task => task.id)]);
  const changed = signature(state.job) !== signature(job);
  state.job = job;
  const busy = job?.status === 'running';
  $('job-panel').hidden = !job || (!busy && job.id === dismissedJob);
  $('close-job').hidden = !job || busy;
  document.querySelectorAll('[data-task]').forEach(updateTaskButton);
  renderYearAction();
  if (changed) renderList();
  if (!job) return;
  $('job-panel').className = job.status;
  const queue = job.queue || [];
  $('job-title').textContent = `${labels[job.action] || 'Task'} · ${busy ? 'in progress' : job.status === 'done' ? 'complete' : 'failed — see progress for details'}${queue.length ? ` · ${queue.length} queued` : ''}`;
  const progress = [job.title, job.log || 'Starting…'].filter(Boolean);
  if (queue.length) progress.push('Queued:\n' + queue.map(task => `${task.position}. ${task.title || 'Paper'}`).join('\n'));
  const failures = (job.completed || []).filter(task => task.status === 'failed' && task.id !== job.id).slice(-5);
  if (failures.length) progress.push('Earlier failed tasks:\n' + failures.map(task => `${task.title || labels[task.action]}\n${task.log || ''}`).join('\n'));
  $('job-log').textContent = progress.join('\n\n');
  if (!$('job-log').hidden) $('job-log').scrollTop = $('job-log').scrollHeight;
}
async function startJob(payload, {quiet = false} = {}) {
  const downloadKey = payload.action === 'download' ? `${payload.project_id || state.projectId}/${payload.paper_id}` : null;
  if (downloadKey && state.submittingDownloads.has(downloadKey)) return false;
  if (downloadKey) state.submittingDownloads.add(downloadKey);
  document.querySelectorAll('[data-task]').forEach(updateTaskButton);
  try {
    const data = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Library-Token': state.token}, body: JSON.stringify({project_id: state.projectId, ...payload})});
    renderJob(data.job);
    if (payload.action === 'outline') switchTab('outline');
    if (!quiet) {
      $('job-log').hidden = false;
      $('toggle-log').textContent = 'Hide progress';
    }
    return true;
  } catch (error) { notify(error.message); return false; }
  finally {
    if (downloadKey) state.submittingDownloads.delete(downloadKey);
    document.querySelectorAll('[data-task]').forEach(updateTaskButton);
  }
}
async function poll() {
  try {
    const {job} = await api('/api/jobs');
    renderJob(job);
    const completed = job?.completed || (job && job.status !== 'running' ? [job] : []);
    const latest = completed.at(-1);
    if (latest && state.completed !== latest.id) {
      const previous = completed.findIndex(task => task.id === state.completed);
      for (const task of completed.slice(previous + 1)) {
        if (task.action === 'catalog' && task.status === 'failed') {
          state.catalogFailure = {key: `${task.project_id}/${task.year}`, message: 'The proceedings could not be reached. Check the progress log and retry.'};
        }
      }
      await refreshLibrary();
      state.completed = latest.id;
    }
    await processPendingCatalog();
  } catch {
    // Retry transient connection failures on the next poll.
  } finally { setTimeout(poll, 1500); }
}
$('search').oninput = renderList;
$('year').onchange = () => {
  clearSelection();
  $('search').value = '';
  $('availability').value = 'all';
  switchTab('papers');
  queueYearCatalog();
};
$('availability').onchange = renderList;
$('papers-tab').onclick = () => switchTab('papers');
$('outline-tab').onclick = () => switchTab('outline');
for (const name of ['papers', 'outline']) $(name + '-tab').onkeydown = event => {
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    const next = name === 'papers' ? 'outline' : 'papers';
    switchTab(next); $(next + '-tab').focus(); event.preventDefault();
  }
};
$('refresh').onclick = () => refreshLibrary().catch(error => notify(error.message));
$('download-year').onclick = () => {
  const year = Number($('year').value);
  if (year) startJob({action: 'download_year', year});
};

function catalogKey(project = state.projectId, year = $('year').value) {
  return `${project}/${year}`;
}
function catalogLookupActive() {
  const key = catalogKey();
  return (state.pendingCatalog && catalogKey(state.pendingCatalog.project_id, state.pendingCatalog.year) === key) ||
    (state.job?.status === 'running' && state.job.action === 'catalog' && catalogKey(state.job.project_id, state.job.year) === key);
}
async function queueYearCatalog(force = false) {
  const year = Number($('year').value);
  state.catalogFailure = null;
  state.pendingCatalog = null;
  if (year && state.project?.kind === 'conference' && (force || !state.yearStatus[String(year)]?.catalogued)) {
    state.pendingCatalog = {project_id: state.projectId, year, force};
  }
  renderList();
  await processPendingCatalog();
}
async function processPendingCatalog() {
  const pending = state.pendingCatalog;
  if (!pending || state.startingCatalog || state.job?.status === 'running') return;
  if (catalogKey(pending.project_id, pending.year) !== catalogKey() || (!pending.force && state.yearStatus[String(pending.year)]?.catalogued)) {
    state.pendingCatalog = null;
    renderList();
    return;
  }
  state.startingCatalog = true;
  try {
    const started = await startJob({action: 'catalog', project_id: pending.project_id, year: pending.year}, {quiet: true});
    if (!started) state.catalogFailure = {key: catalogKey(pending.project_id, pending.year), message: 'Could not start loading the paper list. Please retry.'};
  } finally {
    if (state.pendingCatalog === pending) state.pendingCatalog = null;
    state.startingCatalog = false;
    renderList();
  }
}
$('toggle-log').onclick = () => {
  $('job-log').hidden = !$('job-log').hidden;
  $('toggle-log').textContent = $('job-log').hidden ? 'Show progress' : 'Hide progress';
};
$('close-job').onclick = () => {
  if (!state.job || state.job.status === 'running') return;
  dismissedJob = state.job.id;
  localStorage.setItem('osdi-dismissed-job', dismissedJob);
  renderJob(state.job);
};

// Capture the pointer so dragging continues over the embedded document.
const divider = $('divider');
let width = Number(localStorage.getItem('osdi-sidebar-width')) || 33;
function setWidth(value) {
  width = Math.max(20, Math.min(70, value));
  $('workspace').style.setProperty('--left', width + '%');
  divider.setAttribute('aria-valuenow', String(Math.round(width)));
  localStorage.setItem('osdi-sidebar-width', String(width));
}
setWidth(width);
divider.onpointerdown = event => {
  divider.setPointerCapture(event.pointerId);
  document.body.classList.add('resizing');
  event.preventDefault();
};
divider.onpointermove = event => {
  if (!divider.hasPointerCapture(event.pointerId)) return;
  const rect = $('workspace').getBoundingClientRect();
  const toolsWidth = $('reading-tools').getBoundingClientRect().width;
  setWidth((event.clientX - rect.left - toolsWidth) / rect.width * 100);
};
function stopResize() { document.body.classList.remove('resizing'); }
divider.onpointerup = stopResize;
divider.onpointercancel = stopResize;
divider.onlostpointercapture = stopResize;
divider.ondblclick = () => setWidth(33);
divider.onkeydown = event => {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    setWidth(event.key === 'Home' ? 20 : event.key === 'End' ? 70 : width + (event.key === 'ArrowLeft' ? -2 : 2));
  }
};

function setSidebarHidden(hidden, {persist = true, focus = false} = {}) {
  document.documentElement.dataset.sidebarHidden = String(hidden);
  $('sidebar').hidden = hidden;
  divider.hidden = hidden;
  $('toggle-sidebar').setAttribute('aria-expanded', String(!hidden));
  $('toggle-sidebar').setAttribute('aria-label', hidden ? 'Show sidebar' : 'Hide sidebar');
  $('toggle-sidebar').title = hidden ? 'Show sidebar' : 'Hide sidebar';
  if (persist) localStorage.setItem('paper-sidebar-hidden', String(hidden));
  if (focus) $('toggle-sidebar').focus();
}
function setSearchHidden(hidden, {persist = true, focus = false} = {}) {
  document.documentElement.dataset.searchHidden = String(hidden);
  $('search-controls').hidden = hidden;
  $('toggle-search').setAttribute('aria-expanded', String(!hidden));
  $('toggle-search').setAttribute('aria-label', hidden ? 'Show search' : 'Hide search');
  $('toggle-search').title = hidden ? 'Show search' : 'Hide search';
  if (persist) localStorage.setItem('paper-search-hidden', String(hidden));
  if (hidden) {
    $('search').value = '';
    renderList();
    if (focus) $('toggle-search').focus();
  } else if (focus) $('search').focus();
}
function setTheme(theme, persist = true) {
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('toggle-theme').setAttribute('aria-pressed', String(dark));
  $('toggle-theme').title = dark ? 'Switch to light mode' : 'Switch to dark mode';
  if (persist) localStorage.setItem('paper-theme', dark ? 'dark' : 'light');
}
$('toggle-sidebar').onclick = () => setSidebarHidden(!$('sidebar').hidden, {focus: true});
$('toggle-search').onclick = () => {
  const hidden = !$('sidebar').hidden && !$('search-controls').hidden;
  if ($('sidebar').hidden) setSidebarHidden(false);
  setSearchHidden(hidden, {focus: true});
};
$('toggle-theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('search').addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    setSearchHidden(true, {focus: true});
  }
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => {
  if (!localStorage.getItem('paper-theme')) setTheme(event.matches ? 'dark' : 'light', false);
});
setSidebarHidden(document.documentElement.dataset.sidebarHidden === 'true', {persist: false});
setSearchHidden(document.documentElement.dataset.searchHidden === 'true', {persist: false});
setTheme(document.documentElement.dataset.theme, false);
refreshLibrary().then(restoreSelection).catch(async error => {
  if (state.projectId !== 'osdi' && error.message === 'Project not found.') await switchProject('osdi');
  else notify(error.message);
}).finally(poll);

function writeOptions(method, payload) {
  return {method, headers: {'Content-Type': 'application/json', 'X-Library-Token': state.token}, body: payload === undefined ? undefined : JSON.stringify(payload)};
}
function renderProjects() {
  $('project').replaceChildren();
  for (const kind of ['conference', 'local']) {
    const group = document.createElement('optgroup');
    group.label = kind === 'conference' ? 'Conferences' : 'Local projects';
    for (const project of state.projects.filter(p => p.kind === kind)) group.append(new Option(project.name, project.id));
    $('project').append(group);
  }
  $('project').value = state.projectId;
  const local = state.project?.kind === 'local';
  $('manage-project').hidden = !local;
  $('add-paper').hidden = !local;
  $('download-year').hidden = local;
  $('year').hidden = local;
  $('availability').style.width = local ? '100%' : '';
}
function clearSelection() {
  state.request++;
  state.selected = null;
  state.detail = null;
  $('save-to-project').hidden = true;
  const empty = element('div', 'empty-state');
  empty.append(element('div', 'book-icon', '▤'), element('h2', '', 'Choose a paper to read'));
  $('viewer-container').replaceChildren(empty);
  renderOutline();
}
function restoreSelection() {
  const previous = localStorage.getItem('paper-selected-' + state.projectId) || (state.projectId === 'osdi' ? localStorage.getItem('osdi-paper') : null);
  if (previous && state.papers.some(p => p.id === previous)) return selectPaper(previous);
}
async function switchProject(id) {
  state.projectId = id;
  state.papers = [];
  state.yearStatus = {};
  state.pendingCatalog = null;
  state.catalogFailure = null;
  localStorage.setItem('paper-project', id);
  clearSelection();
  $('search').value = '';
  $('year').value = '';
  $('availability').value = 'all';
  switchTab('papers');
  renderList();
  await refreshLibrary();
  await restoreSelection();
}
$('project').onchange = () => switchProject($('project').value).catch(error => notify(error.message));
let editingProject = false;
let pendingCopy = null;
function projectDialog(edit = false, copy = null) {
  editingProject = edit;
  pendingCopy = copy;
  $('project-dialog-title').textContent = edit ? 'Manage project' : 'New local project';
  $('project-name').value = edit ? state.project.name : '';
  $('project-error').textContent = '';
  $('archive-controls').hidden = !edit;
  $('project-dialog').showModal();
  $('project-name').focus();
}
$('new-project').onclick = () => projectDialog();
$('manage-project').onclick = () => projectDialog(true);
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $(button.dataset.close).close());
$('project-form').onsubmit = async event => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    const url = editingProject ? '/api/projects/' + state.projectId : '/api/projects';
    const data = await api(url, writeOptions(editingProject ? 'PATCH' : 'POST', {name: $('project-name').value}));
    $('project-dialog').close();
    await switchProject(data.project.id);
    if (pendingCopy) {
      await startJob({action: 'copy_paper', ...pendingCopy});
      pendingCopy = null;
    }
  } catch (error) { $('project-error').textContent = error.message; }
  finally { submit.disabled = false; }
};
$('archive-project').onclick = async () => {
  if (!confirm(`Archive “${state.project.name}”? Its papers will be kept on disk.`)) return;
  try {
    await api('/api/projects/' + state.projectId, writeOptions('DELETE'));
    $('project-dialog').close();
    await switchProject('osdi');
  } catch (error) { $('project-error').textContent = error.message; }
};
$('add-paper').onclick = () => {
  $('add-form').reset();
  $('import-error').textContent = '';
  $('add-dialog').showModal();
};
$('paper-url').oninput = () => { if ($('paper-url').value) $('paper-file').value = ''; };
$('paper-file').onchange = () => { if ($('paper-file').files.length) $('paper-url').value = ''; };
$('add-form').onsubmit = async event => {
  event.preventDefault();
  const file = $('paper-file').files[0];
  const url = $('paper-url').value.trim();
  if (!file && !url) { $('import-error').textContent = 'Enter a paper URL or choose a PDF.'; return; }
  $('import-submit').disabled = true;
  try {
    if (file) {
      const form = new FormData();
      form.append('file', file);
      form.append('title', $('import-title').value);
      await api('/api/projects/' + state.projectId + '/upload', {method: 'POST', headers: {'X-Library-Token': state.token}, body: form});
      await refreshLibrary();
    } else if (!await startJob({action: 'import_url', url, title: $('import-title').value})) return;
    $('add-dialog').close();
    switchTab('papers');
  } catch (error) { $('import-error').textContent = error.message; }
  finally { $('import-submit').disabled = false; }
};
$('save-to-project').onclick = () => {
  const projects = state.projects.filter(p => p.kind === 'local' && p.id !== state.projectId);
  if (!projects.length) { projectDialog(false, {source_project: state.projectId, paper_id: state.selected}); return; }
  $('save-project').replaceChildren(...projects.map(p => new Option(p.name, p.id)));
  $('save-error').textContent = '';
  $('save-dialog').showModal();
};
$('save-form').onsubmit = async event => {
  event.preventDefault();
  const saved = await startJob({action: 'copy_paper', project_id: $('save-project').value, source_project: state.projectId, paper_id: state.selected});
  if (saved) $('save-dialog').close();
};
async function removeSelectedPaper() {
  if (!state.detail || !confirm(`Remove “${state.detail.title}” from this project? The file will be kept in the project's archive folder.`)) return;
  try {
    await api(`/api/papers/${state.selected}?project=${encodeURIComponent(state.projectId)}`, writeOptions('DELETE'));
    localStorage.removeItem('paper-selected-' + state.projectId);
    clearSelection();
    await refreshLibrary();
  } catch (error) { notify(error.message); }
}
