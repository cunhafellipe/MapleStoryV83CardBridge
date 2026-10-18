import { hash, fail } from "./protocol.mjs";
import { cashTypes } from "./cash-types.mjs";

export const paymentProviderId = "maplestory.v83";
export const paymentActor = Object.freeze({
  permissions: Object.freeze(["currency.settle"]),
  settlementProviderId: paymentProviderId,
});

// Provider identity and proofs use the framework's canonical JSON wire contract.
export function canonicalPayment(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string")
    return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value))
    return JSON.stringify(value);
  if (Array.isArray(value))
    return "[" + Array.from(value, canonicalPayment).join(",") + "]";
  if (value && Object.getPrototypeOf(value) === Object.prototype)
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ":" + canonicalPayment(value[key]))
        .join(",") +
      "}"
    );
  fail("INVALID_PAYMENT_BINDING", "Invalid payment binding.", 409);
}

export const paymentFingerprint = (intent) => hash(canonicalPayment(intent));
export const paymentPreparationId = ({ providerId, transactionId }) =>
  "ep_" + hash(canonicalPayment({ providerId, transactionId }));
export const paymentCompensationId = (preparationId, fingerprint) =>
  "ec_" + hash(canonicalPayment({ preparationId, fingerprint }));

export function paymentIntent(order) {
  const source = cashTypes.find((c) => c.cashType === order.input.cashType);
  if (!source)
    fail("INVALID_PAYMENT_BINDING", "Unknown original cash balance.", 409);
  return {
    version: 1,
    providerId: paymentProviderId,
    transactionId: order.id,
    userId: order.userId,
    externalCurrency: source.externalCurrency,
    externalUnits: String(order.quote.price.amount),
    quote: {
      productId: order.quote.productId,
      quantity: order.quote.quantity,
      productRevision: order.quote.productRevision,
      catalogVersion: order.quote.catalogVersion,
      adminRevision: order.quote.adminRevision ?? 0,
      price: {
        currencyId: order.quote.price.currencyId,
        amount: order.quote.price.amount,
      },
    },
  };
}

function exactKeys(value, keys) {
  return (
    value &&
    Object.getPrototypeOf(value) === Object.prototype &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",")
  );
}

