(function () {
  var OPTIONS = [
    ['tomorrow', 'Mañana'],
    ['monday', 'Próximo lunes'],
    ['week', 'Próxima semana'],
    ['month', 'Próximo mes'],
    ['manual', 'Manual…']
  ];
  var menu, form, fMode, fNext, fDate, current;

  function hide() { if (menu) menu.hidden = true; }

  function build() {
    form = document.createElement('form');
    form.method = 'post';
    fNext = document.createElement('input');
    fNext.type = 'hidden'; fNext.name = 'next';
    fMode = document.createElement('input');
    fMode.type = 'hidden'; fMode.name = 'mode';
    fDate = document.createElement('input');
    fDate.type = 'date'; fDate.name = 'date';
    fDate.tabIndex = -1;
    fDate.style.cssText = 'position:fixed;opacity:0;width:1px;height:1px;border:0;padding:0;pointer-events:none;';
    form.appendChild(fNext); form.appendChild(fMode); form.appendChild(fDate);
    document.body.appendChild(form);
    fDate.addEventListener('change', function () {
      if (fDate.value) { fMode.value = 'manual'; form.submit(); }
    });

    menu = document.createElement('div');
    menu.className = 'postpone-menu';
    menu.hidden = true;
    OPTIONS.forEach(function (o) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = o[1];
      b.dataset.mode = o[0];
      menu.appendChild(b);
    });
    document.body.appendChild(menu);

    menu.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-mode]');
      if (!b || !current) return;
      if (b.dataset.mode === 'manual') {
        var r = current.getBoundingClientRect();
        fDate.style.top = r.bottom + 'px';
        fDate.style.left = r.left + 'px';
        fDate.value = '';
        void fDate.offsetHeight;
        fDate.focus({ preventScroll: true });
        try { fDate.showPicker(); } catch (err) { fDate.click(); }
        hide();
        return;
      }
      fMode.value = b.dataset.mode;
      fDate.disabled = true;
      form.submit();
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.postpone-btn');
    if (!btn) {
      if (!e.target.closest('.postpone-menu')) hide();
      return;
    }
    if (!menu) build();
    if (!menu.hidden && current === btn) { hide(); return; }
    current = btn;
    form.action = btn.dataset.action;
    fNext.value = btn.dataset.next;
    fDate.disabled = false;
    menu.hidden = false;
    var r = btn.getBoundingClientRect();
    var top = r.bottom + 4;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 4;
    menu.style.top = Math.max(8, top) + 'px';
    menu.style.left = Math.max(8, Math.min(r.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  });

  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
})();
