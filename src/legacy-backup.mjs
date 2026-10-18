import { createHash } from "node:crypto";
import { openSync, closeSync, readSync, fstatSync, lstatSync, readFileSync, realpathSync, readdirSync, mkdtempSync, copyFileSync, rmSync, chmodSync } from "node:fs";
import { resolve, join, relative, isAbsolute } from "node:path";
import { tmpdir } from "node:os";
import { DatabaseSync } from "node:sqlite";
import { fail } from "./protocol.mjs";
import { canonicalPayment, paymentFingerprint } from "./payment-authority.mjs";

const rejected = () => fail("LEGACY_EVIDENCE_REJECTED", "The retained backup or closed migration binding is unsupported or differs.", 409);
const digest = /^[a-f0-9]{64}$/;
const bindings = ["installation.json", "images.lock.json", "private/cards.env", "private/game.env", "private/database.env", "private/config.yaml", "private/catalog.json"];
const required = [...bindings, "database.sql", "state.tar.gz", "compose.json", "runtime-images.tar", "runtime-images.json", "support/nginx.conf"];
const allowed = new Set([...required, "media.tar.gz", "private/test-account.json"]);

function plain(value) { return value && Object.getPrototypeOf(value) === Object.prototype; }
function file(root, name) {
  const path = resolve(root, name), rel = relative(root, path);
  if (!rel || rel.startsWith("..") || isAbsolute(rel)) rejected();
  let cursor = root;
  for (const part of rel.split(/[\\/]/)) {
    cursor = join(cursor, part);
    if (lstatSync(cursor).isSymbolicLink()) rejected();
  }
  if (!lstatSync(path).isFile()) rejected();
  return path;
}
function bytes(root, name, limit = 65536) {
  const path = file(root, name);
  if (lstatSync(path).size > limit) rejected();
  return readFileSync(path);
}
function json(root, name) { return JSON.parse(bytes(root, name).toString("utf8")); }
function fileDigest(root, name) {
  const fd = openSync(file(root, name), "r"), hash = createHash("sha256"), buffer = Buffer.alloc(1024 * 1024);
  try {
    const before = fstatSync(fd);
    for (let count; (count = readSync(fd, buffer)) > 0;) hash.update(buffer.subarray(0, count));
    const after = fstatSync(fd);
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ino !== after.ino) rejected();
    return hash.digest("hex");
  } finally { closeSync(fd); }
}
function members(root, prefix = "") {
  const result = [];
  for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
    const name = prefix + entry.name;
    if (entry.isSymbolicLink()) rejected();
    if (entry.isDirectory()) {
      if (!["private", "support"].includes(name)) rejected();
      result.push(...members(root, name + "/"));
    } else if (entry.isFile()) result.push(name);
    else rejected();
  }
  return result;
}

