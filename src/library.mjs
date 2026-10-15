import { key, fail } from "./protocol.mjs";
import { cashTypes } from "./cash-types.mjs";
import { Administration } from "./administration.mjs";
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
  }) {
    if (typeof framework?.backfillCompletionReservations !== "function")
      throw Error(
        "Incompatible framework runtime. Install the pinned bridge dependencies before starting.",
      );
    framework.backfillCompletionReservations(operator);
    this.core = framework;
    this.store = store;
    this.journal = journal;
    this.game = game;
    this.acceptedCashTypes = acceptedCashTypes;
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
    const inventory = this.core
        .inventory(actor)
        .sort(
          (a, b) =>
            (b.createdAt ?? b.at ?? "").localeCompare(
              a.createdAt ?? a.at ?? "",
            ) || a.id.localeCompare(b.id),
        ),
      groups = new Map();
    let totalCards = 0;
    for (const card of inventory) {
      if (card.definition.type === "code") continue;
      totalCards++;
      const row = groups.get(card.variantId);
      if (row) row.count++;
      else
        groups.set(card.variantId, {
          variantId: card.variantId,
          count: 1,
          card,
        });
    }
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
      inventory: inventory.slice(0, 50),
      inventoryNext: inventory.length > 50 ? inventory[49].id : null,
      inventoryTotal: inventory.length,
      collection: {
        items: [...groups.values()],
        totalCards,
        uniqueCards: groups.size,
      },
      codes,
      albums: this.core.albums(actor),
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
      after.length > 100 ||
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
  packPage(person, { after = "", limit = 50 } = {}) {
    if (
      typeof after !== "string" ||
      after.length > 100 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100
    )
      fail(
        "INVALID_PAGE",
        "Use a supplied pack cursor and a limit from 1 to 100.",
      );
    const packs = this.core
      .packs(this.actor(person))
      .filter((p) => !p.openedAt);
    const start = after ? packs.findIndex((p) => p.id === after) + 1 : 0;
    if (after && !start)
      fail("INVALID_CURSOR", "Pack list changed. Refresh this view.", 409);
    const items = packs.slice(start, start + limit);
    return {
      items,
      next: start + limit < packs.length ? items.at(-1).id : null,
      total: packs.length,
    };
  }
  codePage(person, { after = "", limit = 50 } = {}) {
    const page = this.core.codeHistory(this.actor(person), { after, limit });
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
      if (this.journal.pending().length)
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
          this.journal.writable(),
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
      if (order && !["complete", "rejected"].includes(order.state))
        this.journal.failed(order, error);
      throw error;
    } finally {
      this.#locks.delete(id);
    }
  }
  async #resume(order, { shouldStop = () => false } = {}) {
    if (order.state === "complete") return order.result;
    if (order.state === "rejected")
      fail(
        "PURCHASE_REJECTED",
        "This purchase was declined. Review your balance and try a new purchase.",
        409,
      );
    this.journal.attempt(order);
    if (order.state === "pending") {
      try {
        const paid = await this.game("/debit", {
          orderId: order.id,
          accountId: order.accountId,
          amount: order.quote.price.amount,
          cashType: order.input.cashType ?? 1,
        });
        if (
          paid.orderId !== order.id ||
          paid.amount !== order.quote.price.amount ||
          paid.accountId !== order.accountId ||
          paid.cashType !== (order.input.cashType ?? 1)
        )
          fail("GAME_UNAVAILABLE", "Invalid payment receipt.", 503);
        order.payment = paid;
        this.journal.save(order, "paid");
        order.state = "paid";
      } catch (e) {
        if (
          e.code === "INSUFFICIENT_FUNDS" ||
          e.code === "ACCOUNT_UNAVAILABLE"
        ) {
          this.journal.save(order, "rejected");
          order.state = "rejected";
        }
        this.journal.failed(order, e);
        throw e;
      }
    }
    // Retried settlement and purchase share durable identities; neither can mint twice.
    this.core.settleExternalCredit(operator, {
      providerId: "maplestory.v83",
      transactionId: order.id,
      userId: order.userId,
      currencyId: "nx",
      amount: order.quote.price.amount,
      externalCurrency: cashTypes.find(
        (c) => c.cashType === (order.input.cashType ?? 1),
      ).externalCurrency,
      externalUnits: String(order.quote.price.amount),
    });
    order.result = this.core.purchase(
      { userId: order.userId },
      { key: "purchase-" + order.id, ...order.input },
    );
    this.journal.save(order, "complete");
    await this.reconcile({ accountId: order.accountId, shouldStop });
    return order.result;
  }
  async recover({ shouldStop = () => false, force = true } = {}) {
    const startedAt = new Date().toISOString();
    let completed = 0,
      failed = 0;
    for (const order of this.journal.pending().slice(0, 20)) {
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
        failed++;
        this.journal.failed(this.journal.order(order.id), error);
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
        const accountId = this.store.read((s) =>
          Number(s.users[row.holderId]?.subject),
        );
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
