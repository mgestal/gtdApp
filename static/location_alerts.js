(function () {
  const root = document.getElementById("location-alerts");
  const center = document.getElementById("notification-center");
  if (!root || !center) return;
  const toggle = document.getElementById("notification-toggle");
  const panel = document.getElementById("notification-panel");
  const count = document.getElementById("notification-count");
  const list = document.getElementById("notification-list");
  const status = document.getElementById("notification-status");
  const deviceStatus = document.getElementById("location-device-status");
  const enable = document.getElementById("location-device-enable");
  const disable = document.getElementById("location-device-disable");
  const readAll = document.getElementById("notification-read-all");
  const prefix = `gtd_location:${root.dataset.scope}:`;
  const enabledKey = prefix + "enabled";
  const visitedKey = prefix + "visited";
  const matchesKey = prefix + "matches";
  const tasksKey = prefix + "matched_tasks";
  const readKey = prefix + "read_tasks";
  let locations = [];
  let history = [];
  let historyVersion = 0;
  let generation = 0;
  let readingAll = false;

  function report(message) {
    status.textContent = message;
    if (deviceStatus) deviceStatus.textContent = message;
    const item = document.createElement("div");
    item.className = "flash flash-error";
    item.textContent = message;
    root.replaceChildren(item);
  }

  function updateDeviceStatus() {
    if (deviceStatus) deviceStatus.textContent = localStorage.getItem(enabledKey) === "1"
      ? "Activado en este navegador. El permiso de ubicación se gestiona en el navegador."
      : "Desactivado en este navegador.";
  }

  function storedArray(storage, key) {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("Los datos guardados de ubicación no son válidos.");
    return parsed;
  }

  function unreadLocations() {
    const read = new Set(storedArray(localStorage, readKey));
    return locations.map(location => ({
      ...location, unread: location.task_ids.filter(id => !read.has(id))
    })).filter(location => location.unread.length);
  }

  function makeHeading(text) {
    const heading = document.createElement("h3");
    heading.textContent = text;
    list.appendChild(heading);
  }

  function render() {
    list.replaceChildren();
    const pending = unreadLocations();
    const ids = new Set(pending.flatMap(location => location.unread));
    const unreadHistory = history.filter(item => !item.read);
    const total = ids.size + unreadHistory.length;
    count.textContent = String(total);
    count.hidden = total === 0;
    toggle.setAttribute("aria-label", `Notificaciones: ${total} pendientes`);
    readAll.disabled = readingAll || total === 0;
    if (pending.length) makeHeading("Por ubicación");
    for (const location of pending) {
      const item = document.createElement("div");
      item.className = "notification-item";
      const link = document.createElement("a");
      link.href = location.url;
      link.textContent = `@${location.name}: ${location.unread.length} ${location.unread.length === 1 ? "tarea nueva" : "tareas nuevas"}`;
      link.addEventListener("click", event => {
        event.preventDefault();
        try {
          const seen = new Set(storedArray(localStorage, readKey));
          location.unread.forEach(id => seen.add(id));
          localStorage.setItem(readKey, JSON.stringify([...seen]));
          window.location.assign(link.href);
        } catch (error) {
          report("No se pudo guardar la lectura de la ubicación.");
          console.error(error);
        }
      });
      item.appendChild(link);
      list.appendChild(item);
    }
    if (history.length) makeHeading("Enviadas");
    for (const notification of history) {
      const item = document.createElement("div");
      item.className = "notification-item" + (notification.read ? " notification-item-read" : "");
      const link = document.createElement("a");
      link.href = notification.url;
      link.textContent = notification.title;
      link.addEventListener("click", async event => {
        event.preventDefault();
        try {
          if (!notification.read) await markRead(notification);
          window.location.assign(link.href);
        } catch (error) {
          report(error.message);
        }
      });
      const detail = document.createElement("div");
      detail.className = "hint";
      detail.textContent = `${notification.updated_at} · ${notification.channels.join(", ")}${notification.read ? " · Leída" : ""}`;
      item.appendChild(link);
      item.appendChild(detail);
      list.appendChild(item);
    }
    if (!pending.length && !history.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "No hay notificaciones.";
      list.appendChild(empty);
    }
  }

  async function loadHistory() {
    const version = historyVersion;
    const response = await fetch(center.dataset.historyUrl, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("No se pudo consultar el historial de notificaciones.");
    const data = await response.json();
    if (version !== historyVersion) return;
    history = data.notifications;
    render();
  }

  async function markRead(notification) {
    const response = await fetch(center.dataset.readUrl, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-GTD-Notification-CSRF": center.dataset.csrf},
      body: JSON.stringify({reminder_id: notification.reminder_id, scheduled_at: notification.scheduled_at})
    });
    if (!response.ok) throw new Error("No se pudo marcar el recordatorio como leído.");
    historyVersion++;
    notification.read = true;
    render();
  }

  function getPosition() {
    return new Promise((resolve, reject) => {
      if (!window.isSecureContext || !navigator.geolocation) {
        reject(new Error("La ubicación requiere HTTPS y un navegador compatible."));
        return;
      }
      navigator.geolocation.getCurrentPosition(resolve, error => {
        reject(new Error(`No se pudo obtener la ubicación: ${error.message || "comprueba los permisos del navegador"}.`));
      }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 0});
    });
  }

  function distanceMeters(a, b) {
    const radians = degrees => degrees * Math.PI / 180;
    const lat1 = radians(a.latitude), lat2 = radians(b.latitude);
    const dLat = lat2 - lat1, dLon = radians(b.longitude - a.longitude);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
    return 6371000 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
  }

  async function loadLocations() {
    const response = await fetch(root.dataset.url, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("No se pudieron consultar las tareas por ubicación.");
    return response.json();
  }

  async function restoreLocations() {
    const currentGeneration = generation;
    const ids = new Set(storedArray(sessionStorage, matchesKey));
    if (!ids.size || localStorage.getItem(enabledKey) !== "1") return;
    const data = await loadLocations();
    if (currentGeneration !== generation || localStorage.getItem(enabledKey) !== "1") return;
    locations = data.locations.filter(location => ids.has(location.id));
    if (!data.within_hours) {
      const previous = new Set(storedArray(sessionStorage, tasksKey));
      locations = locations.map(location => ({
        ...location, task_ids: location.task_ids.filter(id => previous.has(id))
      }));
    } else {
      sessionStorage.setItem(tasksKey, JSON.stringify([...new Set(locations.flatMap(location => location.task_ids))]));
    }
    render();
  }

  async function check(position, currentGeneration = generation) {
    try {
      let data = await loadLocations();
      if (currentGeneration !== generation || localStorage.getItem(enabledKey) !== "1") return;
      if (!data.within_hours || !data.locations.length) {
        locations = [];
        sessionStorage.removeItem(matchesKey);
        sessionStorage.removeItem(tasksKey);
        render();
        return;
      }
      const coords = (position || await getPosition()).coords;
      if (![coords.latitude, coords.longitude, coords.accuracy].every(Number.isFinite) || coords.accuracy < 0) {
        throw new Error("El navegador devolvió una ubicación o precisión no válida.");
      }
      data = await loadLocations();
      if (currentGeneration !== generation || localStorage.getItem(enabledKey) !== "1") return;
      if (!data.within_hours) {
        locations = [];
      } else {
        let uncertain = false;
        locations = data.locations.filter(location => {
          const distance = distanceMeters(coords, location);
          if (distance + coords.accuracy <= location.radius_m) return true;
          if (distance - coords.accuracy <= location.radius_m) uncertain = true;
          return false;
        });
        if (uncertain) report("La ubicación no es suficientemente precisa para confirmar alguna zona cercana.");
      }
      sessionStorage.setItem(matchesKey, JSON.stringify(locations.map(location => location.id)));
      sessionStorage.setItem(tasksKey, JSON.stringify([...new Set(locations.flatMap(location => location.task_ids))]));
      render();
    } catch (error) {
      if (currentGeneration !== generation) return;
      locations = [];
      sessionStorage.removeItem(matchesKey);
      sessionStorage.removeItem(tasksKey);
      render();
      console.error("Error al comprobar avisos por ubicación:", error);
      report(error.message || "No se pudieron comprobar los avisos por ubicación.");
    }
  }

  toggle.addEventListener("click", async () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) {
      try {
        await loadHistory();
      } catch (error) {
        report(error.message);
      }
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadHistory().catch(error => report(error.message));
  });
  window.setInterval(() => {
    if (!document.hidden) loadHistory().catch(error => report(error.message));
  }, 60000);
  readAll.addEventListener("click", async () => {
    if (readingAll) return;
    readingAll = true;
    readAll.disabled = true;
    try {
      for (const item of history.filter(item => !item.read)) await markRead(item);
      const read = new Set(storedArray(localStorage, readKey));
      unreadLocations().forEach(location => location.unread.forEach(id => read.add(id)));
      localStorage.setItem(readKey, JSON.stringify([...read]));
      await loadHistory();
    } catch (error) {
      report(error.message || "No se pudieron marcar todos los avisos.");
    } finally {
      readingAll = false;
      render();
    }
  });

  try {
    updateDeviceStatus();
    if (enable && disable) {
      enable.addEventListener("click", async () => {
        const currentGeneration = ++generation;
        enable.disabled = true;
        try {
          const position = await getPosition();
          if (currentGeneration !== generation) return;
          localStorage.setItem(enabledKey, "1");
          updateDeviceStatus();
          await check(position, currentGeneration);
        } catch (error) {
          if (currentGeneration === generation) report(error.message);
        } finally {
          enable.disabled = false;
        }
      });
      disable.addEventListener("click", () => {
        generation++;
        localStorage.removeItem(enabledKey);
        sessionStorage.removeItem(matchesKey);
        sessionStorage.removeItem(tasksKey);
        locations = [];
        root.replaceChildren();
        updateDeviceStatus();
        render();
      });
    }
    const navigation = performance.getEntriesByType("navigation")[0];
    const referrer = document.referrer ? new URL(document.referrer) : null;
    const scope = new URL(root.dataset.scope, window.location.href);
    const launch = new URL(root.dataset.launchUrl, window.location.href);
    const internal = referrer && referrer.origin === scope.origin && referrer.pathname.startsWith(scope.pathname);
    const opening = !sessionStorage.getItem(visitedKey) || navigation?.type === "reload"
      || !internal || (referrer && referrer.pathname === launch.pathname);
    sessionStorage.setItem(visitedKey, "1");
    loadHistory().catch(error => report(error.message));
    if (localStorage.getItem(enabledKey) === "1") {
      if (opening) check();
      else restoreLocations().catch(error => report(error.message));
    }
  } catch (error) {
    console.error("Error al acceder a la configuración de ubicación:", error);
    report("No se puede acceder a la configuración de ubicación de este navegador.");
  }
})();