/** Synchronous attestation of native evidence obtained before a core transaction. */
export class PaymentAuthority {
  #proof = null;
  #intent = null;
  withIntent(order, work) {
    if (this.#intent) fail("PAYMENT_VERIFICATION_BUSY", "Payment verification is already active.", 503);
    this.#intent = { fingerprint: paymentFingerprint(paymentIntent(order)), subject: String(order.accountId) };
    try {
      const result = work();
      if (result?.then) fail("INVALID_PAYMENT_VERIFIER", "Payment verification must complete synchronously.", 503);
      return result;
    } finally { this.#intent = null; }
  }
  validateIntent = ({ intent, user }) => {
    if (
      !exactKeys(intent, [
        "version", "providerId", "transactionId", "userId",
        "externalCurrency", "externalUnits", "quote",
      ]) ||
      intent.version !== 1 || intent.providerId !== paymentProviderId ||
      typeof intent.transactionId !== "string" ||
      !/^[a-f0-9]{64}$/.test(intent.transactionId) ||
      typeof intent.userId !== "string" || !intent.userId.length ||
      intent.userId.length > 128 || /[\u0000-\u001f\u007f]/.test(intent.userId) ||
      user?.id !== intent.userId || user.provider !== paymentProviderId ||
      typeof user.subject !== "string" ||
      !/^[1-9][0-9]{0,9}$/.test(user.subject) ||
      Number(user.subject) > 2147483647 ||
      !this.#intent || user.subject !== this.#intent.subject ||
      !cashTypes.some((c) => c.externalCurrency === intent.externalCurrency) ||
      typeof intent.externalUnits !== "string" ||
      (!/^[1-9][0-9]{0,8}$/.test(intent.externalUnits) && intent.externalUnits !== "100000000") ||
      !exactKeys(intent.quote, [
        "productId", "quantity", "productRevision", "catalogVersion",
        "adminRevision", "price",
      ]) ||
      !exactKeys(intent.quote.price, ["currencyId", "amount"])
    ) return false;
    const { quote } = intent;
    return (
      typeof quote.productId === "string" && quote.productId.length > 0 &&
      quote.productId.length <= 128 && !/[\u0000-\u001f\u007f]/.test(quote.productId) &&
      Number.isSafeInteger(quote.quantity) && quote.quantity >= 1 && quote.quantity <= 100 &&
      [quote.productRevision, quote.catalogVersion, quote.adminRevision].every(
        (revision) => Number.isSafeInteger(revision) && revision >= 0,
      ) &&
      quote.price.currencyId === "nx" &&
      Number.isSafeInteger(quote.price.amount) && quote.price.amount >= 1 &&
      quote.price.amount <= 100000000 &&
      String(quote.price.amount) === intent.externalUnits &&
      paymentFingerprint(intent) === this.#intent.fingerprint
    );
  };

  verifyProof = ({ proof, intent, preparationId, operation }) => {
    if (!this.#proof) return false;
    try {
      return (
        preparationId === paymentPreparationId(intent) &&
        paymentFingerprint(intent) === this.#proof.fingerprint &&
        operation === this.#proof.operation &&
        ["debit", "no_debit", "refund", "resolve_legacy"].includes(operation) &&
        canonicalPayment(proof) === this.#proof.encoded
      );
    } catch { return false; }
  };

  withNativeProof(order, native, kind, work, operation = kind) {
    const intent = paymentIntent(order),
      fingerprint = paymentFingerprint(intent),
      preparationId = paymentPreparationId(intent),
      cashType = order.input.cashType,
      expected = {
        kind,
        reference: `${kind}:${order.id}`,
        providerId: paymentProviderId,
        transactionId: order.id,
        userId: order.userId,
        externalCurrency: intent.externalCurrency,
        externalUnits: intent.externalUnits,
        fingerprint,
        ...(kind === "refund" ? {
          compensationId: paymentCompensationId(preparationId, fingerprint),
          debitReference: `debit:${order.id}`,
        } : {}),
      },
      field = { debit: "debitReceipt", no_debit: "noDebitReceipt", refund: "refundReceipt" }[kind],
      states = {
        debit: ["DEBITED", "FULFILLED"],
        no_debit: ["CANCELLED"], refund: ["REFUNDED"],
      }[kind];
    let encoded, nativeIntent;
    try {
      encoded = canonicalPayment(native?.[field]);
      nativeIntent = canonicalPayment(native?.intent);
    } catch {}
    const incarnation = native?.accountIncarnation;
    if (
      !field || !states.includes(native?.state) ||
      native.transactionId !== order.id || native.accountId !== order.accountId ||
      native.cashType !== cashType || native.amount !== order.quote.price.amount ||
      native.fingerprint !== fingerprint || nativeIntent !== canonicalPayment(intent) ||
      (typeof incarnation !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(incarnation)) &&
        !(kind === "no_debit" && incarnation === null) ||
      (order.accountIncarnation !== undefined && order.accountIncarnation !== incarnation) ||
      typeof native.headroomReserved !== "boolean" ||
      typeof native.owedAmount !== "string" || !/^(0|[1-9][0-9]{0,39})$/.test(native.owedAmount) ||
      (native.state !== "DEBITED" && (native.owedAmount !== "0" || native.headroomReserved !== false)) ||
      !Number.isSafeInteger(native.balance) || native.balance < 0 || native.balance > 2147483647 ||
      encoded !== canonicalPayment(expected)
    ) fail("INVALID_PAYMENT_RECEIPT", "The native payment result could not be verified. Retry the original purchase.", 503);
    if (this.#proof)
      fail("PAYMENT_VERIFICATION_BUSY", "Payment verification is already active.", 503);
    this.#proof = { fingerprint, encoded, operation };
    try {
      const result = this.withIntent(order, () => work({ intent, fingerprint, preparationId, proof: structuredClone(expected) }));
      if (result?.then)
        fail("INVALID_PAYMENT_VERIFIER", "Payment verification must complete synchronously.", 503);
      return result;
    } finally { this.#proof = null; }
  }
}
