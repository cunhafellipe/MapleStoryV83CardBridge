import { fail } from "./protocol.mjs";
import {
  canonicalPayment, paymentActor, paymentProviderId, paymentIntent,
  paymentFingerprint, paymentPreparationId, paymentCompensationId,
} from "./payment-authority.mjs";

/** Network evidence is obtained before synchronous framework transactions. */
export class PaymentCoordinator {
  #nativeCursor = null;
  #coreCursor = null;
  constructor({ core, store, journal, game, authority }) {
    this.core = core;
    this.store = store;
    this.journal = journal;
    this.game = game;
    this.authority = authority;
  }
  assertAccount(order) {
    const identity = this.store.query((q) => q.providerIdentity(order.userId));
    if (identity?.provider !== paymentProviderId || identity.subject !== String(order.accountId))
      this.quarantine(order, "PAYMENT_ACCOUNT_MISMATCH");
  }
  quarantine(order, code = "LEGACY_RECOVERY_REQUIRED") {
    order.reconciliationRequired = true;
    this.journal.save(order, "quarantined");
    this.journal.observePayment(order.id, "framework", true);
    fail(code, "The original payment stores disagree. Restore verified original evidence before retrying; no delivery or refund was attempted.", 409);
  }
  binding(order) {
    const intent = paymentIntent(order);
    return {
      accountId: order.accountId, cashType: order.input.cashType,
      intent, preparationId: paymentPreparationId(intent),
      fingerprint: paymentFingerprint(intent),
    };
  }
  checked(order, status) {
    this.assertAccount(order);
    const binding = this.binding(order);
    if (!status || status.preparationId !== binding.preparationId ||
        status.fingerprint !== binding.fingerprint ||
        canonicalPayment(status.terms) !== canonicalPayment(binding.intent) ||
        !["prepared", "fulfilled", "cancelled", "refund_required", "compensated", "quarantined"].includes(status.state))
      fail("INVALID_PURCHASE_STATUS", "The original purchase status could not be verified.", 503);
    if (["refund_required", "compensated"].includes(status.state) &&
        (status.compensationId !== paymentCompensationId(binding.preparationId, binding.fingerprint) ||
         status.debitReference !== `debit:${order.id}`))
      fail("INVALID_PURCHASE_STATUS", "The original refund status could not be verified.", 503);
    if (status.state === "fulfilled" &&
        (typeof status.purchase?.id !== "string" || !status.purchase.id || !Array.isArray(status.purchase.packs)))
      fail("INVALID_PURCHASE_STATUS", "The original delivery could not be verified.", 503);
    return status;
  }
  lookup(order) {
    try {
      return this.checked(order, this.core.lookupExternalPurchase(paymentActor, {
        providerId: paymentProviderId, transactionId: order.id,
      }));
    } catch (error) {
      if (error.code === "NOT_FOUND") return null;
      throw error;
    }
  }
  attest(order, native, kind, work, operation = kind) {
    this.assertAccount(order);
    return this.authority.withNativeProof(order, native, kind, (binding) => {
      order.accountIncarnation = native.accountIncarnation;
      if (kind === "debit") order.payment = structuredClone(native);
      this.journal.save(order, order.state);
      const result = work(binding);
      return result;
    }, operation);
  }
  async resume(order) {
    this.assertAccount(order);
    this.journal.attempt(order);
    const binding = this.binding(order);
    let status = this.lookup(order);
    if (order.reconciliationRequired && status?.state !== "fulfilled") this.quarantine(order);
    if (order.state === "complete" && status && (status.state !== "fulfilled" ||
        canonicalPayment(order.result) !== canonicalPayment(status.purchase))) this.quarantine(order);
    if (order.state === "refunded" && status?.state !== "compensated") this.quarantine(order);
    if (order.state === "rejected") {
      const native = await this.game("/payment/status", { transactionId: order.id });
      if (status?.state === "cancelled")
        this.attest(order, native, "no_debit", () => {});
      else if (status || native.state !== "UNSEEN" || native.transactionId !== order.id || order.preparationId)
        this.quarantine(order);
      fail("PURCHASE_REJECTED", "This purchase was declined without spending cash. Review your balance and use a new purchase identifier.", 409);
    }
    if (!status) {
      const native = await this.game("/payment/status", { transactionId: order.id });
      if (order.financialVersion !== 1 && ["LEGACY_PAID", "DEBITED"].includes(native.state)) {
        const adopted = await this.game("/payment/adopt-legacy", binding);
        if (adopted.state !== "DEBITED") this.quarantine(order);
        const { version, ...legacyIntent } = binding.intent;
        status = this.attest(order, adopted, "debit", ({ proof }) =>
          this.checked(order, this.core.reconcileLegacyExternalPurchase(paymentActor, {
            key: `legacy-${order.id}`, ...legacyIntent,
            purchaseKey: `purchase-${order.id}`, debitReceipt: proof,
          })));
        order.legacyReconciled = true;
      } else if (native.transactionId !== order.id || native.state !== "UNSEEN" || order.state !== "pending" || order.payment) {
        fail("LEGACY_RECOVERY_REQUIRED", "The original paid purchase needs operator reconciliation. No new payment was requested.", 409);
      }
    }
    if (!status) {
      const { version, ...input } = binding.intent;
      try {
        status = this.authority.withIntent(order, () => this.checked(order, this.core.prepareExternalPurchase(paymentActor, {
          key: `prepare-${order.id}`, ...input,
        })));
      } catch (error) {
        // A failed write may have committed. Inspect durable identity before deciding.
        status = this.lookup(order);
        if (!status) {
          if (["STALE_QUOTE", "OUT_OF_STOCK", "EXTERNAL_PURCHASE_CAPACITY", "CAPACITY_EXCEEDED",
            "INSTALLATION_CAPACITY", "POOL_EXHAUSTED", "SOLD_OUT", "UNAVAILABLE",
            "PRODUCT_UNAVAILABLE", "USER_RESTRICTED", "ACCOUNT_RESTRICTED", "INVALID_INPUT"].includes(error.code))
            this.journal.save(order, "rejected");
          throw error;
        }
      }
    }
    order.financialVersion = 1;
    order.preparationId = status.preparationId;
    order.fingerprint = status.fingerprint;
    this.journal.save(order, status.state);

    if (status.state === "prepared") {
      // Every refusal returns a durable cancellation fence, never an inferred outcome.
      const native = await this.game("/payment/debit", binding);
      if (native.state !== "DEBITED" && native.state !== "CANCELLED") this.quarantine(order);
      if (native.state === "CANCELLED") {
        status = this.attest(order, native, "no_debit", ({ proof }) =>
          this.checked(order, this.core.cancelExternalPurchase(paymentActor, {
            preparationId: binding.preparationId, fingerprint: binding.fingerprint,
            noDebitReceipt: proof,
          })));
        this.journal.save(order, "rejected");
        fail(native.cancellationReason === "INSUFFICIENT_FUNDS" ? "INSUFFICIENT_FUNDS" : "PURCHASE_REJECTED",
          "This purchase was declined without spending cash. Review your selected balance.", 409);
      }
      status = this.attest(order, native, "debit", ({ proof }) =>
        this.checked(order, this.core.commitExternalPurchase(paymentActor, {
          preparationId: binding.preparationId, fingerprint: binding.fingerprint,
          debitReceipt: proof,
        })));
      this.journal.save(order, status.state);
    }
    if (status.state === "fulfilled") {
      const native = await this.game("/payment/finalize", {
        transactionId: order.id, fingerprint: binding.fingerprint,
        purchaseId: status.purchase.id,
      });
      this.attest(order, native, "debit", () => {
        if (native.state !== "FULFILLED" || native.purchaseId !== status.purchase.id)
          fail("INVALID_PAYMENT_RECEIPT", "The original payment finalization could not be verified.", 503);
      }, "verify_delivery");
      delete order.reconciliationRequired;
      this.journal.observePayment(order.id, "framework", false);
      order.result = status.purchase;
      this.journal.save(order, "complete");
      return order.result;
    }
    if (["refund_required", "compensated"].includes(status.state)) {
      const native = await this.game("/payment/refund", {
        transactionId: order.id, fingerprint: binding.fingerprint,
        compensationId: status.compensationId, debitReference: status.debitReference,
      });
      if (native.state === "DEBITED") {
        this.attest(order, native, "debit", () => {});
        this.journal.save(order, "refund_required");
        fail("REFUND_PENDING", "Your original payment is awaiting a refund. An operator must restore the original balance's refund capacity.", 409);
      }
      status = this.attest(order, native, "refund", ({ proof }) =>
        this.checked(order, this.core.confirmExternalCompensation(paymentActor, {
          preparationId: binding.preparationId, fingerprint: binding.fingerprint,
          refundReceipt: proof,
        })));
      order.failureCode = status.failureCode;
      this.journal.save(order, "refunded");
      fail("PURCHASE_REFUNDED", "This pack could not be delivered. Your payment was returned to the original balance.", 409);
    }
    if (status.state === "cancelled") {
      this.journal.save(order, "rejected");
      fail("PURCHASE_REJECTED", "This purchase was cancelled without spending cash.", 409);
    }
    fail("LEGACY_RECOVERY_REQUIRED", "This original purchase is quarantined for operator reconciliation. No automatic refund was attempted.", 409);
  }
  async discover({ shouldStop = () => false } = {}) {
    const native = await this.game("/payment/pending", { after: this.#nativeCursor, limit: 50 });
    if (!Array.isArray(native.items) || native.items.length > 50 ||
        !(native.nextCursor === null || /^[a-f0-9]{64}$/.test(native.nextCursor)))
      fail("INVALID_PAYMENT_DISCOVERY", "Invalid native recovery page.", 503);
    let previous = this.#nativeCursor ?? "";
    for (const row of native.items) {
      if (shouldStop()) return;
      if (!/^[a-f0-9]{64}$/.test(row.transactionId) || row.transactionId <= previous || !["DEBITED", "LEGACY_PAID"].includes(row.state))
        fail("INVALID_PAYMENT_DISCOVERY", "Invalid native recovery identity.", 503);
      previous = row.transactionId;
      const order = this.journal.order(row.transactionId);
      if (order && (row.accountId !== order.accountId || row.cashType !== order.input.cashType || row.amount !== order.quote.price.amount))
        fail("INVALID_PAYMENT_DISCOVERY", "Native recovery terms differ from the original order.", 503);
      this.journal.observePayment(row.transactionId, "game", !order);
      if (order && order.state === "complete") {
        // Earlier successful purchases also need adoption to release native headroom.
        this.journal.save(order, order.financialVersion === 1 ? "fulfilled" : "legacy_reconciliation");
      }
    }
    if (native.nextCursor !== null && native.nextCursor !== previous)
      fail("INVALID_PAYMENT_DISCOVERY", "Invalid native recovery cursor.", 503);
    this.#nativeCursor = native.nextCursor;
    const core = this.core.pendingExternalPurchases(paymentActor, {
      providerId: paymentProviderId, after: this.#coreCursor, limit: 50,
    });
    if (!Array.isArray(core.items) || core.items.length > 50 ||
        !(core.nextCursor === null || /^ep_[a-f0-9]{64}$/.test(core.nextCursor)))
      fail("INVALID_PAYMENT_DISCOVERY", "Invalid framework recovery page.", 503);
    previous = this.#coreCursor ?? "";
    for (const row of core.items) {
      if (shouldStop()) return;
      const id = row.terms?.transactionId;
      if (!/^[a-f0-9]{64}$/.test(id) || !/^ep_[a-f0-9]{64}$/.test(row.preparationId) || row.preparationId <= previous)
        fail("INVALID_PAYMENT_DISCOVERY", "Invalid framework recovery identity.", 503);
      previous = row.preparationId;
      const order = this.journal.order(id);
      if (order) this.checked(order, row);
      if (order && ["complete", "rejected", "refunded"].includes(order.state)) {
        order.reconciliationRequired = true;
        this.journal.save(order, "quarantined");
      }
      this.journal.observePayment(id, "framework", !order || order.reconciliationRequired === true);
    }
    if (core.nextCursor !== null && core.nextCursor !== previous)
      fail("INVALID_PAYMENT_DISCOVERY", "Invalid framework recovery cursor.", 503);
    this.#coreCursor = core.nextCursor;
  }
}
