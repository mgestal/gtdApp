/* Actualiza la página tras completar, etiquetar o arrastrar sin recargarla entera.
 *
 * Expone window.gtdLive. Las vistas que filtran por ubicación (Inbox, carpeta,
 * proyecto) publican window.GTD_VIEW_SCOPE (ver base.html) para saber si una
 * tarea o proyecto movido sigue encajando en la página.
 */
(function () {
  "use strict";

  const ROW_SELECTOR = "tr, .kanban-card, .project-kanban-task, .review-mini-item";
  const SCROLL_KEY = "gtd_live_scroll:" + location.pathname + location.search;

  function sidebarScroller() {
    return document.querySelector(".sidebar");
  }

  // --- Recarga conservando el scroll (casos que no se pueden pintar en el DOM) ---
  function reload() {
    try {
      const side = sidebarScroller();
      sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ y: window.scrollY, side: side ? side.scrollTop : 0 }));
    } catch (_e) { /* sin sessionStorage: recarga normal */ }
    location.reload();
  }

  function restoreScroll() {
    let saved = null;
    try {
      saved = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
      sessionStorage.removeItem(SCROLL_KEY);
    } catch (_e) { return; }
    if (!saved) return;
    window.scrollTo(0, saved.y || 0);
    const side = sidebarScroller();
    if (side) side.scrollTop = saved.side || 0;
  }
  window.addEventListener("load", restoreScroll);

  // --- Contadores de la barra lateral ([data-sidebar-count="inbox"], ...) ---
  function setCounts(counts) {
    if (!counts) return;
    document.querySelectorAll("[data-sidebar-count]").forEach((el) => {
      const value = counts[el.dataset.sidebarCount];
      if (value === undefined) return;
      el.textContent = value === -1 ? "!" : String(value);
    });
  }

  // --- Filas de una tarea en la página (fuera de la barra lateral) ---
  function taskRows(taskId) {
    const rows = new Set();
    document.querySelectorAll(`[data-task-id="${taskId}"]`).forEach((el) => {
      if (!el.closest("aside")) rows.add(el.matches(ROW_SELECTOR) ? el : (el.closest(ROW_SELECTOR) || el));
    });
    document.querySelectorAll("form[action]").forEach((form) => {
      if (!new RegExp(`/tasks/${taskId}/toggle$`).test(form.getAttribute("action") || "")) return;
      const row = form.closest(ROW_SELECTOR);
      if (row && !row.closest("aside")) rows.add(row);
    });
    return [...rows];
  }

  function removeTask(taskId) {
    taskRows(taskId).forEach((row) => row.remove());
  }

  function setTaskDone(taskId, done) {
    taskRows(taskId).forEach((row) => row.classList.toggle("row-done", !!done));
  }

  // ¿Sigue la tarea en esta vista tras moverla a (tipo, id)? Sin ámbito: la vista no filtra por ubicación.
  function taskStillFits(targetType, targetId) {
    const scope = window.GTD_VIEW_SCOPE;
    if (!scope) return true;
    const list = targetType === "folder" ? scope.task_folders : scope.task_projects;
    return (list || []).includes(Number(targetId));
  }

  function projectStillFits(folderId) {
    const scope = window.GTD_VIEW_SCOPE;
    if (!scope) return true;
    return (scope.project_folders || []).includes(Number(folderId));
  }

  // --- Etiquetas ---
  function addTag(taskId, tag) {
    document.querySelectorAll(`[data-task-tags="${taskId}"]`).forEach((box) => {
      const already = [...box.querySelectorAll("a[href], span")].some((el) =>
        (el.getAttribute("href") || "") === tag.url || el.textContent.trim() === "@" + tag.name);
      if (already) return;
      let chip;
      if (box.dataset.tagStyle === "pill") {
        chip = document.createElement("span");
        chip.className = "project-kanban-pill";
        chip.textContent = "@" + tag.name;
      } else {
        const model = box.querySelector("a.tagchip");
        chip = document.createElement("a");
        chip.className = "tagchip taglink";
        if (model && model.getAttribute("style")) chip.setAttribute("style", model.getAttribute("style"));
        chip.href = tag.url;
        chip.textContent = "@" + tag.name;
        if (typeof window.gtdAttachTagRemove === "function") window.gtdAttachTagRemove(chip);
      }
      box.appendChild(chip);
      if (box.dataset.tagStyle !== "pill") box.appendChild(document.createTextNode(" "));
    });
  }

  // --- Árbol de la barra lateral ---
  function treeChildrenOf(folderId) {
    return document.getElementById("node-" + folderId);
  }

  function showTreeChildren(folderId) {
    const body = treeChildrenOf(folderId);
    if (body) body.style.display = "block";
    const icon = document.getElementById("icon-" + folderId);
    if (icon) icon.textContent = "▾";
  }

  function moveProjectInTree(projectId, folderId) {
    const leaf = document.querySelector(`aside .tree-leaf[data-project-id="${projectId}"]`);
    const target = treeChildrenOf(folderId);
    if (!leaf || !target) return false;
    target.appendChild(leaf);  // los proyectos van detrás de las subcarpetas
    showTreeChildren(folderId);
    return true;
  }

  function moveFolderInTree(folderId, parentId) {
    const node = document.querySelector(`aside .tree-node[data-tree-folder="${folderId}"]`);
    const target = treeChildrenOf(parentId);
    if (!node || !target) return false;
    const firstLeaf = target.querySelector(":scope > .tree-leaf");
    target.insertBefore(node, firstLeaf);  // subcarpetas antes que proyectos
    showTreeChildren(parentId);
    return true;
  }

  // --- Completar / descompletar (formularios POST /tasks/<id>/toggle) ---
  async function submitToggle(form) {
    const match = (form.getAttribute("action") || "").match(/\/tasks\/(\d+)\/toggle$/);
    let data = null;
    try {
      const res = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      });
      data = await res.json().catch(() => null);
      if (!res.ok || !data || !data.ok) throw new Error((data && data.error) || "No se pudo actualizar la tarea");
    } catch (err) {
      alert(err.message || "No se pudo actualizar la tarea");
      return;
    }
    if (data.reload) {
      reload();
      return;
    }
    setTaskDone(match ? match[1] : data.task_id, data.completed);
    setCounts(data.counts);
  }

  window.gtdLive = {
    reload,
    setCounts,
    taskRows,
    removeTask,
    setTaskDone,
    taskStillFits,
    projectStillFits,
    addTag,
    moveProjectInTree,
    moveFolderInTree,
    submitToggle,
  };
})();
