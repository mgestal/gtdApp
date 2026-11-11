const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const handlers = {};
const shown = [];
const opened = [];
const cached = new Map([["https://example.test/gtdApp/static/push_settings.js", "old script"]]);
const fetched = [];
const self = {
  location: {origin: "https://example.test"},
  registration: {
    scope: "https://example.test/gtdApp/",
    showNotification: async (title, options) => shown.push({title, options})
  },
  clients: {
    matchAll: async () => [],
    openWindow: async url => opened.push(url)
  },
  addEventListener: (name, callback) => { handlers[name] = callback; }
};
const caches = {
  match: async req => cached.get(req.url),
  open: async () => ({put: async (req, response) => cached.set(req.url, response)})
};
const fetch = async req => {
  fetched.push(req.url);
  return {status: 200, clone: () => "new script"};
};
vm.runInNewContext(fs.readFileSync("static/sw.js", "utf8"), {self, URL, console, caches, fetch});

async function trigger(name, event) {
  let promise;
  handlers[name]({...event, waitUntil: value => { promise = value; }});
  await promise;
}

(async () => {
  await trigger("push", {data: {json: () => ({title: "Aviso", body: "Tarea",
    url: "/gtdApp/tasks/123/edit", tag: "unique"})}});
  assert.equal(shown[0].options.data.url, "https://example.test/gtdApp/tasks/123/edit");
  assert.equal(shown[0].options.body, "Tarea");
  assert.equal(shown[0].options.renotify, false);
  let closed = false;
  await trigger("notificationclick", {notification: {
    data: shown[0].options.data, close: () => { closed = true; }
  }});
  assert.equal(closed, true);
  assert.deepEqual(opened, ["https://example.test/gtdApp/tasks/123/edit"]);
  await trigger("push", {data: {json: () => ({url: "https://evil.example/"})}});
  assert.equal(shown[1].options.data.url, self.registration.scope);
  for (let i = 0; i < 2; i++) {
    await trigger("push", {data: {json: () => ({tag: "gtd-push-test", renotify: true})}});
    assert.equal(shown[i + 2].options.tag, "gtd-push-test");
    assert.equal(shown[i + 2].options.renotify, true);
  }
  let fetchResponse;
  handlers.fetch({
    request: {method: "GET", mode: "no-cors",
      url: "https://example.test/gtdApp/static/push_settings.js?v=2"},
    respondWith: promise => { fetchResponse = promise; }
  });
  assert.equal((await fetchResponse).status, 200);
  assert.deepEqual(fetched, ["https://example.test/gtdApp/static/push_settings.js?v=2"]);
  const apiUrl = "https://example.test/gtdApp/api/location-alerts";
  cached.set(apiUrl, "stale location data");
  handlers.fetch({
    request: {method: "GET", mode: "cors", url: apiUrl},
    respondWith: promise => { fetchResponse = promise; }
  });
  assert.equal((await fetchResponse).status, 200);
  assert.equal(cached.get(apiUrl), "stale location data");
  assert.equal(fetched.at(-1), apiUrl);
  const historyUrl = "https://example.test/gtdApp/api/notifications";
  cached.set(historyUrl, "stale history");
  handlers.fetch({
    request: {method: "GET", mode: "cors", url: historyUrl},
    respondWith: promise => { fetchResponse = promise; }
  });
  assert.equal((await fetchResponse).status, 200);
  assert.equal(cached.get(historyUrl), "stale history");
  assert.equal(fetched.at(-1), historyUrl);
  console.log("Service worker: push, click y protección del destino OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
