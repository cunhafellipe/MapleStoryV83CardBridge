import { chromium } from "playwright";
import { createServer } from "node:http";
import { readFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { fixture } from "./fixture.mjs";
import { createLibraryHttp } from "../src/http.mjs";
import { fail } from "../src/protocol.mjs";
const catalog = JSON.parse(
  readFileSync(
    new URL("../data/catalog.example.json", import.meta.url),
    "utf8",
  ),
);
const files = new Map(
  ["index.html", "app.mjs", "admin.mjs", "style.css"].map((name) => [
    name,
    readFileSync(new URL("../starter/" + name, import.meta.url)),
  ]),
);
const server = createServer((req, res) => {
  const name =
    req.url === "/library/" ? "index.html" : req.url.split("/").at(-1);
  if (!files.has(name)) {
    res.writeHead(404).end();
    return;
  }
  res.setHeader(
    "Content-Type",
    name.endsWith("mjs")
      ? "text/javascript"
      : name.endsWith("css")
        ? "text/css"
        : "text/html",
  );
  res.end(files.get(name));
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const origin = "http://127.0.0.1:" + server.address().port;
const browser = await chromium.launch({
  headless: true,
  ...(process.env.BROWSER_CHANNEL
    ? { channel: process.env.BROWSER_CHANNEL }
    : {}),
});
const page = await browser.newPage({ reducedMotion: "reduce" }),
  errors = [];
page.on("pageerror", (e) => errors.push(e.message));
let active = structuredClone(catalog),
  role = "administrator",
  signedIn = true,
  outage = true,
  stale = false,
  heldOverview,
  hold = false,
  publishCount = 0,
  previewBody,
  boundedProfile = false,
  holdPacks = false,
  heldPacks;
const emptyState = () => ({
  owner: "Operator",
  packs: [],
  inventory: [],
  codes: [],
  orders: [],
  wallet: {
    acceptedCashTypes: [1, 2, 4],
    balances: [
      { cashType: 1, name: "NX Credit", amount: 5000 },
      { cashType: 2, name: "Maple Points", amount: 0 },
      { cashType: 4, name: "NX Prepaid", amount: 6000 },
    ],
  },
});
function boundedState() {
  const snapshot = (index) => ({
    id: "owned-" + catalog.cards[index].id,
    variantId: catalog.variants[index].id,
    rarityId: catalog.variants[index].rarityId,
    definition: catalog.cards[index],
  });
  return {
    ...emptyState(),
    inventory: Array.from({ length: 50 }, (_, i) => ({
      ...snapshot(0),
      id: "copy-" + i,
    })),
    inventoryTotal: 100,
    inventoryNext: "copies-2",
    collection: {
      items: [
        { variantId: catalog.variants[0].id, count: 75, card: snapshot(0) },
        { variantId: catalog.variants[1].id, count: 25, card: snapshot(1) },
      ],
      totalCards: 100,
      uniqueCards: 2,
    },
    packs: Array.from({ length: 50 }, (_, i) => ({
      id: "pack-" + i,
      productId: "shapes",
    })),
    packTotal: 55,
    packNext: "packs-2",
  };
}
await page.route("**/api/library/**", async (route) => {
  const request = route.request(),
    url = new URL(request.url()),
    path = url.pathname.replace("/api/library/", "");
  const reply = (data, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  if (path === "catalog")
    return reply({ ...active, acceptedCashTypes: [1, 2, 4] });
  if (path === "session")
    return outage
      ? reply({ error: "Game unavailable", code: "GAME_UNAVAILABLE" }, 503)
      : reply({
          signedIn,
          username: signedIn ? "Operator" : undefined,
          role,
          csrf: "fixture-csrf",
        });
  if (path === "state")
    return reply(boundedProfile ? boundedState() : emptyState());
  if (path === "packs") {
    if (holdPacks) await new Promise((r) => (heldPacks = r));
    return reply({
      items: Array.from({ length: 5 }, (_, i) => ({
        id: "pack-" + (i + 50),
        productId: "shapes",
      })),
      next: null,
      total: 55,
    });
  }
  if (path === "logout") {
    signedIn = false;
    return reply({ ok: true });
  }
  if (path === "login") {
    signedIn = true;
    return reply({
      signedIn,
      username: "Operator",
      role,
      csrf: "fixture-csrf",
    });
  }
  if (path.startsWith("admin/") && role !== "administrator")
    return reply(
      { error: "Administration access revoked", code: "FORBIDDEN" },
      403,
    );
  if (path === "admin/catalog" || path === "admin/export")
    return reply({ catalog: active, adminRevision: 1 });
  if (path === "admin/overview") {
    if (hold) await new Promise((r) => (heldOverview = r));
    return reply({
      ready: true,
      game: { status: "Connected" },
      rewards: { enabled: false },
      orders: { unresolved: 0 },
      lastRecovery: {
        startedAt: "2026-10-03T17:00:00Z",
        finishedAt: "2026-10-03T17:01:00Z",
        completed: 2,
        failed: 1,
      },
    });
  }
  if (path === "admin/preview") {
    previewBody = request.postDataJSON();
    return reply({
      previewId: "preview-1",
      digest: "fixture-digest",
      expectedVersion: active.version,
      adminRevision: 1,
      policyRevision: 0,
      counts: { changed: 1 },
      warnings: [],
    });
  }
  if (path === "admin/publish") {
    if (stale)
      return reply(
        { error: "The catalog changed. Preview again.", code: "STALE_CATALOG" },
        409,
      );
    publishCount++;
    active = structuredClone(previewBody.catalog);
    return reply({ version: active.version });
  }
  if (path === "admin/orders" || path === "admin/activity")
    return reply({ items: [], next: null });
  return reply({ ok: true });
});
try {
  await page.goto(origin + "/library/");
  await page.locator("#connection-notice").waitFor({ state: "visible" });
  assert.equal(await page.locator("#products .product").count(), 1);
  assert.ok(await page.locator("#collection [data-card]").count());
  assert.equal(await page.locator("#purchase").isDisabled(), true);
  outage = false;
  await page.locator("#connection-retry").click();
  await page.locator("#admin-link").waitFor({ state: "visible" });
  await page.locator("#admin-link").click();
  await page.locator("[data-tab=packs]").waitFor();
  assert.match(
    await page.locator("#admin-content").textContent(),
    /2 completed · 1 failed · 2026-10-03T17:01:00Z/,
  );
  await page.locator("[data-tab=packs]").click();
  await page.locator("[name=packChoice]").selectOption("shapes");
  assert.equal(await page.locator("[name=amount]").inputValue(), "1000");
  await page.locator("[data-tab=import]").click();
  const imported = structuredClone(catalog);
  imported.products[0].price.amount = 1600;
  await page.locator("#catalog-file").setInputFiles({
    name: "catalog.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(imported)),
  });
  await page.locator("#import-file").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#admin-status")
      .textContent.startsWith("Catalog loaded"),
  );
  await page.locator("[data-tab=packs]").click();
  await page.locator("[name=packChoice]").selectOption("shapes");
  assert.equal(await page.locator("[name=amount]").inputValue(), "1600");
  await page.locator("[name=amount]").fill("1700");
  await page.getByRole("button", { name: "Save pack to draft" }).click();
  await page.locator("#admin-preview").click();
  await page.waitForFunction(
    () => !document.querySelector("#admin-publish").disabled,
  );
  assert.equal(previewBody.catalog.products[0].price.amount, 1700);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.locator("#admin-discard").click();
  assert.equal(await page.locator("[name=amount]").inputValue(), "1700");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#admin-discard").click();
  await page.locator("[name=packChoice]").selectOption("shapes");
  assert.equal(await page.locator("[name=amount]").inputValue(), "1000");
  await page.locator("[name=amount]").fill("1200");
  await page.getByRole("button", { name: "Save pack to draft" }).click();
  assert.equal(
    await page.locator("#draft-status").textContent(),
    "Unpublished draft",
    await page.locator("#pack-editor .form-status").textContent(),
  );
  await page.locator("[name=amount]").fill("1300");
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  assert.equal(await page.locator("[name=amount]").inputValue(), "1300");
  await page.locator("[name=amount]").fill("1200");
  await page.getByRole("button", { name: "Save pack to draft" }).click();
  await page.locator("#admin-preview").click();
  await page.locator("#admin-publish").waitFor({ state: "visible" });
  await page.waitForFunction(
    () => !document.querySelector("#admin-publish").disabled,
  );
  stale = true;
  await page.locator("#admin-publish").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#admin-status")
      .textContent.includes("draft is retained"),
  );
  assert.equal(await page.locator("[name=amount]").inputValue(), "1200");
  stale = false;
  await page.locator("#admin-preview").click();
  await page.waitForFunction(
    () => !document.querySelector("#admin-publish").disabled,
  );
  await page.locator("#admin-publish").click();
  await page.waitForFunction(() =>
    document.querySelector("#admin-status").textContent.startsWith("Published"),
  );
  assert.equal(publishCount, 1);
  assert.equal(active.products[0].price.amount, 1200);
  assert.equal(active.products[0].revision, 2);
  await page.locator("[data-tab=cards]").click();
  const cardForm = page.locator("#card-editor form");
  await cardForm.locator("[name=id]").fill("new-card");
  await cardForm.locator("[name=name]").fill("<script>new</script>");
  await cardForm.locator("[name=image]").fill("/assets/library/../private.png");
  await cardForm.getByRole("button").click();
  assert.match(
    await cardForm.locator(".form-status").textContent(),
    /supported static/,
  );
  assert.equal(
    await cardForm.locator("[name=name]").inputValue(),
    "<script>new</script>",
  );
  await cardForm.locator("[name=image]").fill("");
  await cardForm.getByRole("button").click();
  await page.locator("[data-tab=packs]").click();
  await page.locator("[name=packChoice]").selectOption("shapes");
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
      "No horizontal page overflow at " + width,
    );
    if (process.env.QA_DIRECTORY) {
      mkdirSync(process.env.QA_DIRECTORY, { recursive: true });
      await page.locator("#administration").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: join(process.env.QA_DIRECTORY, `admin-packs-${width}.png`),
      });
    }
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.evaluate(() => (document.body.style.zoom = "2"));
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.evaluate(() => (document.body.style.zoom = "1"));
  await page.locator("#products [data-contents]").click();
  await page.locator("#detail[open]").waitFor();
  await page.keyboard.press("Tab");
  assert.equal(
    await page.evaluate(() => !!document.activeElement.closest("dialog")),
    true,
  );
  await page.keyboard.press("Escape");
  await page.locator("#detail").waitFor({ state: "hidden" });
  assert.equal(
    await page
      .locator("#products [data-contents]")
      .evaluate((e) => e === document.activeElement),
    true,
  );
  active.variants.find((v) => v.id === "circle.standard").enabled = false;
  await page.locator("#refresh").click();
  await page.locator("#products [data-contents]").click();
  await page.locator("#detail[open]").waitFor();
  const poolDetails = await page.locator("#detail-content").textContent();
  assert.doesNotMatch(poolDetails, /Circle ·/);
  assert.match(poolDetails, /Square · Common · 26\.05% per draw/);
  await page.keyboard.press("Escape");
  boundedProfile = true;
  await page.locator("#refresh").click();
  await page.waitForFunction(
    () => document.querySelector("#card-count").textContent === "100",
  );
  assert.equal(await page.locator("#card-count").textContent(), "100");
  assert.equal(await page.locator("#pack-count").textContent(), "55");
  assert.equal(
    await page.locator("#overview strong").nth(1).textContent(),
    "100",
  );
  assert.equal(
    await page.locator("#overview strong").nth(2).textContent(),
    "2",
  );
  assert.equal(await page.locator("#collection [data-card]").count(), 2);
  assert.equal(await page.locator("#packs [data-pack]").count(), 50);
  await page.locator('#collection [data-card="owned-square"]').click();
  await page.locator("#detail[open]").waitFor();
  assert.match(
    await page.locator("#detail-content").textContent(),
    /Copies owned25/,
  );
  await page.keyboard.press("Escape");
  await page.locator("#more-packs").click();
  await page.waitForFunction(
    () => document.querySelectorAll("#packs [data-pack]").length === 55,
  );
  assert.equal(await page.locator("#packs [data-pack]").count(), 55);
  assert.equal(await page.locator("#more-packs").isVisible(), false);
  assert.equal(
    await page
      .locator('[data-pack="pack-50"]')
      .evaluate((e) => e === document.activeElement),
    true,
  );
  await page.locator("#refresh").click();
  await page.waitForFunction(
    () => document.querySelectorAll("#packs [data-pack]").length === 50,
  );
  holdPacks = true;
  await page.locator("#more-packs").click();
  await page.locator("#logout").click();
  heldPacks();
  holdPacks = false;
  boundedProfile = false;
  await page.locator("#administration").waitFor({ state: "hidden" });
  assert.equal(await page.locator("#administration").textContent(), "");
  assert.equal(await page.locator("#packs [data-pack]").count(), 0);
  assert.equal(await page.locator("#more-packs").isVisible(), false);
  await page.locator("[name=username]").fill("Operator");
  await page.locator("[name=password]").fill("fixture");
  await page.locator("#login button").click();
  await page.locator("#admin-link").waitFor({ state: "visible" });
  hold = true;
  await page.locator("#admin-link").click();
  await page.waitForFunction(
    () => document.querySelector("#administration").hidden === false,
  );
  await page.locator("#logout").click();
  heldOverview();
  hold = false;
  await page.locator("#administration").waitFor({ state: "hidden" });
  assert.equal(await page.locator("#administration").textContent(), "");
  role = "collector";
  await page.locator("[name=username]").fill("Operator");
  await page.locator("[name=password]").fill("fixture");
  await page.locator("#login button").click();
  await page.locator("#account-strip").waitFor({ state: "visible" });
  assert.equal(await page.locator("#admin-link").isVisible(), false);
  assert.deepEqual(errors, []);
  await realAdministration();
  console.log(
    "Admin browser fixtures: drafts, safe images, preview conflicts, publication, outage browsing, keyboard/mobile/zoom and logout fencing passed.",
  );
} finally {
  await page.close();
  await browser.close();
  await new Promise((r) => server.close(r));
}
async function realAdministration() {
  const x = fixture({ catalog }),
    sessions = new Map();
  x.journal.grant(x.person, true);
  const game = async (path, value) => {
    if (path === "/login") {
      if (value.username !== "Collector" || value.password !== "fixture")
        fail("UNAUTHENTICATED", "Invalid account", 401);
      sessions.set(value.tokenHash, x.person);
      return x.person;
    }
    if (path === "/session") {
      if (!sessions.has(value.tokenHash))
        fail("UNAUTHENTICATED", "Session expired", 401);
      return sessions.get(value.tokenHash);
    }
    if (path === "/logout") {
      sessions.delete(value.tokenHash);
      return { ok: true };
    }
    return x.game(path, value);
  };
  const probe = createServer();
  await new Promise((r) => probe.listen(0, "127.0.0.1", r));
  const port = probe.address().port;
  await new Promise((r) => probe.close(r));
  const origin = "http://127.0.0.1:" + port,
    app = createLibraryHttp({
      library: x.library,
      game,
      origin,
      secret: "browser-admin-shared-".repeat(3),
      csrfSecret: "browser-admin-csrf-".repeat(3),
      authMode: "bridge",
      webRoot: fileURLToPath(new URL("../starter", import.meta.url)),
    });
  await new Promise((r) => app.listen(port, "127.0.0.1", r));
  const page = await browser.newPage(),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  try {
    await page.goto(origin + "/library/");
    await page.locator("[name=username]").fill("Collector");
    await page.locator("[name=password]").fill("fixture");
    await page.locator("#login button").click();
    await page.locator("#admin-link").waitFor({ state: "visible" });
    await page.locator("#admin-link").click();
    await page.locator("[data-tab=packs]").click();
    await page.locator("[name=packChoice]").selectOption("shapes");
    await page.locator("[name=amount]").fill("1400");
    await page.getByRole("button", { name: "Save pack to draft" }).click();
    await page.locator("#admin-preview").click();
    await page.waitForFunction(
      () => !document.querySelector("#admin-publish").disabled,
    );
    await page.locator("#admin-publish").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#admin-status")
        .textContent.startsWith("Published"),
    );
    assert.equal(x.core.catalog().products[0].price.amount, 1400);
    assert.equal(x.core.catalog().version, catalog.version + 1);
    await page.locator("[data-tab=cards]").click();
    await page.locator("[name=cardChoice]").selectOption("circle");
    let variantForm = page.locator("#card-editor form");
    assert.equal(
      await variantForm.locator("[name=rarityId]").isDisabled(),
      true,
    );
    assert.equal(await variantForm.locator("[name=lineId]").isDisabled(), true);
    await variantForm.locator("[name=variantId]").selectOption("");
    await variantForm.locator("[name=newVariantId]").fill("circle.rare");
    await variantForm.locator("[name=rarityId]").selectOption("rare");
    await variantForm.getByRole("button").click();
    await page.locator("[name=cardChoice]").selectOption("");
    variantForm = page.locator("#card-editor form");
    await variantForm.locator("[name=id]").fill("c".repeat(100));
    await variantForm.locator("[name=name]").fill("Long identity card");
    await variantForm.getByRole("button").click();
    assert.match(
      await variantForm.locator(".form-status").textContent(),
      /explicit variant ID/,
    );
    assert.equal(
      await variantForm.locator("[name=id]").inputValue(),
      "c".repeat(100),
    );
    await variantForm.locator("[name=newVariantId]").fill("long-card.standard");
    await variantForm.getByRole("button").click();
    assert.equal(
      await variantForm.locator(".form-status").textContent(),
      "Saved to draft.",
    );
    await page.locator("[name=cardChoice]").selectOption("");
    await page.locator("[data-tab=cards]").click();
    await page.locator("[name=setChoice]").selectOption("shapes");
    await page.locator("#set-editor [name=description]").fill("");
    await page.getByRole("button", { name: "Save set to draft" }).click();
    await page.locator("[name=setChoice]").selectOption("");
    const setForm = page.locator("#set-editor form"),
      cardForm = page.locator("#card-editor form");
    await setForm.locator("[name=id]").fill("new-set");
    await setForm.locator("[name=name]").fill("New set");
    await setForm.getByRole("button").click();
    await cardForm.locator("[name=id]").fill("new-card");
    await cardForm.locator("[name=name]").fill("New card <text>");
    await cardForm.locator("[name=lineId]").selectOption("new-set");
    await cardForm.getByRole("button").click();
    await page.locator("[data-tab=packs]").click();
    await page.locator("[name=packChoice]").selectOption("");
    const packForm = page.locator("#pack-editor form");
    await packForm.locator("[name=id]").fill("new-pack");
    await packForm.locator("[name=name]").fill("New pack");
    await packForm.locator("[name=lineId]").selectOption("new-set");
    await packForm.locator('[data-weight="new-card.standard"]').fill("1");
    await packForm.locator("[name=count]").fill("2");
    await packForm.getByRole("button", { name: "Save pack to draft" }).click();
    await packForm.locator("[name=amount]").fill("400");
    await packForm.getByRole("button", { name: "Save pack to draft" }).click();
    const previewResponse = page.waitForResponse((r) =>
      r.url().endsWith("/admin/preview"),
    );
    await page.locator("#admin-preview").click();
    const checkedPreview = await previewResponse;
    assert.equal(checkedPreview.status(), 200, await checkedPreview.text());
    await page.waitForFunction(
      () => !document.querySelector("#admin-publish").disabled,
    );
    await page.locator("#admin-publish").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#admin-status")
        .textContent.startsWith("Published"),
    );
    assert.equal(
      x.core.catalog().cards.find((c) => c.id === "new-card").name,
      "New card <text>",
    );
    assert.equal(
      x.core.catalog().products.find((p) => p.id === "new-pack").price.amount,
      400,
    );
    assert.equal(
      x.core.catalog().products.filter((p) => p.id === "new-pack").length,
      1,
    );
    assert.equal(
      x.core.catalog().variants.find((v) => v.id === "circle.standard")
        .rarityId,
      "common",
    );
    assert.equal(
      x.core.catalog().variants.find((v) => v.id === "circle.rare").rarityId,
      "rare",
    );
    assert.equal(
      x.core.catalog().variants.find((v) => v.id === "long-card.standard")
        .cardId,
      "c".repeat(100),
    );
    assert.equal(
      x.core.catalog().lines.find((l) => l.id === "shapes").description,
      undefined,
    );
    x.balances.set(1, 150000);
    const purchased = [];
    for (let i = 0; i < 14; i++) {
      const quantity = i === 13 ? 3 : 5;
      const result = await x.library.buy(x.person, {
        key: "bounded-browser-" + i,
        ...x.library.quote(x.person, {
          productId: "shapes",
          quantity,
          cashType: 1,
        }),
      });
      purchased.push(...result.packs);
      if (i === 10)
        for (let opened = 0; opened < 13; opened++)
          await x.library.open(x.person, {
            key: "bounded-open-" + opened,
            packId: purchased[opened].id,
          });
    }
    await page.locator("#refresh").click();
    await page.waitForFunction(
      () => document.querySelector("#card-count").textContent === "104",
    );
    assert.equal(await page.locator("#pack-count").textContent(), "55");
    assert.equal(await page.locator("#packs [data-pack]").count(), 50);
    assert.match(
      await page.locator("#collection-summary").textContent(),
      /104 total/,
    );
    await page.locator("#more-packs").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#packs [data-pack]").length === 55,
    );
    assert.equal(await page.locator("#more-packs").isVisible(), false);
    await page.locator("[data-tab=rewards]").click();
    await page.locator("#admin-registrations").waitFor();
    assert.match(
      await page.locator("#admin-content").textContent(),
      /No reward pool configured/,
    );
    x.journal.grant(x.person, false);
    await page.locator("#admin-preview").evaluate((e) => e.blur());
    await page.locator("#refresh").click();
    await page.locator("#admin-link").waitFor({ state: "hidden" });
    assert.equal(await page.locator("#administration").textContent(), "");
    assert.deepEqual(errors, []);
    console.log(
      "Real bridge HTTP admin browser: durable grant, attributed preview/publication, default reward status and open-session revocation passed.",
    );
  } finally {
    await page.close();
    await new Promise((r) => app.close(r));
    x.close();
  }
}
