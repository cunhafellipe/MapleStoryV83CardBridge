import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, rmSync, copyFileSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SQLiteStore } from "@digital-card/framework/sqlite";
import { Journal } from "../src/journal.mjs";
import { DatabaseSync } from "node:sqlite";
import { hash } from "../src/protocol.mjs";
import { fixture } from "./fixture.mjs";
import { operator } from "../src/library.mjs";
import { paymentActor, paymentProviderId } from "../src/payment-authority.mjs";
import { cashTypes } from "../src/cash-types.mjs";
import { resolveLegacyPayment } from "../src/payment-resolution.mjs";

const shapes = JSON.parse(readFileSync(new URL("../data/catalog.example.json", import.meta.url)));
const brokenGenerator = () => { throw Error("Private provider diagnostic"); };
function status(x, key) {
  const order = x.journal.find(1, key);
  return x.core.lookupExternalPurchase(paymentActor, { providerId: paymentProviderId, transactionId: order.id });
}

test("allocation failure refunds only the original cash source without synthetic framework credit", async () => {
  for (const cashType of [1, 2, 4]) {
    const x = fixture({ generator: brokenGenerator });
    try {
      await assert.rejects(x.buy("failed-allocation", 1, x.person, cashType), { code: "PURCHASE_REFUNDED" });
      assert.deepEqual([x.balances.get(1), x.maplePoints.get(1), x.nxPrepaid.get(1)], [10000, 10000, 10000]);
      assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 1);
      assert.equal(status(x, "failed-allocation").state, "compensated");
      assert.equal(x.core.packs(x.library.actor(x.person)).length, 0);
      assert.equal(x.core.codeInventory(operator).total, 0);
      assert.equal(x.core.wallet(x.library.actor(x.person)).nx ?? 0, 0);
      assert.equal(x.journal.pending().length, 0);
      assert(!JSON.stringify(x.journal.find(1, "failed-allocation")).includes("Private provider diagnostic"));
      await assert.rejects(x.buy("failed-allocation", 1, x.person, cashType), { code: "PURCHASE_REFUNDED" });
      assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 1);
    } finally { x.close(); }
  }
});

test("lost native refund acknowledgement leaves recoverable work and never refunds twice", async () => {
  const x = fixture({ generator: brokenGenerator });
  try {
    x.faults.lostRefund = true;
    await assert.rejects(x.buy("refund-ack-lost", 1, x.person, 4), { code: "GAME_UNAVAILABLE" });
    assert.equal(status(x, "refund-ack-lost").state, "refund_required");
    assert.equal(x.journal.pending()[0].state, "refund_required");
    assert.equal(x.nxPrepaid.get(1), 10000); assert.equal(x.refundCount, 1);
    await x.library.recover();
    assert.equal(status(x, "refund-ack-lost").state, "compensated");
    assert.equal(x.journal.pending().length, 0); assert.equal(x.refundCount, 1);
    assert.equal(x.library.lastRecovery.failed, 0);
  } finally { x.close(); }
});

test("owed refunds remain visible and block another purchase until original capacity returns", async () => {
  const x = fixture({ generator: brokenGenerator });
  try {
    x.faults.refundOwed = true;
    await assert.rejects(x.buy("owed-refund", 1, x.person, 2), { code: "REFUND_PENDING" });
    assert.equal(status(x, "owed-refund").state, "refund_required");
    assert.equal(x.maplePoints.get(1), 9000); assert.equal(x.refundCount, 0);
    await assert.rejects(x.buy("replacement-purchase", 1, x.person, 1), { code: "PURCHASE_PENDING" });
    assert.equal(x.debitCount, 1);
    x.faults.refundOwed = false;
    await x.library.recover();
    assert.equal(x.maplePoints.get(1), 10000); assert.equal(x.refundCount, 1);
    assert.equal(x.journal.pending().length, 0);
  } finally { x.close(); }
});