// Read a qualified dump as data; never execute it against an operating database.
function* statements(sql) {
  let part = "", quote = null;
  for (let i = 0; i < sql.length; i++) {
    const c = sql[i], n = sql[i + 1];
    if (quote) {
      part += c;
      if (c === "\\" && quote !== "`") { if (++i >= sql.length) rejected(); part += sql[i]; }
      else if (c === quote) {
        if (n === quote) part += sql[++i];
        else quote = null;
      }
    } else if (c === "'" || c === '"' || c === "`") { quote = c; part += c; }
    else if ((c === "-" && n === "-" && /\s/.test(sql[i + 2] ?? "")) || c === "#") {
      while (i < sql.length && sql[i] !== "\n") i++;
      part += " ";
    } else if (c === "/" && n === "*") {
      const end = sql.indexOf("*/", i + 2);
      if (end < 0) rejected();
      const comment = sql.slice(i + 2, end);
      if (comment.startsWith("!")) {
        const executable = comment.replace(/^!\d{5,6}\s*/, "");
        part += " " + executable + " ";
      } else part += " ";
      i = end + 1;
    } else if (c === ";") { if (part.trim()) yield part.trim(); part = ""; }
    else part += c;
  }
  if (quote || part.trim()) rejected();
}
function tokens(statement, limit = Infinity) {
  const values = [];
  for (let i = 0; i < statement.length && values.length < limit;) {
    if (/\s/.test(statement[i])) { i++; continue; }
    const start = i, quote = statement[i];
    if (quote === "'" || quote === "`") {
      let value = "", closed = false; i++;
      for (; i < statement.length; i++) {
        const c = statement[i];
        if (c === "\\" && quote === "'") {
          const escape = statement[++i], escapes = { "0": "\0", b: "\b", n: "\n", r: "\r", t: "\t", Z: "\x1a", "\\": "\\", "'": "'", '"': '"', "%": "\\%", _: "\\_" };
          if (!Object.hasOwn(escapes, escape)) rejected();
          value += escapes[escape];
        } else if (c === quote) {
          if (statement[i + 1] === quote) { value += quote; i++; }
          else { closed = true; i++; break; }
        } else value += c;
      }
      if (!closed) rejected();
      values.push({ type: quote === "`" ? "identifier" : "string", value });
    } else {
      const word = /^0x[0-9a-f]+|^[A-Za-z_@][A-Za-z0-9_@.$]*|^-?\d+/i.exec(statement.slice(i));
      if (word) { values.push({ type: "word", value: word[0] }); i += word[0].length; }
      else { values.push({ type: "symbol", value: statement[i++] }); }
    }
    if (i === start) rejected();
  }
  return values;
}
const lower = (token) => token?.value.toLowerCase();
function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)) rejected();
  const canonical = value.replace(" ", "T") + ".000Z", date = new Date(canonical);
  if (!Number.isFinite(+date) || date.toISOString() !== canonical || +date <= 0) rejected();
  return canonical;
}
function integer(token, min = 0) {
  if (token?.type !== "word" || !/^(0|[1-9][0-9]*)$/.test(token.value)) rejected();
  const value = Number(token.value);
  if (!Number.isSafeInteger(value) || value < min || value > 2147483647) rejected();
  return value;
}
function schema(t, table) {
  if (lower(t[0]) !== "create" || lower(t[1]) !== "table" || lower(t[2]) !== table || t[3]?.value !== "(") rejected();
  const definitions = []; let current = [], depth = 0, end = -1;
  for (let i = 4; i < t.length; i++) {
    if (t[i].value === ")" && depth === 0) { definitions.push(current); end = i; break; }
    if (t[i].value === "," && depth === 0) { definitions.push(current); current = []; continue; }
    if (t[i].value === "(") depth++;
    if (t[i].value === ")") depth--;
    current.push(t[i]);
  }
  if (end < 0 || !t.slice(end + 1).some((x, i, rest) => lower(x) === "engine" && rest[i + 1]?.value === "=" && lower(rest[i + 2]) === "innodb")) rejected();
  const columns = definitions.filter((d) => d[0]?.type === "identifier");
  const names = columns.map((d) => lower(d[0]));
  if (new Set(names).size !== names.length) rejected();
  const created = table === "accounts" ? "createdat" : "created_at", id = table === "accounts" ? "id" : "order_id";
  const creation = columns.find((d) => lower(d[0]) === created)?.slice(1).map(lower).join(" ");
  if (!/^timestamp(?: \( 0 \))? not null default current_timestamp(?: \( (?:0 )?\))?$/i.test(creation ?? "")) rejected();
  if (!definitions.some((d) => d.map(lower).join(" ") === `primary key ( ${id} )`)) rejected();
  const needed = table === "accounts" ? [id, created] : [id, "account_id", "cash_type", "amount", "balance_after", created];
  if (!needed.every((name) => names.includes(name))) rejected();
  for (const name of needed.filter((name) => ![created, "order_id"].includes(name))) {
    const definition = columns.find((d) => lower(d[0]) === name).slice(1).map(lower).join(" ");
    if (!/^int(?: \( \d+ \))? not null(?: |$)/.test(definition)) rejected();
  }
  if (table === "card_bridge_payments" && !/^char \( 64 \) (?:character set \w+ )?(?:collate \w+ )?not null(?: |$)/.test(columns.find((d) => lower(d[0]) === id).slice(1).map(lower).join(" "))) rejected();
  return names;
}

