const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("static/subtasks_toggle.js", "utf8");
const storage = {};
const sessionStorage = {
  getItem: key => (key in storage ? storage[key] : null),
  setItem: (key, value) => { storage[key] = String(value); },
};

function page(taskIds) {
  const elements = {};
  for (const id of taskIds) {
    elements[`subtasks-${id}`] = {style: {display: "none"}};
    elements[`subtasks-icon-${id}`] = {textContent: "▸"};
  }
  const window = {};
  let clickHandler = null;
  vm.runInNewContext(code, {
    window, sessionStorage, JSON, Array, Set, String,
    document: {
      readyState: "complete",
      getElementById: id => elements[id] || null,
      addEventListener(name, cb) { if (name === "click") clickHandler = cb; },
    },
  });
  return {elements, window, click: btn => clickHandler({target: {closest: () => btn}})};
}

// Abrir 7 y 9 y cerrar 9 en una página.
let p = page([7, 9]);
p.window.toggleSubtasks(7);
p.window.toggleSubtasks(9);
p.window.toggleSubtasks(9);
assert.equal(p.elements["subtasks-7"].style.display, "block");
assert.equal(p.elements["subtasks-icon-7"].textContent, "▾");
assert.equal(p.elements["subtasks-9"].style.display, "none");

// Tras recargar (marcar una tarea/subtarea), 7 sigue abierta y 9 cerrada.
p = page([7, 9]);
assert.equal(p.elements["subtasks-7"].style.display, "block");
assert.equal(p.elements["subtasks-icon-7"].textContent, "▾");
assert.equal(p.elements["subtasks-9"].style.display, "none");

// El botón con data-task-id (Inicio) usa la misma lógica.
p.click({getAttribute: () => "9"});
assert.equal(p.elements["subtasks-9"].style.display, "block");
assert.deepEqual(JSON.parse(storage["gtd.openSubtasks"]).sort(), ["7", "9"]);

// Una tarea guardada que ya no está en la página no rompe nada.
p = page([9]);
assert.equal(p.elements["subtasks-9"].style.display, "block");

// Sin sessionStorage disponible sigue funcionando (sin recordar).
const broken = {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }};
const els = {"subtasks-1": {style: {display: "none"}}, "subtasks-icon-1": {textContent: "▸"}};
const w = {};
vm.runInNewContext(code, {window: w, sessionStorage: broken, JSON, Array, Set, String,
  document: {readyState: "complete", getElementById: id => els[id] || null, addEventListener() {}}});
w.toggleSubtasks(1);
assert.equal(els["subtasks-1"].style.display, "block");

console.log("ok");