test("lost framework allocation response resumes delivery without a second mint or refund", async () => {
  const x = fixture({ catalog: shapes });
  try {
    const commit = x.core.commitExternalPurchase.bind(x.core);
    x.core.commitExternalPurchase = (...args) => {
      commit(...args);
      throw Object.assign(Error("Response lost after commit"), { code: "GAME_UNAVAILABLE" });
    };
    await assert.rejects(x.library.buy(x.person, { key: "commit-ack-lost", ...x.library.quote(x.person, { productId: "shapes" }) }),
      { code: "GAME_UNAVAILABLE" });
    assert.equal(status(x, "commit-ack-lost").state, "fulfilled");
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    await x.library.recover();
    assert.equal(x.journal.find(1, "commit-ack-lost").state, "complete");
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
  } finally { x.close(); }
});

test("lost native finalization acknowledgement resumes its exact delivered purchase", async () => {
  const x = fixture();
  try {
    x.faults.lostFinalize = true;
    await assert.rejects(x.buy("finalize-ack-lost"), { code: "GAME_UNAVAILABLE" });
    const original = status(x, "finalize-ack-lost").purchase;
    await x.library.recover();
    assert.deepEqual(x.journal.find(1, "finalize-ack-lost").result, original);
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
  } finally { x.close(); }
});

test("a contradictory native debit cannot mint or authorize a refund", async () => {
  const x = fixture(), original = x.library.game;
  try {
    x.library.game = async (path, input) => {
      const result = await original(path, input);
      return path === "/payment/debit" ? { ...result, cashType: 4 } : result;
    };
    await assert.rejects(x.buy("contradictory-receipt"), { code: "INVALID_PAYMENT_RECEIPT" });
    assert.equal(status(x, "contradictory-receipt").state, "prepared");
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 0);
    assert.equal(x.refundCount, 0);
    x.library.game = original;
    await x.library.recover();
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
  } finally { x.close(); }
});

test("allocation capacity admission refuses before any native debit", async () => {
  const x = fixture({ catalog: shapes, coreOptions: { limits: { copiesPerUser: 1 } } });
  try {
    await assert.rejects(x.library.buy(x.person, { key: "capacity-before-debit", ...x.library.quote(x.person, { productId: "shapes" }) }),
      { code: "INSTALLATION_CAPACITY" });
    assert.equal(x.debitCount, 0); assert.equal(x.refundCount, 0);
    assert.equal(x.balances.get(1), 10000);
    assert.equal(x.journal.pending().length, 0);
  } finally { x.close(); }
});

test("a durable native debit without its modern core operation is quarantined instead of reallocated", async () => {
  const x = fixture();
  try {
    x.faults.lostDebit = true;
    await assert.rejects(x.buy("missing-modern-core"), { code: "GAME_UNAVAILABLE" });
    const originalLookup = x.core.lookupExternalPurchase.bind(x.core);
    x.core.lookupExternalPurchase = () => { throw Object.assign(Error("Missing operation"), { code: "NOT_FOUND" }); };
    await assert.rejects(x.buy("missing-modern-core"), { code: "LEGACY_RECOVERY_REQUIRED" });
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
    assert.equal(x.core.packs(x.library.actor(x.person)).length, 0);
    x.core.lookupExternalPurchase = originalLookup;
    await x.library.recover();
    assert.equal(x.journal.pending().length, 0);
  } finally { x.close(); }
});

async function legacy(x, scenario, cashType, suffix = "") {
  const quote = x.library.quote(x.person, { productId: "first-light", cashType }),
    input = { productId: quote.productId, quantity: quote.quantity,
      catalogVersion: quote.catalogVersion, productRevision: quote.productRevision, cashType },
    order = x.journal.create(1, `legacy-${scenario}-${cashType}${suffix}`, input, x.library.actor(x.person).userId, quote);
  delete order.financialVersion;
  order.payment = await x.game("/debit", { orderId: order.id, accountId: 1, cashType, amount: quote.price.amount });
  const native = x.payments.get(order.id);
  native.state = "LEGACY_PAID"; native.transactionId = order.id;
  if (scenario !== "debit-only")
    x.core.settleExternalCredit(operator, {
      providerId: paymentProviderId, transactionId: order.id, userId: order.userId,
      currencyId: "nx", amount: quote.price.amount,
      externalCurrency: cashTypes.find((c) => c.cashType === cashType).externalCurrency,
      externalUnits: String(quote.price.amount),
    });
  if (["complete", "response-lost", "spent-elsewhere"].includes(scenario))
    order.result = x.core.purchase({ userId: order.userId }, {
      key: scenario === "spent-elsewhere" ? "alternate-old-purchase" + suffix : `purchase-${order.id}`, ...input,
    });
  x.journal.save(order, scenario === "complete" ? "complete" : "paid");
  return order;
}

