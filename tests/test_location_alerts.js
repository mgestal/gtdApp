const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync("static/location_alerts.js", "utf8");
const prefix = "gtd_location:/gtd/:";

function storage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)),
    removeItem: key => data.delete(key)};
}
function element() {
  return {dataset: {}, children: [], textContent: "", disabled: false, hidden: true, attrs: {},
    addEventListener(name, callback) { this[name] = callback; },
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; }};
}
const locations = [
  {id: 5, name: "Casa <segura>", latitude: 42, longitude: -8, radius_m: 200, task_ids: [1, 2], url: "/gtd/tags/5"},
  {id: 6, name: "Cerca", latitude: 42, longitude: -8, radius_m: 200, task_ids: [2, 3], url: "/gtd/tags/6"}
];
const reminder = {reminder_id: 7, scheduled_at: "2026-11-11T12:00:00", title: "Recordatorio",
  channels: ["Telegram", "Navegador: móvil"], updated_at: "11-11-2026 13:00", read: false, url: "/gtd/task/1"};

async function run(options = {}) {
  const ids = ["location-alerts", "notification-center", "notification-toggle", "notification-panel",
    "notification-count", "notification-list", "notification-status", "notification-read-all",
    "location-device-status", "location-device-enable", "location-device-disable"];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  elements["location-alerts"].dataset = {url: "/gtd/api/location-alerts", scope: "/gtd/", launchUrl: "/gtd/pwa-launch"};
  elements["notification-center"].dataset = {historyUrl: "/gtd/api/notifications", readUrl: "/gtd/api/notifications/read", csrf: "token"};
  const local = options.local || storage({[prefix + "enabled"]: "1"});
  const session = options.session || storage();
  const calls = {gps: 0, requests: [], errors: [], navigated: null};
  let releasePosition;
  const state = {locations: options.locations || locations, history: options.history || [reminder], within: options.within ?? true};
  const context = {
    document: {referrer: options.referrer ?? "", getElementById: id => elements[id], createElement: element,
      addEventListener() {}},
    window: {isSecureContext: true, setInterval() {}, location: {href: "https://example.test/gtd/", assign: url => { calls.navigated = url; }}},
    navigator: {geolocation: {getCurrentPosition(success, failure) {
      calls.gps++;
      if (options.deferred) { releasePosition = success; return; }
      if (options.error) failure({message: options.error});
      else success({coords: options.coords || {latitude: 42, longitude: -8, accuracy: 10}});
    }}},
    localStorage: local, sessionStorage: session,
    performance: {getEntriesByType: () => [{type: options.navigation || "navigate"}]},
    fetch: async (url, config) => {
      calls.requests.push({url, config});
      if (options.fetchError) throw new Error("Sin conexión");
      if (url.endsWith("/read")) {
        if (options.readError) return {ok: false};
        const body = JSON.parse(config.body);
        state.history = state.history.map(item => item.reminder_id === body.reminder_id ? {...item, read: true} : item);
        return {ok: true};
      }
      const data = url.endsWith("location-alerts")
        ? {within_hours: state.within, locations: state.locations}
        : {notifications: state.history.map(item => ({...item}))};
      return {ok: true, json: async () => data};
    },
    URL, console: {error: (...args) => calls.errors.push(args)}, Promise, Number, Math, Set, JSON
  };
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, local, session, calls, state, release: coords => releasePosition({coords})};
}

(async () => {
  let r = await run();
  assert.equal(r.calls.gps, 1);
  assert.equal(r.elements["notification-count"].textContent, "4");
  assert.equal(r.elements["notification-count"].hidden, false);
  let list = r.elements["notification-list"].children;
  assert.equal(list[1].children[0].textContent, "@Casa <segura>: 2 tareas nuevas");
  assert.equal(list[4].children[1].textContent, "11-11-2026 13:00 · Telegram, Navegador: móvil");
  await list[4].children[0].click({preventDefault() {}});
  assert.equal(r.calls.navigated, "/gtd/task/1");
  assert.equal(r.elements["notification-count"].textContent, "3");
  assert.equal(r.calls.requests.at(-1).config.headers["X-GTD-Notification-CSRF"], "token");
  list[1].children[0].click({preventDefault() {}});
  assert.equal(r.calls.navigated, "/gtd/tags/5");
  assert.deepEqual(JSON.parse(r.local.getItem(prefix + "read_tasks")), [1, 2]);

  r = await run({local: r.local, session: r.session, referrer: "https://example.test/gtd/tasks/1",
    history: [{...reminder, read: true}]});
  assert.equal(r.calls.gps, 0);
  assert.equal(r.elements["notification-count"].textContent, "1");
  r = await run({local: r.local, session: r.session, referrer: "https://example.test/gtd/tasks/1",
    within: false, locations: [{...locations[0], task_ids: [1, 2, 4]}, locations[1]], history: []});
  assert.equal(r.calls.gps, 0);
  assert.equal(r.elements["notification-count"].textContent, "1");
  r.state.locations = [{...locations[0], task_ids: [1, 2, 4]}, locations[1]];
  r = await run({local: r.local, session: r.session, navigation: "reload", locations: r.state.locations, history: []});
  assert.equal(r.elements["notification-count"].textContent, "2");
  await r.elements["notification-read-all"].click();
  assert.equal(r.elements["notification-count"].hidden, true);

  r = await run({coords: {latitude: 42.001, longitude: -8, accuracy: 88.9}, history: []});
  assert.match(r.elements["notification-status"].textContent, /no es suficientemente precisa/);
  assert.equal(r.elements["notification-count"].hidden, true);
  r = await run({within: false, history: []});
  assert.equal(r.calls.gps, 0);
  r = await run({error: "Permiso denegado"});
  assert.match(r.elements["notification-status"].textContent, /Permiso denegado/);
  r = await run({readError: true});
  list = r.elements["notification-list"].children;
  await list[4].children[0].click({preventDefault() {}});
  assert.equal(r.calls.navigated, null);
  assert.match(r.elements["notification-status"].textContent, /No se pudo marcar/);

  r = await run();
  await r.elements["notification-read-all"].click();
  assert.equal(r.elements["notification-count"].hidden, true);
  assert.equal(r.state.history[0].read, true);
  assert.deepEqual(JSON.parse(r.local.getItem(prefix + "read_tasks")).sort(), [1, 2, 3]);

  r = await run({local: storage(), deferred: true, history: []});
  const enabling = r.elements["location-device-enable"].click();
  r.elements["location-device-disable"].click();
  r.release({latitude: 42, longitude: -8, accuracy: 10});
  await enabling;
  assert.equal(r.local.getItem(prefix + "enabled"), null);
  console.log("Campana: ubicación, deduplicación, historial, lectura y permisos OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
