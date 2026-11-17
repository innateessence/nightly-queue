(function () {
  var root = document.documentElement;
  var toggle = document.querySelector('.theme-toggle');
  function updateThemeButton() {
    var dark = root.dataset.theme === 'dark';
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.setAttribute('aria-label', 'Switch to ' + (dark ? 'light' : 'dark') + ' theme');
  }
  if (toggle) {
    updateThemeButton();
    toggle.addEventListener('click', function () {
      var theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = theme;
      updateThemeButton();
      try { localStorage.setItem('theme', theme); } catch (e) {}
    });
  }
  function openPanel(id, focus) {
    var panel = document.getElementById(id);
    if (!panel || !panel.matches('details.add-panel')) { return; }
    panel.open = true;
    if (focus) {
      panel.scrollIntoView({block: 'start'});
      var input = panel.querySelector('input:not([type="hidden"]), select, textarea');
      if (input) { input.focus({preventScroll: true}); }
    }
  }
  document.querySelectorAll('[data-open-panel]').forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      openPanel(link.dataset.openPanel, true);
    });
  });
  document.querySelectorAll('[data-close-panel]').forEach(function (button) {
    button.addEventListener('click', function () {
      var panel = button.closest('details');
      panel.open = false;
      var opener = document.querySelector('[data-open-panel="' + panel.id + '"]');
      if (opener) { opener.focus(); }
    });
  });
  function openHash() { openPanel(location.hash.slice(1), false); }
  window.addEventListener('hashchange', openHash);
  openHash();
  root.classList.add('js');
})();
