import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DatabaseSync } from "node:sqlite";
import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { hash, verifyRequest } from "../src/protocol.mjs";
import { paymentFingerprint } from "../src/payment-authority.mjs";
import { projectLegacyDump, verifiedLegacyEvidence, retainedJournalOrder } from "../src/legacy-backup.mjs";

const order = { id: "a".repeat(64), accountId: 7, input: { cashType: 4 }, quote: { price: { amount: 1000 } } };
const sql = `-- MySQL dump
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ \`cosmic\` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;
USE \`cosmic\`;
DROP TABLE IF EXISTS \`accounts\`;
CREATE TABLE \`accounts\` (
 \`id\` int NOT NULL AUTO_INCREMENT,
 \`name\` varchar(13) NOT NULL,
 \`password\` varchar(128) NOT NULL,
 \`createdat\` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
 \`email\` varchar(45) DEFAULT NULL,
 PRIMARY KEY (\`id\`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
LOCK TABLES \`accounts\` WRITE;
/*!40000 ALTER TABLE \`accounts\` DISABLE KEYS */;
INSERT INTO \`accounts\` VALUES (3,'other','fixture', '2005-01-01 00:00:00',NULL),(7,'renamed','comma,semi;quote\\\'slash\\\\line\\n雪','2005-05-11 00:00:00','double''quote');
/*!40000 ALTER TABLE \`accounts\` ENABLE KEYS */;
UNLOCK TABLES;
DROP TABLE IF EXISTS \`card_bridge_payments\`;
CREATE TABLE \`card_bridge_payments\` (
 \`order_id\` char(64) NOT NULL,
 \`account_id\` int NOT NULL,
 \`cash_type\` int NOT NULL DEFAULT '1',
 \`amount\` int NOT NULL,
 \`balance_after\` int NOT NULL,
 \`created_at\` timestamp(0) NOT NULL DEFAULT current_timestamp(),
 PRIMARY KEY (\`order_id\`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO \`card_bridge_payments\` VALUES ('${"b".repeat(64)}',3,1,400,8000,'2026-01-01 00:00:00'),('${order.id}',7,4,1000,9000,'2026-01-01 00:00:00');
CREATE TABLE \`keymap\` (\`id\` int NOT NULL, \`accountid\` int NOT NULL, FOREIGN KEY (\`accountid\`) REFERENCES \`accounts\` (\`id\`)) ENGINE=InnoDB;
CREATE TABLE \`card_bridge_account_identity\` (\`account_id\` int NOT NULL, \`live_account_id\` int DEFAULT NULL, FOREIGN KEY (\`live_account_id\`) REFERENCES \`accounts\` (\`id\`)) ENGINE=InnoDB;
INSERT INTO \`notes\` VALUES (1,'accounts are retained; card_bridge_payments are evidence');
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
`;
const rejected = (work) => assert.throws(work, { code: "LEGACY_EVIDENCE_REJECTED" });

