(function () {
  "use strict";

  function buildPreviewUrl(form) {
    if (!form || !form.action) return null;
    let url;
    try {
      url = new URL(form.action, window.location.origin);
    } catch (_err) {
      return null;
    }

    const m = url.pathname.match(/^(.*)\/tasks\/(\d+)\/toggle$/);
    if (!m) return null;

    const prefix = m[1] || "";
    const taskId = m[2];
    return `${prefix}/api/tasks/${taskId}/toggle_preview`;
  }

  function addChoiceInput(form, value) {
    let input = form.querySelector('input[name="recurrence_due_choice"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "recurrence_due_choice";
      form.appendChild(input);
    }
    input.value = value;
  }

  function formatIsoDate(iso) {
    if (!iso) return "";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return String(iso);
    return `${m[3]}-${m[2]}-${m[1]}`;
  }

  function escapeHtml(text) {
    return String(text || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function askPeriodicChoice(data) {
    return new Promise((resolve) => {
      const keepDate = formatIsoDate(data.keep_due);
      const futureDate = formatIsoDate(data.future_due);
      const title = escapeHtml(data.title || "Tarea periódica");

      const dlg = document.createElement("dialog");
      dlg.className = "modal";
      dlg.innerHTML = `
        <div class="modal-card" role="document" aria-labelledby="periodic-choice-title">
          <div class="modal-header">
            <div>
              <div id="periodic-choice-title" class="modal-title">Fecha de siguiente repetición</div>
              <div class="modal-sub">Tarea: ${title}</div>
            </div>
          </div>
          <div class="modal-body">
            <p class="hint" style="margin:0 0 8px 0;">La próxima repetición calculada queda en el pasado.</p>
            <p class="hint" style="margin:0 0 6px 0;">Mantener próxima fecha: <strong>${keepDate}</strong></p>
            <p class="hint" style="margin:0;">Mover a fecha válida: <strong>${futureDate}</strong></p>
          </div>
          <div class="modal-footer" style="justify-content:space-between; flex-wrap:wrap;">
            <button type="button" class="btn btn-soft" data-choice="cancel">Cancelar</button>
            <div style="display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end;">
              <button type="button" class="btn btn-soft" data-choice="keep">Mantener ${keepDate}</button>
              <button type="button" class="btn btn-primary" data-choice="future">Usar ${futureDate}</button>
            </div>
          </div>
        </div>
      `;

      function closeWith(choice) {
        try {
          dlg.close();
        } catch (_err) {
          // no-op
        }
        dlg.remove();
        resolve(choice);
      }

      dlg.addEventListener("cancel", function (ev) {
        ev.preventDefault();
        closeWith("cancel");
      });

      dlg.querySelectorAll("[data-choice]").forEach((btn) => {
        btn.addEventListener("click", function () {
          closeWith(this.getAttribute("data-choice") || "cancel");
        });
      });

      document.body.appendChild(dlg);
      if (typeof dlg.showModal === "function") {
        dlg.showModal();
      } else {
        // Fallback para navegadores antiguos
        const keep = window.confirm(
          "Esta tarea periódica sigue quedando en el pasado.\n\n" +
            `Aceptar: mantener próxima fecha ${keepDate}.\n` +
            `Cancelar: mover a la próxima fecha válida ${futureDate}.`
        );
        closeWith(keep ? "keep" : "future");
      }
    });
  }

  async function maybeConfirmPeriodicChoice(form) {
    const previewUrl = buildPreviewUrl(form);
    if (!previewUrl) return true;

    try {
      const res = await fetch(previewUrl, {
        method: "GET",
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
      });

      if (!res.ok) return true;

      const data = await res.json();
      if (!data || !data.requires_choice) return true;

      const choice = await askPeriodicChoice(data);
      if (choice === "cancel") return false;

      addChoiceInput(form, choice === "future" ? "future" : "keep");
      return true;
    } catch (_err) {
      return true;
    }
  }

  document.addEventListener("submit", async function (ev) {
    const form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.periodicConfirmHandled === "1") return;

    const isToggleForm = /\/tasks\/\d+\/toggle$/.test(form.getAttribute("action") || "");
    if (!isToggleForm) return;

    ev.preventDefault();

    const ok = await maybeConfirmPeriodicChoice(form);
    if (!ok) return;

    form.dataset.periodicConfirmHandled = "1";
    form.submit();
  });
})();