export function projectLegacyDump(sql, order) {
  if (typeof sql !== "string" || Buffer.byteLength(sql) > 128 * 1024 * 1024 || /(?:^|\n)\s*DELIMITER\b/i.test(sql)) rejected();
  const columns = new Map(), records = new Map(); let utc = false, standardEscapes = false, cosmic = false;
  for (const statement of statements(sql)) {
    if (/^USE\b/i.test(statement)) {
      if (!/^USE `cosmic`$/i.test(statement)) rejected();
      cosmic = true; continue;
    }
    if (/^SET\b/i.test(statement)) {
      if (/\bSQL_MODE\s*=/i.test(statement)) {
        if (!/\bSQL_MODE\s*=\s*'NO_AUTO_VALUE_ON_ZERO'\s*$/i.test(statement)) {
          if (/\bSQL_MODE\s*=\s*@OLD_SQL_MODE\s*$/i.test(statement)) standardEscapes = false;
          else rejected();
        } else standardEscapes = true;
      }
      if (/\bTIME_ZONE\s*=/i.test(statement)) {
        if (/^SET\s+TIME_ZONE\s*=\s*'\+00:00'$/i.test(statement)) utc = true;
        else if (/^SET\s+TIME_ZONE\s*=\s*@OLD_TIME_ZONE$/i.test(statement)) utc = false;
        else if (!/^SET\s+@OLD_TIME_ZONE\s*=\s*@@TIME_ZONE$/i.test(statement)) rejected();
      }
      continue;
    }
    const prefix = tokens(statement, 7), operation = lower(prefix[0]);
    // A dump contains inserts and schema definitions, never live row mutations.
    // Refuse ambiguous multi-table mutations even when their first table differs.
    if (["update", "delete", "truncate", "rename", "replace", "call", "load"].includes(operation)) rejected();
    let targetIndex;
    if (["create", "drop", "alter"].includes(operation) && lower(prefix[1]) === "table")
      targetIndex = lower(prefix[2]) === "if" ? operation === "create" ? 5 : 4 : 2;
    else if (operation === "insert") {
      const into = prefix.findIndex((x) => lower(x) === "into");
      if (into < 0) rejected();
      targetIndex = into + 1;
    }
    else if (operation === "lock" && lower(prefix[1]) === "tables") targetIndex = 2;
    else continue;
    const target = prefix[targetIndex]?.value.split(".").at(-1)?.toLowerCase(),
      qualifiedTarget = prefix[targetIndex + 1]?.value === "." ? lower(prefix[targetIndex + 2]) : target;
    if (!["accounts", "card_bridge_payments"].includes(qualifiedTarget)) continue;
    if (!cosmic) rejected();
    const t = tokens(statement), table = lower(t[2]);
    if (lower(t[0]) === "create" && lower(t[1]) === "table" && ["accounts", "card_bridge_payments"].includes(table)) {
      if (columns.has(table)) rejected();
      columns.set(table, schema(t, table)); continue;
    }
    if (lower(t[0]) === "insert" && lower(t[1]) === "into" && ["accounts", "card_bridge_payments"].includes(table)) {
      if (!utc || !standardEscapes || !columns.has(table) || lower(t[3]) !== "values") rejected();
      const names = columns.get(table); let i = 4;
      while (i < t.length) {
        if (t[i++]?.value !== "(") rejected();
        const row = [];
        for (;;) {
          const value = t[i++]; if (!value || ["(", ")", ","].includes(value.value) && value.type === "symbol") rejected();
          row.push(value);
          if (t[i]?.value === ")") { i++; break; }
          if (t[i++]?.value !== ",") rejected();
        }
        if (row.length !== names.length) rejected();
        const data = Object.fromEntries(names.map((name, index) => [name, row[index]]));
        const selected = table === "accounts" ? integer(data.id, 1) === order.accountId : data.order_id?.type === "string" && data.order_id.value === order.id;
        if (selected) {
          if (records.has(table)) rejected();
          records.set(table, data);
        }
        if (i < t.length && t[i++]?.value !== ",") rejected();
      }
      continue;
    }
    // Standard mysqldump lock/index and initial drop statements carry no row data.
    if (/^LOCK TABLES `(?:accounts|card_bridge_payments)` WRITE$/i.test(statement) ||
        /^ALTER TABLE `(?:accounts|card_bridge_payments)` (?:DISABLE|ENABLE) KEYS$/i.test(statement) ||
        /^DROP TABLE IF EXISTS `(?:accounts|card_bridge_payments)`$/i.test(statement) && !columns.has(lower(t[4]))) continue;
    rejected();
  }
  const a = records.get("accounts"), p = records.get("card_bridge_payments");
  if (!a || !p || integer(p.account_id, 1) !== order.accountId || integer(p.cash_type, 1) !== order.input.cashType || integer(p.amount, 1) !== order.quote.price.amount) rejected();
  const accountCreatedAt = timestamp(a.createdat.value), paymentCreatedAt = timestamp(p.created_at.value);
  if (a.createdat.type !== "string" || p.created_at.type !== "string" || accountCreatedAt >= paymentCreatedAt) rejected();
  if (p.payment_state && (p.payment_state.type !== "string" || p.payment_state.value !== "LEGACY_PAID") ||
      ["intent", "account_incarnation", "fingerprint", "preparation_id", "purchase_id", "compensation_id"].some((name) => p[name] && (p[name].type !== "word" || p[name].value !== "NULL"))) rejected();
  return { account: { accountId: order.accountId, createdAt: accountCreatedAt },
    payment: { transactionId: order.id, accountId: order.accountId, cashType: order.input.cashType,
      amount: order.quote.price.amount, balanceAfter: integer(p.balance_after), createdAt: paymentCreatedAt } };
}