test("retained dump projection handles actual extended INSERT grammar, escaping and NULL without exporting unrelated fields", () => {
  const expected = {
    account: { accountId: 7, createdAt: "2005-05-11T00:00:00.000Z" },
    payment: { transactionId: order.id, accountId: 7, cashType: 4, amount: 1000, balanceAfter: 9000, createdAt: "2026-01-01T00:00:00.000Z" },
  };
  assert.deepEqual(projectLegacyDump(sql, order), expected);
  assert.deepEqual(projectLegacyDump(sql.replace("current_timestamp()", "current_timestamp(0)"), order), expected);
});
test("ambiguous dumps and changed original terms refuse attribution", () => {
  for (const mutation of [
    (s) => s.replace("TIME_ZONE='+00:00'", "TIME_ZONE='+01:00'"),
    (s) => s.replace("NO_AUTO_VALUE_ON_ZERO", "NO_BACKSLASH_ESCAPES"),
    (s) => s.replace("USE `cosmic`", "USE `other`"),
    (s) => s.replace("USE `cosmic`;", ""),
    (s) => s.replace("timestamp NOT NULL", "datetime NOT NULL"),
    (s) => s.replace("timestamp(0)", "timestamp(6)"),
    (s) => s.replace("timestamp NOT NULL", "timestamp NULL"),
    (s) => s.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT '2005-01-01 00:00:00'"),
    (s) => s.replace("'2005-05-11 00:00:00'", "'2026-01-01 00:00:00'"),
    (s) => s.replace("'2005-05-11 00:00:00'", "'2005-02-31 00:00:00'"),
    (s) => s.replace("'2005-05-11 00:00:00'", "NULL"),
    (s) => s.replace("',7,4,1000,9000", "',8,4,1000,9000"),
    (s) => s.replace("',7,4,1000,9000", "',7,1,1000,9000"),
    (s) => s.replace("',7,4,1000,9000", "',7,4,999,9000"),
    (s) => s.replace("(7,'renamed'", "(8,'renamed'"),
    (s) => s + "UPDATE accounts SET createdat='2005-05-11 00:00:00' WHERE id=7;\n",
    (s) => s + "UPDATE other JOIN accounts ON other.id=accounts.id SET accounts.createdat='2005-05-11 00:00:00';\n",
    (s) => s + "INSERT INTO `accounts` VALUES (7,'duplicate','fixture','2005-05-11 00:00:00',NULL);\n",
    (s) => s + "DELIMITER ;;\n",
    (s) => s.replace("line\\n", "line\\q"),
    (s) => s.replace("PRIMARY KEY (`order_id`)", "KEY (`order_id`)"),
  ]) rejected(() => projectLegacyDump(mutation(sql), order));
});

function backupFixture() {
  const directory = mkdtempSync(join(tmpdir(), "legacy-evidence-")), backup = join(directory, "backups", "retained"),
    meta = { project: "cosmic-cards-0123456789", directory, bridgeHash: "candidate" }, oldMeta = { ...meta, bridgeHash: "original" };
  const env = { GAME_URL: "http://127.0.0.1:1", GAME_SHARED_KEY: "fixture-signing-secret-".repeat(4),
    STATE_KEY: Buffer.alloc(32, 1).toString("base64"), CODE_ENCRYPTION_KEY: Buffer.alloc(32, 2).toString("base64"), CODE_INDEX_KEY: Buffer.alloc(32, 3).toString("base64") };
  const names = ["installation.json", "images.lock.json", "private/cards.env", "private/game.env", "private/database.env", "private/config.yaml", "private/catalog.json"];
  const save = (root, name, value) => { const path = join(root, name); mkdirSync(join(path, ".."), { recursive: true }); writeFileSync(path, value); };
  const bindings = {};
  for (const name of names) {
    const value = name === "installation.json" ? JSON.stringify(meta) : name === "private/cards.env" ? Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n") + "\n" : "fixture";
    save(directory, name, value); bindings[name] = hash(value);
    save(backup, name, name === "installation.json" ? JSON.stringify(oldMeta) : value);
  }
  for (const name of ["database.sql", "state.tar.gz", "compose.json", "runtime-images.tar", "runtime-images.json", "support/nginx.conf"])
    save(backup, name, name === "database.sql" ? sql : "fixture");
  const sums = {}, oldBinding = {};
  for (const name of [...names, "database.sql", "state.tar.gz", "compose.json", "runtime-images.tar", "runtime-images.json", "support/nginx.conf"]) sums[name] = hash(readFileSync(join(backup, name)));
  save(backup, "private/test-account.json", JSON.stringify({ name: "fixture-only" }));
  sums["private/test-account.json"] = hash(readFileSync(join(backup, "private/test-account.json")));
  for (const name of names) oldBinding[name] = sums[name];
  save(backup, "checksums.json", JSON.stringify(sums));
  const candidate = { services: { network: { ports: ["8484:8484"] } } };
  save(directory, "compose.json", JSON.stringify({ services: { network: { ports: [] } } }));
  const marker = { format: 1, kind: "legacy", phase: "validating", meta, oldMeta, backup, candidate,
    backupDigest: hash(readFileSync(join(backup, "checksums.json"))), binding: bindings, oldBinding };
  save(directory, "migration.pending.json", JSON.stringify(marker));
  return { directory, backup, marker, env, save, names };
}

