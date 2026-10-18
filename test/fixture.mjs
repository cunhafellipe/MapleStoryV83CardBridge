import { readFileSync } from "node:fs";
import {
  CardFramework,
  MemoryStore,
  createCodeVault,
} from "@digital-card/framework";
import { Journal } from "../src/journal.mjs";
import { Library, operator } from "../src/library.mjs";
import { generateSeriesOneCode } from "../src/series-one.mjs";
import { fail } from "../src/protocol.mjs";
import { PaymentAuthority, paymentProviderId, paymentFingerprint, paymentIntent } from "../src/payment-authority.mjs";
export const catalog = JSON.parse(
  readFileSync(new URL("./reward-catalog.json", import.meta.url)),
);
export function fixture({
  store = new MemoryStore(),
  journal = new Journal(),
  catalog: inputCatalog = catalog,
  generator = generateSeriesOneCode,
  acceptedCashTypes = [1, 2, 4],
  coreOptions = {},
} = {}) {
  const paymentAuthority = new PaymentAuthority();
  const core = new CardFramework({
    ...coreOptions,
    store,
    codeVault: createCodeVault({
      activeKeyId: "test",
      keys: { test: Buffer.alloc(32, 1) },
      indexKey: Buffer.alloc(32, 2),
    }),
    codeGenerators: { "series-one": generator },
    externalPurchaseProviders: { [paymentProviderId]: paymentAuthority },
  });
  if (!store.read((s) => s.catalog))
    core.publishCatalog(operator, inputCatalog);
  if (inputCatalog.variants.some((v) => v.codes?.length))
    core.configureCodePool(operator, {
      key: "pool-test",
      pool: {
        id: "v83.series-one",
        providerId: "maplestory.v83",
        name: "Series One",
        generator: "series-one",
        normalization: "upper-trim",
      },
    });
  const payments = new Map(),
    registrations = new Map(),
    balances = new Map([
      [1, 10000],
      [2, 10000],
    ]);
  const maplePoints = new Map([
      [1, 10000],
      [2, 10000],
    ]),
    nxPrepaid = new Map([
      [1, 10000],
      [2, 10000],
    ]),
    wallets = new Map([
      [1, balances],
      [2, maplePoints],
      [4, nxPrepaid],
    ]);
  const faults = { lostDebit: false, registration: false, lostRefund: false, lostFinalize: false };
  let debitCount = 0, refundCount = 0;
  const game = async (path, value) => {
    if (path === "/health") {
      if (faults.health)
        fail("GAME_UNAVAILABLE", "Fixture game is unavailable", 503);
      return {
        ok: true,
        leaseReady: true,
        protocol: "v83-card-bridge/1",
        paymentProtocol: "cosmic-payments/1",
        callbackReady: true,
        sessionSource: faults.sessionSource ?? "bridge",
        acceptedCashTypes,
      };
    }
    if (path === "/session") {
      if (value.tokenHash !== "valid")
        fail("UNAUTHENTICATED", "Invalid session", 401);
      return { accountId: 1, name: "Collector" };
    }
    if (path === "/wallet")
      return {
        nx: balances.get(value.accountId),
        acceptedCashTypes,
        balances: [
          {
            cashType: 1,
            name: "NX Credit",
            amount: balances.get(value.accountId),
          },
          {
            cashType: 2,
            name: "Maple Points",
            amount: maplePoints.get(value.accountId),
          },
          {
            cashType: 4,
            name: "NX Prepaid",
            amount: nxPrepaid.get(value.accountId),
          },
        ],
      };
    if (path === "/payment/status")
      return structuredClone(payments.get(value.transactionId) ?? { transactionId: value.transactionId, state: "UNSEEN" });
    if (path === "/payment/pending") {
      const rows = [...payments.values()].filter((p) => ["DEBITED", "LEGACY_PAID"].includes(p.state) &&
        p.transactionId > (value.after ?? "")).sort((a, b) => a.transactionId.localeCompare(b.transactionId));
      return { items: structuredClone(rows.slice(0, value.limit)), nextCursor: rows.length > value.limit ? rows[value.limit - 1].transactionId : null };
    }
    if (["/payment/debit", "/payment/cancel", "/payment/adopt-legacy"].includes(path)) {
      const id = value.intent.transactionId, amount = Number(value.intent.externalUnits),
        balance = wallets.get(value.cashType), old = payments.get(id);
      if (old && old.state !== "LEGACY_PAID") {
        if (paymentFingerprint(old.intent) !== value.fingerprint || old.accountId !== value.accountId || old.cashType !== value.cashType)
          fail("PAYMENT_CONFLICT", "Changed payment");
        if (path === "/payment/cancel" && old.state !== "CANCELLED") fail("PAYMENT_TERMINAL_CONFLICT", "Already debited");
        return structuredClone(old);
      }
      if (path === "/payment/adopt-legacy" && !old) fail("NOT_FOUND", "Original debit missing", 404);
      const cancelled = path === "/payment/cancel" || !acceptedCashTypes.includes(value.cashType) || balance.get(value.accountId) < amount;
      if (!cancelled && !old) {
        balance.set(value.accountId, balance.get(value.accountId) - amount);
        debitCount++;
      }
      const kind = cancelled ? "no_debit" : "debit";
      const proof = {
        kind, reference: `${kind}:${id}`, providerId: paymentProviderId,
        transactionId: id, userId: value.intent.userId,
        externalCurrency: value.intent.externalCurrency,
        externalUnits: value.intent.externalUnits, fingerprint: value.fingerprint,
      };
      const receipt = {
        transactionId: id, accountId: value.accountId, cashType: value.cashType, amount,
        balance: balance.get(value.accountId), state: cancelled ? "CANCELLED" : "DEBITED",
        accountIncarnation: `00000000-0000-4000-8000-${String(value.accountId).padStart(12, "0")}`,
        intent: structuredClone(value.intent), fingerprint: value.fingerprint,
        preparationId: value.preparationId, headroomReserved: !cancelled, owedAmount: "0",
        [cancelled ? "noDebitReceipt" : "debitReceipt"]: proof,
        ...(cancelled ? { cancellationReason: "INSUFFICIENT_FUNDS" } : {}),
      };
      payments.set(id, receipt);
      if (faults.lostDebit && !cancelled) {
        faults.lostDebit = false;
        fail("GAME_UNAVAILABLE", "Payment response lost", 503);
      }
      return structuredClone(receipt);
    }
    if (["/payment/finalize", "/payment/refund"].includes(path)) {
      const row = payments.get(value.transactionId);
      if (!row || row.fingerprint !== value.fingerprint) fail("PAYMENT_CONFLICT", "Changed payment");
      const refund = path === "/payment/refund", state = refund ? "REFUNDED" : "FULFILLED";
      if (!["DEBITED", state].includes(row.state)) fail("PAYMENT_TERMINAL_CONFLICT", "Already settled");
      const field = refund ? "compensationId" : "purchaseId";
      if (row.state === state && row[field] !== value[field]) fail("PAYMENT_CONFLICT", "Changed outcome");
      if (refund && row.state === "DEBITED") {
        if (faults.refundOwed) {
          row.owedAmount = String(row.amount); row.headroomReserved = false;
          return structuredClone(row);
        }
        const balance = wallets.get(row.cashType);
        balance.set(row.accountId, balance.get(row.accountId) + row.amount);
        refundCount++;
        row.refundReceipt = { ...row.debitReceipt, kind: "refund", reference: `refund:${row.transactionId}`,
          compensationId: value.compensationId, debitReference: value.debitReference };
      }
      row.state = state; row[field] = value[field]; row.headroomReserved = false; row.owedAmount = "0";
      const fault = refund ? "lostRefund" : "lostFinalize";
      if (faults[fault]) { faults[fault] = false; fail("GAME_UNAVAILABLE", "Completion response lost", 503); }
      return structuredClone(row);
    }
    if (path === "/debit") {
      if (payments.has(value.orderId)) {
        const old = payments.get(value.orderId);
        if (
          old.cashType !== value.cashType ||
          old.amount !== value.amount ||
          old.accountId !== value.accountId
        )
          fail("CONFLICT", "Changed debit");
        return old;
      }
      if (!acceptedCashTypes.includes(value.cashType))
        fail("UNSUPPORTED_CASH_TYPE", "Unsupported cash type");
      const balance = wallets.get(value.cashType);
      if (balance.get(value.accountId) < value.amount)
        fail("INSUFFICIENT_FUNDS", "Insufficient NX", 409);
      balance.set(value.accountId, balance.get(value.accountId) - value.amount);
      debitCount++;
      const receipt = { ...value, balance: balance.get(value.accountId) };
      payments.set(value.orderId, receipt);
      if (faults.lostDebit) {
        faults.lostDebit = false;
        fail("GAME_UNAVAILABLE", "Payment response lost", 503);
      }
      return receipt;
    }
    if (path === "/codes/register") {
      if (faults.registration)
        fail("GAME_UNAVAILABLE", "Registration unavailable", 503);
      const old = registrations.get(value.issuanceId);
      if (old && JSON.stringify(old.material) !== JSON.stringify(value))
        fail("CONFLICT", "Registration conflict", 409);
      if (!old)
        registrations.set(value.issuanceId, {
          material: value,
          status: "READY",
        });
      return { issuanceId: value.issuanceId, status: "READY" };
    }
    if (path === "/codes/status")
      return {
        issuanceId: value.issuanceId,
        ...registrations.get(value.issuanceId),
      };
    throw Error("Unknown method");
  };
  const library = new Library({
      framework: core,
      store,
      journal,
      game,
      acceptedCashTypes,
      paymentAuthority,
    }),
    person = { accountId: 1, name: "Collector" },
    other = { accountId: 2, name: "OtherCollector" };
  const buy = (key = "purchase-test", quantity = 1, p = person, cashType = 1) =>
    library.buy(p, {
      key,
      ...library.quote(p, { productId: "first-light", quantity, cashType }),
    });
  return {
    store,
    core,
    journal,
    library,
    person,
    other,
    buy,
    game,
    payments,
    registrations,
    balances,
    maplePoints,
    nxPrepaid,
    faults,
    get debitCount() {
      return debitCount;
    },
    get refundCount() { return refundCount; },
    close() {
      journal.close();
      core.close();
    },
  };
}