test("legacy debit-only, credit-only and already-delivered orders retain their original identities", async () => {
  for (const scenario of ["debit-only", "credit-only", "response-lost", "complete"]) {
    for (const cashType of [1, 2, 4]) {
      const x = fixture();
      try {
        const old = await legacy(x, scenario, cashType), original = old.result;
        await x.library.recover();
        const updated = x.journal.order(old.id);
        assert.equal(updated.state, "complete");
        assert.equal(updated.legacyReconciled, true);
        if (original) assert.deepEqual(updated.result, original);
        assert.equal(x.core.packs(x.library.actor(x.person)).length, 1);
        assert.equal(x.core.codeInventory(operator).total, 1);
        assert.equal(x.core.wallet(x.library.actor(x.person)).nx ?? 0, 0);
        assert.equal(x.payments.get(old.id).state, "FULFILLED");
        assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
        await x.library.recover();
        assert.equal(x.journal.pending().length, 0); assert.equal(x.debitCount, 1);
      } finally { x.close(); }
    }
  }
});

test("spent legacy credit is quarantined with its existing alternate cards preserved", async () => {
  const x = fixture();
  try {
    const old = await legacy(x, "spent-elsewhere", 4), original = old.result;
    await x.library.recover();
    assert.equal(x.journal.order(old.id).state, "quarantined");
    assert.equal(status(x, "legacy-spent-elsewhere-4").state, "quarantined");
    assert.deepEqual(x.core.packs(x.library.actor(x.person)).map((p) => p.id), original.packs.map((p) => p.id));
    assert.equal(x.nxPrepaid.get(1), 9000); assert.equal(x.refundCount, 0);
    assert.equal(x.core.wallet(x.library.actor(x.person)).nx ?? 0, 0);
  } finally { x.close(); }
});

test("missing journal identities are persisted for operator recovery and pause new payments", async () => {
  const x = fixture();
  try {
    x.faults.lostDebit = true;
    await assert.rejects(x.buy("orphan-payment"), { code: "GAME_UNAVAILABLE" });
    const original = x.journal.find(1, "orphan-payment");
    x.journal.db.prepare("DELETE FROM orders WHERE id=?").run(original.id);
    await x.library.recover();
    assert.equal(x.journal.diagnostics().orphanedPayments, 1);
    assert.equal(x.library.readiness().ok, false);
    await assert.rejects(x.buy("another-payment"), { code: "STORAGE_UNAVAILABLE" });
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
    const recreated = x.journal.create(original.accountId, "orphan-payment", original.input,
      original.userId, original.quote, original.accountName);
    x.journal.save({ ...recreated, ...original }, original.state);
    await x.library.recover();
    assert.equal(x.journal.diagnostics().orphanedPayments, 0);
    assert.equal(x.journal.order(original.id).state, "complete");
    assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
  } finally { x.close(); }
});