test("managed evidence is bound to all backup files, original installation, staged keys and closed marker", () => {
  const f = backupFixture();
  try {
    const evidence = verifiedLegacyEvidence(f.directory, order);
    assert.equal(evidence.backupDigest, f.marker.backupDigest);
    assert.equal(evidence.databaseDigest, hash(sql));
    assert.equal(evidence.payment.balanceAfter, 9000);
    const markerBytes = readFileSync(join(f.directory, "migration.pending.json"));
    for (const phase of ["staging", "activating", "complete"]) {
      f.save(f.directory, "migration.pending.json", JSON.stringify({ ...f.marker, phase }));
      rejected(() => verifiedLegacyEvidence(f.directory, order));
    }
    f.save(f.directory, "migration.pending.json", markerBytes);
    f.save(f.directory, "upgrade.pending.json", "{}"); rejected(() => verifiedLegacyEvidence(f.directory, order));
    rmSync(join(f.directory, "upgrade.pending.json"));
    for (const [root, name] of [[f.directory, "private/cards.env"], [f.directory, "compose.json"], [f.backup, "runtime-images.tar"], [f.backup, "checksums.json"], [f.backup, "installation.json"], [f.backup, "database.sql"], [f.backup, "private/test-account.json"]]) {
      const original = readFileSync(join(root, name)); f.save(root, name, "modified");
      rejected(() => verifiedLegacyEvidence(f.directory, order)); f.save(root, name, original);
    }
    f.save(f.backup, "unexpected-file", "fixture"); rejected(() => verifiedLegacyEvidence(f.directory, order));
    rmSync(join(f.backup, "unexpected-file"));
    assert.deepEqual(verifiedLegacyEvidence(f.directory, order), evidence);
  } finally { rmSync(f.directory, { recursive: true, force: true }); }
});

test("journal attribution reads private WAL recovery copies without changing the original schema or files", () => {
  const directory = mkdtempSync(join(tmpdir(), "legacy-journal-")); let db;
  try {
    db = new DatabaseSync(join(directory, "bridge.sqlite"));
    db.exec("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; PRAGMA user_version=2; CREATE TABLE marker(id); PRAGMA wal_checkpoint(TRUNCATE)");
    db.exec("CREATE TABLE orders(id TEXT PRIMARY KEY,account_id INTEGER,body TEXT,state TEXT)");
    db.prepare("INSERT INTO orders VALUES(?,?,?,?)").run(order.id, order.accountId, JSON.stringify(order), "quarantined");
    const hashes = () => Object.fromEntries(readdirSync(directory).map((name) => [name, hash(readFileSync(join(directory, name)))])), before = hashes();
    assert.deepEqual(retainedJournalOrder(directory, order.id), { ...order, state: "quarantined" });
    assert.deepEqual(hashes(), before);
    rejected(() => retainedJournalOrder(directory, "f".repeat(64))); assert.deepEqual(hashes(), before);
    db.exec("PRAGMA user_version=99"); const future = hashes();
    rejected(() => retainedJournalOrder(directory, order.id)); assert.deepEqual(hashes(), future);
  } finally { db?.close(); rmSync(directory, { recursive: true, force: true }); }
});

