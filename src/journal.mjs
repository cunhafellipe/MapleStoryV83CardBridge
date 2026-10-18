import { DatabaseSync } from "node:sqlite";
import { copyFileSync, existsSync, mkdtempSync, rmSync, statSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { hash, fail } from "./protocol.mjs";
function inspectJournal(path) {
  const members = ["", "-wal", "-journal"];
  const stamp = () => members.map((suffix) => {
    try {
      const s = statSync(path + suffix, { bigint: true });
      return [suffix, s.dev, s.ino, s.size, s.mtimeNs, s.ctimeNs].join(":");
    } catch (error) { if (error.code === "ENOENT") return suffix + ":absent"; throw error; }
  }).join("|");
  for (let attempt = 0; attempt < 10; attempt++) {
    const before = stamp();
    let directory, probe;
    try {
      directory = mkdtempSync(join(tmpdir(), "card-journal-probe-"));
      chmodSync(directory, 0o700);
      const target = join(directory, "journal.sqlite");
      for (const suffix of members) if (existsSync(path + suffix)) {
        copyFileSync(path + suffix, target + suffix); chmodSync(target + suffix, 0o600);
      }
      if (stamp() !== before) continue;
      // SQLite can change WAL sidecars even during schema inspection. Recover
      // only the private copy until the source version has been accepted.
      probe = new DatabaseSync(target, { timeout: 5000 });
      const schema = probe.prepare("PRAGMA user_version").get().user_version;
      if (stamp() !== before) continue;
      if (schema > 2) throw Error("Unsupported bridge state schema");
      return;
    } catch (error) { if (stamp() === before) throw error; }
    finally { probe?.close(); if (directory) rmSync(directory, { recursive: true, force: true }); }
  }
  throw Error("Bridge state changed during inspection; retry opening it");
}
export class Journal {
  constructor(path = ":memory:") {
    if (path !== ":memory:" && existsSync(path)) inspectJournal(path);
    this.db = new DatabaseSync(path, { timeout: 5000 });
    try {
    this.db.exec("BEGIN IMMEDIATE");
    const schema = this.db.prepare("PRAGMA user_version").get().user_version;
    if (schema > 2) throw Error("Unsupported bridge state schema");
    this.db.exec(
      "CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(account_id,request_key)) STRICT; CREATE TABLE IF NOT EXISTS registrations(code_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;",
    );
    this.db
      .exec(`CREATE TABLE IF NOT EXISTS administrator_grants(account_id INTEGER PRIMARY KEY, name TEXT NOT NULL, granted_at TEXT NOT NULL) STRICT;
      CREATE TABLE IF NOT EXISTS operator_activity(id INTEGER PRIMARY KEY, account_id INTEGER, action TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
      CREATE TABLE IF NOT EXISTS catalog_previews(id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
      CREATE TABLE IF NOT EXISTS registration_diagnostics(code_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_attempt_at TEXT NOT NULL, last_error TEXT, next_attempt_at TEXT) STRICT;
      CREATE TABLE IF NOT EXISTS readiness_probe(id INTEGER PRIMARY KEY CHECK(id=1),checked_at TEXT NOT NULL) STRICT;
      CREATE TABLE IF NOT EXISTS payment_discovery(id TEXT PRIMARY KEY,source TEXT NOT NULL CHECK(source IN ('game','framework')),first_seen TEXT NOT NULL,checked_at TEXT NOT NULL) STRICT;
      PRAGMA user_version=2;`);
    // Earlier installations stored NX Credit implicitly. Persist that choice before replay.
      for (const row of this.db.prepare("SELECT id,body FROM orders").all()) {
        const value = JSON.parse(row.body);
        if (Object.hasOwn(value.input, "cashType")) continue;
        value.input.cashType = 1;
        value.quote.cashType = 1;
        if (value.payment) value.payment.cashType = 1;
        this.db
          .prepare("UPDATE orders SET input_hash=?,body=? WHERE id=?")
          .run(
            hash(JSON.stringify(value.input)),
            JSON.stringify(value),
            row.id,
          );
      }
      this.db.exec("COMMIT");
      this.db.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;");
    } catch (e) {
      try { this.db.exec("ROLLBACK"); } catch {}
      this.db.close();
      throw e;
    }
  }
  create(accountId, requestKey, input, userId, quote, accountName = null) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      let row = this.db
        .prepare("SELECT * FROM orders WHERE account_id=? AND request_key=?")
        .get(accountId, requestKey);
      const digest = hash(JSON.stringify(input));
      if (row && row.input_hash !== digest)
        fail(
          "IDEMPOTENCY_CONFLICT",
          "This request identifier has different purchase details.",
          409,
        );
      if (!row) {
        const id = hash(accountId + ":" + requestKey),
          body = {
            id,
            accountId,
            accountName,
            userId,
            input,
            quote,
            createdAt: new Date().toISOString(),
            result: null,
            financialVersion: 1,
          };
        this.db
          .prepare("INSERT INTO orders VALUES(?,?,?,?,?,?)")
          .run(
            id,
            accountId,
            requestKey,
            digest,
            JSON.stringify(body),
            "pending",
          );
        row = { body: JSON.stringify(body), state: "pending" };
      }
      this.db.exec("COMMIT");
      return { ...JSON.parse(row.body), state: row.state };
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  save(order, state) {
    const now = new Date().toISOString();
    if (order.state !== state) order.stateChangedAt = now;
    order.state = state;
    order.updatedAt = now;
    this.db
      .prepare("UPDATE orders SET body=?,state=? WHERE id=?")
      .run(JSON.stringify({ ...order, state }), state, order.id);
  }
  administrator(accountId) {
    return !!this.db
      .prepare("SELECT 1 FROM administrator_grants WHERE account_id=?")
      .get(accountId);
  }
  grants() {
    return this.db
      .prepare(
        "SELECT account_id AS accountId,name,granted_at AS grantedAt FROM administrator_grants ORDER BY account_id",
      )
      .all();
  }
  grant(person, enabled) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const previous = this.administrator(person.accountId);
      if (enabled)
        this.db
          .prepare(
            "INSERT INTO administrator_grants VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET name=excluded.name",
          )
          .run(person.accountId, person.name, new Date().toISOString());
      else
        this.db
          .prepare("DELETE FROM administrator_grants WHERE account_id=?")
          .run(person.accountId);
      if (previous !== enabled)
        this.activity(
          null,
          enabled ? "administrator.granted" : "administrator.revoked",
          { accountId: person.accountId, name: person.name },
        );
      this.db.exec("COMMIT");
      return {
        ...person,
        role: enabled ? "administrator" : "collector",
        changed: previous !== enabled,
      };
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
  activity(accountId, action, details) {
    this.db
      .prepare(
        "INSERT INTO operator_activity(account_id,action,body,created_at) VALUES(?,?,?,?)",
      )
      .run(
        accountId,
        action,
        JSON.stringify(details),
        new Date().toISOString(),
      );
  }
  activityPage({ after = 0, limit = 25 } = {}) {
    const rows = this.db
      .prepare(
        "SELECT * FROM operator_activity WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
      )
      .all(Number(after), Number(after), limit + 1);
    return {
      items: rows
        .slice(0, limit)
        .map((r) => ({
          id: r.id,
          accountId: r.account_id,
          action: r.action,
          ...JSON.parse(r.body),
          createdAt: r.created_at,
        })),
      next: rows.length > limit ? String(rows[limit - 1].id) : null,
    };
  }
  preview(id, accountId, value) {
    this.db
      .prepare("DELETE FROM catalog_previews WHERE created_at<?")
      .run(new Date(Date.now() - 86400000).toISOString());
    this.db
      .prepare("INSERT INTO catalog_previews VALUES(?,?,?,?)")
      .run(id, accountId, JSON.stringify(value), new Date().toISOString());
    this.db
      .prepare(
        "DELETE FROM catalog_previews WHERE account_id=? AND id NOT IN (SELECT id FROM catalog_previews WHERE account_id=? ORDER BY rowid DESC LIMIT 10)",
      )
      .run(accountId, accountId);
  }
  getPreview(id, accountId) {
    const row = this.db
      .prepare(
        "SELECT body,created_at FROM catalog_previews WHERE id=? AND account_id=?",
      )
      .get(id, accountId);
    return row && Date.parse(row.created_at) > Date.now() - 86400000
      ? JSON.parse(row.body)
      : null;
  }
  order(id) {
    const r = this.db
      .prepare("SELECT body,state FROM orders WHERE id=?")
      .get(id);
    return r ? { ...JSON.parse(r.body), state: r.state } : null;
  }
  orderPage({ after = "", limit = 25, search = "" } = {}) {
    const rows = this.db
      .prepare(
        "SELECT rowid,body,state FROM orders WHERE (?='' OR rowid<?) AND (?='' OR id=? OR CAST(account_id AS TEXT)=?) ORDER BY rowid DESC LIMIT ?",
      )
      .all(after, Number(after), search, search, search, limit + 1);
    return {
      items: rows
        .slice(0, limit)
        .map((r) => ({ ...JSON.parse(r.body), state: r.state })),
      next: rows.length > limit ? String(rows[limit - 1].rowid) : null,
    };
  }
  attempt(order) {
    order.attempts = (order.attempts ?? 0) + 1;
    order.lastAttemptAt = new Date().toISOString();
    this.save(order, order.state);
  }
  failed(order, error) {
    order.lastError = /^[A-Z][A-Z0-9_]{0,80}$/.test(error.code ?? "")
      ? error.code
      : "UNAVAILABLE";
    order.nextAttemptAt = new Date(
      Date.now() +
        Math.min(300000, 1000 * 2 ** Math.min(order.attempts ?? 1, 8)),
    ).toISOString();
    this.save(order, order.state);
  }
  diagnostics() {
    const rows = this.db
      .prepare(
        "SELECT COUNT(*) AS count,MIN(json_extract(body,'$.createdAt')) AS oldest FROM orders WHERE state NOT IN ('complete','rejected','refunded')",
      )
      .get();
    return {
      unresolvedOrders: rows.count,
      orphanedPayments: this.db.prepare("SELECT COUNT(*) AS count FROM payment_discovery").get().count,
      oldestOrderAt: rows.oldest,
      registrations: this.db
        .prepare(
          "SELECT state,COUNT(*) AS count FROM registrations GROUP BY state",
        )
        .all(),
    };
  }
  observePayment(id, source, orphaned) {
    if (!/^[a-f0-9]{64}$/.test(id) || !["game", "framework"].includes(source))
      fail("INVALID_PAYMENT_DISCOVERY", "Invalid payment recovery identity.", 503);
    if (!orphaned) {
      this.db.prepare("DELETE FROM payment_discovery WHERE id=? AND source=?").run(id, source);
      return;
    }
    if (!this.db.prepare("SELECT 1 FROM payment_discovery WHERE id=?").get(id) &&
        this.db.prepare("SELECT COUNT(*) AS count FROM payment_discovery").get().count >= 1000)
      fail("PAYMENT_DISCOVERY_CAPACITY", "Too many unmatched payment identities require operator reconciliation.", 503);
    const now = new Date().toISOString();
    this.db.prepare("INSERT INTO payment_discovery VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at").run(id, source, now, now);
  }
  paymentDiscoveryPage({ after = "", limit = 25 } = {}) {
    const rows = this.db.prepare("SELECT id,source,first_seen AS firstSeen,checked_at AS checkedAt FROM payment_discovery WHERE id>? ORDER BY id LIMIT ?").all(after, limit + 1);
    return { items: rows.slice(0, limit), next: rows.length > limit ? rows[limit - 1].id : null };
  }
  writable() {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db
        .prepare(
          "INSERT INTO readiness_probe VALUES(1,?) ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at",
        )
        .run(new Date().toISOString());
      this.db.exec("COMMIT");
      return true;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
  pending() {
    return this.db
      .prepare(
        "SELECT body,state FROM orders WHERE state NOT IN ('complete','rejected','refunded')",
      )
      .all()
      .map((r) => ({ ...JSON.parse(r.body), state: r.state }));
  }
  dueOrders({ force = false, limit = 20 } = {}) {
    return this.db.prepare("SELECT body,state FROM orders WHERE state NOT IN ('complete','rejected','refunded','quarantined') AND (? OR json_extract(body,'$.nextAttemptAt') IS NULL OR json_extract(body,'$.nextAttemptAt')<=?) ORDER BY COALESCE(json_extract(body,'$.lastAttemptAt'),'') ASC,rowid ASC LIMIT ?")
      .all(force ? 1 : 0, new Date().toISOString(), limit)
      .map((r) => ({ ...JSON.parse(r.body), state: r.state }));
  }
  find(accountId, requestKey) {
    const row = this.db
      .prepare(
        "SELECT body,state FROM orders WHERE account_id=? AND request_key=?",
      )
      .get(accountId, requestKey);
    return row ? { ...JSON.parse(row.body), state: row.state } : null;
  }
  due(codeId, force = false) {
    const row = this.db
      .prepare("SELECT updated_at FROM registrations WHERE code_id=?")
      .get(codeId);
    return force || !row || Date.now() - Date.parse(row.updated_at) >= 60000;
  }
  checked(codeId) {
    this.db
      .prepare("UPDATE registrations SET updated_at=? WHERE code_id=?")
      .run(new Date().toISOString(), codeId);
  }
  orders(accountId) {
    return this.db
      .prepare(
        "SELECT body,state FROM orders WHERE account_id=? ORDER BY rowid DESC LIMIT 100",
      )
      .all(accountId)
      .map((r) => ({ ...JSON.parse(r.body), state: r.state }));
  }
  registration(codeId) {
    return (
      this.db
        .prepare("SELECT state FROM registrations WHERE code_id=?")
        .get(codeId)?.state ?? "pending"
    );
  }
  register(codeId, state) {
    this.db
      .prepare(
        "INSERT INTO registrations VALUES(?,?,?) ON CONFLICT(code_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
      )
      .run(codeId, state, new Date().toISOString());
    this.db
      .prepare(
        "UPDATE registration_diagnostics SET last_error=NULL,next_attempt_at=NULL WHERE code_id=?",
      )
      .run(codeId);
  }
  registrationAttempt(codeId) {
    this.db
      .prepare(
        "INSERT INTO registration_diagnostics VALUES(?,1,?,NULL,NULL) ON CONFLICT(code_id) DO UPDATE SET attempts=attempts+1,last_attempt_at=excluded.last_attempt_at",
      )
      .run(codeId, new Date().toISOString());
  }
  registrationFailure(codeId, error) {
    const attempts =
      this.db
        .prepare(
          "SELECT attempts FROM registration_diagnostics WHERE code_id=?",
        )
        .get(codeId)?.attempts ?? 1;
    const code = /^[A-Z][A-Z0-9_]{0,80}$/.test(error.code ?? "")
      ? error.code
      : "UNAVAILABLE";
    this.db
      .prepare(
        "UPDATE registration_diagnostics SET last_error=?,next_attempt_at=? WHERE code_id=?",
      )
      .run(
        code,
        new Date(
          Date.now() + Math.min(300000, 1000 * 2 ** Math.min(attempts, 8)),
        ).toISOString(),
        codeId,
      );
  }
  registrationDue(codeId) {
    const r = this.db
      .prepare(
        "SELECT next_attempt_at FROM registration_diagnostics WHERE code_id=?",
      )
      .get(codeId);
    return !r?.next_attempt_at || Date.parse(r.next_attempt_at) <= Date.now();
  }
  registrationPage({ after = "", limit = 25 } = {}) {
    const rows = this.db
      .prepare(
        "SELECT code_id AS id,attempts,last_attempt_at AS lastAttemptAt,last_error AS lastError,next_attempt_at AS nextAttemptAt FROM registration_diagnostics WHERE (?='' OR code_id>?) ORDER BY code_id LIMIT ?",
      )
      .all(after, after, limit + 1);
    return {
      items: rows
        .slice(0, limit)
        .map((r) => ({ ...r, state: this.registration(r.id) })),
      next: rows.length > limit ? rows[limit - 1].id : null,
    };
  }
  close() {
    this.db.close();
  }
}