test("explicit local resolution adopts only the proven existing legacy delivery and replays the same decision", async () => {
  const x = fixture();
  try {
    const old = await legacy(x, "spent-elsewhere", 1);
    await x.library.recover();
    const command = { library: x.library, orderId: old.id,
      purchaseKey: "alternate-old-purchase", decisionKey: "operator-decision-1",
      operatorId: "operator-fixture", reason: "Adopt the verified original-credit-funded purchase." };
    await assert.rejects(resolveLegacyPayment({ ...command, purchaseKey: "missing-purchase" }), { code: "LEGACY_RESOLUTION_REJECTED" });
    const before = x.store.read((s) => s), first = await resolveLegacyPayment(command), after = x.store.read((s) => s);
    assert.equal(first.state, "complete"); assert.equal(first.purchaseId, old.result.id);
    for (const field of ["copies", "packs", "codes", "balances", "ledger", "events", "supply"])
      assert.deepEqual(after[field], before[field]);
    assert.equal(x.payments.get(old.id).state, "FULFILLED");
    assert.equal(x.balances.get(1), 9000); assert.equal(x.refundCount, 0);
    assert.deepEqual(await resolveLegacyPayment(command), first);
    await assert.rejects(resolveLegacyPayment({ ...command, reason: "Different decision reason" }), { code: "IDEMPOTENCY_CONFLICT" });
    assert.equal(x.core.audit(operator).ok, true);
  } finally { x.close(); }
});

test("encrypted state and journal restart reconcile each ambiguous payment boundary", async () => {
  for (const boundary of ["lostDebit", "lostFinalize", "lostRefund", "lostConfirmation"]) {
    const directory = mkdtempSync(join(tmpdir(), "payment-restart-"));
    let x;
    const open = (generator) => fixture({
      store: new SQLiteStore(join(directory, "framework.sqlite"), { encryptionKey: Buffer.alloc(32, 3) }),
      journal: new Journal(join(directory, "bridge.sqlite")), generator,
    });
    try {
      const refund = ["lostRefund", "lostConfirmation"].includes(boundary);
      x = open(refund ? brokenGenerator : undefined);
      if (boundary === "lostConfirmation") {
        const confirm = x.core.confirmExternalCompensation.bind(x.core);
        x.core.confirmExternalCompensation = (...args) => {
          confirm(...args);
          throw Object.assign(Error("Confirmation response lost"), { code: "GAME_UNAVAILABLE" });
        };
      } else x.faults[boundary] = true;
      await assert.rejects(x.buy("restart-original-order", 1, x.person, 4), { code: "GAME_UNAVAILABLE" });
      const prior = x, native = x.game;
      x.close(); x = null;
      x = open(refund ? brokenGenerator : undefined);
      x.library.game = native;
      await x.library.recover();
      assert.equal(x.journal.find(1, "restart-original-order").state, refund ? "refunded" : "complete");
      assert.equal(x.core.packs(x.library.actor(x.person)).length, refund ? 0 : 1);
      assert.equal(prior.nxPrepaid.get(1), refund ? 10000 : 9000);
      assert.equal(prior.debitCount, 1); assert.equal(prior.refundCount, refund ? 1 : 0);
      assert.equal(x.journal.pending().length, 0);
      assert.equal(x.core.audit(operator).ok, true);
    } finally { x?.close(); rmSync(directory, { recursive: true, force: true }); }
  }
});

test("older prepared stores cannot remint an already finalized native purchase", async () => {
  for (const restoreJournal of [false, true]) {
    const x = fixture();
    try {
      x.faults.lostDebit = true;
      await assert.rejects(x.buy("inconsistent-restore"), { code: "GAME_UNAVAILABLE" });
      const before = x.store.read((s) => s), oldOrder = x.journal.find(1, "inconsistent-restore");
      await x.buy("inconsistent-restore");
      const finalized = x.payments.get(oldOrder.id).purchaseId;
      x.store.transact((s) => { for (const key of Object.keys(s)) delete s[key]; Object.assign(s, structuredClone(before)); });
      if (restoreJournal) x.journal.save(oldOrder, oldOrder.state);
      await assert.rejects(x.buy("inconsistent-restore"), { code: "LEGACY_RECOVERY_REQUIRED" });
      await x.library.recover();
      assert.equal(x.core.packs(x.library.actor(x.person)).length, 0);
      assert.equal(x.core.codeInventory(operator).total, 0);
      assert.equal(x.payments.get(oldOrder.id).purchaseId, finalized);
      assert.equal(x.debitCount, 1); assert.equal(x.refundCount, 0);
      assert.equal(x.journal.order(oldOrder.id).state, "quarantined");
      assert.equal(x.library.readiness().ok, false);
    } finally { x.close(); }
  }
});

