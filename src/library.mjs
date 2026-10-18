import { key, fail } from "./protocol.mjs";
import { Administration } from "./administration.mjs";
import { PaymentAuthority } from "./payment-authority.mjs";
import { PaymentCoordinator } from "./payment-coordinator.mjs";
export const operator = { role: "admin" };
export class Library {
  #locks = new Map();
  #admission = Promise.resolve();
  #identities = new Map();
  constructor({
    framework,
    store,
    journal,
    game,
    acceptedCashTypes = [1, 2, 4],
    rewardsEnabled = false,
    paymentAuthority = new PaymentAuthority(),
  }) {
    for (const method of ["prepareExternalPurchase", "commitExternalPurchase",
      "cancelExternalPurchase", "confirmExternalCompensation", "lookupExternalPurchase",
      "pendingExternalPurchases", "backfillCompletionReservations"])
      if (typeof framework?.[method] !== "function")
        throw Error("Incompatible framework runtime. Install the pinned bridge dependencies before starting.");
    this.core = framework;
    this.core.backfillCompletionReservations(operator);
    this.store = store;
    this.journal = journal;
    this.game = game;
    this.acceptedCashTypes = acceptedCashTypes;
    this.payments = new PaymentCoordinator({ core: framework, store, journal,
      game: (path, input) => this.game(path, input), authority: paymentAuthority });
    this.admin = new Administration(this, { rewardsEnabled });
  }
  actor(person) {
    if (
      !Number.isSafeInteger(person?.accountId) ||
      person.accountId < 1 ||
      typeof person.name !== "string"
    )
      fail("UNAUTHENTICATED", "Please sign in to your game account.", 401);
    const cached = this.#identities.get(person.accountId);
    if (cached?.name === person.name) return { userId: cached.userId };
    const userId = this.core.registerUser(operator, {
      provider: "maplestory.v83",
      subject: String(person.accountId),
      displayName: person.name,
    }).id;
    if (this.#identities.size >= 10000)
      this.#identities.delete(this.#identities.keys().next().value);
    this.#identities.set(person.accountId, { name: person.name, userId });
    return { userId };
  }
  catalog() {
    const catalog = this.core.catalog(),
      availability = this.core.availability();
    const disabled = new Set(
      catalog.variants.filter((v) => v.enabled === false).map((v) => v.id),
    );
    const remaining = new Map(
      availability.variants.map((v) => [
        v.id,
        disabled.has(v.id) ? 0 : v.remaining,
      ]),
    );
    for (const product of catalog.products) {
      const offered =
        availability.products.find((p) => p.id === product.id)?.available ===
        true;
      const exhausted = product.slots.some((slot) => {
        if (
          slot.probability &&
          slot.probability.numerator < slot.probability.denominator
        )
          return false;
        return slot.pool.every((p) => remaining.get(p.variantId) === 0);
      });
      product.available = offered && !exhausted;
      product.availabilityReason = exhausted
        ? "Out of stock"
        : !offered
          ? "Not currently available"
          : null;
    }
    for (const variant of catalog.variants)
      variant.remaining = remaining.get(variant.id) ?? null;
    return catalog;
  }
  async state(person) {
    const actor = this.actor(person),
      wallet = await this.game("/wallet", { accountId: person.accountId });
    const codePage = this.codePage(person),
      codes = codePage.items;
    wallet.acceptedCashTypes = (wallet.acceptedCashTypes ?? [1, 2, 4]).filter(
      (t) => this.acceptedCashTypes.includes(t),
    );
    const inventory = this.core.inventoryPage(actor, { limit: 50 }),
      collection = this.core.collectionSummary(actor, { limit: 200, excludeTypes: ["code"] });
    const packPage = this.packPage(person, { limit: 50 });
    return {
      owner: person.name,
      role: this.journal.administrator(person.accountId)
        ? "administrator"
        : "collector",
      wallet,
      packs: packPage.items,
      packNext: packPage.next,
      packTotal: packPage.total,
      inventory: inventory.items,
      inventoryNext: inventory.next,
      inventoryTotal: inventory.total,
      collection,
      codes,
      albums: this.store.query((q) => Object.values(q.value("albums") ?? {}).filter((album) => album.ownerId === actor.userId)),
      codeNext: codePage.next,
      codeTotal: codePage.total,
      orders: this.journal.orders(person.accountId).map((o) => ({
        id: o.id,
        state: o.state,
        result: o.result,
        createdAt: o.createdAt,
      })),
    };
  }
  inventoryPage(person, { after = "", limit = 50 } = {}) {
    if (
      typeof after !== "string" ||
      after.length > 512 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100
    )
      fail(
        "INVALID_PAGE",
        "Use a supplied inventory cursor and a limit from 1 to 100.",
      );
    return this.core.inventoryPage(this.actor(person), { after, limit });
  }
  collectionPage(person, { after = "", limit = 200 } = {}) {
    if (typeof after !== "string" || after.length > 512 || !Number.isInteger(limit) || limit < 1 || limit > 200)
      fail("INVALID_PAGE", "Use a supplied collection cursor and a limit from 1 to 200.");
    return this.core.collectionSummary(this.actor(person), { after, limit, excludeTypes: ["code"] });
  }
  packPage(person, { after = "", limit = 50 } = {}) {
    if (
      typeof after !== "string" ||
      after.length > 512 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100
    )
      fail(
        "INVALID_PAGE",
        "Use a supplied pack cursor and a limit from 1 to 100.",
      );
    return this.core.packsPage(this.actor(person), { after, limit, status: "unopened", sort: "ordinal" });
  }
  codePage(person, { after = "", limit = 50 } = {}) {
    if (typeof after !== "string" || after.length > 512 || !Number.isInteger(limit) || limit < 1 || limit > 100)
      fail("INVALID_PAGE", "Use a supplied code cursor and a limit from 1 to 100.");
    const actor = this.actor(person);
    const page = this.core.codeHistory(actor, { after, limit });
    return {
      ...page,
      items: page.items.map((c) => ({
        ...c,
        registration: this.journal.registration(c.id),
      })),
    };
  }
  quote(person, input) {
    const actor = this.actor(person),
      cashType = input.cashType ?? 1;
    if (!this.acceptedCashTypes.includes(cashType))
      fail("UNSUPPORTED_CASH_TYPE", "Choose an accepted cash balance.");
    const quote = {
      ...this.core.quote(actor, {
        productId: input.productId,
        quantity: input.quantity ?? 1,
      }),
      cashType,
    };
    if (
      quote.price.currencyId !== "nx" ||
      !Number.isSafeInteger(quote.price.amount) ||
      quote.price.amount < 1 ||
      quote.price.amount > 100000000
    )
      fail(
        "INVALID_PRICE",
        "The total must be between 1 and 100,000,000 NX.",
        409,
      );
    return quote;
  }
  async buy(person, input) {
    return this.#coordinate(() => this.#buy(person, input));
  }
  async #coordinate(work) {
    const prior = this.#admission;
    let release;
    this.#admission = new Promise((resolve) => {
      release = resolve;
    });
    await prior;
    try {
      return await work();
    } finally {
      release();
    }
  }
  async coordinatePublication(work) {
    return this.#coordinate(async () => {
      if (this.journal.pending().length || this.journal.diagnostics().orphanedPayments)
        fail(
          "PURCHASE_PENDING",
          "Complete outstanding purchases before publishing a catalog.",
          409,
        );
      return work();
    });
  }
  readiness() {
    try {
      return {
        ok:
          (!this.store.integrity || this.store.integrity()) &&
          this.journal.writable() && !this.journal.diagnostics().orphanedPayments,
        profile: "single-writer",
        capacity: this.core.capacity?.() ?? { qualified: false },
      };
    } catch {
      return {
        ok: false,
        profile: "single-writer",
        error: "STORAGE_UNAVAILABLE",
      };
    }
  }
  async #buy(person, input) {
    key(input.key);
    const clean = {
      productId: input.productId,
      quantity: input.quantity ?? 1,
      catalogVersion: input.catalogVersion,
      productRevision: input.productRevision,
      ...(input.adminRevision !== undefined
        ? { adminRevision: input.adminRevision }
        : {}),
      cashType: input.cashType ?? 1,
    };
    const actor = this.actor(person),
      existing = this.journal.find(person.accountId, input.key);
    if (existing) {
      const restored = this.journal.create(
        person.accountId,
        input.key,
        clean,
        actor.userId,
        existing.quote,
      );
      return this.#exclusive(restored.id, () => this.#resume(restored));
    }
    if (!this.readiness().ok)
      fail(
        "STORAGE_UNAVAILABLE",
        "New purchases are paused while storage is unavailable.",
        503,
      );
    if (this.journal.pending().some((o) => o.accountId === person.accountId))
      fail(
        "PURCHASE_PENDING",
        "Your previous purchase is still being completed. Please check the saved purchase shortly.",
        409,
      );
    const quote = this.quote(person, clean);
    if (quote.price.amount < 1 || quote.price.currencyId !== "nx")
      fail(
        "INVALID_PRODUCT",
        "This installation purchases packs with a selected game cash balance.",
        409,
      );
    if (
      quote.catalogVersion !== clean.catalogVersion ||
      quote.productRevision !== clean.productRevision ||
      quote.adminRevision !== clean.adminRevision
    )
      fail(
        "STALE_QUOTE",
        "The pack changed. Please review the current price.",
        409,
      );
    const order = this.journal.create(
      person.accountId,
      input.key,
      clean,
      actor.userId,
      quote,
      person.name,
    );
    return this.#exclusive(order.id, () => this.#resume(order));
  }
  async #exclusive(id, work) {
    if (this.#locks.has(id)) return this.#locks.get(id);
    const pending = work();
    this.#locks.set(id, pending);
    try {
      return await pending;
    } catch (error) {
      const order = this.journal.order(id);
      if (order && !["complete", "rejected", "refunded"].includes(order.state))
        this.journal.failed(order, error);
      throw error;
    } finally {
      this.#locks.delete(id);
    }
  }
  async #resume(order, { shouldStop = () => false } = {}) {
    const result = await this.payments.resume(order);
    await this.reconcile({ accountId: order.accountId, shouldStop });
    return result;
  }
  async recover({ shouldStop = () => false, force = true } = {}) {
    const startedAt = new Date().toISOString();
    let completed = 0,
      failed = 0;
    if (shouldStop()) return;
    try {
      await this.#coordinate(() => this.payments.discover({ shouldStop }));
    } catch (error) {
      this.lastRecovery = { startedAt, completed, failed: 1, phase: "discovery",
        failureCode: /^[A-Z][A-Z0-9_]{0,80}$/.test(error.code ?? "") ? error.code : "UNAVAILABLE",
        finishedAt: new Date().toISOString() };
      if (error.code === "GAME_UNAVAILABLE") return;
      throw error;
    }
    for (const order of this.journal.dueOrders({ force, limit: 20 })) {
      if (shouldStop()) return;
      if (
        !force &&
        order.nextAttemptAt &&
        Date.parse(order.nextAttemptAt) > Date.now()
      )
        continue;
      try {
        await this.#coordinate(() =>
          this.#exclusive(order.id, () => this.#resume(order, { shouldStop })),
        );
        completed++;
      } catch (error) {
        const current = this.journal.order(order.id);
        if (["complete", "rejected", "refunded"].includes(current.state)) {
          completed++;
          continue;
        }
        failed++;
        if (!["complete", "rejected", "refunded"].includes(current.state))
          this.journal.failed(current, error);
        if (error.code === "GAME_UNAVAILABLE") break;
      }
    }
    this.lastRecovery = {
      startedAt,
      completed,
      failed,
      finishedAt: new Date().toISOString(),
    };
    if (failed)
      console.warn(
        JSON.stringify({ event: "purchase_recovery", failed, completed }),
      );
  }
  async retry(order, before = async () => {}) {
    return this.#coordinate(async () => {
      await before();
      return this.#exclusive(order.id, () =>
        this.#resume(this.journal.order(order.id)),
      );
    });
  }
  async reconcile({
    accountId: scope,
    issuanceId,
    force = false,
    shouldStop = () => false,
  } = {}) {
    if (this.store.query((q) => q.pageCodes({ limit: 1 }).total) === 0) return;
    let after = "",
      attempts = 0;
    do {
      const page = this.core.codeInventory(operator, { limit: 200, after });
      after = page.next;
      for (const row of page.items) {
        if (shouldStop()) return;
        if (issuanceId && row.id !== issuanceId) continue;
        if (
          !row.holderId ||
          !row.copyId ||
          ["redeemed", "revoked"].includes(row.status)
        )
          continue;
        const identity = this.store.query((q) => q.providerIdentity(row.holderId)),
          accountId = identity?.provider === "maplestory.v83" ? Number(identity.subject) : NaN;
        if (
          !Number.isSafeInteger(accountId) ||
          accountId < 1 ||
          (scope && scope !== accountId)
        )
          continue;
        try {
          if (this.journal.registration(row.id) !== "ready") {
            if (!force && !this.journal.registrationDue(row.id)) continue;
            if (++attempts > 50) return;
            this.journal.registrationAttempt(row.id);
            const material = this.core.codeRegistrationMaterial(
              operator,
              row.id,
            );
            const result = await this.game("/codes/register", {
              issuanceId: row.id,
              accountId,
              code: material.code,
              itemId: material.metadata.itemId,
              quantity: material.metadata.quantity,
              petDays: material.metadata.petDays,
              series: 1,
            });
            if (result.issuanceId !== row.id)
              fail("GAME_UNAVAILABLE", "Invalid registration receipt.", 503);
            this.journal.register(row.id, "ready");
          }
          if (!row.revealedAt || !this.journal.due(row.id, force)) continue;
          const state = await this.game("/codes/status", {
            issuanceId: row.id,
          });
          this.journal.checked(row.id);
          if (state.status === "USED")
            this.used({
              issuanceId: row.id,
              receiptId: state.receiptId,
              occurredAt: state.usedAt,
            });
        } catch (error) {
          this.journal.registrationFailure(row.id, error);
          if (error.code === "GAME_UNAVAILABLE") return;
          console.warn(
            JSON.stringify({
              event: "code_registration_retry",
              code: /^[A-Z0-9_]+$/.test(error.code ?? "")
                ? error.code
                : "UNAVAILABLE",
            }),
          );
        }
      }
    } while (after);
  }
  used({ issuanceId, receiptId, occurredAt }) {
    if (
      typeof receiptId !== "string" ||
      !/^[a-zA-Z0-9-]{16,100}$/.test(receiptId)
    )
      fail("INVALID_RECEIPT", "Invalid reward receipt.");
    return this.core.confirmCodeStatus(operator, {
      providerId: "maplestory.v83",
      codeId: issuanceId,
      eventId: receiptId,
      status: "redeemed",
      occurredAt,
    });
  }
  open(person, input) {
    key(input.key);
    return this.core.openPack(this.actor(person), {
      key: input.key,
      packId: input.packId,
    });
  }
  reveal(person, input) {
    key(input.key);
    const actor = this.actor(person);
    if (this.journal.registration(input.codeId) !== "ready")
      fail(
        "CODE_PREPARING",
        "The server is registering this code with the Cash Shop. Please try shortly.",
        409,
      );
    return this.core.revealCode(actor, {
      key: input.key,
      codeId: input.codeId,
    });
  }
  album(person, input) {
    key(input.key);
    return this.core.saveAlbum(this.actor(person), {
      key: input.key,
      albumId: input.albumId,
      name: input.name,
      visibility: "private",
      placements: input.placements,
      expectedVersion: input.expectedVersion,
    });
  }
}
