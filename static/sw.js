const CACHE_VERSION = "gtdapp-v4";
const OFFLINE_URL = "./static/offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      return cache.addAll([
        "./",
        "./pwa-launch",
        "./static/style.css",
        "./static/tag_autocomplete.js",
        "./static/periodic_toggle_confirm.js",
        "./static/icons/emoji-icon-192.png?v=3",
        "./static/icons/emoji-icon-512.png?v=3",
        OFFLINE_URL,
      ]);
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((key) => key !== CACHE_VERSION)
          .map((oldKey) => caches.delete(oldKey))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  if (req.method !== "GET") {
    return;
  }

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) {
        return cached;
      }
      return fetch(req)
        .then((networkResponse) => {
          if (!networkResponse || networkResponse.status !== 200) {
            return networkResponse;
          }
          const copy = networkResponse.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy));
          return networkResponse;
        })
        .catch(() => caches.match(OFFLINE_URL));
    })
  );
});