test("a mismatched journal owner cannot spend one account's cash for another account's cards", async () => {
  const x = fixture();
  try {
    const quote = x.library.quote(x.person, { productId: "first-light" }),
      input = { productId: quote.productId, quantity: quote.quantity, catalogVersion: quote.catalogVersion,
        productRevision: quote.productRevision, adminRevision: quote.adminRevision, cashType: 1 };
    x.journal.create(1, "mismatched-owner", input, x.library.actor(x.other).userId, quote);
    await assert.rejects(x.buy("mismatched-owner"), { code: "PAYMENT_ACCOUNT_MISMATCH" });
    assert.equal(x.debitCount, 0); assert.equal(x.refundCount, 0);
    assert.equal(x.core.packs(x.library.actor(x.other)).length, 0);
    assert.equal(x.balances.get(1), 10000); assert.equal(x.balances.get(2), 10000);
    assert.equal(x.journal.find(1, "mismatched-owner").state, "quarantined");
    assert.equal(x.library.readiness().ok, false);
  } finally { x.close(); }
});

test("quarantined legacy work does not starve a later recoverable debit", async () => {
  const x = fixture();
  try {
    x.balances.set(1, 100000);
    for (let i = 0; i < 20; i++) await legacy(x, "spent-elsewhere", 1, "-" + i);
    await x.library.recover();
    assert.equal(x.journal.pending().filter((o) => o.state === "quarantined").length, 20);
    x.faults.lostDebit = true;
    await assert.rejects(x.buy("later-recoverable-order", 1, x.other), { code: "GAME_UNAVAILABLE" });
    await x.library.recover();
    assert.equal(x.journal.find(2, "later-recoverable-order").state, "complete");
    assert.equal(x.journal.pending().length, 20);
    assert.equal(x.balances.get(2), 9000);
  } finally { x.close(); }
});

test("opening a future journal schema does not create tables or alter its bytes", () => {
  const directory = mkdtempSync(join(tmpdir(), "future-journal-"));
  try {
    const path = join(directory, "future.sqlite"), future = new DatabaseSync(path);
    future.exec("CREATE TABLE future_records(id INTEGER PRIMARY KEY); PRAGMA user_version=99;"); future.close();
    const before = hash(readFileSync(path));
    assert.throws(() => new Journal(path), /Unsupported bridge state schema/);
    assert.equal(hash(readFileSync(path)), before);
    const inspect = new DatabaseSync(path, { readOnly: true });
    assert.deepEqual(inspect.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((r) => r.name), ["future_records"]);
    inspect.close();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test("a future journal version present only in WAL is refused without changing source files", async () => {
  const directory = mkdtempSync(join(tmpdir(), "future-journal-wal-"));
  const live = join(directory, "live.sqlite"), copied = join(directory, "copied.sqlite");
  const child = spawn(process.execPath, ["--input-type=module", "-e", `
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync(process.argv[1]);
    db.exec('PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE future_records(id INTEGER PRIMARY KEY); PRAGMA wal_checkpoint(TRUNCATE); PRAGMA user_version=99;');
    process.stdout.write('ready'); setInterval(()=>{},1000);
  `, live], { stdio: ["ignore", "pipe", "pipe"] });
  const exited = once(child, "exit");
  try {
    await Promise.race([once(child.stdout, "data"), exited.then(() => { throw Error("WAL fixture exited early"); })]);
    for (const suffix of ["", "-wal"]) copyFileSync(live + suffix, copied + suffix);
    const before = ["", "-wal"].map((suffix) => hash(readFileSync(copied + suffix)));
    assert.throws(() => new Journal(copied), /Unsupported bridge state schema/);
    assert.deepEqual(["", "-wal"].map((suffix) => hash(readFileSync(copied + suffix))), before);
    assert.equal(existsSync(copied + "-shm"), false);
  } finally {
    child.kill(); await exited; rmSync(directory, { recursive: true, force: true });
  }
});
