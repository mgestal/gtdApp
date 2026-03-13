(function () {
  "use strict";

  const TAG_TRIGGER = "@";
  const MIN_QUERY_LEN = 1;
  const MAX_ITEMS = 8;

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

  function findActiveTagToken(input) {
    const value = input.value || "";
    const caret = getCaretPosition(input);
    const before = value.slice(0, caret);

    const atIndex = before.lastIndexOf(TAG_TRIGGER);
    if (atIndex < 0) return null;

    if (atIndex > 0) {
      const prevChar = before.charAt(atIndex - 1);
      if (!/\s|,|\(|\[|\{/.test(prevChar)) {
        return null;
      }
    }

    const token = before.slice(atIndex + 1);

    if (/\s/.test(token)) return null;

    return {
      start: atIndex,
      end: caret,
      query: token.trim(),
    };
  }

  async function fetchTags(query) {
    const baseUrl = window.TAG_SEARCH_URL;
    if (!baseUrl) {
      throw new Error("TAG_SEARCH_URL no está definido.");
    }

    const url = `${baseUrl}?q=${encodeURIComponent(query)}`;
    const response = await fetch(url, {
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
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

    if (!items.length) {
      box.innerHTML = '<div class="tag-autocomplete-empty">Sin coincidencias</div>';
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
            data-tag-name="${escapeHtml(name)}"
            data-idx="${idx}"
          >@${escapeHtml(name)}</button>
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
        applySelectedTag(input, this.dataset.tagName);
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

  function applySelectedTag(input, tagName) {
    const state = input._tagAutocompleteState;
    if (!state || !state.tokenInfo) return;

    const value = input.value || "";
    const { start, end } = state.tokenInfo;

    const before = value.slice(0, start);
    const after = value.slice(end);

    const insertion = `@${tagName}`;
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
    const tokenInfo = findActiveTagToken(input);

    if (!tokenInfo || tokenInfo.query.length < MIN_QUERY_LEN) {
      closeDropdown(input);
      return;
    }

    try {
      const items = await fetchTags(tokenInfo.query);
      renderItems(input, items, tokenInfo);
    } catch (err) {
      console.error("Autocomplete de etiquetas:", err);
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
        if (ev.key === "@") {
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
          applySelectedTag(input, active.name);
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