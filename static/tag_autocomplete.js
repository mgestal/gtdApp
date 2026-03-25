(function () {
  "use strict";

  const MAX_ITEMS = 8;

  // Configuración de cada trigger:
  //   trigger  → texto que abre el autocompletado (1 o 2 chars)
  //   getUrl   → función que devuelve la URL del endpoint
  //   display  → prefijo que se muestra en el desplegable
  //   minQuery → longitud mínima de consulta para disparar búsqueda
  const TRIGGER_CONFIGS = [
    {
      type: "tag",
      trigger: "@",
      getUrl: () => window.TAG_SEARCH_URL,
      display: "@",
      minQuery: 1,
    },
    {
      type: "project",
      trigger: "#",
      getUrl: () => window.PROJECT_SEARCH_URL,
      display: "#",
      minQuery: 0,
    },
    {
      type: "folder",
      trigger: "f:",
      getUrl: () => window.FOLDER_SEARCH_URL,
      display: "f:",
      minQuery: 0,
    },
  ];

  function debounce(fn, wait) {
    let t = null;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function ensureWrap(input) {
    let wrap = input.closest(".quick-input-wrap");
    if (wrap) return wrap;

    wrap = document.createElement("div");
    wrap.className = "quick-input-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    return wrap;
  }

  function getDropdown(input) {
    const wrap = ensureWrap(input);
    let box = wrap.querySelector(".tag-autocomplete");

    if (!box) {
      box = document.createElement("div");
      box.className = "tag-autocomplete";
      box.hidden = true;
      wrap.appendChild(box);
    }

    return box;
  }

  function closeDropdown(input) {
    const box = getDropdown(input);
    box.hidden = true;
    box.innerHTML = "";
    input._tagAutocompleteState = null;
  }

  function escapeHtml(text) {
    return String(text)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function getCaretPosition(input) {
    if (typeof input.selectionStart !== "number") return input.value.length;
    return input.selectionStart;
  }

  // Busca el trigger activo más cercano al cursor.
  // Devuelve { config, start, end, query, quoted } o null.
  function findActiveToken(input) {
    const value = input.value || "";
    const caret = getCaretPosition(input);
    const before = value.slice(0, caret);

    let best = null;

    for (const config of TRIGGER_CONFIGS) {
      const trig = config.trigger;
      const idx = before.lastIndexOf(trig);
      if (idx < 0) continue;

      // El trigger debe estar precedido por espacio, inicio de línea o separadores.
      if (idx > 0) {
        const prevChar = before.charAt(idx - 1);
        if (!/[\s,(\[{]/.test(prevChar)) continue;
      }

      const afterTrig = before.slice(idx + trig.length);
      let query;
      let quoted = false;

      if (afterTrig.startsWith('"')) {
        // Modo entrecomillado: permitir espacios hasta comilla de cierre
        const closeIdx = afterTrig.indexOf('"', 1);
        if (closeIdx >= 0) continue; // ya tiene comilla de cierre → token completado
        quoted = true;
        query = afterTrig.slice(1); // sin la comilla de apertura
      } else {
        // Modo normal: sin espacios
        if (/\s/.test(afterTrig)) continue;
        query = afterTrig.trim();
      }

      // Tomamos el candidato más tardío (más cercano al cursor).
      if (best === null || idx > best.start) {
        best = { config, start: idx, end: caret, query, quoted };
      }
    }

    return best;
  }

  async function fetchItems(query, config) {
    const baseUrl = config.getUrl();
    if (!baseUrl) {
      throw new Error(`URL de búsqueda no definida para tipo "${config.type}".`);
    }

    const url = `${baseUrl}?q=${encodeURIComponent(query)}`;
    const response = await fetch(url, {
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
      credentials: "same-origin",
    });

    if (!response.ok) {
      throw new Error(`Error HTTP ${response.status}`);
    }

    const data = await response.json();
    return Array.isArray(data.items) ? data.items.slice(0, MAX_ITEMS) : [];
  }

  function renderItems(input, items, tokenInfo) {
    const box = getDropdown(input);
    const display = tokenInfo.config.display;

    if (!items.length) {
      box.innerHTML =
        '<div class="tag-autocomplete-empty">Sin coincidencias</div>';
      box.hidden = false;
      input._tagAutocompleteState = {
        items: [],
        activeIndex: -1,
        tokenInfo,
      };
      return;
    }

    box.innerHTML = items
      .map((item, idx) => {
        const name = item && item.name ? item.name : "";
        return `
          <button
            type="button"
            class="tag-autocomplete-item${idx === 0 ? " active" : ""}"
            data-item-name="${escapeHtml(name)}"
            data-idx="${idx}"
          >${escapeHtml(display)}${escapeHtml(name)}</button>
        `;
      })
      .join("");

    box.hidden = false;

    input._tagAutocompleteState = {
      items,
      activeIndex: 0,
      tokenInfo,
    };

    box.querySelectorAll(".tag-autocomplete-item").forEach((btn) => {
      btn.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
      });

      btn.addEventListener("click", function () {
        applySelected(input, this.dataset.itemName);
      });
    });
  }

  function updateActiveItem(input, newIndex) {
    const state = input._tagAutocompleteState;
    const box = getDropdown(input);

    if (!state || !state.items.length) return;

    const items = Array.from(box.querySelectorAll(".tag-autocomplete-item"));
    if (!items.length) return;

    if (newIndex < 0) newIndex = items.length - 1;
    if (newIndex >= items.length) newIndex = 0;

    items.forEach((el) => el.classList.remove("active"));
    items[newIndex].classList.add("active");

    state.activeIndex = newIndex;
  }

  function applySelected(input, itemName) {
    const state = input._tagAutocompleteState;
    if (!state || !state.tokenInfo) return;

    const value = input.value || "";
    const { start, end, config, quoted } = state.tokenInfo;

    const before = value.slice(0, start);
    const after = value.slice(end);

    // Si el nombre contiene espacios o estaba en modo quoted, usar forma entrecomillada
    const insertion =
      itemName.includes(" ") || quoted
        ? `${config.trigger}"${itemName}"`
        : `${config.trigger}${itemName}`;

    const needsSpace = after.length === 0 || !after.startsWith(" ");
    const finalInsert = needsSpace ? `${insertion} ` : insertion;

    input.value = before + finalInsert + after;

    const newCaret = (before + finalInsert).length;
    input.focus();
    if (typeof input.setSelectionRange === "function") {
      input.setSelectionRange(newCaret, newCaret);
    }

    closeDropdown(input);

    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const debouncedLookup = debounce(async function (input) {
    const tokenInfo = findActiveToken(input);

    if (!tokenInfo || tokenInfo.query.length < tokenInfo.config.minQuery) {
      closeDropdown(input);
      return;
    }

    try {
      const items = await fetchItems(tokenInfo.query, tokenInfo.config);
      renderItems(input, items, tokenInfo);
    } catch (err) {
      console.error("Autocomplete:", err);
      closeDropdown(input);
    }
  }, 120);

  function attachAutocomplete(input) {
    if (!input || input.dataset.tagAutocompleteReady === "1") return;
    input.dataset.tagAutocompleteReady = "1";

    ensureWrap(input);

    input.addEventListener("input", function () {
      debouncedLookup(input);
    });

    input.addEventListener("click", function () {
      debouncedLookup(input);
    });

    input.addEventListener("keyup", function (ev) {
      const state = input._tagAutocompleteState;
      const isOpen = state && !getDropdown(input).hidden;

      if (!isOpen) {
        // Disparar inmediatamente al escribir los chars de trigger
        if (ev.key === "@" || ev.key === "#" || ev.key === ":" || ev.key === '"') {
          debouncedLookup(input);
        }
        return;
      }

      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        updateActiveItem(input, state.activeIndex + 1);
        return;
      }

      if (ev.key === "ArrowUp") {
        ev.preventDefault();
        updateActiveItem(input, state.activeIndex - 1);
        return;
      }

      if (ev.key === "Enter") {
        const active = state.items[state.activeIndex];
        if (active && active.name) {
          ev.preventDefault();
          applySelected(input, active.name);
        }
        return;
      }

      if (ev.key === "Escape") {
        closeDropdown(input);
      }
    });

    input.addEventListener("blur", function () {
      setTimeout(() => closeDropdown(input), 150);
    });
  }

  function initTagAutocomplete() {
    const selectors = [
      'input[name="quick"]',
      'textarea[name="quick"]',
      'input[name="tags_csv"]',
      'textarea[name="tags_csv"]',
      ".js-tag-autocomplete",
    ];

    const inputs = document.querySelectorAll(selectors.join(","));
    inputs.forEach(attachAutocomplete);
  }

  document.addEventListener("DOMContentLoaded", initTagAutocomplete);
})();