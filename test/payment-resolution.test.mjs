import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { SQLiteStore } from "@digital-card/framework/sqlite";
import { fixture } from "./fixture.mjs";
import { Journal } from "../src/journal.mjs";
import { operator } from "../src/library.mjs";
import { hash, verifyRequest } from "../src/protocol.mjs";

const execute = promisify(execFile), cli = fileURLToPath(new URL("../tools/payments.mjs", import.meta.url)),
  shapes = JSON.parse(readFileSync(new URL("../data/catalog.example.json", import.meta.url)));
test("local recovery CLI requires an offline bridge and preserves an exact audited decision through lost finalization", async () => {
  const directory = mkdtempSync(join(tmpdir(), "legacy-resolution-cli-")),
    stateKey = Buffer.alloc(32, 3), secret = "native-fixture-secret-".repeat(4), nonces = new Map();
  let x, server;
  try {
    x = fixture({ catalog: shapes,
      store: new SQLiteStore(join(directory, "framework.sqlite"), { encryptionKey: stateKey }),
      journal: new Journal(join(directory, "bridge.sqlite")) });
    const quote = x.library.quote(x.person, { productId: "shapes", cashType: 4 }),
      input = { productId: quote.productId, quantity: quote.quantity,
        catalogVersion: quote.catalogVersion, productRevision: quote.productRevision, cashType: 4 },
      old = x.journal.create(1, "old-quarantined-order", input, x.library.actor(x.person).userId, quote);
    delete old.financialVersion;
    old.payment = await x.game("/debit", { orderId: old.id, accountId: 1, cashType: 4, amount: 1000 });
    Object.assign(x.payments.get(old.id), { state: "LEGACY_PAID", transactionId: old.id });
    x.core.settleExternalCredit(operator, { providerId: "maplestory.v83", transactionId: old.id,
      userId: old.userId, currencyId: "nx", amount: 1000, externalCurrency: "NX_PREPAID", externalUnits: "1000" });
    const delivered = x.core.purchase({ userId: old.userId }, { key: "alternate-legacy-purchase", ...input });
    x.journal.save(old, "paid");
    await x.library.recover();
    assert.equal(x.journal.order(old.id).state, "quarantined");
    const native = x.game, prior = x;
    x.close(); x = null;
    let calls = 0;
    server = createServer(async (req, res) => {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString("utf8");
      try {
        assert.equal(verifyRequest({ secret, method: req.method, path: req.url, headers: req.headers, body, nonces }), true);
        calls++;
        const result = await native(req.url, JSON.parse(body));
        res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(result));
      } catch (error) {
        res.writeHead(error.status ?? 503, { "Content-Type": "application/json" }).end(JSON.stringify({ code: error.code ?? "GAME_UNAVAILABLE", message: "Fixture response unavailable" }));
      }
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const env = { ...process.env, STATE_DIRECTORY: directory, STATE_KEY: stateKey.toString("base64"),
      CODE_ENCRYPTION_KEY: Buffer.alloc(32, 1).toString("base64"), CODE_INDEX_KEY: Buffer.alloc(32, 2).toString("base64"),
      GAME_URL: "http://127.0.0.1:" + server.address().port, GAME_SHARED_KEY: secret },
      args = [cli, "resolve-legacy", "--order-id", old.id, "--purchase-key", "alternate-legacy-purchase",
        "--decision-key", "original-decision-key", "--reason", "Adopt the verified existing delivery.", "--operator-id", "fixture-operator"];
    const originalHashes = ["framework.sqlite", "bridge.sqlite"].map((p) => hash(readFileSync(join(directory, p))));
    await assert.rejects(execute(process.execPath, args, { env }), (e) => e.code === 2);
    assert.equal(calls, 0);
    assert.deepEqual(["framework.sqlite", "bridge.sqlite"].map((p) => hash(readFileSync(join(directory, p)))), originalHashes);
    prior.faults.lostFinalize = true;
    await assert.rejects(execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env }),
      (e) => e.code === 1 && e.stderr.includes("GAME_UNAVAILABLE") && !e.stderr.includes(secret));
    const result = JSON.parse((await execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env })).stdout);
    assert.deepEqual(result, { orderId: old.id, state: "complete", purchaseId: delivered.id });
    assert.deepEqual(JSON.parse((await execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env })).stdout), result);
    const journal = new Journal(join(directory, "bridge.sqlite"));
    assert.equal(journal.order(old.id).state, "complete");
    assert.deepEqual(journal.order(old.id).result, delivered);
    journal.close();
    assert.equal(prior.nxPrepaid.get(1), 9000); assert.equal(prior.debitCount, 1); assert.equal(prior.refundCount, 0);
  } finally {
    x?.close();
    if (server) await new Promise((r) => server.close(r));
    rmSync(directory, { recursive: true, force: true });
  }
});