export function verifiedLegacyEvidence(deploymentDirectory, order) {
  try {
    const directory = resolve(deploymentDirectory);
    if (realpathSync(directory) !== directory || lstatSync(directory).isSymbolicLink()) rejected();
    if (["upgrade.pending.json", "setup.pending.json", "recovery.pending.json"].some((name) => {
      try { lstatSync(join(directory, name)); return true; } catch (e) { if (e.code === "ENOENT") return false; throw e; }
    })) rejected();
    const markerBytes = bytes(directory, "migration.pending.json"), markerDigest = createHash("sha256").update(markerBytes).digest("hex"),
      marker = JSON.parse(markerBytes.toString("utf8")), meta = json(directory, "installation.json");
    if (marker.format !== 1 || marker.kind !== "legacy" || !["migrating", "validating"].includes(marker.phase) ||
        !digest.test(marker.backupDigest ?? "") || canonicalPayment(meta) !== canonicalPayment(marker.meta) ||
        meta.directory !== directory || !/^cosmic-cards-[a-f0-9]{10}$/.test(meta.project ?? "") ||
        !plain(marker.binding) || Object.keys(marker.binding).sort().join() !== [...bindings].sort().join() ||
        !plain(marker.oldBinding) || Object.keys(marker.oldBinding).sort().join() !== [...bindings].sort().join()) rejected();
    for (const name of bindings) if (!digest.test(marker.binding[name] ?? "") || fileDigest(directory, name) !== marker.binding[name]) rejected();
    if (!plain(marker.candidate) || !plain(marker.candidate.services?.network)) rejected();
    const closed = JSON.parse(JSON.stringify(marker.candidate)); closed.services.network.ports = [];
    if (canonicalPayment(json(directory, "compose.json")) !== canonicalPayment(closed)) rejected();
    const backup = resolve(marker.backup);
    if (realpathSync(backup) !== backup || lstatSync(backup).isSymbolicLink() || !backup.startsWith(join(directory, "backups") + (process.platform === "win32" ? "\\" : "/"))) rejected();
    const sums = json(backup, "checksums.json");
    if (fileDigest(backup, "checksums.json") !== marker.backupDigest || !plain(sums) ||
        required.some((name) => !Object.hasOwn(sums, name)) || Object.keys(sums).some((name) => !allowed.has(name)) ||
        members(backup).sort().join() !== [...Object.keys(sums), "checksums.json"].sort().join()) rejected();
    for (const [name, expected] of Object.entries(sums)) if (!digest.test(expected ?? "") || fileDigest(backup, name) !== expected) rejected();
    const oldMeta = json(backup, "installation.json");
    if (canonicalPayment(oldMeta) !== canonicalPayment(marker.oldMeta) || oldMeta.project !== meta.project || oldMeta.directory !== directory) rejected();
    for (const name of bindings) if (marker.oldBinding[name] !== sums[name]) rejected();
    const source = bytes(backup, "database.sql", 128 * 1024 * 1024);
    // Tie the bytes parsed below to the manifest, including a concurrent replacement.
    if (createHash("sha256").update(source).digest("hex") !== sums["database.sql"]) rejected();
    const projection = projectLegacyDump(new TextDecoder("utf-8", { fatal: true }).decode(source), order);
    if (fileDigest(directory, "migration.pending.json") !== markerDigest || fileDigest(backup, "checksums.json") !== marker.backupDigest) rejected();
    for (const name of bindings) if (fileDigest(directory, name) !== marker.binding[name]) rejected();
    return { version: 1, kind: "managed-cosmic-backup/1", schema: "cosmic-createdat-timestamp/1",
      backupDigest: marker.backupDigest, databaseDigest: sums["database.sql"], ...projection };
  } catch (error) { if (error.code === "LEGACY_EVIDENCE_REJECTED") throw error; rejected(); }
}

