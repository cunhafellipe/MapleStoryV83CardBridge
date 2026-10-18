import test from "node:test";
import assert from "node:assert/strict";
import {
  canonicalPayment, paymentIntent, paymentFingerprint, paymentPreparationId,
  paymentCompensationId, PaymentAuthority,
} from "../src/payment-authority.mjs";

const order = {
  id: "a".repeat(64), accountId: 7, userId: "collector-7",
  input: { cashType: 4 },
  quote: { productId: "shapes", quantity: 1, productRevision: 2, catalogVersion: 3,
    adminRevision: 4, price: { currencyId: "nx", amount: 1000 } },
};
const intent = paymentIntent(order), fingerprint = paymentFingerprint(intent),
  preparationId = paymentPreparationId(intent);
function receipt(kind = "debit") {
  const proof = {
    kind, reference: `${kind}:${order.id}`, providerId: "maplestory.v83",
    transactionId: order.id, userId: order.userId,
    externalCurrency: "NX_PREPAID", externalUnits: "1000", fingerprint,
    ...(kind === "refund" ? {
      compensationId: paymentCompensationId(preparationId, fingerprint),
      debitReference: `debit:${order.id}`,
    } : {}),
  };
  return {
    transactionId: order.id, accountId: 7, cashType: 4, amount: 1000,
    balance: 9000, accountIncarnation: "00000000-0000-4000-8000-000000000007",
    state: { debit: "DEBITED", no_debit: "CANCELLED", refund: "REFUNDED" }[kind],
    fingerprint, intent: structuredClone(intent), headroomReserved: kind === "debit",
    owedAmount: "0",
    [{ debit: "debitReceipt", no_debit: "noDebitReceipt", refund: "refundReceipt" }[kind]]: proof,
  };
}

test("canonical payment identity preserves exact values and rejects missing/non-JSON data", () => {
  assert.equal(canonicalPayment({ z: ["é", 2], a: { b: false, a: "<>&" } }),
    '{"a":{"a":"<>&","b":false},"z":["é",2]}');
  assert.equal(paymentFingerprint({ b: 2, a: 1 }), paymentFingerprint({ a: 1, b: 2 }));
  assert.notEqual(paymentFingerprint({ a: "é" }), paymentFingerprint({ a: "e\u0301" }));
  for (const invalid of [undefined, NaN, Infinity, 1.5, BigInt(1), new Date(), { a: undefined }, Array(1)])
    assert.throws(() => canonicalPayment(invalid), { code: "INVALID_PAYMENT_BINDING" });
  assert.equal(paymentPreparationId(intent).length, 67);
  assert.equal(paymentCompensationId(preparationId, fingerprint).length, 67);
  assert.notEqual(paymentPreparationId(intent), paymentPreparationId({ ...intent, providerId: "other" }));
});

test("trusted intent validation binds registered native identity and exact supported units", () => {
  const authority = new PaymentAuthority(),
    user = { id: order.userId, provider: "maplestory.v83", subject: "7" };
  assert.equal(authority.validateIntent({ intent, user }), false);
  const validate = (input) => authority.withIntent(order, () => authority.validateIntent(input));
  assert.equal(validate({ intent, user }), true);
  for (const badUser of [null, { ...user, id: "other" }, { ...user, provider: "other" },
    { ...user, subject: 7 }, { ...user, subject: "07" }, { ...user, subject: "2147483648" }])
    assert.equal(validate({ intent, user: badUser }), false);
  assert.equal(validate({ intent, user: { ...user, subject: "8" } }), false);
  for (const change of [
    { providerId: "other" }, { externalCurrency: "USD" }, { externalUnits: "01000" },
    { externalUnits: 1000 }, { externalUnits: "1001" }, { extra: true },
    { quote: { ...intent.quote, price: { currencyId: "nx", amount: 100000001 } } },
    { quote: { ...intent.quote, adminRevision: undefined } },
    { quote: { ...intent.quote, price: { ...intent.quote.price, extra: true } } },
  ]) assert.equal(validate({ intent: { ...intent, ...change }, user }), false);
});

test("proof attestation exists only within synchronous native verification and binds every receipt field", () => {
  const authority = new PaymentAuthority(), native = receipt();
  const verify = (proof = native.debitReceipt, terms = intent) =>
    authority.verifyProof({ proof, intent: terms, preparationId, operation: "debit" });
  assert.equal(verify(), false);
  assert.equal(authority.withNativeProof(order, native, "debit", ({ proof }) => {
    assert.equal(verify(proof), true);
    for (const operation of [undefined, "no_debit", "refund", "resolve_legacy", "unknown"])
      assert.equal(authority.verifyProof({ proof, intent, preparationId, operation }), false);
    for (const field of Object.keys(proof))
      assert.equal(verify({ ...proof, [field]: "changed" }), false);
    assert.equal(verify({ ...proof, extra: "customer-assertion" }), false);
    assert.equal(verify(proof, { ...intent, userId: "other" }), false);
    return "verified";
  }), "verified");
  assert.equal(verify(), false);
  assert.throws(() => authority.withNativeProof(order, native, "debit", () => { throw Error("stop"); }));
  assert.equal(verify(), false);
  assert.throws(() => authority.withNativeProof(order, native, "debit", () => Promise.resolve()),
    { code: "INVALID_PAYMENT_VERIFIER" });
  assert.equal(verify(), false);
});

test("native account/source/outcome contradictions cannot authorize allocation or compensation", () => {
  const authority = new PaymentAuthority(), native = receipt();
  for (const change of [
    { transactionId: "b".repeat(64) }, { accountId: 8 }, { cashType: 1 },
    { amount: 999 }, { fingerprint: "b".repeat(64) }, { intent: { ...intent, userId: "other" } },
    { balance: -1 }, { balance: 2147483648 }, { accountIncarnation: null },
    { state: "REFUNDED" }, { headroomReserved: "true" }, { owedAmount: 0 },
  ]) assert.throws(() => authority.withNativeProof(order, { ...native, ...change }, "debit", () => assert.fail()),
    { code: "INVALID_PAYMENT_RECEIPT" });
  assert.throws(() => authority.withNativeProof({ ...order, accountIncarnation: "different" }, native, "debit", () => assert.fail()),
    { code: "INVALID_PAYMENT_RECEIPT" });
  for (const kind of ["no_debit", "refund"])
    assert.equal(authority.withNativeProof(order, receipt(kind), kind, ({ proof }) =>
      authority.verifyProof({ proof, intent, preparationId, operation: kind })), true);
  for (const change of [{ owedAmount: "1" }, { headroomReserved: true }, { state: "DEBITED" }])
    assert.throws(() => authority.withNativeProof(order, { ...receipt("refund"), ...change }, "refund", () => assert.fail()),
      { code: "INVALID_PAYMENT_RECEIPT" });
  const cancelled = receipt("no_debit"); cancelled.accountIncarnation = null;
  assert.equal(authority.withNativeProof(order, cancelled, "no_debit", () => true), true);
});
