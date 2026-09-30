'use strict';

(() => {
  let conferences = [];
  let years = new Set();
  let submitting = false;
  const selectedConferences = () => $('download-all-conferences').checked ? conferences
    : conferences.filter(project => $('download-conferences').querySelector(`input[value="${project.id}"]`).checked);
  function checkbox(value, label, checked, disabled, change) {
    const row = element('label', 'download-check');
    const input = element('input');
    input.type = 'checkbox';
    input.value = value;
    input.checked = checked;
    input.disabled = disabled;
    input.onchange = change;
    row.append(input, document.createTextNode(label));
    return row;
  }
  function renderYears() {
    const available = [...new Set(selectedConferences().flatMap(project => project.years))].sort((a, b) => b - a);
    years = new Set([...years].filter(year => available.includes(year)));
    $('download-years').replaceChildren(...available.map(year => checkbox(String(year), String(year),
      $('download-all-years').checked || years.has(year), $('download-all-years').checked, event => {
        if (event.target.checked) years.add(year);
        else years.delete(year);
        renderSelection();
      })));
    if (!available.length) $('download-years').append(element('p', 'field-hint', 'Select a conference to see its years.'));
    renderSelection();
  }
  function renderSelection() {
    const editions = selectedConferences().map(project => ({project,
      years: project.years.filter(year => $('download-all-years').checked || years.has(year)).sort((a, b) => b - a)
    })).filter(edition => edition.years.length);
    const count = editions.reduce((total, edition) => total + edition.years.length, 0);
    $('download-summary').textContent = count
      ? `${count} edition${count === 1 ? '' : 's'} across ${editions.length} conference${editions.length === 1 ? '' : 's'}`
      : 'Select at least one conference and year.';
    $('download-editions').replaceChildren(...editions.map(edition =>
      element('li', '', `${edition.project.name}: ${edition.years.join(', ')}`)));
    $('download-preview').hidden = !count;
    $('download-submit').disabled = submitting || !count;
    $('download-submit').textContent = submitting ? 'Queuing…' : count ? `Queue ${count} edition${count === 1 ? '' : 's'}` : 'Queue downloads';
  }
  $('download-scope').onclick = () => {
    if (submitting) return;
    conferences = state.projects.filter(project => project.kind === 'conference');
    const current = conferences.find(project => project.id === state.projectId) || conferences[0];
    $('download-form').reset();
    $('download-error').textContent = '';
    years = new Set([Number($('year').value) || Math.max(...(current?.years || []))]);
    $('download-conferences').replaceChildren(...conferences.map(project =>
      checkbox(project.id, project.name, project.id === current?.id, false, renderYears)));
    renderYears();
    $('download-dialog').showModal();
  };
  $('download-all-conferences').onchange = () => {
    const all = $('download-all-conferences').checked;
    for (const input of $('download-conferences').querySelectorAll('input')) {
      if (all) input.dataset.previousChecked = String(input.checked);
      input.checked = all || input.dataset.previousChecked === 'true';
      input.disabled = all;
    }
    renderYears();
  };
  $('download-all-years').onchange = renderYears;
  $('download-dialog').oncancel = event => { if (submitting) event.preventDefault(); };
  $('download-form').onsubmit = async event => {
    event.preventDefault();
    if (submitting || $('download-submit').disabled) return;
    const payload = {
      conferences: $('download-all-conferences').checked ? 'all' : selectedConferences().map(project => project.id),
      years: $('download-all-years').checked ? 'all' : [...years].sort((a, b) => b - a),
    };
    submitting = true;
    $('download-error').textContent = '';
    $('download-form').querySelectorAll('input, button').forEach(control => control.disabled = true);
    renderSelection();
    try {
      const result = await api('/api/downloads', writeOptions('POST', payload));
      renderJob(result.job);
      $('job-log').hidden = false;
      $('toggle-log').textContent = 'Hide progress';
      $('download-dialog').close();
      notify(result.added ? `Added ${result.added} edition${result.added === 1 ? '' : 's'} to the download queue.` : 'Those editions are already in the download queue.');
    } catch (error) { $('download-error').textContent = error.message; }
    finally {
      submitting = false;
      $('download-form').querySelectorAll('input, button').forEach(control => control.disabled = false);
      for (const input of $('download-conferences').querySelectorAll('input')) input.disabled = $('download-all-conferences').checked;
      renderYears();
    }
  };
})();