export function requireManagedEnvironment(deploymentDirectory, env) {
  try {
    const text = bytes(resolve(deploymentDirectory), "private/cards.env").toString("utf8"), values = new Map();
    for (const line of text.split(/\r?\n/)) {
      if (!line || line.startsWith("#")) continue;
      const match = /^([A-Z][A-Z0-9_]*)=(.*)$/.exec(line);
      if (!match || values.has(match[1])) rejected();
      values.set(match[1], match[2]);
    }
    for (const name of ["GAME_URL", "GAME_SHARED_KEY", "STATE_KEY", "CODE_ENCRYPTION_KEY", "CODE_INDEX_KEY"])
      if (!values.get(name) || env[name] !== values.get(name)) rejected();
  } catch (error) { if (error.code === "LEGACY_EVIDENCE_REJECTED") throw error; rejected(); }
}

export function retainedJournalOrder(stateDirectory, orderId) {
  let temporary, db;
  try {
    const root = resolve(stateDirectory); if (!digest.test(orderId ?? "")) rejected();
    temporary = mkdtempSync(join(tmpdir(), "card-attribution-")); chmodSync(temporary, 0o700);
    const before = new Map(); let total = 0;
    for (const name of ["bridge.sqlite", "bridge.sqlite-wal"]) {
      try {
        const path = file(root, name), info = lstatSync(path);
        if ((total += info.size) > 256 * 1024 * 1024) rejected();
        const hash = fileDigest(root, name); copyFileSync(path, join(temporary, name)); chmodSync(join(temporary, name), 0o600);
        if (fileDigest(temporary, name) !== hash) rejected(); before.set(name, hash);
      } catch (error) { if (name.endsWith("-wal") && error.code === "ENOENT") continue; throw error; }
    }
    db = new DatabaseSync(join(temporary, "bridge.sqlite"));
    if (![1, 2].includes(db.prepare("PRAGMA user_version").get().user_version) || db.prepare("PRAGMA quick_check").get().quick_check !== "ok") rejected();
    const row = db.prepare("SELECT id,account_id,body,state FROM orders WHERE id=?").get(orderId);
    if (!row) rejected();
    const order = JSON.parse(row.body);
    if (order.id !== row.id || order.accountId !== row.account_id) rejected();
    for (const [name, hash] of before) if (fileDigest(root, name) !== hash) rejected();
    if (!before.has("bridge.sqlite-wal")) {
      try { lstatSync(join(root, "bridge.sqlite-wal")); rejected(); } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return { ...order, state: row.state };
  } catch (error) { if (error.code === "LEGACY_EVIDENCE_REJECTED") throw error; rejected(); }
  finally { db?.close(); if (temporary) rmSync(temporary, { recursive: true, force: true }); }
}

export async function attributeLegacyPayment({ library, deploymentDirectory, orderId, decisionKey, operatorId, reason }) {
  if (!digest.test(orderId ?? "") || typeof decisionKey !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(decisionKey) ||
      typeof operatorId !== "string" || !/^[A-Za-z0-9._:@-]{1,128}$/.test(operatorId) ||
      [operatorId, reason].some((value) => typeof value !== "string" || !value.trim() || /[\u0000-\u001f\u007f]/.test(value)) ||
      Array.from(operatorId).length > 128 || Array.from(reason).length > 500) rejected();
  const order = library.journal.order(orderId);
  if (!order || !Number.isSafeInteger(order.accountId) || order.accountId < 1) rejected();
  const evidence = verifiedLegacyEvidence(deploymentDirectory, order);
  const health = await library.game("/health", {});
  if (health.leaseReady !== true || health.legacyAttributionProtocol !== "cosmic-legacy-attribution/1") rejected();
  const request = { transactionId: order.id, accountId: order.accountId, cashType: order.input.cashType,
    amount: order.quote.price.amount, decisionKey, operatorId, reason, evidence };
  const result = await library.game("/payment/attribute-legacy", request);
  const expected = { version: 1, state: "ATTRIBUTED", transactionId: order.id, accountId: order.accountId,
    cashType: order.input.cashType, amount: order.quote.price.amount, accountIncarnation: result?.accountIncarnation,
    decisionKey, operatorId, reason, evidenceFingerprint: paymentFingerprint(evidence),
    backupDigest: evidence.backupDigest, databaseDigest: evidence.databaseDigest,
    accountCreatedAt: evidence.account.createdAt, paymentCreatedAt: evidence.payment.createdAt };
  if (!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(result?.accountIncarnation ?? "") || canonicalPayment(result) !== canonicalPayment(expected)) rejected();
  return { orderId, state: "attributed", decisionKey, evidenceFingerprint: expected.evidenceFingerprint };
}
