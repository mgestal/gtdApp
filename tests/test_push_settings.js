const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const elements = {};
function element(id) {
  return elements[id] ||= {
    textContent: "", hidden: false, disabled: false, children: [],
    addEventListener(name, callback) { this[name] = callback; },
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); }
  };
}
const root = element("push-settings");
root.dataset = {csrf: "token", testAllUrl: "/admin/push/test-all"};
root.querySelectorAll = () => [];
const calls = [];
const context = {
  document: {
    getElementById: element,
    createElement: () => ({textContent: ""})
  },
  window: {isSecureContext: false},
  navigator: {},
  fetch: async (url, options) => {
    calls.push({url, options});
    return {ok: true, json: async () => ({ok: true, results: [
      {id: 1, name: "Uno", error: null},
      {id: 2, name: "<Dos>", error: "Suscripción caducada"}
    ]})};
  }
};
vm.runInNewContext(fs.readFileSync("static/push_settings.js", "utf8"), context);

(async () => {
  const button = element("push-test-all");
  await button.click.call(button);
  assert.equal(calls[0].url, "/admin/push/test-all");
  assert.equal(calls[0].options.headers["X-GTD-Push-CSRF"], "token");
  assert.equal(element("push-test-results").hidden, false);
  assert.equal(element("push-test-results").children[0].textContent,
               "Uno (n.º 1): aceptada por el servicio push");
  assert.equal(element("push-test-results").children[1].textContent,
               "<Dos> (n.º 2): Suscripción caducada");
  assert.match(element("browser-notification-status").textContent, /1 aceptadas, 1 no enviadas/);
  assert.equal(button.disabled, false);
  console.log("Configuración push: prueba a todos y resultados parciales OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
