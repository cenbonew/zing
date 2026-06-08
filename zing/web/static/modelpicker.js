/* zing web UI — reusable, page-agnostic claimed-model picker.
 *
 * Plain browser global, no modules, no external deps. Exposes:
 *   window.ZingModelPicker.enhance({ claimedInput, providerInput, label })
 *
 * Turns an EXISTING claimed-model text <input> into a compact two-step picker:
 *   供应商 (provider) <select>  →  模型 (model) <select>
 * The original input stays in the DOM as the canonical value holder (hidden in
 * select mode), so any form-submit code that reads `input.value` keeps working.
 * Selecting a model writes its id into the input and fires input+change events.
 *
 * A "自定义输入" / "← 选择模型" text toggle flips to free-text mode, which simply
 * re-shows the original input for manual typing, and back. On init: if the input
 * already holds a value that is NOT a known model id, it starts in custom mode
 * showing that value; otherwise it starts in select mode.
 *
 * Data: GET /api/kb (cached at module scope as a single shared promise). If the
 * fetch fails, enhance() is a no-op and the page keeps its plain text input.
 *
 * Styling: keyed off the page CSS vars (--line, --teal, --ink, --ink2, --sans,
 * #fbfdfa input bg) so it visually matches the .in / select look on every page.
 *
 * Usage (per-page agent copy):
 *   ZingModelPicker.enhance({ claimedInput: "#e-claimed", providerInput: "#e-provider" });
 */
