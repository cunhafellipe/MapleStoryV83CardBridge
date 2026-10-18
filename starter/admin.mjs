const escape = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const clone = (v) => structuredClone(v);
function recoverySummary(value) {
  if (!value) return "Not yet reported";
  if (value.finishedAt || value.startedAt)
    return `${value.completed ?? 0} completed · ${value.failed ?? 0} failed · ${value.finishedAt ?? value.startedAt}`;
  return value.message ?? value.error ?? value.at ?? "Not yet reported";
}
const imageAllowed = (v) =>
  !v ||
  /^\/assets\/library\/(?:[a-zA-Z0-9_-]+\/)*[a-zA-Z0-9_-]+\.(png|jpg|jpeg|webp)$/.test(
    v,
  ) ||
  /^https:\/\/maplestoryitcg\.weebly\.com\/uploads\/(?:\d+\/)+[a-zA-Z0-9_.-]+\.(png|jpg|jpeg)$/.test(
    v,
  );
export function createAdministration({ api, current, onPublish }) {
  const root = document.getElementById("administration"),
    link = document.getElementById("admin-link");
  const tabViews = new Map(),
    modifiedForms = new Set();
  const disabledBeforeRequest = new WeakMap();
  let draftRevision = 0;
  let renderedTab;
  let base,
    draft,
    preview,
    tab = "overview",
    dirty = false,
    loading = false,
    epoch = 0,
    loaded = false,
    overview,
    orderAfter,
    activityAfter;
  const $ = (id) => root.querySelector(`#${id}`);
  const allowed = () =>
    current().session.signedIn &&
    current().session.role === "administrator" &&
    current().available;
  const status = (text, error = false) => {
    $("admin-status").textContent = text;
    $("admin-status").classList.toggle("error", error);
  };
  function clear() {
    epoch++;
    base = draft = preview = overview = undefined;
    dirty = loaded = loading = false;
    orderAfter = activityAfter = undefined;
    tabViews.clear();
    modifiedForms.clear();
    renderedTab = undefined;
    root.hidden = true;
    link.hidden = true;
    root.replaceChildren();
  }
  function sync() {
    link.hidden = !allowed();
    if (
      !current().session.signedIn ||
      current().session.role !== "administrator"
    )
      clear();
    else if (!current().available) root.hidden = true;
  }
  const guard = (run) => {
    const e = epoch,
      g = current().generation;
    return () =>
      e === epoch && g === current().generation && allowed() && run !== false;
  };
  function shell() {
    root.innerHTML = `<div class="section-heading"><div><p class="eyebrow">SERVER ADMINISTRATION</p><h2>Make this collection your own</h2></div><span id="admin-version"></span></div><p class="muted">Edits stay in this tab until you preview and publish. Closing the page or signing out discards unpublished work.</p><nav class="admin-tabs" aria-label="Administration sections">${[
      ["overview", "Overview"],
      ["cards", "Cards & sets"],
      ["packs", "Packs"],
      ["import", "Import & export"],
      ["rewards", "Rewards"],
      ["activity", "Orders & activity"],
    ]
      .map(
        ([id, name]) =>
          `<button type="button" class="quiet" data-tab="${id}" aria-pressed="false">${name}</button>`,
      )
      .join(
        "",
      )}</nav><p id="admin-status" role="status" aria-live="polite"></p><div id="admin-content"></div><div class="admin-publish panel"><div><strong id="draft-status">Published cards and packs</strong><p id="preview-summary" class="muted">Save form changes to your draft, then review them before publishing.</p></div><div class="actions"><button id="admin-preview" type="button">Preview changes</button><button id="admin-publish" type="button" disabled>Publish reviewed changes</button><button id="admin-discard" type="button" class="quiet">Discard draft</button></div><div id="preview-details"></div></div>`;
    root.querySelectorAll("[data-tab]").forEach(
      (b) =>
        (b.onclick = () => {
          tab = b.dataset.tab;
          renderTab();
        }),
    );
    $("admin-preview").onclick = makePreview;
    $("admin-publish").onclick = publish;
    $("admin-discard").onclick = () => {
      if (
        (dirty || modifiedForms.size) &&
        !window.confirm("Discard unpublished changes in this tab?")
      )
        return;
      draft = clone(base);
      dirty = false;
      resetEditors();
      invalidate();
      renderTab();
      status("Draft discarded. The live catalog is unchanged.");
    };
    updateDraft();
  }
  function updateDraft() {
    if (!draft) return;
    $("admin-version").textContent = `Live version ${base.version}`;
    $("draft-status").textContent = dirty
      ? "Unpublished draft"
      : "Published cards and packs · no saved changes";
    $("admin-preview").disabled =
      loading || !dirty || modifiedForms.size > 0 || !allowed();
    $("admin-publish").disabled = loading || !preview || !allowed();
    root.querySelectorAll("[data-tab]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
      b.disabled = loading;
    });
    $("admin-discard").disabled = loading;
    root
      .querySelectorAll(
        "#admin-content input, #admin-content select, #admin-content textarea, #admin-content button",
      )
      .forEach((control) => {
        if (loading) {
          if (!disabledBeforeRequest.has(control))
            disabledBeforeRequest.set(control, control.disabled);
          control.disabled = true;
        } else if (disabledBeforeRequest.has(control)) {
          control.disabled = disabledBeforeRequest.get(control);
          disabledBeforeRequest.delete(control);
        }
      });
  }
  function resetEditors() {
    draftRevision++;
    tabViews.clear();
    modifiedForms.clear();
    renderedTab = undefined;
  }
  function invalidate() {
    preview = undefined;
    $("preview-details").replaceChildren();
    $("preview-summary").textContent =
      "Draft changed. Preview again before publishing.";
    updateDraft();
  }
  function changed() {
    draftRevision++;
    dirty = true;
    draft.version = base.version + 1;
    invalidate();
    status("Saved to draft. Review and publish to make it live.");
  }
  async function open() {
    if (!allowed()) return;
    root.hidden = false;
    root.scrollIntoView({ block: "start" });
    if (loaded) return;
    const valid = guard();
    loading = true;
    try {
      const [data, health] = await Promise.all([
        api("admin/catalog"),
        api("admin/overview"),
      ]);
      if (!valid()) return;
      base = clone(data.catalog ?? data);
      draft = clone(base);
      overview = health;
      loaded = true;
      shell();
      renderTab();
    } catch (e) {
      if (valid()) {
        root.innerHTML = `<h2>Administration unavailable</h2><p role="alert">${escape(e.message)}</p><button id="admin-load-retry">Retry administration</button>`;
        $("admin-load-retry").onclick = open;
      }
    } finally {
      if (valid()) {
        loading = false;
        if (loaded) updateDraft();
      }
    }
  }
  link.onclick = (e) => {
    e.preventDefault();
    open();
  };
  function field(label, name, value = "", attrs = "") {
    return `<label>${label}<input name="${name}" value="${escape(value)}" ${attrs}></label>`;
  }
  function select(label, name, rows, value) {
    return `<label>${label}<select name="${name}">${rows.map((r) => `<option value="${escape(r.id)}" ${r.id === value ? "selected" : ""}>${escape(r.name ?? r.id)}</option>`).join("")}</select></label>`;
  }
  function formStatus(form, message) {
    form.querySelector(".form-status").textContent = message;
  }
  function validateIdentity(form, existing) {
    if (!form.elements.name.value.trim()) throw Error("Name: enter a name.");
    const id = form.elements.id.value.trim();
    if (
      !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/.test(id) ||
      ["constructor", "prototype", "__proto__"].includes(id)
    )
      throw Error(
        "ID: use 1–100 letters, numbers, dots, underscores or hyphens.",
      );
    if (
      !existing &&
      [...draft.lines, ...draft.cards, ...draft.products].some(
        (x) => x.id === id,
      )
    )
      throw Error("This ID is already used. Choose a different ID.");
    return id;
  }
  function bindForm(form, save) {
    form.addEventListener("input", () => markModified(form));
    form.onsubmit = (e) => {
      e.preventDefault();
      if (!allowed()) return;
      try {
        save();
        modifiedForms.delete(form);
        changed();
        formStatus(form, "Saved to draft.");
      } catch (e) {
        formStatus(form, e.message);
      }
    };
  }
  function markModified(form) {
    draftRevision++;
    preview = undefined;
    modifiedForms.add(form);
    $("preview-summary").textContent =
      "Save your form changes to the draft before previewing.";
    updateDraft();
  }
  function changeSelection(select, editor, render) {
    let selected = select.value;
    select.onchange = () => {
      const form = editor.querySelector("form");
      if (form && modifiedForms.has(form)) {
        if (
          !window.confirm(
            "Discard unsaved form fields? Changes already saved to the draft are retained.",
          )
        ) {
          select.value = selected;
          return;
        }
        modifiedForms.delete(form);
      }
      selected = select.value;
      render();
      updateDraft();
    };
  }
  function renderTab() {
    if (!draft || !allowed()) return;
    updateDraft();
    const content = $("admin-content");
    if (renderedTab === tab && !["overview", "activity"].includes(tab)) return;
    if (
      renderedTab &&
      renderedTab !== tab &&
      !["overview", "activity"].includes(renderedTab)
    ) {
      const saved = document.createDocumentFragment();
      while (content.firstChild) saved.append(content.firstChild);
      tabViews.set(renderedTab, saved);
    }
    if (renderedTab !== tab && tabViews.has(tab)) {
      content.replaceChildren(tabViews.get(tab));
      tabViews.delete(tab);
      renderedTab = tab;
      for (const input of content.querySelectorAll("select[name=lineId]"))
        for (const row of draft.lines) {
          if (![...input.options].some((o) => o.value === row.id))
            input.add(new Option(row.name, row.id));
        }
      for (const group of content.querySelectorAll(".slot-weights"))
        for (const variant of draft.variants) {
          if (
            [...group.querySelectorAll("[data-weight]")].some(
              (i) => i.dataset.weight === variant.id,
            )
          )
            continue;
          const card = draft.cards.find((c) => c.id === variant.cardId);
          group.insertAdjacentHTML(
            "beforeend",
            `<label>${escape(card?.name ?? variant.id)}<input type="number" min="0" max="1000000" value="0" data-weight="${escape(variant.id)}" aria-label="${escape(variant.id)} weight"></label>`,
          );
        }
      return;
    }
    renderedTab = tab;
    if (tab === "overview") {
      const h = overview ?? {},
        orders = h.orders ?? {},
        rewards = h.rewards ?? {};
      content.innerHTML = `<div class="admin-summary"><article class="panel"><h3>Game and website</h3><dl><dt>Game connection</dt><dd>${h.gameReady === undefined ? escape(h.game?.status ?? "Unknown") : h.gameReady ? "Connected" : "Unavailable · purchases paused"}</dd><dt>Game server write access</dt><dd>${h.leaseReady === undefined ? "Not reported" : h.leaseReady ? "Ready" : "Unavailable"}</dd><dt>Reward confirmation connection</dt><dd>${h.callbackReady === undefined ? "Not configured" : h.callbackReady ? "Ready" : "Unavailable"}</dd><dt>Storage</dt><dd>${h.storage?.ok === undefined ? "Not reported" : h.storage.ok ? "Ready" : "Needs attention"} · ${escape(h.storage?.profile ?? "Single writer")}</dd></dl><p>Catalog version ${base.version}. Rewards ${rewards.enabled || h.rewardsEnabled ? "enabled" : "off"}.</p><button id="admin-health" class="quiet">Check connections</button></article><article class="panel"><h3>Purchases that need attention</h3><p>${escape(orders.unresolved ?? h.unresolvedOrders ?? 0)} purchases needing attention</p><p>Oldest unresolved: ${escape(h.oldestOrderAt ?? "None")}</p><p>${escape(h.orphanedPayments ?? 0)} payments missing their purchase records. New purchases stay paused until an operator restores the original records.</p><p>Code registrations with the game: ${escape(h.registrations?.reduce((n, r) => n + Number(r.count ?? 0), 0) ?? 0)}</p><p>Last recovery: ${escape(recoverySummary(h.lastRecovery))}</p><p>Open Orders & activity to inspect and retry an existing purchase.</p></article></div><details><summary>Technical connection details</summary><pre>${escape(JSON.stringify(h, null, 2))}</pre></details>`;
      $("admin-health").onclick = async () => {
        const valid = guard();
        try {
          const h = await api("admin/overview");
          if (valid()) {
            overview = h;
            renderTab();
          }
        } catch (e) {
          if (valid()) status(e.message, true);
        }
      };
      if (h.gameDiagnostics) {
        const q = h.gameDiagnostics;
        content.insertAdjacentHTML(
          "beforeend",
          `<article class="panel"><h3>Game reward queue</h3><p>${escape(q.pendingRewards ?? 0)} rewards pending · ${escape(q.pendingCallbacks ?? 0)} confirmations pending</p><p>Oldest pending: ${escape(q.oldestPendingAt ?? "None")} · Last attempt: ${escape(q.lastAttemptAt ?? "None")}</p><p>${escape(q.attempts ?? 0)} attempts · ${escape(q.lastError ?? "No error reported")}</p><p>Open Rewards to check whether codes have reached the game server. Pending game confirmations retry automatically when the game connection recovers.</p></article>`,
        );
      }
    } else if (tab === "cards") renderCards(content);
    else if (tab === "packs") renderPacks(content);
    else if (tab === "import") renderImport(content);
    else if (tab === "rewards") {
      const r = overview?.rewards ?? {
        enabled: overview?.rewardsEnabled,
        provider: overview?.provider,
        registrations: overview?.registrations,
      };
      content.innerHTML = `<div class="panel"><h3>${r.enabled || overview?.rewardsEnabled ? "Configured rewards" : "No reward pool configured"}</h3><p>The default Shapes collection contains collectibles only. Reward codes require the supported Series One profile to be explicitly enabled during deployment.</p><p>Turning off new rewards does not cancel codes already given to players. Operators cannot reveal another account’s code or manually mark it used.</p><p><a href="https://github.com/zkkng/MapleStoryV83CardBridge/blob/main/docs/code-rules.md" target="_blank" rel="noopener noreferrer">Reward setup guide ↗</a></p><pre>${escape(JSON.stringify(r, null, 2))}</pre><h4>Reward delivery</h4><div id="admin-registrations"></div><button id="registrations-more" class="quiet" hidden>Show more reward codes</button></div>`;
      loadRegistrations();
    } else renderActivity(content);
  }
  function loadRegistrations() {
    let after;
    async function load(append = false) {
      const valid = guard();
      try {
        const query = new URLSearchParams({ limit: "25" });
        if (append && after) query.set("after", after);
        const page = await api("admin/registrations?" + query);
        if (!valid() || tab !== "rewards") return;
        const html =
          page.items
            .map(
              (r) =>
                `<article class="order-row"><strong>${escape(r.id)}</strong><p>${escape(r.state)} · ${escape(r.attempts)} attempts · ${escape(r.lastError ?? "No error")}</p><p>Last attempt: ${escape(r.lastAttemptAt ?? "None")} · Next retry: ${escape(r.nextAttemptAt ?? "None")}</p>${r.state === "pending" || r.lastError ? `<button class="quiet" data-registration="${escape(r.id)}">Retry registration</button>` : ""}</article>`,
            )
            .join("") || "<p>No reward deliveries need reporting.</p>";
        if (append)
          $("admin-registrations").insertAdjacentHTML("beforeend", html);
        else $("admin-registrations").innerHTML = html;
        after = page.next;
        $("registrations-more").hidden = !after;
        $("admin-registrations")
          .querySelectorAll("[data-registration]")
          .forEach(
            (b) =>
              (b.onclick = async () => {
                const valid = guard();
                b.disabled = true;
                try {
                  await api(
                    "admin/registrations/" +
                      encodeURIComponent(b.dataset.registration) +
                      "/retry",
                    {},
                  );
                  if (valid()) {
                    status("Retried sending the same code to the game server.");
                    await load();
                  }
                } catch (e) {
                  if (valid()) {
                    status(e.message, true);
                    b.disabled = false;
                  }
                }
              }),
          );
      } catch (e) {
        if (valid()) status(e.message, true);
      }
    }
    $("registrations-more").onclick = () => load(true);
    load();
  }
  function renderCards(content) {
    content.innerHTML = `<div class="admin-editor-grid"><div class="panel"><h3>Sets</h3>${select("Edit set", "setChoice", [{ id: "", name: "Create a set" }, ...draft.lines], "")}<div id="set-editor"></div></div><div class="panel"><h3>Cards</h3><label>Find card<input id="admin-card-search" type="search" maxlength="120"></label>${select("Edit card", "cardChoice", [{ id: "", name: "Create a card" }, ...draft.cards], "")}<div id="card-editor"></div></div></div>`;
    const setSelect = content.querySelector("[name=setChoice]"),
      cardSelect = content.querySelector("[name=cardChoice]");
    function setForm() {
      let row = draft.lines.find((x) => x.id === setSelect.value);
      $("set-editor").innerHTML =
        `<form class="admin-form">${field("Set ID (permanent)", "id", row?.id, "required maxlength=100 " + (row ? "readonly" : ""))}${field("Set name", "name", row?.name, "required maxlength=200")}<label>Description<textarea name="description" maxlength="2000">${escape(row?.description)}</textarea></label><p class="form-status" role="status"></p><button>Save set to draft</button></form>`;
      bindForm($("set-editor").querySelector("form"), () => {
        const f = $("set-editor").querySelector("form"),
          id = validateIdentity(f, row),
          value = {
            ...row,
            id,
            name: f.elements.name.value.trim(),
            description: f.elements.description.value.trim(),
          };
        if (!value.description) delete value.description;
        if (row) {
          Object.assign(row, value);
          if (!value.description) delete row.description;
        } else {
          draft.lines.push(value);
          row = value;
          setSelect.add(new Option(value.name, value.id));
          setSelect.value = value.id;
          f.elements.id.readOnly = true;
        }
        for (const input of root.querySelectorAll("select[name=lineId]")) {
          if (![...input.options].some((o) => o.value === id))
            input.add(new Option(value.name, id));
          else
            input.querySelectorAll("option").forEach((o) => {
              if (o.value === id) o.textContent = value.name;
            });
        }
      });
    }
    function cardForm() {
      let row = draft.cards.find((x) => x.id === cardSelect.value);
      const variants = draft.variants.filter((v) => v.cardId === row?.id);
      $("card-editor").innerHTML =
        `<form class="admin-form">${field("Card ID (permanent)", "id", row?.id, "required maxlength=100 " + (row ? "readonly" : ""))}${field("Card name", "name", row?.name, "required maxlength=200")}${select("Set", "lineId", draft.lines, row?.lineId)}<label>Description<textarea name="description" maxlength="2000">${escape(row?.metadata?.description)}</textarea></label>${field("Card symbol", "symbol", row?.metadata?.symbol ?? "◇", "maxlength=8")}${field("Image path (optional)", "image", row?.metadata?.image, "maxlength=2048")}<small>Use /assets/library/name.png, .jpg or .webp. The explicit iTCG scan source is also supported. Layered art and arbitrary remote hosts are not rendered.</small>${select("Card version", "variantId", [...variants, { id: "", name: "Add a card version" }], variants[0]?.id ?? "")}${field("New card version ID (optional)", "newVariantId", "", "maxlength=100")}<small>For a new card version, leave its ID blank to use card-id.standard, or enter a unique ID up to 100 characters. A published card version keeps its rarity, and a published card stays in its set. Add a card version to use a different rarity.</small>${select("Rarity", "rarityId", draft.rarities, variants[0]?.rarityId ?? draft.rarities[0]?.id)}<p class="form-status" role="status"></p><button>Save card to draft</button></form>`;
      const f = $("card-editor").querySelector("form");
      f.elements.lineId.disabled = base.cards.some((c) => c.id === row?.id);
      function variantControls() {
        const selected = f.elements.variantId.value;
        f.elements.rarityId.disabled = base.variants.some(
          (v) => v.id === selected,
        );
        f.elements.newVariantId.disabled = !!selected;
      }
      f.elements.variantId.onchange = () => {
        f.elements.rarityId.value =
          draft.variants.find((v) => v.id === f.elements.variantId.value)
            ?.rarityId ?? draft.rarities[0]?.id;
        variantControls();
      };
      variantControls();
      bindForm(f, () => {
        const id = validateIdentity(f, row),
          image = f.elements.image.value.trim();
        if (!draft.lines.some((x) => x.id === f.elements.lineId.value))
          throw Error("Set: create a set before adding cards.");
        if (!imageAllowed(image))
          throw Error(
            "Image: use a supported static raster path or explicit iTCG source URL.",
          );
        const existingVariant = draft.variants.find(
          (v) => v.id === f.elements.variantId.value,
        );
        const variantId =
          existingVariant?.id ??
          (f.elements.newVariantId.value.trim() || id + ".standard");
        if (
          !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/.test(variantId) ||
          ["constructor", "prototype", "__proto__"].includes(variantId)
        )
          throw Error(
            "New variant ID: enter a unique ID up to 100 characters. Card IDs longer than 91 characters need an explicit variant ID.",
          );
        if (!existingVariant && draft.variants.some((v) => v.id === variantId))
          throw Error(
            "New variant ID: this ID is already used. Enter a unique ID.",
          );
        const publishedVariant = base.variants.find((v) => v.id === variantId);
        if (
          publishedVariant &&
          publishedVariant.rarityId !== f.elements.rarityId.value
        )
          throw Error(
            "Rarity: published variants cannot change rarity. Choose Add a card version.",
          );
        const publishedCard = base.cards.find((c) => c.id === id);
        if (publishedCard && publishedCard.lineId !== f.elements.lineId.value)
          throw Error(
            "Set: a published card cannot move to another set. Create a new card instead.",
          );
        const value = {
          ...row,
          id,
          lineId: f.elements.lineId.value,
          name: f.elements.name.value.trim(),
          metadata: {
            ...row?.metadata,
            description: f.elements.description.value.trim(),
            symbol: f.elements.symbol.value.trim(),
          },
        };
        if (image) value.metadata.image = image;
        else delete value.metadata.image;
        if (row) Object.assign(row, value);
        else {
          draft.cards.push(value);
          row = value;
          cardSelect.add(new Option(value.name, value.id));
          cardSelect.value = value.id;
          f.elements.id.readOnly = true;
        }
        if (existingVariant)
          existingVariant.rarityId = f.elements.rarityId.value;
        else {
          draft.variants.push({
            id: variantId,
            cardId: id,
            rarityId: f.elements.rarityId.value,
          });
          f.elements.variantId.add(new Option(variantId, variantId));
          f.elements.variantId.value = variantId;
          variantControls();
        }
      });
    }
    changeSelection(setSelect, $("set-editor"), setForm);
    changeSelection(cardSelect, $("card-editor"), cardForm);
    $("admin-card-search").oninput = () => {
      const q = $("admin-card-search").value.toLowerCase();
      for (const o of cardSelect.options)
        o.hidden = !!o.value && !o.textContent.toLowerCase().includes(q);
    };
    setForm();
    cardForm();
  }
  function renderPacks(content) {
    content.innerHTML = `<div class="panel">${select("Edit pack", "packChoice", [{ id: "", name: "Create a pack" }, ...draft.products], "")}<div id="pack-editor"></div></div>`;
    const choice = content.querySelector("[name=packChoice]");
    function edit() {
      let row = draft.products.find((x) => x.id === choice.value);
      let slots = clone(row?.slots ?? [{ id: "cards", count: 8, pool: [] }]);
      $("pack-editor").innerHTML =
        `<form class="admin-form">${field("Pack ID (permanent)", "id", row?.id, "required maxlength=100 " + (row ? "readonly" : ""))}${field("Pack name", "name", row?.name, "required maxlength=200")}${select("Set", "lineId", draft.lines, row?.lineId)}<label><input type="checkbox" name="enabled" ${row?.enabled !== false ? "checked" : ""}> Available for new purchases</label><small>Turning this off stops new purchases. Players keep their cards and unopened packs.</small>${field("Price per pack", "amount", row?.price?.amount ?? 1000, "type=number min=1 max=100000000 required")}${field("Maximum packs per purchase", "maxQuantity", row?.maxQuantity ?? 5, "type=number min=1 max=100 required")}<h4>Card groups</h4><p class="muted">Each group picks cards from the versions below. Duplicates are possible. Higher weights make a version more likely: 2 is twice as likely as 1.</p><div id="slot-editors"></div><button id="add-slot" type="button" class="quiet">Add card group</button><p class="form-status" role="status"></p><button>Save pack to draft</button></form>`;
      const f = $("pack-editor").querySelector("form");
      function draw() {
        $("slot-editors").innerHTML = slots
          .map(
            (slot, i) =>
              `<fieldset data-slot="${i}"><legend>Group ${i + 1}</legend>${field("Group ID", "slotId", slot.id, "required maxlength=100")}${field("Cards drawn", "count", slot.count, "type=number min=1 max=100 required")}<div class="slot-weights">${draft.variants
                .map((v) => {
                  const c = draft.cards.find((c) => c.id === v.cardId),
                    p = slot.pool.find((p) => p.variantId === v.id);
                  return `<label>${escape(c?.name ?? v.id)} · ${escape(draft.rarities.find((r) => r.id === v.rarityId)?.name ?? v.rarityId)}<input type="number" min="0" max="1000000" value="${p?.weight ?? 0}" data-weight="${escape(v.id)}" aria-label="${escape(v.id)} weight"></label>`;
                })
                .join(
                  "",
                )}</div><small>Use 0 to leave a card version out. Use whole numbers of 1 or more to include it.</small><button type="button" class="quiet" data-remove-slot="${i}">Remove group</button></fieldset>`,
          )
          .join("");
        $("slot-editors")
          .querySelectorAll("[data-remove-slot]")
          .forEach(
            (b) =>
              (b.onclick = () => {
                readSlots();
                slots.splice(Number(b.dataset.removeSlot), 1);
                draw();
                markModified(f);
              }),
          );
      }
      function readSlots() {
        slots = [...$("slot-editors").querySelectorAll("fieldset")].map(
          (s, i) => ({
            ...slots[i],
            id: s.querySelector("[name=slotId]").value.trim(),
            count: Number(s.querySelector("[name=count]").value),
            pool: [...s.querySelectorAll("[data-weight]")]
              .filter((input) => Number(input.value) > 0)
              .map((input) => ({
                variantId: input.dataset.weight,
                weight: Number(input.value),
              })),
          }),
        );
      }
      $("add-slot").onclick = () => {
        readSlots();
        if (slots.length >= 20) {
          formStatus(f, "Use at most 20 slots.");
          return;
        }
        slots.push({ id: "cards-" + (slots.length + 1), count: 1, pool: [] });
        draw();
        markModified(f);
      };
      draw();
      bindForm(f, () => {
        readSlots();
        const id = validateIdentity(f, row),
          amount = Number(f.elements.amount.value),
          maxQuantity = Number(f.elements.maxQuantity.value);
        if (
          !Number.isSafeInteger(amount) ||
          amount < 1 ||
          amount * maxQuantity > 100000000
        )
          throw Error(
            "Price: the maximum purchase total must not exceed 100,000,000 NX.",
          );
        if (
          !slots.length ||
          slots.some(
            (s) =>
              !s.pool.length ||
              !Number.isSafeInteger(s.count) ||
              s.count < 1 ||
              s.pool.some((p) => !Number.isSafeInteger(p.weight)),
          )
        )
          throw Error(
            "Slots: choose at least one variant and positive whole-number weights for each draw slot.",
          );
        if (new Set(slots.map((s) => s.id)).size !== slots.length)
          throw Error("Slot IDs must be unique.");
        const prior = base.products.find((p) => p.id === id),
          value = {
            ...row,
            id,
            name: f.elements.name.value.trim(),
            lineId: f.elements.lineId.value,
            enabled: f.elements.enabled.checked,
            price: { currencyId: "nx", amount },
            maxQuantity,
            slots,
            revision: (prior?.revision ?? 0) + 1,
          };
        if (row) Object.assign(row, value);
        else {
          draft.products.push(value);
          row = value;
          choice.add(new Option(value.name, value.id));
          choice.value = value.id;
          f.elements.id.readOnly = true;
        }
      });
    }
    changeSelection(choice, $("pack-editor"), edit);
    edit();
  }
  function renderImport(content) {
    content.innerHTML = `<div class="admin-editor-grid"><div class="panel"><h3>Import a catalog</h3><p>Choose a card-and-pack catalog file in JSON format (maximum 2 MiB). Import replaces unpublished edits and updates matching live IDs. Published cards and packs missing from the file are kept. Turn off a pack to stop selling it. Import does not publish automatically.</p><label>Catalog JSON<input id="catalog-file" type="file" accept="application/json,.json"></label><button id="import-file">Load into draft</button></div><div class="panel"><h3>Export active catalog</h3><p>Download the live catalog for reuse or review. Backups are still required to preserve accounts, purchases and private keys.</p><button id="export-catalog">Download published catalog</button></div></div>`;
    $("import-file").onclick = async () => {
      const valid = guard(),
        file = $("catalog-file").files[0];
      if (!file) {
        status("Choose a catalog JSON file.", true);
        return;
      }
      if (file.size > 2 * 1024 * 1024) {
        status("Catalog exceeds the 2 MiB import limit.", true);
        return;
      }
      try {
        const revision = draftRevision;
        const value = JSON.parse(await file.text());
        if (!valid() || revision !== draftRevision) return;
        if (
          !value ||
          typeof value !== "object" ||
          !["cards", "variants", "products", "lines", "rarities"].every((k) =>
            Array.isArray(value[k]),
          )
        )
          throw Error(
            "Catalog must contain cards, variants, products, lines and rarities arrays.",
          );
        if (
          (dirty || modifiedForms.size) &&
          !window.confirm(
            "Replace your unpublished draft and unsaved form fields with this catalog import? Existing live IDs are retained.",
          )
        )
          return;
        const merged = { ...clone(base), ...value };
        for (const section of [
          "currencies",
          "lines",
          "rarities",
          "cards",
          "variants",
          "products",
          "recipes",
          "combinations",
          "cardTypes",
          "displayFields",
        ]) {
          if (!Array.isArray(base[section]) || value[section] === undefined)
            continue;
          if (!Array.isArray(value[section]))
            throw Error(section + ": provide an array.");
          const ids = new Set();
          for (const row of value[section]) {
            if (!row || typeof row.id !== "string" || ids.has(row.id))
              throw Error(section + ": use unique stable IDs.");
            ids.add(row.id);
          }
          const incoming = new Map(value[section].map((row) => [row.id, row]));
          merged[section] = base[section].map(
            (row) => incoming.get(row.id) ?? row,
          );
          const existing = new Set(base[section].map((row) => row.id));
          merged[section].push(
            ...value[section].filter((row) => !existing.has(row.id)),
          );
        }
        draft = merged;
        resetEditors();
        changed();
        renderTab();
        status(
          "Catalog loaded into draft. Preview validates it before publication.",
        );
      } catch (e) {
        if (valid()) status(e.message, true);
      }
    };
    $("export-catalog").onclick = async () => {
      const valid = guard();
      try {
        const data = await api("admin/export");
        if (!valid()) return;
        const url = URL.createObjectURL(
            new Blob([JSON.stringify(data.catalog ?? data, null, 2)], {
              type: "application/json",
            }),
          ),
          a = document.createElement("a");
        a.href = url;
        a.download = "card-catalog-v" + base.version + ".json";
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (e) {
        if (valid()) status(e.message, true);
      }
    };
  }
  async function makePreview() {
    if (!dirty || modifiedForms.size || loading || !allowed()) return;
    loading = true;
    updateDraft();
    const valid = guard();
    const revision = draftRevision;
    try {
      if (
        new TextEncoder().encode(JSON.stringify({ catalog: draft })).length >
        2 * 1024 * 1024
      )
        throw Error("Catalog exceeds the 2 MiB preview request limit.");
      const p = await api("admin/preview", { catalog: draft });
      if (!valid()) return;
      if (revision !== draftRevision) {
        status(
          "The draft changed during preview. Preview the current draft again.",
          true,
        );
        return;
      }
      preview = { ...p, idempotencyKey: crypto.randomUUID() };
      $("preview-summary").textContent =
        `Review version ${p.expectedVersion ?? base.version} → ${draft.version}. Publish applies only this reviewed draft.`;
      $("preview-details").innerHTML =
        `<h4>Review changes</h4><pre>${escape(JSON.stringify({ counts: p.counts, changes: p.changes, warnings: p.warnings, digest: p.digest }, null, 2))}</pre>`;
      status(
        "Preview validated. Review the changes, then choose Publish reviewed changes.",
      );
    } catch (e) {
      if (valid()) {
        preview = undefined;
        status(e.message, true);
      }
    } finally {
      if (valid()) {
        loading = false;
        updateDraft();
      }
    }
  }
  async function publish() {
    if (!preview || loading || !allowed()) return;
    const valid = guard();
    loading = true;
    updateDraft();
    try {
      await api("admin/publish", {
        previewId: preview.previewId,
        digest: preview.digest,
        expectedVersion: preview.expectedVersion,
        policyRevision: preview.policyRevision,
        adminRevision: preview.adminRevision,
        idempotencyKey: preview.idempotencyKey,
      });
      if (!valid()) return;
      const data = await api("admin/catalog");
      if (!valid()) return;
      base = clone(data.catalog ?? data);
      draft = clone(base);
      dirty = false;
      resetEditors();
      invalidate();
      renderTab();
      status("Published. Players now see the updated catalog.");
      await onPublish();
    } catch (e) {
      if (valid())
        status(
          e.message +
            " Your draft is retained. Refresh the active catalog or preview again to resolve a version conflict.",
          true,
        );
    } finally {
      if (valid()) {
        loading = false;
        updateDraft();
      }
    }
  }
  function renderActivity(content) {
    content.innerHTML = `<div class="panel"><h3>Orders</h3><form id="order-search"><label>Account name, account ID or order ID<input name="search" type="search" maxlength="100" placeholder="Exact account name, account ID or order ID"></label><button>Search orders</button></form><div id="admin-orders"></div><button id="orders-more" class="quiet" hidden>More orders</button></div><div class="panel"><h3>Recent activity</h3><div id="admin-activity"></div><button id="activity-more" class="quiet" hidden>More activity</button></div>`;
    orderAfter = activityAfter = undefined;
    async function orders(append = false) {
      const valid = guard();
      try {
        const q = new URLSearchParams({
          limit: "25",
          search: $("order-search").elements.search.value,
        });
        if (append && orderAfter) q.set("after", orderAfter);
        const page = await api("admin/orders?" + q);
        if (!valid() || tab !== "activity") return;
        const items = page.items ?? page.orders ?? [];
        const html =
          items
            .map(
              (o) =>
                `<article class="order-row"><strong>${escape(o.id ?? o.orderId)}</strong><p>Account ${escape(o.accountName ?? o.owner ?? o.accountId ?? o.userId)} · ${escape(o.cashType)} · ${escape(o.amount ?? o.quote?.price?.amount)} points</p><p>${escape(({complete:"Delivered",rejected:"Declined without payment",refunded:"Refunded to original balance",refund_required:"Refund pending",quarantined:"Operator reconciliation required",prepared:"Payment pending",fulfilled:"Finalizing delivered cards"})[o.state] ?? o.state)} · ${escape(o.attempts ?? o.attemptCount ?? 0)} attempts · ${escape(o.lastError?.code ?? o.lastError ?? "No error")}</p><small>Created ${escape(o.createdAt)} · Last attempt ${escape(o.lastAttemptAt ?? "None")} · Next retry ${escape(o.nextAttemptAt ?? "None")}</small>${!["complete", "rejected", "refunded", "compensated", "quarantined"].includes(o.state) ? `<button class="quiet" data-order="${escape(o.id ?? o.orderId)}">Retry this order</button>` : ""}</article>`,
            )
            .join("") || "<p>No matching orders.</p>";
        if (append) $("admin-orders").insertAdjacentHTML("beforeend", html);
        else $("admin-orders").innerHTML = html;
        orderAfter = page.next;
        $("orders-more").hidden = !orderAfter;
        $("admin-orders")
          .querySelectorAll("[data-order]")
          .forEach(
            (b) =>
              (b.onclick = async () => {
                const active = guard();
                b.disabled = true;
                try {
                  await api(
                    "admin/orders/" +
                      encodeURIComponent(b.dataset.order) +
                      "/retry",
                    {},
                  );
                  if (active()) {
                    status(
                      "Original order retried. No replacement purchase was created.",
                    );
                    await orders();
                  }
                } catch (e) {
                  if (active()) {
                    status(e.message, true);
                    b.disabled = false;
                  }
                }
              }),
          );
      } catch (e) {
        if (valid()) status(e.message, true);
      }
    }
    async function activity(append = false) {
      const valid = guard();
      try {
        const q = new URLSearchParams({ limit: "25" });
        if (append && activityAfter) q.set("after", activityAfter);
        const page = await api("admin/activity?" + q);
        if (!valid() || tab !== "activity") return;
        const html =
          (page.items ?? page.events ?? [])
            .map(
              (e) =>
                `<article class="order-row"><strong>${escape(e.type ?? e.action)}</strong><p>${escape(e.actor?.userId ?? e.actor ?? e.accountId ?? "Service")} · ${escape(e.createdAt ?? e.at)}</p></article>`,
            )
            .join("") || "<p>No activity yet.</p>";
        if (append) $("admin-activity").insertAdjacentHTML("beforeend", html);
        else $("admin-activity").innerHTML = html;
        activityAfter = page.next;
        $("activity-more").hidden = !activityAfter;
      } catch (e) {
        if (valid()) status(e.message, true);
      }
    }
    $("order-search").onsubmit = (e) => {
      e.preventDefault();
      orders();
    };
    $("orders-more").onclick = () => orders(true);
    $("activity-more").onclick = () => activity(true);
    orders();
    activity();
  }
  return {
    sync,
    clear,
    hasDraft: () =>
      dirty ||
      modifiedForms.size > 0 ||
      !!root.querySelector("form:focus-within"),
    isOpen: () => !root.hidden,
  };
}
