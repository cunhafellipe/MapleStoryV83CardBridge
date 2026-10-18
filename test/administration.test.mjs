import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fixture } from "./fixture.mjs";
import { Journal } from "../src/journal.mjs";
import { createLibraryHttp } from "../src/http.mjs";
import { hash, fail } from "../src/protocol.mjs";
const shapes = JSON.parse(
  readFileSync(new URL("../data/catalog.example.json", import.meta.url)),
);
const draft = (x) => structuredClone(x.core.operatorCatalog({ role: "admin" }));
const publishInput = (preview) => ({
  ...preview,
  idempotencyKey: "publish-test-key",
});

test("grants persist by numeric identity, are idempotent and attributed, and do not infer GM status", () => {
  const directory = mkdtempSync(join(tmpdir(), "bridge-role-")),
    path = join(directory, "bridge.sqlite");
  let journal = new Journal(path);
  try {
    assert.equal(journal.administrator(1), false);
    assert.equal(
      journal.grant({ accountId: 1, name: "Collector" }, true).changed,
      true,
    );
    assert.equal(
      journal.grant({ accountId: 1, name: "Renamed" }, true).changed,
      false,
    );
    journal.close();
    journal = new Journal(path);
    assert.equal(journal.administrator(1), true);
    assert.equal(journal.grants()[0].name, "Renamed");
    assert.equal(journal.activityPage().items.length, 1);
    assert.equal(
      journal.grant({ accountId: 1, name: "Renamed" }, false).changed,
      true,
    );
    assert.equal(
      journal.grant({ accountId: 1, name: "Renamed" }, false).changed,
      false,
    );
  } finally {
    journal.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
test("preview binds operator, digest, version and policy; publication preserves snapshots and replays", async () => {
  const x = fixture({ catalog: shapes });
  try {
    assert.throws(
      () =>
        x.library.admin.catalog({ ...x.person, gm: 5, role: "administrator" }),
      (e) => e.status === 403,
    );
    x.journal.grant(x.person, true);
    const candidate = draft(x);
    candidate.products[0].price.amount = 2000;
    const preview = x.library.admin.preview(x.person, { catalog: candidate });
    assert.equal(x.core.catalog().products[0].price.amount, 1000);
    assert.equal(
      x.library.admin.catalog(x.person).catalog.version,
      shapes.version,
    );
    await assert.rejects(
      x.library.admin.publish(x.person, {
        ...publishInput(preview),
        digest: "0".repeat(64),
      }),
      (e) => e.code === "PREVIEW_CHANGED",
    );
    await x.library.admin.publish(x.person, publishInput(preview));
    assert.equal(x.core.catalog().products[0].price.amount, 2000);
    assert.equal(x.core.catalog().products[0].revision, 2);
    await x.library.admin.publish(x.person, publishInput(preview));
    assert.equal(
      x.journal
        .activityPage()
        .items.filter((r) => r.action === "catalog.published").length,
      1,
    );
    x.journal.grant(x.other, true);
    await assert.rejects(
      x.library.admin.publish(x.other, publishInput(preview)),
      (e) => e.code === "PREVIEW_EXPIRED",
    );
  } finally {
    x.close();
  }
});
test("revocation and native session expiry during commit revalidation fail closed", async () => {
  const x = fixture({ catalog: shapes });
  try {
    x.journal.grant(x.person, true);
    const preview = x.library.admin.preview(x.person, { catalog: draft(x) });
    await assert.rejects(
      x.library.admin.publish(x.person, publishInput(preview), async () => {
        x.journal.grant(x.person, false);
        return x.person;
      }),
      (e) => e.status === 403,
    );
    assert.equal(x.core.catalog().version, shapes.version);
    x.journal.grant(x.person, true);
    await assert.rejects(
      x.library.admin.publish(
        x.person,
        publishInput(preview),
        async () => null,
      ),
      (e) => e.status === 401,
    );
  } finally {
    x.close();
  }
});
test("catalog publication cannot slip into the awaited payment interval", async () => {
  const x = fixture({ catalog: shapes });
  try {
    x.journal.grant(x.person, true);
    const candidate = draft(x);
    candidate.products[0].price.amount = 2000;
    const preview = x.library.admin.preview(x.person, { catalog: candidate });
    const original = x.library.game;
    let release, started;
    const entered = new Promise((r) => (started = r)),
      gate = new Promise((r) => (release = r));
    x.library.game = async (path, value) => {
      if (path === "/payment/debit") {
        started();
        await gate;
      }
      return original(path, value);
    };
    const quote = x.library.quote(x.person, { productId: "shapes" });
    const bought = x.library.buy(x.person, {
      key: "publish-race-buy",
      ...quote,
    });
    await entered;
    const published = x.library.admin.publish(x.person, publishInput(preview));
    release();
    const purchase = await bought;
    await published;
    assert.equal(purchase.paid.amount, 1000);
    assert.equal(purchase.packs.length, 1);
    assert.equal(x.debitCount, 1);
    assert.equal(x.core.catalog().products[0].price.amount, 2000);
    await assert.rejects(
      x.library.buy(x.person, { key: "stale-pack-buy", ...quote }),
      (e) => e.code === "STALE_QUOTE",
    );
    assert.equal(x.debitCount, 1);
  } finally {
    x.close();
  }
});
test("queued operator retry rechecks revocation and expiry before any economic effect", async () => {
  for (const expired of [false, true]) {
    const x = fixture({ catalog: shapes });
    try {
      x.journal.grant(x.person, true);
      const otherQuote = x.library.quote(x.other, { productId: "shapes" });
      const input = {
        productId: "shapes",
        quantity: 1,
        catalogVersion: otherQuote.catalogVersion,
        productRevision: otherQuote.productRevision,
        cashType: 1,
      };
      const target = x.journal.create(
        x.other.accountId,
        "queued-target-key",
        input,
        x.library.actor(x.other).userId,
        otherQuote,
      );
      const original = x.library.game;
      let entered, release;
      const started = new Promise((r) => (entered = r)),
        gate = new Promise((r) => (release = r));
      x.library.game = async (path, value) => {
        if (path === "/payment/debit" && value.accountId === x.person.accountId) {
          entered();
          await gate;
        }
        return original(path, value);
      };
      const ownQuote = x.library.quote(x.person, { productId: "shapes" });
      const blocking = x.library.buy(x.person, {
        key: "blocking-admin-key",
        ...ownQuote,
      });
      await started;
      const retry = x.library.admin.retry(x.person, target.id, async () =>
        expired ? null : x.person,
      );
      if (!expired) x.journal.grant(x.person, false);
      release();
      await blocking;
      await assert.rejects(retry, (e) => e.status === (expired ? 401 : 403));
      assert.equal(x.journal.order(target.id).state, "pending");
      assert.equal(x.balances.get(x.other.accountId), 10000);
      assert.equal(x.debitCount, 1);
    } finally {
      x.close();
    }
  }
});
test("admin HTTP enforces native session, current grants, Origin, CSRF and bounded queries", async (t) => {
  const x = fixture({ catalog: shapes }),
    token = "A".repeat(43),
    secret = "shared-key-".repeat(5);
  let valid = true;
  const game = async (path, value) => {
    if (path === "/session") {
      if (!valid || value.tokenHash !== hash(token))
        fail("UNAUTHENTICATED", "Expired", 401);
      return x.person;
    }
    return x.game(path, value);
  };
  const server = createLibraryHttp({
    library: x.library,
    game,
    origin: "http://127.0.0.1:8487",
    secret,
    csrfSecret: secret,
    authMode: "bridge",
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    await new Promise((r) => server.close(r));
    x.close();
  });
  const base = `http://127.0.0.1:${server.address().port}/api/library/`,
    cookie = { Cookie: "card_library=" + token };
  for (const route of [
    "admin/catalog",
    "admin/export",
    "admin/overview",
    "admin/orders",
    "admin/activity",
  ]) {
    assert.equal((await fetch(base + route)).status, 401);
    assert.equal((await fetch(base + route, { headers: cookie })).status, 403);
  }
  x.journal.grant(x.person, true);
  const session = await (
    await fetch(base + "session", { headers: cookie })
  ).json();
  assert.equal(session.role, "administrator");
  const headers = {
    ...cookie,
    Origin: "http://127.0.0.1:8487",
    "X-CSRF-Token": session.csrf,
    "Content-Type": "application/json",
  };
  assert.equal(
    (await fetch(base + "admin/orders?limit=101", { headers: cookie })).status,
    400,
  );
  const post = (route, value, h = headers) =>
    fetch(base + route, {
      method: "POST",
      headers: h,
      body: JSON.stringify(value),
    });
  assert.equal(
    (
      await post(
        "admin/preview",
        { catalog: draft(x) },
        { ...headers, Origin: "http://elsewhere" },
      )
    ).status,
    403,
  );
  const preview = await (
    await post("admin/preview", { catalog: draft(x) })
  ).json();
  assert.ok(preview.previewId);
  x.journal.grant(x.person, false);
  assert.equal(
    (await post("admin/publish", publishInput(preview))).status,
    403,
  );
  valid = false;
  assert.equal(
    (await fetch(base + "admin/catalog", { headers: cookie })).status,
    401,
  );
  assert.equal((await fetch(base + "catalog")).status, 200);
});
