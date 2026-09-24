// Apply saved preferences before painting, including when a paper is reopened.
(() => {
  const root = document.documentElement;
  try {
    const theme = localStorage.getItem('paper-theme');
    root.dataset.theme = theme === 'dark' || theme === 'light' ? theme :
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    root.dataset.sidebarHidden = String(localStorage.getItem('paper-sidebar-hidden') === 'true');
    root.dataset.searchHidden = String(localStorage.getItem('paper-search-hidden') === 'true');
  } catch {
    root.dataset.theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
})();
