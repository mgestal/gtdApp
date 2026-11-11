(function () {
  const root = document.getElementById("push-settings");
  if (!root) return;
  const status = document.getElementById("browser-notification-status");
  const name = document.getElementById("push-device-name");
  const results = document.getElementById("push-test-results");
  const supported = window.isSecureContext && "serviceWorker" in navigator &&
    "PushManager" in window && "Notification" in window;
  status.textContent = "Comprobando...";

  async function post(url, data) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-GTD-Push-CSRF": root.dataset.csrf},
      body: JSON.stringify(data || {})
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "La solicitud ha fallado.");
    return result;
  }

  async function registration() {
    await navigator.serviceWorker.register(root.dataset.swUrl);
    return navigator.serviceWorker.ready;
  }

  async function currentSubscription() {
    return (await registration()).pushManager.getSubscription();
  }

  async function refresh() {
    if (!supported) {
      status.textContent = "No disponible: se necesita HTTPS y un navegador compatible con Web Push.";
      return;
    }
    if (Notification.permission === "denied") {
      status.textContent = "Permiso bloqueado; cambia los ajustes del navegador o del sistema.";
      return;
    }
    if (Notification.permission !== "granted") {
      status.textContent = "Permiso aún no concedido.";
      return;
    }
    const subscription = await currentSubscription();
    if (!subscription) {
      status.textContent = "Permiso concedido, pero este dispositivo no está suscrito.";
      return;
    }
    const data = await post(root.dataset.statusUrl, {endpoint: subscription.endpoint});
    status.textContent = data.registered ? "Suscrito: " + data.name : "Suscripción local no registrada en el servidor.";
  }

  function showError(error) {
    status.textContent = error.message || "No se pudo completar la operación.";
  }

  document.getElementById("push-enable").addEventListener("click", async function () {
    if (!supported) return showError(new Error("Este navegador no admite Web Push o no usa HTTPS."));
    if (!name.value.trim()) return showError(new Error("Indica un nombre para este dispositivo."));
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") throw new Error("No se ha concedido el permiso de notificaciones.");
      const key = (await post(root.dataset.keyUrl)).publicKey;
      const raw = atob((key + "=".repeat((4 - key.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
      const applicationServerKey = Uint8Array.from(raw, c => c.charCodeAt(0));
      const worker = await registration();
      let subscription = await worker.pushManager.getSubscription();
      if (subscription) {
        const existing = await post(root.dataset.statusUrl, {endpoint: subscription.endpoint});
        if (!existing.registered) {
          if (!await subscription.unsubscribe()) throw new Error("No se pudo renovar la suscripción anterior.");
          subscription = null;
        }
      }
      if (!subscription) subscription = await worker.pushManager.subscribe({userVisibleOnly: true, applicationServerKey});
      await post(root.dataset.subscribeUrl, {subscription: subscription.toJSON(), name: name.value.trim(), origin: location.origin});
      location.reload();
    } catch (error) {
      showError(error);
    }
  });

  document.getElementById("push-test").addEventListener("click", async function () {
    try {
      if (!supported) throw new Error("Este navegador no admite Web Push.");
      const subscription = await currentSubscription();
      if (!subscription) throw new Error("Activa primero las notificaciones en este dispositivo.");
      await post(root.dataset.testUrl, {endpoint: subscription.endpoint});
      status.textContent = "Prueba aceptada por el servicio push; comprueba el dispositivo.";
    } catch (error) {
      showError(error);
    }
  });

  document.getElementById("push-test-all").addEventListener("click", async function () {
    const button = this;
    button.disabled = true;
    results.replaceChildren();
    results.hidden = true;
    status.textContent = "Enviando pruebas a los dispositivos activos...";
    try {
      const data = await post(root.dataset.testAllUrl);
      for (const result of data.results) {
        const item = document.createElement("li");
        item.textContent = result.name + " (n.º " + result.id + "): " +
          (result.error || "aceptada por el servicio push");
        results.appendChild(item);
      }
      results.hidden = false;
      const failed = data.results.filter(result => result.error).length;
      status.textContent = "Pruebas: " + (data.results.length - failed) + " aceptadas, " + failed +
        " no enviadas. La aceptación no garantiza que el dispositivo las muestre.";
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("push-disable").addEventListener("click", async function () {
    try {
      if (!supported) throw new Error("Este navegador no admite Web Push.");
      const subscription = await currentSubscription();
      if (!subscription) throw new Error("Este dispositivo no tiene una suscripción local.");
      await post(root.dataset.unsubscribeUrl, {endpoint: subscription.endpoint});
      if (!await subscription.unsubscribe()) throw new Error("El servidor eliminó la suscripción, pero el navegador no pudo desactivarla.");
      location.reload();
    } catch (error) {
      showError(error);
    }
  });

  root.querySelectorAll(".js-push-remove").forEach(button => button.addEventListener("click", async function () {
    if (!confirm("¿Eliminar el dispositivo " + button.dataset.name + "?")) return;
    try {
      await post(button.dataset.url);
      location.reload();
    } catch (error) {
      showError(error);
    }
  }));
  refresh().catch(showError);
})();
