import { fail, key } from "./protocol.mjs";
import { paymentActor } from "./payment-authority.mjs";

/** Local operators may adopt only delivery proven to have spent this legacy credit. */
export async function resolveLegacyPayment({ library, orderId, purchaseKey, decisionKey, reason, operatorId }) {
  if (!/^[a-f0-9]{64}$/.test(orderId ?? "") ||
      typeof reason !== "string" || Array.from(reason).length < 1 || Array.from(reason).length > 500 || /[\u0000-\u001f\u007f]/.test(reason) ||
      typeof operatorId !== "string" || !operatorId.trim() || Array.from(operatorId).length > 128 || /[\u0000-\u001f\u007f]/.test(operatorId))
    fail("INVALID_INPUT", "Provide the original order, an attributed operator identity and a concise reason.");
  key(purchaseKey); key(decisionKey);
  const order = library.journal.order(orderId);
  if (!order) fail("NOT_FOUND", "The original journal order is required.", 404);
  const current = library.payments.lookup(order);
  if (!current || !["quarantined", "fulfilled"].includes(current.state))
    fail("LEGACY_RESOLUTION_REJECTED", "This operation does not have a provable legacy purchase resolution.", 409);
  const native = await library.game("/payment/status", { transactionId: order.id });
  if (native.state !== "DEBITED" && !(native.state === "FULFILLED" && current.state === "fulfilled" &&
      native.purchaseId === current.purchase?.id))
    fail("LEGACY_RESOLUTION_REJECTED", "The native payment already has a different outcome.", 409);
  const actor = { ...paymentActor, permissions: ["currency.settle", "maintenance.run"], id: operatorId };
  const resolution = library.payments.attest(order, native, "debit", ({ fingerprint, preparationId }) =>
    library.payments.checked(order, library.core.resolveLegacyExternalPurchase(actor, {
      preparationId, fingerprint, key: decisionKey, reason,
      decision: { kind: "adopt_purchase", purchaseKey },
    })), "resolve_legacy");
  if (resolution.state !== "fulfilled")
    fail("LEGACY_RESOLUTION_REJECTED", "The existing delivery could not be verified.", 409);
  // Finalization remains recoverable if the native response or journal write is lost.
  await library.payments.resume(order);
  return { orderId, state: "complete", purchaseId: resolution.purchase.id };
}
