// Tests de static/live_update.js con un DOM mínimo simulado (node tests/test_live_update.js).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const script = fs.readFileSync("static/live_update.js", "utf8");

function classList(initial = []) {
  const set = new Set(initial);
  return {
    toggle(name, force) { (force === undefined ? !set.has(name) : force) ? set.add(name) : set.delete(name); },
    contains: name => set.has(name),
  };
}

function el(props = {}) {
  return Object.assign({
    dataset: {}, attrs: {}, children: [], textContent: "", classList: classList(),
    getAttribute(name) { return this.attrs[name] ?? null; },
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { this.children.push(child); return child; },
    closest(sel) { return sel === "aside" ? null : this.row || null; },
    matches() { return !!this.isRow; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    remove() { this.removed = true; },
  }, props);
}

function load({ selectors = {}, scope = null, fetchResponse = null } = {}) {
  const calls = { fetch: [], reload: 0, stored: {} };
  const context = {
    console,
    URL,
    RegExp,
    FormData: class { constructor(form) { this.form = form; } },
    sessionStorage: {
      getItem: key => calls.stored[key] ?? null,
      setItem: (key, value) => { calls.stored[key] = value; },
      removeItem: key => { delete calls.stored[key]; },
    },
    location: { pathname: "/gtd/", search: "", reload: () => { calls.reload += 1; } },
    alert: msg => { calls.alert = msg; },
    fetch: async (url, options) => {
      calls.fetch.push({ url, options });
      return { ok: true, json: async () => fetchResponse };
    },
    document: {
      querySelector: () => null,
      querySelectorAll: sel => selectors[sel] || [],
      createElement: tag => el({ tag }),
      createTextNode: text => ({ text }),
      getElementById: () => null,
    },
  };
  context.window = Object.assign(context, {
    scrollY: 120, addEventListener() {}, scrollTo() {}, GTD_VIEW_SCOPE: scope,
  });
  vm.runInNewContext(script, context);
  return { live: context.window.gtdLive, calls, context };
}

(async () => {
  // Ámbito: sin ámbito todo encaja; con ámbito solo los destinos listados.
  let { live } = load();
  assert.equal(live.taskStillFits("folder", 3), true);
  ({ live } = load({ scope: { task_folders: [1, 4], task_projects: [9], project_folders: [1] } }));
  assert.equal(live.taskStillFits("folder", 4), true);
  assert.equal(live.taskStillFits("folder", 2), false);
  assert.equal(live.taskStillFits("project", 9), true);
  assert.equal(live.projectStillFits(1), true);
  assert.equal(live.projectStillFits("7"), false);
  ({ live } = load({ scope: { task_folders: [], task_projects: [], project_folders: [] } }));  // Inbox
  assert.equal(live.taskStillFits("project", 9), false);

  // Contadores de la barra lateral (-1 = falta la etiqueta NextAction)
  const inbox = el({ dataset: { sidebarCount: "inbox" } });
  const next = el({ dataset: { sidebarCount: "next" } });
  ({ live } = load({ selectors: { "[data-sidebar-count]": [inbox, next] } }));
  live.setCounts({ inbox: 3, next: -1 });
  assert.equal(inbox.textContent, "3");
  assert.equal(next.textContent, "!");

  // Completar: sin recarga, la fila se marca y los contadores se actualizan
  const row = el({ isRow: true });
  const form = el({ attrs: { action: "/gtd/tasks/5/toggle" }, action: "/gtd/tasks/5/toggle" });
  form.row = row;
  const count = el({ dataset: { sidebarCount: "today" } });
  let env = load({
    selectors: { '[data-task-id="5"]': [row], "form[action]": [form], "[data-sidebar-count]": [count] },
    fetchResponse: { ok: true, task_id: 5, completed: true, reload: false, counts: { today: 0 } },
  });
  await env.live.submitToggle(form);
  assert.equal(env.calls.fetch[0].options.headers["X-Requested-With"], "fetch");
  assert.equal(row.classList.contains("row-done"), true);
  assert.equal(count.textContent, "0");
  assert.equal(env.calls.reload, 0);

  // Periódica o promoción de NextAction: recarga guardando el scroll
  env = load({ selectors: { "form[action]": [form] }, fetchResponse: { ok: true, reload: true } });
  await env.live.submitToggle(form);
  assert.equal(env.calls.reload, 1);
  assert.equal(JSON.parse(env.calls.stored["gtd_live_scroll:/gtd/"]).y, 120);

  // Etiquetas: se añaden a cada contenedor de la tarea, sin duplicar
  const box = el();
  const boxWithTag = el({ querySelectorAll: () => [el({ attrs: { href: "/tags/3" } })] });
  const board = el({ dataset: { tagStyle: "pill" } });
  ({ live } = load({ selectors: { '[data-task-tags="5"]': [box, boxWithTag, board] } }));
  live.addTag(5, { id: 3, name: "Casa", url: "/tags/3" });
  assert.equal(box.children[0].href, "/tags/3");
  assert.equal(box.children[0].textContent, "@Casa");
  assert.equal(boxWithTag.children.length, 0);
  assert.equal(board.children[0].className, "project-kanban-pill");

  console.log("live_update: ámbito, contadores, completar, recarga con scroll y etiquetas OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
