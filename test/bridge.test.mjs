import test from "node:test";
import assert from "node:assert/strict";
import { fixture } from "./fixture.mjs";
import {
  seriesOne,
  generateSeriesOneCode,
  normalizeCode,
} from "../src/series-one.mjs";
import { Library, operator } from "../src/library.mjs";
import { hash, mac, verifyRequest } from "../src/protocol.mjs";
import { createLibraryHttp } from "../src/http.mjs";
import { Journal } from "../src/journal.mjs";
import { SQLiteStore } from "@digital-card/framework/sqlite";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("all 17 Series One outcomes use the correct layout and quantities", () => {
  assert.equal(seriesOne.rewards.length, 17);
  for (let i = 0; i < 17; i++) {
    let draws = 0;
    const code = generateSeriesOneCode(
      { copy: { source: { purchaseId: "example" } } },
      (sum) => (draws++ === 0 ? i : 1),
    );
    const reward = seriesOne.rewards[i];
    assert.equal(code.metadata.itemId, reward.itemId);
    assert.equal(code.metadata.quantity, reward.quantity);
    assert.equal(code.code.length, reward.petDays ? 18 : 15);
    assert.equal(
      normalizeCode(
        code.code
          .toLowerCase()
          .match(/.{1,5}/g)
          .join("-"),
      ),
      code.code,
    );
  }
  assert.throws(() => normalizeCode("not-a-code"));
  assert.equal(seriesOne.rewards.filter((r) => r.petDays === 30).length, 3);
});
test("a purchased pack contains eight collectibles and exactly one newly generated code; retries preserve everything", async () => {
  const x = fixture();
  try {
    const [a, b] = await Promise.all([x.buy(), x.buy()]);
    assert.deepEqual(a, b);
    assert.equal(x.debitCount, 1);
    const opened = x.library.open(x.person, {
      key: "open-test",
      packId: a.packs[0].id,
    });
    assert.equal(opened.cards.length, 9);
    assert.equal(
      opened.cards.filter((c) => c.definition.type === "code").length,
      1,
    );
    assert.equal(x.registrations.size, 1);
    const state = await x.library.state(x.person),
      code = state.codes[0];
    const secret = x.library.reveal(x.person, {
      key: "reveal-test",
      codeId: code.id,
    }).code;
    assert.equal(normalizeCode(secret), secret);
    assert(!JSON.stringify(state).includes(secret));
    assert(!JSON.stringify(x.store.read((s) => s)).includes(secret));
    assert.equal(x.balances.get(1), 9000);
    assert.equal(x.core.wallet(x.library.actor(x.person)).nx, 0);
  } finally {
    x.close();
  }
});
test("lost payment responses recover without a second debit, pack, or code", async () => {
  const x = fixture();
  try {
    x.faults.lostDebit = true;
    await assert.rejects(x.buy(), (e) => e.code === "GAME_UNAVAILABLE");
    assert.equal(x.debitCount, 1);
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 0);
    await x.library.recover();
    assert.equal(x.debitCount, 1);
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.core.codeInventory(operator).total, 1);
    assert.equal(x.journal.pending().length, 0);
  } finally {
    x.close();
  }
});
test("declined purchases do not allocate cards and a reused identifier cannot change terms", async () => {
  const x = fixture();
  try {
    x.balances.set(1, 0);
    await assert.rejects(x.buy(), (e) => e.code === "INSUFFICIENT_FUNDS");
    assert.equal(x.core.codeInventory(operator).total, 0);
    x.balances.set(1, 10000);
    await assert.rejects(x.buy(), (e) => e.code === "PURCHASE_REJECTED");
    await x.buy("fresh-purchase");
    await assert.rejects(
      x.buy("fresh-purchase", 2),
      (e) => e.code === "IDEMPOTENCY_CONFLICT",
    );
  } finally {
    x.close();
  }
});
test("a code remains hidden until registration; another account cannot open or reveal it", async () => {
  const x = fixture();
  try {
    x.faults.registration = true;
    const bought = await x.buy();
    assert.throws(() =>
      x.library.open(x.other, {
        key: "other-open",
        packId: bought.packs[0].id,
      }),
    );
    const opened = x.library.open(x.person, {
        key: "owner-open",
        packId: bought.packs[0].id,
      }),
      id = opened.cards.find((c) => c.definition.type === "code").codes[0].id;
    assert.throws(
      () => x.library.reveal(x.person, { key: "early-reveal", codeId: id }),
      (e) => e.code === "CODE_PREPARING",
    );
    x.faults.registration = false;
    await x.library.reconcile({ force: true });
    assert.throws(() =>
      x.library.reveal(x.other, { key: "other-reveal", codeId: id }),
    );
    assert.equal(
      x.library.reveal(x.person, { key: "owner-reveal", codeId: id }).code
        .length >= 15,
      true,
    );
  } finally {
    x.close();
  }
});
test("USED callbacks and polling share the same durable receipt", async () => {
  const x = fixture();
  try {
    const bought = await x.buy();
    x.library.open(x.person, { key: "open-used", packId: bought.packs[0].id });
    const id = (await x.library.state(x.person)).codes[0].id;
    x.library.reveal(x.person, { key: "reveal-used", codeId: id });
    const proof = {
      issuanceId: id,
      receiptId: "11111111-2222-4333-8444-555555555555",
      occurredAt: "2026-10-02T12:00:00.000Z",
    };
    const a = x.library.used(proof),
      b = x.library.used(proof);
    assert.deepEqual(a, b);
    assert.equal((await x.library.state(x.person)).codes[0].status, "redeemed");
    assert.throws(() => x.library.used({ ...proof, occurredAt: "invalid" }));
  } finally {
    x.close();
  }
});
test("generated code collisions roll back the framework mint and remain recoverable after payment", async () => {
  const x = fixture({
    generator: () => ({
      code: "BBBBBBBBBBBBBBB",
      externalId: "constant",
      metadata: { itemId: 2000005, quantity: 10, petDays: 0 },
    }),
  });
  try {
    await x.buy("first-collision");
    await assert.rejects(
      x.buy("second-collision"),
      (e) => e.code === "DUPLICATE_CODE",
    );
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.core.codeInventory(operator).total, 1);
    assert.equal(x.journal.pending()[0].state, "paid");
  } finally {
    x.close();
  }
});
test("encrypted framework state and journal restart preserve purchased packs and code registration", async () => {
  const dir = mkdtempSync(join(tmpdir(), "card-bridge-"));
  let x;
  try {
    let store = new SQLiteStore(join(dir, "cards.sqlite"), {
        encryptionKey: Buffer.alloc(32, 3),
      }),
      journal = new Journal(join(dir, "bridge.sqlite"));
    x = fixture({ store, journal });
    const bought = await x.buy();
    x.library.open(x.person, {
      key: "open-restart",
      packId: bought.packs[0].id,
    });
    const id = (await x.library.state(x.person)).codes[0].id,
      raw = x.library.reveal(x.person, {
        key: "before-restart",
        codeId: id,
      }).code;
    x.close();
    store = new SQLiteStore(join(dir, "cards.sqlite"), {
      encryptionKey: Buffer.alloc(32, 3),
    });
    journal = new Journal(join(dir, "bridge.sqlite"));
    x = fixture({ store, journal });
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.journal.registration(id), "ready");
    assert.equal(
      x.library.reveal(x.person, { key: "after-restart", codeId: id }).code,
      raw,
    );
    assert(!readFileSync(join(dir, "cards.sqlite")).includes(Buffer.from(raw)));
    x.close();
    x = null;
  } finally {
    if (x) x.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
test("HMAC authentication binds the exact body, path, time and nonce and rejects replay", () => {
  const secret = "test-key-".repeat(6),
    now = 1790942400000,
    time = String(now),
    nonce = "a".repeat(32),
    body = '{"example":1}',
    path = "/api/library/provider/used";
  const headers = {
      "x-bridge-time": time,
      "x-bridge-nonce": nonce,
      "x-bridge-signature": mac(
        secret,
        ["POST", path, time, nonce, hash(body)].join("\n"),
      ),
    },
    nonces = new Map();
  assert.equal(
    verifyRequest({ secret, method: "POST", path, headers, body, nonces, now }),
    true,
  );
  assert.equal(
    verifyRequest({ secret, method: "POST", path, headers, body, nonces, now }),
    false,
  );
  assert.equal(
    verifyRequest({
      secret,
      method: "POST",
      path,
      headers,
      body: body + " ",
      nonces: new Map(),
      now,
    }),
    false,
  );
  assert.equal(
    verifyRequest({
      secret,
      method: "POST",
      path,
      headers,
      body,
      nonces: new Map(),
      now: now + 61000,
    }),
    false,
  );
});
test("future-dated signatures retain replay protection through the inclusive validity boundary", () => {
  const secret = "replay-fixture-".repeat(4),
    now = 1790942400000,
    path = "/api/library/provider/used",
    body = "{}",
    nonces = new Map();
  const signed = (at, nonce) => ({
    "x-bridge-time": String(at),
    "x-bridge-nonce": nonce,
    "x-bridge-signature": mac(
      secret,
      ["POST", path, String(at), nonce, hash(body)].join("\n"),
    ),
  });
  const check = (headers, at, cache = nonces) =>
    verifyRequest({
      secret,
      method: "POST",
      path,
      headers,
      body,
      nonces: cache,
      now: at,
    });
  const headers = signed(now + 59000, "b".repeat(32));
  assert.equal(check(headers, now), true);
  assert.equal(check(headers, now), false);
  assert.equal(check(headers, now + 61000), false);
  assert.equal(check(headers, now + 119000), false);
  assert.equal(check(headers, now + 119001), false);
  assert.equal(check(signed(now + 119001, "b".repeat(32)), now + 119001), true);
  const past = signed(now - 60000, "c".repeat(32)),
    pastCache = new Map();
  assert.equal(check(past, now, pastCache), true);
  assert.equal(check(past, now, pastCache), false);
  assert.equal(check(past, now + 1, pastCache), false);
  const capped = new Map(
    Array.from({ length: 10000 }, (_, i) => [String(i), now]),
  );
  assert.equal(check(signed(now, "d".repeat(32)), now, capped), false);
  assert.equal(check(signed(now + 1, "d".repeat(32)), now + 1, capped), true);
  assert.equal(capped.size, 1);
});
test("HTTP preview stays public while collection mutations require a verified session, origin and CSRF", async () => {
  const x = fixture(),
    secret = "shared-test-".repeat(4),
    csrfSecret = "csrf-test-".repeat(4),
    token = "A".repeat(43);
  const game = async (path, v) =>
    path === "/session" && v.tokenHash === hash(token)
      ? x.person
      : x.game(path, v);
  const server = createLibraryHttp({
    library: x.library,
    game,
    origin: "http://127.0.0.1:8487",
    secret,
    csrfSecret,
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = "http://127.0.0.1:" + server.address().port;
  try {
    assert.equal((await fetch(base + "/api/library/catalog")).status, 200);
    assert.equal((await fetch(base + "/api/library/state")).status, 401);
    const session = await fetch(base + "/api/library/session", {
      headers: { Cookie: "qg_account=" + token },
    }).then((r) => r.json());
    assert.equal(session.signedIn, true);
    const quote = x.library.quote(x.person, { productId: "first-light" }),
      input = { ...quote, key: "http-purchase" };
    const headers = {
      "Content-Type": "application/json",
      Cookie: "qg_account=" + token,
      Origin: "http://127.0.0.1:8487",
    };
    assert.equal(
      (
        await fetch(base + "/api/library/buy", {
          method: "POST",
          headers,
          body: JSON.stringify(input),
        })
      ).status,
      403,
    );
    headers["X-CSRF-Token"] = session.csrf;
    assert.equal(
      (
        await fetch(base + "/api/library/buy", {
          method: "POST",
          headers,
          body: JSON.stringify(input),
        })
      ).status,
      200,
    );
    headers.Origin = "https://example.org";
    assert.equal(
      (
        await fetch(base + "/api/library/buy", {
          method: "POST",
          headers,
          body: JSON.stringify(input),
        })
      ).status,
      403,
    );
    assert.equal(
      (
        await fetch(base + "/api/library/provider/used", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        })
      ).status,
      403,
    );
  } finally {
    await new Promise((r) => server.close(r));
    x.close();
  }
});

test("purchase registration stays scoped and recovery backs off immediately during a game outage", async () => {
  const x = fixture();
  try {
    x.faults.registration = true;
    await x.buy("first-waiting", 1, x.person);
    x.faults.registration = false;
    await x.buy("second-ready", 1, x.other);
    assert.equal(x.registrations.size, 1);
    assert.equal([...x.registrations.values()][0].material.accountId, 2);
    await x.library.reconcile({ force: true });
    assert.equal(x.registrations.size, 2);
    x.faults.lostDebit = true;
    await assert.rejects(x.buy("pending-first", 1, x.person));
    x.faults.lostDebit = true;
    await assert.rejects(x.buy("pending-second", 1, x.other));
    const original = x.library.game;
    let requests = 0;
    x.library.game = async () => {
      requests++;
      throw Object.assign(Error("Unavailable"), {
        code: "GAME_UNAVAILABLE",
        status: 503,
      });
    };
    await x.library.recover();
    assert.equal(requests, 1);
    assert.equal(x.journal.pending().length, 2);
    await x.library.recover({ shouldStop: () => true });
    assert.equal(requests, 1);
    x.library.game = original;
    await x.library.recover();
    assert.equal(x.journal.pending().length, 0);
  } finally {
    x.close();
  }
});

test("startup backfills existing pack completion reservations without changing ownership", async () => {
  const x = fixture();
  try {
    await x.buy("backfill-startup");
    const before = x.store.read((s) => ({
      copies: s.copies,
      packs: s.packs,
      balances: s.balances,
    }));
    x.store.transact((s) => {
      s.completionObligations = {};
    });
    assert.throws(
      () =>
        x.core.grantCurrency(operator, {
          userId: x.library.actor(x.person).userId,
          currencyId: "nx",
          amount: 1,
          reason: "completion migration fixture",
          key: "before-backfill",
        }),
      (e) => e.code === "COMPLETION_MIGRATION_REQUIRED",
    );
    const restored = new Library({
      framework: x.core,
      store: x.store,
      journal: x.journal,
      game: x.game,
    });
    restored.quote(x.person, { productId: "first-light" });
    assert.deepEqual(
      x.store.read((s) => ({
        copies: s.copies,
        packs: s.packs,
        balances: s.balances,
      })),
      before,
    );
    assert.equal(x.core.backfillCompletionReservations(operator).count, 0);
  } finally {
    x.close();
  }
});
