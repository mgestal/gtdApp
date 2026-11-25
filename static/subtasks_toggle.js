// Desplegables de subtareas en las listas (Inicio, NextActions, Próximo, proyecto, carpeta).
// Recuerda en sessionStorage qué tareas tienen las subtareas abiertas, para que al
// marcar/desmarcar una tarea o subtarea (que recarga la página) se mantenga el estado.
(function () {
  const KEY = 'gtd.openSubtasks';

  function load() {
    try {
      const ids = JSON.parse(sessionStorage.getItem(KEY) || '[]');
      return new Set(Array.isArray(ids) ? ids.map(String) : []);
    } catch (e) {
      return new Set();
    }
  }

  function save(set) {
    try { sessionStorage.setItem(KEY, JSON.stringify(Array.from(set))); } catch (e) { /* sin almacenamiento */ }
  }

  function setOpen(taskId, open) {
    const el = document.getElementById('subtasks-' + taskId);
    if (!el) return false;
    const icon = document.getElementById('subtasks-icon-' + taskId);
    el.style.display = open ? 'block' : 'none';
    if (icon) icon.textContent = open ? '▾' : '▸';
    return true;
  }

  window.toggleSubtasks = function (taskId) {
    const id = String(taskId);
    const el = document.getElementById('subtasks-' + id);
    if (!el) return;
    const open = el.style.display === 'none' || !el.style.display;
    setOpen(id, open);
    const set = load();
    if (open) set.add(id); else set.delete(id);
    save(set);
  };

  document.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.js-subtasks-toggle');
    if (!btn) return;
    const taskId = btn.getAttribute('data-task-id');
    if (taskId) window.toggleSubtasks(taskId);
  });

  function restore() {
    load().forEach(function (id) { setOpen(id, true); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restore);
  } else {
    restore();
  }
})();
