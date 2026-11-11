const CACHE_VERSION = "gtdapp-v13";
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

self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let payload = {};
    if (event.data) {
      try {
        payload = event.data.json();
      } catch (error) {
        console.error("Notificación push inválida", error);
      }
    }
    const target = new URL(payload.url || self.registration.scope, self.registration.scope);
    const safeTarget = target.origin === self.location.origin && target.href.startsWith(self.registration.scope)
      ? target.href : self.registration.scope;
    await self.registration.showNotification(payload.title || "GTDApp", {
      body: payload.body || "Tienes una notificación.",
      icon: new URL("static/icons/emoji-icon-192.png", self.registration.scope).href,
      tag: payload.tag || "gtdapp",
      renotify: payload.renotify === true,
      data: {url: safeTarget}
    });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const target = event.notification.data?.url || self.registration.scope;
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const existing = windows.find(client => client.url === target);
    if (existing) return existing.focus();
    return self.clients.openWindow(target);
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  if (req.method !== "GET") {
    return;
  }

  if ([new URL("api/location-alerts", self.registration.scope).href,
       new URL("api/notifications", self.registration.scope).href].includes(new URL(req.url).href)) {
    event.respondWith(fetch(req));
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
