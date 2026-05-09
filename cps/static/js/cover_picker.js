/*
 * Calibre-Web-NextGen — focused cover-picker UI
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Plain ES2017+ — no transpiler, no bundler. Lives at /book/<id>/cover.
 * The template injects window.cwaCoverPicker with endpoints + i18n.
 *
 * Architecture: every panel (URL paste, file upload, API keys, candidate
 * grid) is its own small section. The template keeps each section simple
 * via <details>; the JS hooks events on each. No framework.
 *
 * Adding a new candidate source means adding a metadata provider in
 * cps/metadata_provider/ — the picker grid surfaces every provider that
 * returns covers for the current book. No JS changes required.
 */
(function () {
  "use strict";

  if (!window.cwaCoverPicker) return;
  const cfg = window.cwaCoverPicker;

  // -------- HTTP helpers --------
  async function postJson(url, body) {
    const headers = { "Content-Type": "application/json", "Accept": "application/json" };
    if (cfg.csrfToken) headers["X-CSRFToken"] = cfg.csrfToken;
    const resp = await fetch(url, { method: "POST", headers, credentials: "same-origin", body: JSON.stringify(body || {}) });
    if (!resp.ok && resp.status >= 500) throw new Error("server-" + resp.status);
    let payload;
    try { payload = await resp.json(); } catch (_) { payload = {}; }
    payload.__status = resp.status;
    return payload;
  }
  async function postForm(url, formData) {
    const headers = { "Accept": "application/json" };
    if (cfg.csrfToken) headers["X-CSRFToken"] = cfg.csrfToken;
    const resp = await fetch(url, { method: "POST", headers, credentials: "same-origin", body: formData });
    if (!resp.ok && resp.status >= 500) throw new Error("server-" + resp.status);
    let payload;
    try { payload = await resp.json(); } catch (_) { payload = {}; }
    payload.__status = resp.status;
    return payload;
  }
  async function getJson(url) {
    const resp = await fetch(url, { method: "GET", credentials: "same-origin", headers: { "Accept": "application/json" } });
    return resp.ok ? resp.json() : [];
  }

  // -------- Candidate grid --------
  const grid = document.getElementById("cwa-cover-picker-grid");
  const status = document.getElementById("cwa-cover-picker-status");
  const providerStatus = document.getElementById("cwa-cover-picker-provider-status");

  function setGridStatus(state, text) {
    status.innerHTML = "";
    const pill = document.createElement("span");
    pill.className = "cwa-cover-picker__status-pill cwa-cover-picker__status-pill--" + state;
    pill.textContent = text;
    status.appendChild(pill);
  }

  function renderEmpty(text) {
    grid.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "cwa-cover-picker__grid-empty text-muted";
    empty.textContent = text;
    grid.appendChild(empty);
  }

  function renderProviderPills(providers) {
    providerStatus.innerHTML = "";
    providers.forEach((p) => {
      const pill = document.createElement("span");
      pill.className = "cwa-cover-picker__provider-pill cwa-cover-picker__provider-pill--" + (p.status || "empty");
      const ms = p.duration_ms ? ` (${p.duration_ms}ms)` : "";
      const count = p.count != null ? ` · ${p.count}` : "";
      pill.textContent = `${p.name} — ${p.status}${count}${ms}`;
      if (p.message) pill.title = p.message;
      providerStatus.appendChild(pill);
    });
  }

  function renderCard(candidate) {
    const card = document.createElement("div");
    card.className = "cwa-cover-picker__card";
    card.dataset.candidateId = candidate.candidate_id || "";
    card.dataset.coverUrl = candidate.cover_url || "";

    const img = document.createElement("img");
    img.className = "cwa-cover-picker__card-cover";
    img.alt = candidate.title || "";
    img.loading = "lazy";
    img.src = candidate.cover_url;
    img.onerror = function () {
      // Network failure / CORS-strict provider / 404. Mark the card so
      // the CSS-driven failure placeholder takes over the image area
      // instead of showing an empty card. The candidate is still
      // pickable — the user might know the URL is fine and want to apply
      // it anyway.
      card.classList.add("is-cover-failed");
    };
    img.onload = function () {
      // Surface the natural dimensions when the candidate didn't carry
      // them (most providers don't). For the picker the browser is the
      // source of truth — the image had to render anyway.
      const dimsEl = card.querySelector(".cwa-cover-picker__card-dims");
      if (dimsEl && img.naturalWidth) {
        dimsEl.textContent = img.naturalWidth + "×" + img.naturalHeight;
        if (img.naturalWidth < 600 || img.naturalHeight < 800) {
          dimsEl.classList.add("cwa-cover-picker__card-dims--low");
        }
      }
    };
    card.appendChild(img);

    // Hidden by default — CSS reveals it when .is-cover-failed is set.
    const failedMsg = document.createElement("div");
    failedMsg.className = "cwa-cover-picker__card-failed-msg";
    failedMsg.innerHTML =
      '<span class="glyphicon glyphicon-picture" aria-hidden="true"></span>' +
      '<div>Cover not reachable<br><small>Tap to apply anyway</small></div>';
    card.appendChild(failedMsg);

    const info = document.createElement("div");
    info.className = "cwa-cover-picker__card-info";
    const sourceRow = document.createElement("div");
    sourceRow.className = "cwa-cover-picker__card-source";
    const sourceName = document.createElement("span");
    sourceName.textContent = candidate.source_name || candidate.source_id;
    const sourceBadge = document.createElement("span");
    sourceBadge.className = "cwa-cover-picker__card-source-badge";
    sourceBadge.textContent = candidate.source_id || "src";
    sourceRow.appendChild(sourceName);
    sourceRow.appendChild(sourceBadge);
    info.appendChild(sourceRow);

    const dims = document.createElement("div");
    dims.className = "cwa-cover-picker__card-dims";
    if (candidate.width && candidate.height) {
      dims.textContent = candidate.width + "×" + candidate.height;
      if (candidate.width < 600 || candidate.height < 800) {
        dims.classList.add("cwa-cover-picker__card-dims--low");
      }
    } else {
      dims.textContent = "—";
    }
    info.appendChild(dims);

    if (candidate.title || candidate.year) {
      const title = document.createElement("div");
      title.className = "cwa-cover-picker__card-title";
      title.textContent = (candidate.title || "") + (candidate.year ? " (" + candidate.year + ")" : "");
      info.appendChild(title);
    }

    card.appendChild(info);
    card.addEventListener("click", () => openConfirmModal(candidate, card));
    return card;
  }

  async function loadCandidates() {
    setGridStatus("loading", cfg.i18n.searching);
    renderEmpty(cfg.i18n.searching);
    try {
      const payload = await postJson(cfg.endpoints.candidates, {});
      const candidates = payload.candidates || [];
      renderProviderPills(payload.providers || []);
      grid.innerHTML = "";
      if (candidates.length === 0) {
        renderEmpty(cfg.i18n.noResults);
        setGridStatus("empty", cfg.i18n.noResults);
        return;
      }
      candidates.forEach((c) => grid.appendChild(renderCard(c)));
      setGridStatus("ok", candidates.length + " candidates");
    } catch (err) {
      renderEmpty(cfg.i18n.error);
      setGridStatus("empty", cfg.i18n.error);
    }
  }

  // -------- Confirm-replace modal --------
  const $confirm = $("#cwa-cover-picker-confirm");
  const confirmCurrent = document.getElementById("cwa-cover-picker-confirm-current");
  const confirmNew = document.getElementById("cwa-cover-picker-confirm-new");
  const confirmMeta = document.getElementById("cwa-cover-picker-confirm-meta");
  const confirmApply = document.getElementById("cwa-cover-picker-confirm-apply");
  let pendingCandidate = null;

  function openConfirmModal(candidate, sourceCard) {
    pendingCandidate = { candidate, sourceCard };
    confirmCurrent.src = currentImg().src;
    confirmNew.src = candidate.cover_url;
    confirmMeta.innerHTML = "";
    if (candidate.source_name) {
      const src = document.createElement("div");
      src.innerHTML = "<strong>" + escapeHtml(candidate.source_name) + "</strong>";
      confirmMeta.appendChild(src);
    }
    if (candidate.title) {
      const t = document.createElement("div");
      t.textContent = candidate.title + (candidate.year ? " (" + candidate.year + ")" : "");
      confirmMeta.appendChild(t);
    }
    $confirm.modal("show");
  }

  confirmApply.addEventListener("click", async () => {
    if (!pendingCandidate) return;
    const { candidate, sourceCard } = pendingCandidate;
    confirmApply.disabled = true;
    confirmApply.textContent = cfg.i18n.applying;
    sourceCard.classList.add("is-applying");
    let payload;
    if (candidate.source_id === "embedded") {
      payload = await postJson(cfg.endpoints.apply, { kind: "embedded" });
    } else {
      payload = await postJson(cfg.endpoints.apply, { kind: "url", url: candidate.cover_url });
    }
    sourceCard.classList.remove("is-applying");
    confirmApply.disabled = false;
    confirmApply.textContent = "Use this cover";  // gettext on next render
    if (payload.ok) {
      bumpCurrentCover(payload.cover_url);
      $confirm.modal("hide");
      pendingCandidate = null;
    } else {
      alert(payload.error_message || cfg.i18n.error);
    }
  });

  // -------- URL panel --------
  const urlInput = document.getElementById("cwa-cover-picker-url-input");
  const urlFeedback = document.getElementById("cwa-cover-picker-url-feedback");
  const urlActions = document.getElementById("cwa-cover-picker-url-actions");
  const urlThumb = document.getElementById("cwa-cover-picker-url-thumb");
  const urlMeta = document.getElementById("cwa-cover-picker-url-meta");
  const urlApply = document.getElementById("cwa-cover-picker-url-apply");
  let urlDebounce = null;
  let lastValidatedUrl = null;
  let lastValidationResult = null;

  urlInput.addEventListener("input", () => {
    clearTimeout(urlDebounce);
    urlActions.hidden = true;
    urlFeedback.className = "cwa-cover-picker__feedback";
    urlFeedback.textContent = "";
    const url = (urlInput.value || "").trim();
    if (url.length < 8) return;
    urlDebounce = setTimeout(() => validateUrl(url), 400);
  });

  async function validateUrl(url) {
    urlFeedback.className = "cwa-cover-picker__feedback";
    urlFeedback.textContent = cfg.i18n.searching;
    let payload;
    try {
      payload = await postJson(cfg.endpoints.preview, { url });
    } catch (_) {
      urlFeedback.classList.add("is-error");
      urlFeedback.textContent = cfg.i18n.error;
      return;
    }
    if (!payload.valid) {
      urlFeedback.classList.add("is-error");
      urlFeedback.textContent = payload.error_message || cfg.i18n.error;
      // Make sure a previous successful preview's "Use this cover" button
      // doesn't linger when the user types a worse URL afterwards.
      urlActions.hidden = true;
      lastValidatedUrl = null;
      lastValidationResult = null;
      return;
    }
    lastValidatedUrl = url;
    lastValidationResult = payload;
    urlFeedback.classList.add("is-ok");
    urlFeedback.textContent = "Looks good.";
    urlThumb.src = url;
    const meta = [];
    if (payload.width && payload.height) meta.push(payload.width + "×" + payload.height);
    if (payload.size_bytes) meta.push(Math.round(payload.size_bytes / 1024) + " KB");
    if (payload.content_type) meta.push(payload.content_type);
    urlMeta.textContent = meta.join(" · ");
    urlActions.hidden = false;
  }

  urlApply.addEventListener("click", async () => {
    if (!lastValidatedUrl) return;
    urlApply.disabled = true;
    urlApply.textContent = cfg.i18n.applying;
    const payload = await postJson(cfg.endpoints.apply, { kind: "url", url: lastValidatedUrl });
    urlApply.disabled = false;
    urlApply.textContent = "Use this cover";
    if (payload.ok) {
      bumpCurrentCover(payload.cover_url);
      urlFeedback.classList.remove("is-error");
      urlFeedback.classList.add("is-ok");
      urlFeedback.textContent = cfg.i18n.applied;
    } else {
      alert(payload.error_message || cfg.i18n.error);
    }
  });

  // -------- Upload panel --------
  const uploadInput = document.getElementById("cwa-cover-picker-upload-input");
  const uploadApply = document.getElementById("cwa-cover-picker-upload-apply");
  const uploadFeedback = document.getElementById("cwa-cover-picker-upload-feedback");

  uploadInput.addEventListener("change", () => {
    uploadApply.disabled = !uploadInput.files || uploadInput.files.length === 0;
    uploadFeedback.className = "cwa-cover-picker__feedback";
    uploadFeedback.textContent = "";
  });

  uploadApply.addEventListener("click", async () => {
    if (!uploadInput.files || !uploadInput.files[0]) return;
    const fd = new FormData();
    fd.append("file", uploadInput.files[0]);
    uploadApply.disabled = true;
    uploadFeedback.className = "cwa-cover-picker__feedback";
    uploadFeedback.textContent = cfg.i18n.applying;
    let payload;
    try {
      payload = await postForm(cfg.endpoints.apply, fd);
    } catch (_) {
      payload = { ok: false, error_message: cfg.i18n.error };
    }
    uploadApply.disabled = false;
    if (payload.ok) {
      uploadFeedback.classList.add("is-ok");
      uploadFeedback.textContent = cfg.i18n.applied;
      bumpCurrentCover(payload.cover_url);
      uploadInput.value = "";
    } else {
      uploadFeedback.classList.add("is-error");
      uploadFeedback.textContent = payload.error_message || cfg.i18n.error;
    }
  });

  // -------- Lock toggle --------
  const lockCheckbox = document.getElementById("cwa-cover-picker-lock");
  lockCheckbox.addEventListener("change", async () => {
    const desired = lockCheckbox.checked;
    const payload = await postJson(cfg.endpoints.lock, { locked: desired });
    if (typeof payload.locked === "boolean") {
      lockCheckbox.checked = payload.locked;
    }
  });

  // -------- API-keys panel (mirrors metadata-search modal) --------
  const keysList = document.getElementById("cwa-cover-picker-keys-list");
  let keysLoaded = false;
  document.getElementById("cwa-cover-picker-keys-panel").addEventListener("toggle", function () {
    if (!this.open || keysLoaded) return;
    loadKeys();
  });

  async function loadKeys() {
    keysLoaded = true;
    const entries = await getJson(cfg.endpoints.keysList);
    keysList.innerHTML = "";
    if (!entries || entries.length === 0) {
      keysList.textContent = "No keys to configure.";
      return;
    }
    entries.forEach(renderKeyRow);
  }

  function renderKeyRow(entry) {
    const row = document.createElement("div");
    row.className = "cwa-cover-picker__keys-row";

    const name = document.createElement("div");
    name.className = "cwa-cover-picker__keys-name";
    name.textContent = entry.name;
    row.appendChild(name);

    const state = document.createElement("div");
    state.className = "cwa-cover-picker__keys-state";
    state.textContent = entry.configured ? "Configured" : "Not configured";
    state.style.color = entry.configured ? "#137333" : "#888";
    row.appendChild(state);

    if (entry.can_edit) {
      const inputWrap = document.createElement("div");
      inputWrap.className = "cwa-cover-picker__keys-input";
      const input = document.createElement("input");
      input.type = "text";
      input.className = "form-control input-sm";
      input.placeholder = entry.configured ? "Replace key…" : "Paste API key…";
      inputWrap.appendChild(input);
      row.appendChild(inputWrap);

      const save = document.createElement("button");
      save.className = "btn btn-default btn-sm";
      save.textContent = "Save";
      save.addEventListener("click", async () => {
        const value = input.value.trim();
        const url = cfg.endpoints.keysSave.replace("__PROV__", encodeURIComponent(entry.id));
        const payload = await postJson(url, { value });
        if (payload && payload.id) {
          state.textContent = payload.configured ? "Configured" : "Not configured";
          state.style.color = payload.configured ? "#137333" : "#888";
          input.value = "";
        }
      });
      row.appendChild(save);
    }
    keysList.appendChild(row);
  }

  // -------- Refresh + bookkeeping --------
  document.getElementById("cwa-cover-picker-refresh").addEventListener("click", loadCandidates);

  function currentImg() { return document.getElementById("cwa-cover-picker-current-img"); }

  function bumpCurrentCover(url) {
    const img = currentImg();
    img.src = url || (cfg.endpoints.coverGet + "?ts=" + Date.now());
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c];
    });
  }

  // Kick off the candidate fetch as soon as the page renders.
  document.addEventListener("DOMContentLoaded", loadCandidates);
  if (document.readyState !== "loading") loadCandidates();

  // -------- Kobo preview (issue #84) ----------------------------------------
  // When the operator has Kobo cover padding enabled at the admin level,
  // the template renders an extra <details> panel with a toggle + the same
  // aspect / fill_mode / color controls that live in Settings. Flipping the
  // toggle on swaps every visible cover <img> to a server-padded variant.
  // Picker-session-local — does NOT mutate global config.
  //
  // Concurrency model: a monotonic `generation` counter increments on every
  // refresh. Each fetch closes over its own gen; when it resolves, it only
  // mutates UI state (img.src, in-flight count) if its gen is still current.
  // Aborts of older bursts therefore can't corrupt the live counter. The
  // `cache` keyed by settings populates regardless of generation, so a
  // late-arriving response is never wasted — the user will see it the next
  // time those settings come back.
  (function setupKoboPreview() {
    const panel = document.getElementById("cwa-cover-picker-kobo-panel");
    if (!panel) return;

    const toggle = document.getElementById("cwa-cover-picker-kobo-enabled");
    const aspectSel = document.getElementById("cwa-cover-picker-kobo-aspect");
    const fillSel = document.getElementById("cwa-cover-picker-kobo-fill-mode");
    const colorInput = document.getElementById("cwa-cover-picker-kobo-color");
    const statusEl = document.getElementById("cwa-cover-picker-kobo-status");
    const endpoint = cfg.endpoints.koboPreview;
    if (!toggle || !aspectSel || !fillSel || !colorInput || !endpoint) return;

    const cache = new WeakMap();
    let currentBurst = null;
    let generation = 0;
    let activeGen = 0;
    let activeInFlight = 0;

    function updateStatus() {
      if (!statusEl) return;
      if (activeInFlight > 0) {
        statusEl.textContent = " · " + (cfg.i18n.koboRendering || "Rendering Kobo previews:") + " " + activeInFlight + "…";
        statusEl.hidden = false;
      } else {
        statusEl.hidden = true;
      }
    }
    function abortCurrentBurst() {
      if (currentBurst) {
        currentBurst.abort();
        currentBurst = null;
      }
      activeInFlight = 0;
      updateStatus();
    }

    function settingsKey() {
      return aspectSel.value + "|" + fillSel.value + "|" + (colorInput.value || "");
    }

    function syncColorEnabled() {
      const isManual = fillSel.value === "manual";
      colorInput.disabled = !isManual;
      colorInput.style.opacity = isManual ? "1" : "0.5";
    }
    syncColorEnabled();
    fillSel.addEventListener("change", syncColorEnabled);

    function originalSrcOf(img) {
      if (!img.dataset.koboOriginalSrc) {
        img.dataset.koboOriginalSrc = img.src;
      }
      return img.dataset.koboOriginalSrc;
    }

    function isSameOriginCoverUrl(url) {
      // Same-origin /cover/<id>/... — let server load from disk; cw_advocate
      // SSRF guard refuses to fetch our own host.
      try {
        const u = new URL(url, location.href);
        return u.origin === location.origin && /^\/cover\/\d+/.test(u.pathname);
      } catch { return false; }
    }

    async function fetchPreview(srcUrl, signal) {
      const body = {
        aspect: aspectSel.value,
        fill_mode: fillSel.value,
        color: colorInput.value || "",
      };
      if (srcUrl && srcUrl.startsWith("data:")) {
        body.embedded = true;
      } else if (srcUrl && !isSameOriginCoverUrl(srcUrl)) {
        body.candidate_url = srcUrl;
      }
      const resp = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": cfg.csrfToken },
        body: JSON.stringify(body),
        signal,
      });
      if (!resp.ok) throw new Error("kobo-preview HTTP " + resp.status);
      const data = await resp.json();
      if (!data || !data.ok) throw new Error("kobo-preview body !ok");
      return data.data_url;
    }

    function revertImg(img) {
      const orig = img.dataset.koboOriginalSrc;
      if (orig) img.src = orig;
    }

    function visibleCoverImgs() {
      const ids = [
        "cwa-cover-picker-current-img",
        "cwa-cover-picker-confirm-current",
        "cwa-cover-picker-confirm-new",
        "cwa-cover-picker-url-thumb",
      ];
      const out = [];
      ids.forEach(function (id) {
        const el = document.getElementById(id);
        if (el && el.tagName === "IMG" && el.src) out.push(el);
      });
      const grid = document.getElementById("cwa-cover-picker-grid");
      if (grid) Array.prototype.forEach.call(grid.querySelectorAll("img"), function (i) { out.push(i); });
      return out;
    }

    // Max parallel fetches per refresh. The server-side BoundedSemaphore is
    // 4 — going beyond that just queues at the worker, ties up gunicorn
    // workers, and starves unrelated routes (login, /static/) until the
    // burst drains. Cap the same on the client so the rest of the app
    // stays responsive while ~36 candidate covers re-render.
    const KOBO_MAX_CONCURRENT = 4;

    function refreshAll() {
      if (!toggle.checked) {
        // Toggle off: cancel work and revert every img to its original src.
        abortCurrentBurst();
        visibleCoverImgs().forEach(revertImg);
        return;
      }

      // Bump the generation BEFORE aborting. Old in-flight promises observe
      // myGen != generation when they settle and don't touch the live UI.
      generation += 1;
      const myGen = generation;
      abortCurrentBurst();
      const burst = new AbortController();
      currentBurst = burst;
      activeGen = myGen;

      const key = settingsKey();
      const imgs = visibleCoverImgs();
      const queue = [];

      imgs.forEach(function (img) {
        const orig = originalSrcOf(img);
        if (!orig) return;
        let perImg = cache.get(img);
        if (!perImg) { perImg = new Map(); cache.set(img, perImg); }

        if (perImg.has(key)) {
          // Cache hit: instant swap, no fetch.
          img.src = perImg.get(key);
          return;
        }

        queue.push({ img: img, orig: orig, perImg: perImg });
      });

      activeInFlight = queue.length;
      updateStatus();

      function runOne(slot) {
        if (myGen !== generation) return Promise.resolve();
        const job = queue.shift();
        if (!job) return Promise.resolve();
        return fetchPreview(job.orig, burst.signal).then(function (dataUrl) {
          // Always cache — useful for any future refresh that lands on
          // these settings, even if this generation is no longer current.
          job.perImg.set(key, dataUrl);
          if (generation === myGen && toggle.checked && key === settingsKey()) {
            job.img.src = dataUrl;
          }
        }).catch(function (e) {
          if (e && e.name === "AbortError") return;
          console.info("[cover-picker] kobo preview unavailable for one cover", e);
        }).finally(function () {
          if (activeGen === myGen) {
            activeInFlight = Math.max(0, activeInFlight - 1);
            updateStatus();
          }
        }).then(function () {
          // Pull the next job into this slot until the queue drains.
          return runOne(slot);
        });
      }

      const slots = Math.min(KOBO_MAX_CONCURRENT, queue.length);
      for (let i = 0; i < slots; i++) runOne(i);
    }

    toggle.addEventListener("change", refreshAll);
    // Closing the <details> panel implicitly cancels too — same effect as
    // toggle off as far as "stop burning server cycles" goes.
    panel.addEventListener("toggle", function () {
      if (!panel.open && currentBurst) abortCurrentBurst();
    });

    // Discrete-event controls (dropdowns) refresh immediately. Continuous
    // input (manual hex color) is debounced so per-keystroke fetches don't
    // pile up. Both call refreshAll, which cancels the prior burst before
    // starting fresh, so even rapid changes don't pile work onto the server.
    aspectSel.addEventListener("change", refreshAll);
    fillSel.addEventListener("change", refreshAll);
    let colorTimer = null;
    colorInput.addEventListener("input", function () {
      clearTimeout(colorTimer);
      colorTimer = setTimeout(function () {
        if (toggle.checked) refreshAll();
      }, 250);
    });

    // Watch the candidate grid for new <img> nodes (the API response renders
    // ~67 cards in a burst). Debounce so we trigger one refresh per burst
    // instead of one per card — without this we'd fire 67 concurrent fetches
    // and the browser hits ERR_INSUFFICIENT_RESOURCES.
    const grid = document.getElementById("cwa-cover-picker-grid");
    if (grid) {
      let obsTimer = null;
      const obs = new MutationObserver(function () {
        if (!toggle.checked) return;
        clearTimeout(obsTimer);
        obsTimer = setTimeout(refreshAll, 200);
      });
      obs.observe(grid, { childList: true, subtree: true });
    }
  })();
})();