(function () {
  "use strict";

  var _kbPromise = null; // shared across every enhance() call on the page

  // Inject layout CSS once. A class selector (.zmp .zmp-row) outranks the element
  // rules some pages put on `div`/`select`, so the two selects always grid evenly
  // instead of collapsing to their intrinsic widths.
  function injectStyle() {
    if (document.getElementById("zmp-style")) return;
    var css =
      ".zmp{width:100%}" +
      ".zmp .zmp-row{display:grid !important;" +
      "grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;width:100%}" +
      ".zmp .zmp-row>select{width:100% !important;min-width:0;box-sizing:border-box}";
    var st = document.createElement("style");
    st.id = "zmp-style";
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  function loadKb() {
    if (_kbPromise == null) {
      _kbPromise = fetch("/api/kb")
        .then(function (r) {
          if (!r.ok) throw new Error("kb http " + r.status);
          return r.json();
        })
        .then(function (data) {
          var providers = (data && data.providers) || [];
          return Array.isArray(providers) ? providers : [];
        });
    }
    return _kbPromise;
  }

  function resolveEl(ref) {
    if (ref == null) return null;
    if (typeof ref === "string") return document.querySelector(ref);
    return ref; // assume an element
  }

  // Set of every known model id across all providers (for init mode detection).
  function knownIds(providers) {
    var ids = Object.create(null);
    providers.forEach(function (p) {
      (p.models || []).forEach(function (m) {
        if (m && m.id) ids[m.id] = true;
      });
    });
    return ids;
  }

  // Inline styles keyed off CSS vars (with --zmp-* theme overrides) so the picker
  // matches the local .in / select look on light pages AND adapts to the dark
  // console, which sets --zmp-* in its :root. Fallbacks reproduce the light look.
  function styleSelect(sel) {
    sel.style.flex = "1 1 170px";
    sel.style.minWidth = "0"; // allow shrink + ellipsis instead of overflow
    sel.style.boxSizing = "border-box";
    sel.style.border = "1.5px solid var(--zmp-border, var(--line, #e7ece5))";
    sel.style.background = "var(--zmp-bg, #fbfdfa)";
    sel.style.borderRadius = "var(--zmp-radius, 13px)";
    sel.style.fontFamily = "var(--sans, system-ui, sans-serif)";
    sel.style.fontWeight = "500";
    sel.style.fontSize = "15px";
    sel.style.color = "var(--zmp-fg, var(--ink, #0c211e))";
    sel.style.padding = "12px 13px";
    sel.style.outline = "none";
    sel.style.cursor = "pointer";
    sel.addEventListener("focus", function () {
      sel.style.borderColor = "var(--zmp-focus, var(--teal, #0f6f5f))";
      sel.style.boxShadow = "0 0 0 4px var(--zmp-ring, rgba(15,111,95,.12))";
    });
    sel.addEventListener("blur", function () {
      sel.style.borderColor = "var(--zmp-border, var(--line, #e7ece5))";
      sel.style.boxShadow = "none";
    });
  }

  // Short, human label for a provider: drop the long parenthetical / secondary
  // names so the dropdown reads "DeepSeek" not "DeepSeek (DeepSeek-AI / …)".
  function shortProvider(name) {
    if (!name) return name || "";
    var s = String(name)
      .replace(/\s*[（(].*$/, "") // from the first parenthesis onward
      .replace(/\s+\/\s+.*$/, "") // after " / " (secondary/native names)
      .replace(/[,;].*$/, "") // trailing clauses
      .trim();
    return s || name;
  }

  function opt(value, text) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    return o;
  }

  function enhance(opts) {
    opts = opts || {};
    var claimedInput = resolveEl(opts.claimedInput);
    if (!claimedInput) return; // nothing to enhance
    var providerInput = resolveEl(opts.providerInput); // optional

    loadKb()
      .then(function (providers) {
        if (!providers.length) return; // empty kb → leave plain input
        build(claimedInput, providerInput, providers, opts.label);
      })
      .catch(function () {
        // Defensive: any failure leaves the original input untouched.
      });
  }

  function build(claimedInput, providerInput, providers, label) {
    injectStyle();
    var ids = knownIds(providers);

    // Container inserted right before the original input; the input is moved
    // inside it so "free-text mode" can re-show it in place.
    var wrap = document.createElement("div");
    wrap.className = "zmp";
    wrap.style.display = "flex";
    wrap.style.flexDirection = "column";
    wrap.style.gap = "8px";
    wrap.style.width = "100%";

    var parent = claimedInput.parentNode;
    parent.insertBefore(wrap, claimedInput);

    // The two selects sit side by side and wrap to stacked when the row is
    // narrow, so the picker looks right whether it's a full-width row or a
    // half-width grid cell. They hide/show as a unit when toggling to custom.
    var selectRow = document.createElement("div");
    selectRow.className = "zmp-row";

    var provSel = document.createElement("select");
    var modelSel = document.createElement("select");
    styleSelect(provSel);
    styleSelect(modelSel);

    if (label) {
      provSel.setAttribute("aria-label", label + " 供应商");
      modelSel.setAttribute("aria-label", label + " 模型");
    }

    provSel.appendChild(opt("", "选择供应商…"));
    providers.forEach(function (p) {
      var o = opt(p.provider, shortProvider(p.display_name || p.provider));
      o.title = p.display_name || p.provider; // full name on hover
      provSel.appendChild(o);
    });
    modelSel.appendChild(opt("", "选择模型…"));
    modelSel.disabled = true;

    selectRow.appendChild(provSel);
    selectRow.appendChild(modelSel);

    // Toggle link between select mode and free-text (custom) mode.
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.style.alignSelf = "flex-start";
    toggle.style.font = "inherit";
    toggle.style.fontSize = "12px";
    toggle.style.fontWeight = "700";
    toggle.style.fontFamily = "var(--sans, system-ui, sans-serif)";
    toggle.style.color = "var(--zmp-accent, var(--teal, #0f6f5f))";
    toggle.style.background = "none";
    toggle.style.border = "0";
    toggle.style.padding = "0";
    toggle.style.cursor = "pointer";

    wrap.appendChild(selectRow);
    // Move the original input into the wrap so it shows in custom mode in place.
    wrap.appendChild(claimedInput);
    wrap.appendChild(toggle);

    function fillModels(providerId) {
      modelSel.innerHTML = "";
      modelSel.appendChild(opt("", "选择模型…"));
      var prov = null;
      for (var i = 0; i < providers.length; i++) {
        if (providers[i].provider === providerId) {
          prov = providers[i];
          break;
        }
      }
      var models = (prov && prov.models) || [];
      models.forEach(function (m) {
        if (!m || !m.id) return;
        var alias = m.aliases && m.aliases.length ? m.aliases[0] : "";
        // Show id, with a short alias hint when it differs from the id.
        var text =
          alias && alias.toLowerCase() !== m.id.toLowerCase()
            ? m.id + " · " + alias
            : m.id;
        modelSel.appendChild(opt(m.id, text));
      });
      modelSel.disabled = models.length === 0;
      return prov;
    }

    function setClaimed(value) {
      claimedInput.value = value;
      claimedInput.dispatchEvent(new Event("input", { bubbles: true }));
      claimedInput.dispatchEvent(new Event("change", { bubbles: true }));
    }

    provSel.addEventListener("change", function () {
      var pid = provSel.value;
      fillModels(pid);
      if (providerInput && pid) {
        providerInput.value = pid;
        providerInput.dispatchEvent(new Event("input", { bubbles: true }));
        providerInput.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    modelSel.addEventListener("change", function () {
      if (modelSel.value) setClaimed(modelSel.value);
    });

    // ---- Mode switching ------------------------------------------------- //
    var custom = false;

    function applyMode() {
      if (custom) {
        selectRow.style.display = "none";
        claimedInput.style.display = "";
        toggle.textContent = "← 选择模型";
      } else {
        selectRow.style.display = "";
        claimedInput.style.display = "none";
        toggle.textContent = "自定义输入";
      }
    }

    toggle.addEventListener("click", function () {
      custom = !custom;
      applyMode();
      if (custom) claimedInput.focus();
    });

    // ---- Initial mode --------------------------------------------------- //
    var current = (claimedInput.value || "").trim();
    if (current && !ids[current]) {
      // Existing value isn't a known model id → free-text mode showing it.
      custom = true;
    } else if (current && ids[current]) {
      // Pre-select the matching provider + model in the selects.
      for (var i = 0; i < providers.length; i++) {
        var p = providers[i];
        var hit = (p.models || []).some(function (m) {
          return m && m.id === current;
        });
        if (hit) {
          provSel.value = p.provider;
          fillModels(p.provider);
          modelSel.value = current;
          if (providerInput && !providerInput.value) providerInput.value = p.provider;
          break;
        }
      }
    }
    applyMode();
  }

  window.ZingModelPicker = { enhance: enhance };
})();
