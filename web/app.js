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
state.draggedPaper = null;
state.movingPaper = null;
state.renamingPaper = null;
state.paperListPending = false;
try {
  state.collapsedCategories = new Set(JSON.parse(localStorage.getItem('paper-collapsed-categories') || '[]'));
} catch { state.collapsedCategories = new Set(); }
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
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
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
    button.disabled = state.job?.status === 'running' || Boolean(state.movingPaper);
    return;
  }
  const task = downloadTask(button.dataset.paperId, button.dataset.projectId);
  const submitting = state.submittingDownloads.has(`${button.dataset.projectId}/${button.dataset.paperId}`);
  button.disabled = Boolean(task || submitting);
  button.textContent = task?.status === 'running' ? 'Downloading…' : task ? `Queued · ${task.position}` : submitting ? 'Adding…' : button.dataset.label;
}
function filteredPapers() {
  const query = $('search').value.toLowerCase().trim();
  return state.papers.filter(p => (!query || `${p.title} ${p.category || ''}`.toLowerCase().includes(query)) &&
    (!$('year').value || p.year === $('year').value) &&
    ($('availability').value === 'all' ||
      $('availability').value === 'downloaded' && p.downloaded ||
      $('availability').value === 'missing' && !p.downloaded ||
      $('availability').value === 'outlines' && p.section_count > 0));
}
function paperCard(paper) {
  const button = element('button', 'paper-card' + (state.selected === paper.id ? ' selected' : ''));
  button.dataset.paperId = paper.id;
  button.setAttribute('aria-pressed', String(state.selected === paper.id));
  const moving = state.movingPaper?.projectId === state.projectId && state.movingPaper.paperId === paper.id;
  button.setAttribute('aria-busy', String(moving));
  if (paper.project_kind === 'local') {
    button.draggable = !state.movingPaper;
    button.title = 'Drag onto a category to move this paper';
    button.ondragstart = event => {
      if (state.job?.status === 'running' || state.movingPaper || state.renamingPaper || savingCategory) {
        event.preventDefault();
        return;
      }
      state.draggedPaper = {projectId: state.projectId, paperId: paper.id};
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('application/x-paper-reading-paper', JSON.stringify(state.draggedPaper));
      button.classList.add('dragging');
      $('paper-list').classList.add('dragging-paper');
    };
    button.ondragend = endPaperDrag;
  }
  const meta = element('span', 'paper-meta');
  meta.append(element('span', 'year-tag', paper.project_kind === 'local' ? 'Local paper' : `${paper.project_name} ${paper.year}`));
  const task = downloadTask(paper.id, paper.project_id);
  const status = moving ? 'Moving…' : task?.status === 'running' ? 'Downloading…' : task ? `Queued · ${task.position}` : paper.downloaded ? 'Downloaded' : paper.pdf_unavailable ? 'No PDF found' : 'Not downloaded';
  meta.append(element('span', 'badge' + (paper.downloaded ? ' ready' : ''), status));
  if (paper.section_count) meta.append(element('span', 'badge' + (paper.outline_status === 'needs_review' ? ' review' : ''), 'Outline'));
  const title = element('span', 'paper-title', paper.title);
  if (paper.project_kind === 'local') {
    title.title = 'Double-click to rename';
    title.ondblclick = event => {
      event.preventDefault();
      event.stopPropagation();
      renamePaper(paper, button);
    };
    button.onkeydown = event => {
      if (event.key === 'F2') { event.preventDefault(); renamePaper(paper, button); }
    };
    button.setAttribute('aria-keyshortcuts', 'F2');
  }
  button.append(meta, title);
  button.onclick = event => { if (event.detail < 2) selectPaper(paper.id); };
  return button;
}
function renderPaperSelection() {
  $('paper-list').querySelectorAll('button.paper-card').forEach(card => {
    const selected = card.dataset.paperId === state.selected;
    card.classList.toggle('selected', selected);
    card.setAttribute('aria-pressed', String(selected));
  });
  $('categorize-paper').hidden = Boolean(window.ConferenceSearch?.active()) || state.project?.kind !== 'local' || !state.papers.some(p => p.id === state.selected);
}
function renamePaper(paper, card) {
  if (state.renamingPaper || state.draggedPaper || state.movingPaper || savingCategory) return;
  if (state.job?.status === 'running') {
    notify('Wait for the current task to finish before renaming papers.');
    return;
  }
  const projectId = state.projectId;
  const editor = element('div', card.className + ' paper-renaming');
  const input = element('input', 'paper-name-input');
  input.type = 'text';
  input.value = paper.title;
  input.maxLength = 300;
  input.setAttribute('aria-label', 'Paper name');
  const message = element('span', 'paper-rename-hint', 'Enter to save · Esc to cancel');
  message.id = 'paper-rename-status';
  message.setAttribute('role', 'status');
  input.setAttribute('aria-describedby', message.id);
  editor.append(card.querySelector('.paper-meta').cloneNode(true), input, message);
  const editing = {saving: false, cancel: () => finish()};
  function finish() {
    if (state.renamingPaper !== editing) return;
    const focused = editor.contains(document.activeElement);
    state.renamingPaper = null;
    state.paperListPending = false;
    renderList();
    if (focused && state.projectId === projectId) {
      $('paper-list').querySelector(`[data-paper-id="${paper.id}"]`)?.focus({preventScroll: true});
    }
  }
  async function save() {
    if (state.renamingPaper !== editing || editing.saving) return;
    const title = input.value.trim().replace(/\s+/g, ' ');
    if (!title) {
      message.textContent = 'Enter a paper name, or press Esc to cancel.';
      message.classList.add('form-error');
      input.setAttribute('aria-invalid', 'true');
      return;
    }
    if (title === paper.title) { finish(); return; }
    editing.saving = true;
    input.readOnly = true;
    editor.setAttribute('aria-busy', 'true');
    message.classList.remove('form-error');
    message.textContent = 'Saving…';
    try {
      const result = await api(`/api/papers/${paper.id}?project=${encodeURIComponent(projectId)}`, writeOptions('PATCH', {title}));
      if (state.projectId === projectId) {
        for (const row of state.papers) if (row.id === paper.id) row.title = result.title;
        if (state.detail?.id === paper.id) {
          state.detail.title = result.title;
          const frame = $('viewer-container').querySelector('iframe');
          if (frame) frame.title = result.title;
        }
      }
      finish();
    } catch (error) {
      if (state.renamingPaper === editing) {
        message.textContent = error.message;
        message.classList.add('form-error');
      } else notify('Could not rename paper: ' + error.message);
    } finally {
      editing.saving = false;
      input.readOnly = false;
      editor.removeAttribute('aria-busy');
    }
  }
  input.oninput = () => input.removeAttribute('aria-invalid');
  input.onkeydown = event => {
    if (event.isComposing) return;
    if (event.key === 'Enter') { event.preventDefault(); save(); }
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!editing.saving) finish();
    }
  };
  input.onblur = save;
  state.renamingPaper = editing;
  card.replaceWith(editor);
  input.focus();
  input.select();
}
function endPaperDrag() {
  state.draggedPaper = null;
  $('paper-list').classList.remove('dragging-paper');
  $('paper-list').querySelectorAll('.dragging, .drop-target').forEach(node => node.classList.remove('dragging', 'drop-target'));
  if (state.paperListPending) {
    state.paperListPending = false;
    renderList();
  }
}
function categoryDropTarget(details, category) {
  const projectId = state.projectId;
  const name = category === 'Uncategorized' ? '' : category;
  function draggedPaper(event) {
    const drag = state.draggedPaper;
    if (!drag || drag.projectId !== projectId || state.projectId !== projectId ||
        state.project?.kind !== 'local' || state.movingPaper || savingCategory || state.job?.status === 'running' ||
        !Array.from(event.dataTransfer?.types || []).includes('application/x-paper-reading-paper')) return null;
    return state.papers.find(p => p.id === drag.paperId && (p.category || '') !== name);
  }
  details.ondragenter = details.ondragover = event => {
    if (!draggedPaper(event)) {
      details.classList.remove('drop-target');
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    details.classList.add('drop-target');
  };
  details.ondragleave = event => {
    if (!details.contains(event.relatedTarget)) details.classList.remove('drop-target');
  };
  details.ondrop = event => {
    const paper = draggedPaper(event);
    if (!paper) return;
    event.preventDefault();
    event.stopPropagation();
    endPaperDrag();
    movePaperToCategory(projectId, paper.id, name);
  };
}
async function movePaperToCategory(projectId, paperId, category) {
  state.movingPaper = {projectId, paperId};
  document.querySelectorAll('[data-task]').forEach(updateTaskButton);
  renderList();
  let saved = false;
  try {
    const result = await api(`/api/papers/${paperId}?project=${encodeURIComponent(projectId)}`, writeOptions('PATCH', {category}));
    saved = true;
    state.collapsedCategories.delete(JSON.stringify([projectId, 'local', result.category || 'Uncategorized']));
    localStorage.setItem('paper-collapsed-categories', JSON.stringify([...state.collapsedCategories]));
    if (state.projectId === projectId) await refreshLibrary();
  } catch (error) {
    notify((saved ? 'Paper moved, but the library could not refresh: ' : 'Could not move paper: ') + error.message);
  } finally {
    state.movingPaper = null;
    document.querySelectorAll('[data-task]').forEach(updateTaskButton);
    renderList();
  }
}
function categoryGroups(papers) {
  const local = state.project?.kind === 'local';
  const groups = new Map();
  for (const paper of papers) {
    const category = paper.category || 'Uncategorized';
    const key = JSON.stringify([state.projectId, local ? 'local' : paper.year, category]);
    if (!groups.has(key)) groups.set(key, {key, category, year: paper.year, papers: [], order: Infinity});
    const group = groups.get(key);
    group.papers.push(paper);
    group.order = Math.min(group.order, local ? Infinity : paper.program_order ?? Infinity);
  }
  if (local && papers.length && !papers.some(p => !p.category)) {
    const key = JSON.stringify([state.projectId, 'local', 'Uncategorized']);
    groups.set(key, {key, category: 'Uncategorized', year: 'local', papers: [], order: Infinity});
  }
  return [...groups.values()].sort((a, b) => (!local && Number(b.year) - Number(a.year)) ||
    (a.category === 'Uncategorized') - (b.category === 'Uncategorized') ||
    a.order - b.order || a.category.localeCompare(b.category));
}
function renderList() {
  // Keep an active drag or name editor intact during background polling.
  if (state.draggedPaper || state.renamingPaper) { state.paperListPending = true; return; }
  const globalSearch = window.ConferenceSearch?.active();
  $('year').disabled = Boolean(globalSearch);
  if (globalSearch) {
    $('categorize-paper').hidden = true;
    window.ConferenceSearch.render();
    renderYearAction();
    return;
  }
  window.ConferenceSearch?.reset();
  const local = state.project?.kind === 'local';
  const papers = filteredPapers();
  $('categorize-paper').hidden = !local || !state.papers.some(paper => paper.id === state.selected);
  const list = $('paper-list');
  const scrollTop = list.scrollTop;
  const focusedCategory = list.contains(document.activeElement) ? document.activeElement.dataset.categoryKey : null;
  list.replaceChildren();
  const lookingUp = catalogLookupActive();
  const failure = state.catalogFailure?.key === catalogKey() ? state.catalogFailure.message : '';
  if (papers.length && lookingUp) list.append(element('p', 'catalog-status', 'Loading the complete paper list…'));
  if (papers.length && failure && !lookingUp) {
    const note = element('div', 'catalog-status', 'Could not refresh paper categories. ');
    const retry = element('button', 'quiet', 'Retry');
    retry.onclick = () => queueYearCatalog(true);
    note.append(retry);
    list.append(note);
  }
  $('paper-count').textContent = papers.length;
  if (state.project) {
    for (const group of categoryGroups(papers)) {
      const details = element('details', 'paper-category');
      if (local) {
        details.classList.toggle('empty-category', group.papers.length === 0);
        categoryDropTarget(details, group.category);
      }
      details.open = Boolean($('search').value.trim()) || !state.collapsedCategories.has(group.key);
      const summary = element('summary', 'category-heading');
      summary.dataset.categoryKey = group.key;
      const title = local || $('year').value ? group.category : `${group.year} · ${group.category}`;
      summary.append(element('span', 'category-title', title), element('span', 'category-count', group.papers.length));
      if (local && group.category !== 'Uncategorized') {
        const edit = element('button', 'quiet category-edit', '⋯');
        edit.type = 'button';
        edit.title = 'Edit category';
        edit.setAttribute('aria-label', 'Edit category ' + group.category);
        edit.dataset.task = 'true';
        updateTaskButton(edit);
        edit.onclick = () => categoryDialog(null, group.category);
        summary.append(edit);
      }
      summary.onclick = event => {
        if (event.target.closest('button')) return;
        event.preventDefault();
        details.open = !details.open;
        if (details.open) state.collapsedCategories.delete(group.key);
        else state.collapsedCategories.add(group.key);
        localStorage.setItem('paper-collapsed-categories', JSON.stringify([...state.collapsedCategories]));
      };
      summary.onkeydown = event => {
        if (event.target !== summary) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          if (!event.repeat) summary.click();
        }
      };
      const content = element('div', 'category-papers');
      group.papers.sort((a, b) => (local
        ? (a.category_order ?? 0) - (b.category_order ?? 0)
        : (a.program_order ?? Infinity) - (b.program_order ?? Infinity)) || a.title.localeCompare(b.title));
      for (const paper of group.papers) content.append(paperCard(paper));
      details.append(summary, content);
      list.append(details);
      if (focusedCategory === group.key) summary.focus({preventScroll: true});
    }
  } else {
    for (const paper of papers) list.append(paperCard(paper));
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
  list.scrollTop = scrollTop;
  renderYearAction();
}
function renderYearAction() {
  const year = $('year').value;
  const complete = state.yearStatus[year]?.status === 'downloaded';
  const task = [...(state.job?.status === 'running' ? [state.job] : []), ...(state.job?.queue || [])]
    .find(job => job.action === 'download_year' && job.project_id === state.projectId && String(job.year) === year);
  $('download-year').textContent = task ? (task.status === 'running' ? `Downloading ${year}…` : `${year} queued`)
    : complete ? `${year} downloaded` : year ? `Download ${year}` : 'Download year';
  $('download-year').disabled = !year || complete || Boolean(task);
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
  else header.append(taskButton(paper.pdf_unavailable ? 'Check again' : 'Download paper', 'download'));
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
  const url = section?.url || paper.viewer_url;
  if (paper.viewer_type === 'pdf' && section?.page && PaperPDF.jumpTo(url, section.page)) return;
  PaperPDF.close();
  if (paper.viewer_url) {
    const frame = element('iframe');
    frame.title = paper.title;
    if (paper.viewer_type === 'pdf') PaperPDF.prepare(frame, paper, url, section?.page);
    else {
      frame.setAttribute('sandbox', '');
      frame.src = url;
    }
    container.replaceChildren(frame);
    return;
  }
  const empty = element('div', 'empty-state');
  empty.append(element('div', 'book-icon', '▤'));
  if (paper.downloaded) {
    empty.append(element('h2', '', 'Ready to prepare for reading'), element('p', '', 'This older paper needs a one-time conversion before it can open in your browser.'), taskButton('Open paper in reader', 'prepare'));
  } else {
    empty.append(element('h2', '', paper.pdf_unavailable ? 'No PDF found' : 'Add this paper to your library'),
      element('p', '', paper.pdf_unavailable ? paper.error : 'Download the full paper to read it here and explore its structure.'),
      taskButton(paper.pdf_unavailable ? 'Check again' : 'Download paper', 'download'));
    if (/^https?:\/\//i.test(paper.source_url || '')) {
      const source = element('a', 'paper-source', 'Open source');
      source.href = paper.source_url; source.target = '_blank'; source.rel = 'noopener noreferrer';
      empty.append(source);
    }
    if (paper.error && !paper.pdf_unavailable) {
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
  if (!previous || previous.id !== paper.id || previous.viewer_url !== paper.viewer_url || previous.downloaded !== paper.downloaded || previous.pdf_unavailable !== paper.pdf_unavailable || previous.error !== paper.error) showDocument(paper);
}
async function selectPaper(id, {persist = true} = {}) {
  const requestId = ++state.request;
  const projectId = state.projectId;
  state.selected = id;
  renderPaperSelection();
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
  if ($('year').value && catalogNeedsRefresh() && !catalogLookupActive() && !state.catalogFailure) await queueYearCatalog();
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
  $('job-title').textContent = `${labels[job.action] || 'Task'} · ${busy ? 'in progress' : job.status === 'done' ? 'complete' : job.status === 'unavailable' ? 'no PDF found' : 'failed — see progress for details'}${queue.length ? ` · ${queue.length} queued` : ''}`;
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
$('search').oninput = () => window.ConferenceSearch ? window.ConferenceSearch.input() : renderList();
$('year').onchange = () => {
  clearSelection();
  $('search').value = '';
  $('availability').value = 'all';
  switchTab('papers');
  queueYearCatalog();
};
$('availability').onchange = () => window.ConferenceSearch?.active() ? window.ConferenceSearch.input() : renderList();
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
function catalogNeedsRefresh(year = $('year').value) {
  const info = state.yearStatus[String(year)];
  return !info?.catalogued || !info?.categories_loaded;
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
  if (year && state.project?.kind === 'conference' && (force || catalogNeedsRefresh(year))) {
    state.pendingCatalog = {project_id: state.projectId, year, force};
  }
  renderList();
  await processPendingCatalog();
}
async function processPendingCatalog() {
  const pending = state.pendingCatalog;
  if (!pending || state.startingCatalog || state.job?.status === 'running') return;
  if (catalogKey(pending.project_id, pending.year) !== catalogKey() || (!pending.force && !catalogNeedsRefresh(pending.year))) {
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
  if (!state.searchScopeInitialized) {
    $('search-scope').value = local ? 'current' : 'all';
    state.searchScopeInitialized = true;
  }
  $('manage-project').hidden = !local;
  $('add-paper').hidden = !local;
  $('upload-papers').hidden = !local;
  $('download-year').hidden = local;
  $('download-scope').hidden = local;
  $('year').hidden = local;
  $('availability').style.width = local ? '100%' : '';
}
function clearSelection() {
  state.renamingPaper?.cancel();
  endPaperDrag();
  PaperPDF.close();
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
async function switchProject(id, {keepSearch = false, restore = true} = {}) {
  state.projectId = id;
  state.project = state.projects.find(project => project.id === id) || null;
  state.papers = [];
  state.yearStatus = {};
  state.pendingCatalog = null;
  state.catalogFailure = null;
  localStorage.setItem('paper-project', id);
  clearSelection();
  if (!keepSearch) {
    $('search').value = '';
    $('search-scope').value = state.project?.kind === 'local' ? 'current' : 'all';
  }
  $('year').value = '';
  if (!keepSearch) $('availability').value = 'all';
  switchTab('papers');
  renderList();
  await refreshLibrary();
  if (restore && state.projectId === id) await restoreSelection();
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
let importTarget = null;
let importing = false;
function addPaperDialog(upload) {
  if (importing) return;
  importTarget = {projectId: state.projectId, upload};
  $('add-form').reset();
  $('add-dialog-title').textContent = upload ? 'Upload PDFs' : 'Add paper by URL';
  $('import-url-fields').hidden = upload;
  $('paper-url').disabled = upload;
  $('paper-url').required = !upload;
  $('import-file-fields').hidden = !upload;
  $('paper-file').disabled = !upload;
  $('paper-file').required = upload;
  $('import-title-field').hidden = false;
  $('import-progress').hidden = true;
  $('import-error').textContent = '';
  $('add-dialog').showModal();
  $(upload ? 'paper-file' : 'paper-url').focus();
}
$('add-paper').onclick = () => addPaperDialog(false);
$('upload-papers').onclick = () => addPaperDialog(true);
$('paper-file').onchange = () => {
  $('import-title-field').hidden = $('paper-file').files.length > 1;
  $('import-error').textContent = '';
};
$('add-dialog').oncancel = event => { if (importing) event.preventDefault(); };
$('add-form').onsubmit = async event => {
  event.preventDefault();
  if (importing || !importTarget) return;
  const {projectId, upload} = importTarget;
  const files = [...$('paper-file').files];
  const url = $('paper-url').value.trim();
  const title = $('import-title').value.trim();
  $('import-error').textContent = '';
  if (upload && !files.length) { $('import-error').textContent = 'Choose a PDF to upload.'; return; }
  const oversized = files.find(file => file.size > 100 * 1024 * 1024);
  if (oversized) { $('import-error').textContent = `${oversized.name} exceeds the 100 MB limit.`; return; }
  importing = true;
  $('add-form').querySelectorAll('input, button').forEach(control => control.disabled = true);
  let lastPaperId = null;
  let completed = 0;
  try {
    if (upload) {
      $('import-progress').hidden = false;
      for (const file of files) {
        $('import-progress').textContent = `Uploading ${completed + 1} of ${files.length}: ${file.name}`;
        const form = new FormData();
        form.append('file', file);
        form.append('title', files.length === 1 ? title : '');
        const result = await api('/api/projects/' + projectId + '/upload', {method: 'POST', headers: {'X-Library-Token': state.token}, body: form});
        lastPaperId = result.paper_id;
        completed++;
      }
    } else if (!await startJob({action: 'import_url', project_id: projectId, url, title})) return;
    $('add-dialog').close();
    switchTab('papers');
  } catch (error) {
    $('import-error').textContent = upload
      ? `${files[completed]?.name || 'Upload'}: ${error.message}${completed ? ` ${completed} PDF(s) already saved; retrying will skip duplicates.` : ''}`
      : error.message;
  } finally {
    if (completed && projectId === state.projectId) {
      try {
        $('search').value = '';
        $('availability').value = 'all';
        await refreshLibrary();
        if (projectId === state.projectId && lastPaperId) await selectPaper(lastPaperId);
        switchTab('papers');
      } catch (error) { notify(`PDFs saved. Could not refresh the library: ${error.message}`); }
    }
    importing = false;
    $('import-progress').hidden = true;
    $('add-form').querySelectorAll('input, button').forEach(control => control.disabled = false);
    $('paper-url').disabled = upload;
    $('paper-file').disabled = !upload;
  }
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

let categoryTarget = null;
let savingCategory = false;
function categoryDialog(paper, category = '') {
  if (savingCategory || state.movingPaper || state.project?.kind !== 'local') return;
  categoryTarget = {projectId: state.projectId, paperId: paper?.id, category: paper?.category || category};
  $('category-dialog-title').textContent = paper ? 'Categorize paper' : 'Edit category';
  $('category-context').textContent = paper ? paper.title : `${state.papers.filter(p => p.category === category).length} papers in “${category}”`;
  $('category-name').value = categoryTarget.category;
  const names = [...new Set(state.papers.map(p => p.category).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  $('category-suggestions').replaceChildren(...names.map(name => new Option(name, name)));
  $('category-help').textContent = paper
    ? 'Choose an existing category or enter a new name. Leave blank for Uncategorized.'
    : 'An existing name merges the groups. Removing the category keeps its papers in Uncategorized.';
  $('remove-category').hidden = Boolean(paper);
  $('category-error').textContent = '';
  $('category-dialog').showModal();
  $('category-name').focus();
  $('category-name').select();
}
$('categorize-paper').onclick = () => {
  const paper = state.papers.find(p => p.id === state.selected);
  if (paper) categoryDialog(paper);
};
$('category-dialog').oncancel = event => { if (savingCategory) event.preventDefault(); };
async function saveCategory(name) {
  if (savingCategory || !categoryTarget) return;
  const target = categoryTarget;
  savingCategory = true;
  $('category-error').textContent = '';
  $('category-form').querySelectorAll('input, button').forEach(control => control.disabled = true);
  try {
    const url = target.paperId ? `/api/papers/${target.paperId}?project=${encodeURIComponent(target.projectId)}`
      : `/api/projects/${target.projectId}/categories`;
    const payload = target.paperId ? {category: name} : {category: target.category, name};
    const result = await api(url, writeOptions('PATCH', payload));
    const key = category => JSON.stringify([target.projectId, 'local', category || 'Uncategorized']);
    state.collapsedCategories.delete(key(result.category));
    if (!target.paperId) state.collapsedCategories.delete(key(target.category));
    localStorage.setItem('paper-collapsed-categories', JSON.stringify([...state.collapsedCategories]));
    $('category-dialog').close();
    if (state.projectId === target.projectId) {
      $('search').value = '';
      await refreshLibrary();
    }
  } catch (error) {
    if ($('category-dialog').open) $('category-error').textContent = error.message;
    else notify(error.message);
  } finally {
    savingCategory = false;
    $('category-form').querySelectorAll('input, button').forEach(control => control.disabled = false);
  }
}
$('category-form').onsubmit = event => { event.preventDefault(); saveCategory($('category-name').value); };
$('remove-category').onclick = () => saveCategory('');