test("signed local attribution CLI retries exact verified evidence after lost acknowledgement with no framework or wallet mutation", async () => {
  const f = backupFixture(), state = join(f.directory, "state"), execute = promisify(execFile); let server, db, saved, lost = true, calls = 0;
  try {
    mkdirSync(state); writeFileSync(join(state, "framework.sqlite"), "untouched-framework-fixture");
    db = new DatabaseSync(join(state, "bridge.sqlite"));
    db.exec("PRAGMA user_version=2; CREATE TABLE orders(id TEXT PRIMARY KEY,account_id INTEGER,body TEXT,state TEXT)");
    db.prepare("INSERT INTO orders VALUES(?,?,?,?)").run(order.id, order.accountId, JSON.stringify(order), "quarantined"); db.close(); db = null;
    const nonce = new Map();
    server = createServer(async (req, res) => {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString("utf8");
      try {
        assert.equal(verifyRequest({ secret: f.env.GAME_SHARED_KEY, method: req.method, path: req.url, headers: req.headers, body, nonces: nonce }), true);
        let result;
        if (req.url === "/health") result = { leaseReady: true, legacyAttributionProtocol: "cosmic-legacy-attribution/1" };
        else {
          assert.equal(req.url, "/payment/attribute-legacy"); calls++;
          const request = JSON.parse(body); assert.deepEqual(request.evidence, verifiedLegacyEvidence(f.directory, order));
          if (saved) assert.deepEqual(request, saved); else saved = request;
          if (lost) { lost = false; throw Error("lost acknowledgement"); }
          result = { version: 1, state: "ATTRIBUTED", transactionId: order.id, accountId: 7, cashType: 4, amount: 1000,
            accountIncarnation: "12345678-1234-1234-1234-123456789abc", decisionKey: request.decisionKey,
            operatorId: request.operatorId, reason: request.reason, evidenceFingerprint: paymentFingerprint(request.evidence),
            backupDigest: request.evidence.backupDigest, databaseDigest: request.evidence.databaseDigest,
            accountCreatedAt: request.evidence.account.createdAt, paymentCreatedAt: request.evidence.payment.createdAt };
        }
        res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(result));
      } catch { res.writeHead(503, { "Content-Type": "application/json" }).end(JSON.stringify({ code: "GAME_UNAVAILABLE", message: "Fixture unavailable" })); }
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    f.env.GAME_URL = "http://127.0.0.1:" + server.address().port;
    f.save(f.directory, "private/cards.env", Object.entries(f.env).map(([k, v]) => `${k}=${v}`).join("\n") + "\n");
    f.marker.binding["private/cards.env"] = hash(readFileSync(join(f.directory, "private/cards.env")));
    f.save(f.directory, "migration.pending.json", JSON.stringify(f.marker));
    const args = [fileURLToPath(new URL("../tools/payments.mjs", import.meta.url)), "attribute-legacy", "--order-id", order.id,
      "--deployment-directory", f.directory, "--decision-key", "retained-attribution-decision", "--operator-id", "fixture-operator", "--reason", "Verify retained original account."];
    const env = { ...process.env, ...f.env, STATE_DIRECTORY: state }, hashes = () => ["framework.sqlite", "bridge.sqlite"].map((n) => hash(readFileSync(join(state, n)))), before = hashes();
    await assert.rejects(execute(process.execPath, args, { env }), (e) => e.code === 2); assert.equal(calls, 0);
    await assert.rejects(execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env }), (e) => e.code === 1 && e.stderr.includes("GAME_UNAVAILABLE") && !e.stderr.includes(f.env.GAME_SHARED_KEY));
    const result = JSON.parse((await execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env })).stdout);
    assert.equal(result.state, "attributed"); assert.equal(calls, 2); assert.deepEqual(hashes(), before);
    await assert.rejects(execute(process.execPath, [...args, "--confirm-bridge-stopped"], { env: { ...env, GAME_SHARED_KEY: "different" } }), (e) => e.code === 1 && e.stderr.includes("LEGACY_EVIDENCE_REJECTED"));
    assert.equal(calls, 2); assert.deepEqual(hashes(), before);
  } finally { db?.close(); if (server) await new Promise((r) => server.close(r)); rmSync(f.directory, { recursive: true, force: true }); }
});
