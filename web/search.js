'use strict';

window.ConferenceSearch = (() => {
  let rows = [], total = 0, coverage = null, error = '', loading = false;
  let signature = '', limit = 100, timer, controller, selecting = 0;
  const active = () => $('search-scope').value === 'all' && Boolean($('search').value.trim());
  const key = () => JSON.stringify([$('search').value.trim(), $('availability').value]);
  function reset() {
    clearTimeout(timer);
    controller?.abort();
    controller = null;
    signature = '';
    loading = false;
    rows = [];
  }
  function input() {
    reset();
    limit = 100;
    error = '';
    coverage = null;
    total = 0;
    switchTab('papers');
    renderList();
  }
  function render() {
    if (!active()) return;
    if (signature !== key()) {
      signature = key();
      loading = true;
      clearTimeout(timer);
      timer = setTimeout(fetchResults, 250);
    }
    const list = $('paper-list');
    const scroll = list.scrollTop;
    const focusedPaper = list.contains(document.activeElement) ? document.activeElement.dataset.searchPaper : null;
    list.replaceChildren();
    $('paper-count').textContent = total;
    const note = element('div', 'catalog-status');
    let text = 'All conferences · all years';
    if (coverage && coverage.indexed < coverage.total) {
      text += ` · ${coverage.indexed}/${coverage.total} editions indexed`;
      if (coverage.running) text += ' · Loading paper lists…';
      else if (coverage.errors.length) text += ` · ${coverage.errors.length} unavailable`;
    }
    note.append(document.createTextNode(text));
    if (coverage?.errors.length && !coverage.running) {
      const retry = element('button', 'quiet', 'Retry missing lists');
      retry.title = coverage.errors.map(item => `${item.conference.toUpperCase()} ${item.year}: ${item.message}`).join('\n');
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          const result = await api('/api/search/index', writeOptions('POST', {retry: true}));
          coverage = result.index;
          clearTimeout(timer);
          fetchResults();
        } catch (failure) { error = failure.message; render(); }
      };
      note.append(retry);
    }
    list.append(note);
    if (error) {
      const message = element('div', 'catalog-status', error + ' ');
      const retry = element('button', 'quiet', 'Retry');
      retry.onclick = () => { clearTimeout(timer); fetchResults(); };
      message.append(retry);
      list.append(message);
    }
    for (const paper of rows) {
      const card = paperCard(paper);
      card.dataset.searchPaper = paper.id;
      card.onclick = () => openPaper(paper);
      list.append(card);
      if (paper.id === focusedPaper) card.focus({preventScroll: true});
    }
    if (!rows.length && !error) list.append(element('p', 'panel-empty', loading ? 'Searching conference papers…'
      : coverage?.running ? 'No matches yet. More paper lists are loading…'
      : 'No matching papers in the indexed conferences.'));
    if (rows.length < total) {
      if (limit < 5000) {
        const more = element('button', 'quiet search-more', `Show more (${rows.length} of ${total})`);
        more.disabled = loading;
        more.onclick = () => { limit += 100; clearTimeout(timer); fetchResults(); };
        list.append(more);
      } else list.append(element('p', 'catalog-status', `${total} matches. Refine your search to see fewer results.`));
    }
    list.scrollTop = scroll;
  }
  async function fetchResults() {
    if (!active()) return;
    controller?.abort();
    const request = controller = new AbortController();
    const requestedKey = signature = key();
    loading = true;
    try {
      const params = new URLSearchParams({q: $('search').value.trim(), availability: $('availability').value, limit});
      const data = await api('/api/search?' + params, {signal: request.signal});
      if (request !== controller || !active() || requestedKey !== key()) return;
      rows = data.papers;
      total = data.total;
      coverage = data.index;
      error = '';
      if (!coverage.started && !coverage.running && coverage.indexed < coverage.total) {
        const result = await api('/api/search/index', {...writeOptions('POST', {}), signal: request.signal});
        if (request !== controller || !active() || requestedKey !== key()) return;
        coverage = result.index;
      }
    } catch (failure) {
      if (failure.name === 'AbortError') return;
      if (request !== controller || !active() || requestedKey !== key()) return;
      error = failure.status === 404
        ? 'The running server needs a restart to enable global search. Let any downloads finish first, then restart the server and refresh this page.'
        : failure.message;
    } finally {
      if (request === controller && active() && requestedKey === key()) {
        loading = false;
        render();
        timer = setTimeout(fetchResults, 2500);
      }
    }
  }
  async function openPaper(paper) {
    const request = ++selecting;
    try {
      if (state.projectId !== paper.project_id) await switchProject(paper.project_id, {keepSearch: true, restore: false});
      if (request !== selecting || state.projectId !== paper.project_id) return;
      await selectPaper(paper.id);
    } catch (failure) { notify(failure.message); }
  }
  $('search-scope').onchange = input;
  return {active, render, input, reset};
})();
