const DEFAULT_BASE_URL = "http://localhost:5000";

const state = {
  baseUrl: DEFAULT_BASE_URL,
  scope: "today",
  activeTab: null,
};

const el = {
  baseUrlInput: document.getElementById("baseUrlInput"),
  saveConfigBtn: document.getElementById("saveConfigBtn"),
  settingsDetails: document.getElementById("settingsDetails"),
  settingsUrlPreview: document.getElementById("settingsUrlPreview"),
  reloadBtn: document.getElementById("reloadBtn"),
  status: document.getElementById("status"),
  taskList: document.getElementById("taskList"),
  scopeButtons: Array.from(document.querySelectorAll(".scope-btn")),
  activePageUrl: document.getElementById("activePageUrl"),
  replacementInput: document.getElementById("replacementInput"),
  prioritySelect: document.getElementById("prioritySelect"),
  addPageBtn: document.getElementById("addPageBtn"),
};

async function loadConfig() {
  const saved = await chrome.storage.sync.get(["gtdBaseUrl"]);
  state.baseUrl = normalizeBaseUrl(saved.gtdBaseUrl || DEFAULT_BASE_URL);
  el.baseUrlInput.value = state.baseUrl;
  updateSettingsPreview();
  if (!saved.gtdBaseUrl) {
    el.settingsDetails.setAttribute("open", "");
  }
}

function normalizeBaseUrl(value) {
  const raw = (value || "").trim();
  if (!raw) {
    return DEFAULT_BASE_URL;
  }
  return raw.replace(/\/+$/, "");
}

function setStatus(msg, isError = false) {
  el.status.textContent = msg || "";
  el.status.style.color = isError ? "#b42318" : "";
}

function updateSettingsPreview() {
  if (el.settingsUrlPreview) {
    el.settingsUrlPreview.textContent = state.baseUrl;
  }
}

function formatDateTime(task) {
  const parts = [];
  if (task.due_date) {
    const [year, month, day] = task.due_date.split("-");
    parts.push(`${day}-${month}-${year}`);
  }
  if (task.due_time) {
    parts.push(task.due_time);
  }
  if (task.folder_name) {
    parts.push(`📂 ${task.folder_name}`);
  }
  if (task.project_name) {
    parts.push(`💼 ${task.project_name}`);
  } else if (!task.folder_name) {
    parts.push("Inbox");
  }
  return parts.join(" · ");
}

function renderTasks(items) {
  el.taskList.innerHTML = "";

  if (!Array.isArray(items) || items.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No hay tareas en este rango.";
    el.taskList.appendChild(li);
    return;
  }

  for (const task of items) {
    const li = document.createElement("li");
    li.className = "task-item";

    const checkCol = document.createElement("div");
    checkCol.className = "check-col";

    if ([1, 2, 3].includes(task.priority)) {
      const pr = document.createElement("span");
      pr.className = `prio prio-${task.priority}`;
      pr.setAttribute("aria-hidden", "true");
      checkCol.appendChild(pr);
    }

    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "task-toggle";
    toggle.checked = !!task.completed;
    toggle.addEventListener("change", () => onToggleTask(task.id, toggle));
    checkCol.appendChild(toggle);

    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "task-title";
    title.textContent = task.title;

    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.textContent = formatDateTime(task);

    body.appendChild(title);
    body.appendChild(meta);

    li.appendChild(checkCol);
    li.appendChild(body);
    el.taskList.appendChild(li);
  }
}

async function fetchTasks() {
  setStatus("Cargando tareas...");
  try {
    const response = await fetch(`${state.baseUrl}/api/extension/tasks?scope=${encodeURIComponent(state.scope)}`, {
      credentials: "include",
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();
    if (!data.ok) {
      throw new Error(data.error || "No se pudieron cargar tareas");
    }

    renderTasks(data.items || []);
    setStatus(`Mostrando ${data.items.length} tarea(s)`);
  } catch (error) {
    renderTasks([]);
    setStatus(`Error al cargar: ${error.message}`, true);
  }
}

async function onToggleTask(taskId, checkboxEl) {
  checkboxEl.disabled = true;
  try {
    const response = await fetch(`${state.baseUrl}/api/extension/tasks/${taskId}/toggle`, {
      method: "POST",
      credentials: "include",
    });

    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }

    setStatus("Tarea actualizada");
    await fetchTasks();
  } catch (error) {
    setStatus(`No se pudo actualizar: ${error.message}`, true);
    checkboxEl.checked = !checkboxEl.checked;
  } finally {
    checkboxEl.disabled = false;
  }
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  state.activeTab = tabs[0] || null;

  if (!state.activeTab || !state.activeTab.url) {
    el.activePageUrl.textContent = "No hay pestaña activa disponible.";
    return;
  }

  el.activePageUrl.textContent = state.activeTab.url;
  el.activePageUrl.title = state.activeTab.url;
}

async function onAddPageTask() {
  if (!state.activeTab || !state.activeTab.url) {
    setStatus("No se detecta URL de pestaña activa.", true);
    return;
  }

  const replacementText = (el.replacementInput.value || "").trim();
  el.addPageBtn.disabled = true;
  setStatus("Guardando página en Inbox...");

  try {
    const response = await fetch(`${state.baseUrl}/api/extension/tasks/add_page`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        url: state.activeTab.url,
        page_title: state.activeTab.title || "",
        replacement_text: replacementText,
        priority: parseInt(el.prioritySelect.value) || null,
      }),
    });

    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }

    el.replacementInput.value = "";
    el.prioritySelect.value = "";
    setStatus("Página añadida a Inbox");
    await fetchTasks();
  } catch (error) {
    setStatus(`No se pudo añadir: ${error.message}`, true);
  } finally {
    el.addPageBtn.disabled = false;
  }
}

async function onSaveConfig() {
  state.baseUrl = normalizeBaseUrl(el.baseUrlInput.value);
  await chrome.storage.sync.set({ gtdBaseUrl: state.baseUrl });
  updateSettingsPreview();
  el.settingsDetails.removeAttribute("open");
  setStatus("Configuración guardada");
  await fetchTasks();
}

function bindEvents() {
  el.saveConfigBtn.addEventListener("click", onSaveConfig);
  el.reloadBtn.addEventListener("click", fetchTasks);
  el.addPageBtn.addEventListener("click", onAddPageTask);

  for (const btn of el.scopeButtons) {
    btn.addEventListener("click", async () => {
      state.scope = btn.dataset.scope;

      for (const b of el.scopeButtons) {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      }

      await fetchTasks();
    });
  }
}

async function init() {
  await loadConfig();
  bindEvents();
  await getActiveTab();
  await fetchTasks();
}

init();
