import { randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { fail, key } from "./protocol.mjs";

export function pageOptions(input = {}) {
  const limit = Number(input.limit ?? 25),
    after = String(input.after ?? ""),
    search = String(input.search ?? "");
  if (
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 100 ||
    (after && !/^\d{1,16}$/.test(after)) ||
    search.length > 100
  )
    fail(
      "INVALID_PAGE",
      "Use a page size from 1 to 100 and the supplied cursor.",
    );
  return { limit, after, search };
}
const safeImage = (value) =>
  typeof value === "string" &&
  (/^\/assets\/library\/(?:[a-zA-Z0-9_-]+\/)*[a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|webp)$/.test(
    value,
  ) ||
    /^https:\/\/maplestoryitcg\.weebly\.com\/uploads\/(?:\d+\/)+[a-zA-Z0-9_.-]+\.(?:png|jpg|jpeg)$/.test(
      value,
    ));
export function validateWebsiteCatalog(
  catalog,
  previous,
  { rewardsEnabled = false } = {},
) {
  if (!catalog || typeof catalog !== "object" || Array.isArray(catalog))
    fail("INVALID_CATALOG", "Submit a complete catalog object.");
  for (const name of [
    "cards",
    "variants",
    "products",
    "lines",
    "rarities",
    "currencies",
  ])
    if (
      !Array.isArray(catalog[name]) ||
      catalog[name].length > (name === "variants" ? 2000 : 1000)
    )
      fail("INVALID_CATALOG", `${name}: provide a bounded array.`);
  for (const card of catalog.cards) {
    if (card.layers?.length || card.presentation)
      fail(
        "UNSUPPORTED_PRESENTATION",
        `cards.${card.id}: this website supports symbols or one metadata.image, not layered presentations.`,
      );
    if (card.metadata?.image !== undefined && !safeImage(card.metadata.image))
      fail(
        "INVALID_IMAGE",
        `cards.${card.id}.metadata.image: use a supported static image path.`,
      );
    if (
      card.metadata?.symbol !== undefined &&
      (typeof card.metadata.symbol !== "string" ||
        card.metadata.symbol.length > 8)
    )
      fail(
        "INVALID_SYMBOL",
        `cards.${card.id}.metadata.symbol: use a short original symbol.`,
      );
    if (card.onOpen?.length)
      fail(
        "UNSUPPORTED_ACTION",
        "cards." +
          card.id +
          ": opening actions require a separate host integration.",
      );
  }
  for (const product of catalog.products) {
    if (
      product.price?.currencyId !== "nx" ||
      !Number.isSafeInteger(product.price?.amount) ||
      product.price.amount < 1 ||
      product.price.amount * product.maxQuantity > 100000000
    )
      fail(
        "INVALID_PRICE",
        `products.${product.id}.price: positive NX price and maximum quantity must total no more than 100,000,000.`,
      );
    const policy = product.duplicatePolicy;
    const independent =
      policy === undefined ||
      policy === "allow" ||
      (policy?.scope === "none" && policy?.fallback === "allow");
    if (product.pity || !independent)
      fail(
        "UNSUPPORTED_ODDS",
        `products.${product.id}: this website supports independent weighted draws with duplicates allowed.`,
      );
    if (!Array.isArray(product.slots))
      fail(
        "INVALID_SLOTS",
        `products.${product.id}.slots: provide draw slots.`,
      );
    for (const slot of product.slots)
      if (
        slot.probability &&
        slot.probability.numerator !== slot.probability.denominator
      )
        fail(
          "UNSUPPORTED_ODDS",
          `products.${product.id}.slots.${slot.id}: this website supports guaranteed slots only.`,
        );
  }
  for (const variant of catalog.variants) {
    if (variant.layers?.length || variant.presentation)
      fail(
        "UNSUPPORTED_PRESENTATION",
        `variants.${variant.id}: layered presentations are not supported.`,
      );
    if (variant.onOpen?.length || Object.keys(variant.bindings ?? {}).length)
      fail(
        "UNSUPPORTED_ACTION",
        `variants.${variant.id}: custom actions and data bindings require a separate host integration.`,
      );
    if (variant.codes?.length && !rewardsEnabled)
      fail(
        "REWARDS_DISABLED",
        `variants.${variant.id}: configure the supported reward provider before enabling codes.`,
      );
    const old = previous?.variants.find((v) => v.id === variant.id);
    if (!isDeepStrictEqual(variant.codes ?? [], old?.codes ?? []))
      fail(
        "REWARD_CONFIGURATION",
        `variants.${variant.id}: change code assignments through the documented provider deployment process.`,
      );
  }
  return catalog;
}

export class Administration {
  constructor(library, { rewardsEnabled = false } = {}) {
    this.library = library;
    this.rewardsEnabled = rewardsEnabled;
  }
  require(person) {
    if (!person || !Number.isSafeInteger(person.accountId))
      fail("UNAUTHENTICATED", "Sign in to administer this website.", 401);
    if (!this.library.journal.administrator(person.accountId))
      fail("FORBIDDEN", "Website administrator access is required.", 403);
    return {
      ...this.library.actor(person),
      permissions: [
        "catalog.read",
        "catalog.preview",
        "catalog.publish",
        "audit.read",
        "admin.read",
      ],
    };
  }
  catalog(person) {
    return {
      catalog: this.library.core.operatorCatalog(this.require(person)),
      adminRevision: this.revision(),
    };
  }
  revision() {
    return this.library.core.adminOverview
      ? this.library.core.adminOverview({ permissions: ["admin.read"] })
          .revision
      : 0;
  }
  preview(person, { catalog }) {
    const actor = this.require(person),
      base = this.library.core.operatorCatalog(actor),
      submitted = structuredClone(catalog);
    validateWebsiteCatalog(submitted, base, {
      rewardsEnabled: this.rewardsEnabled,
    });
    const candidate = { ...structuredClone(base), ...submitted };
    for (const section of [
      "currencies",
      "lines",
      "rarities",
      "cards",
      "variants",
      "products",
      "recipes",
      "combinations",
      "cardTypes",
      "displayFields",
    ]) {
      if (!Array.isArray(base[section]) || submitted[section] === undefined)
        continue;
      if (!Array.isArray(submitted[section]))
        fail("INVALID_CATALOG", section + ": provide an array.");
      const incomingIds = new Set();
      for (const row of submitted[section]) {
        if (!row || typeof row.id !== "string" || incomingIds.has(row.id))
          fail(
            "INVALID_CATALOG",
            section + ": every entry needs a unique stable ID.",
          );
        incomingIds.add(row.id);
      }
      const replacements = new Map(
        submitted[section].map((row) => [row.id, row]),
      );
      candidate[section] = base[section].map(
        (row) => replacements.get(row.id) ?? row,
      );
      const oldIds = new Set(base[section].map((row) => row.id));
      candidate[section].push(
        ...submitted[section].filter((row) => !oldIds.has(row.id)),
      );
    }
    validateWebsiteCatalog(candidate, base, {
      rewardsEnabled: this.rewardsEnabled,
    });
    candidate.version = base.version + 1;
    for (const product of candidate.products) {
      const old = base.products.find((p) => p.id === product.id);
      product.revision = old ? old.revision : 1;
      if (old && !isDeepStrictEqual(product, old)) product.revision++;
    }
    const preview = this.library.core.previewImport(actor, {
      source: candidate,
      mode: "replace",
      expectedVersion: base.version,
    });
    const value = { ...preview, adminRevision: this.revision() },
      previewId = randomUUID();
    this.library.journal.preview(previewId, person.accountId, value);
    const counts = { added: 0, changed: 0, retired: 0 };
    for (const change of preview.changes)
      counts[change.action === "add" ? "added" : "changed"]++;
    counts.retired = candidate.products.filter(
      (p) =>
        p.enabled === false &&
        base.products.find((old) => old.id === p.id)?.enabled !== false,
    ).length;
    return {
      previewId,
      digest: preview.digest,
      expectedVersion: preview.expectedVersion,
      policyRevision: preview.policyRevision,
      adminRevision: value.adminRevision,
      counts,
      changes: preview.changes,
      warnings: preview.warnings,
    };
  }
  async publish(person, input, revalidate = async () => person) {
    key(input.idempotencyKey);
    this.require(person);
    return this.library.coordinatePublication(async () => {
      const fresh = await revalidate(),
        actor = this.require(fresh);
      const preview = this.library.journal.getPreview(
        input.previewId,
        person.accountId,
      );
      if (!preview)
        fail("PREVIEW_EXPIRED", "Preview again before publishing.", 409);
      for (const field of [
        "digest",
        "expectedVersion",
        "policyRevision",
        "adminRevision",
      ])
        if (input[field] !== preview[field])
          fail(
            "PREVIEW_CHANGED",
            "The submitted preview differs from the reviewed draft.",
            409,
          );
      // A newer catalog makes a fresh import with this old version impossible;
      // allow the framework to verify an exact committed replay after response loss.
      if (this.revision() !== preview.adminRevision &&
          this.library.core.catalog().version <= preview.expectedVersion)
        fail(
          "STALE_IMPORT",
          "Administrator policy changed; preview again.",
          409,
        );
      const result = this.library.core.commitImport(actor, {
        key: input.idempotencyKey,
        manifest: preview.manifest,
        digest: preview.digest,
        expectedVersion: preview.expectedVersion,
        policyRevision: preview.policyRevision,
      });
      const prior = this.library.journal.getPreview(
        input.previewId,
        person.accountId,
      );
      if (!prior.published) {
        this.library.journal.activity(person.accountId, "catalog.published", {
          version: result.version,
          digest: preview.digest,
        });
        this.library.journal.db
          .prepare("UPDATE catalog_previews SET body=? WHERE id=?")
          .run(
            JSON.stringify({ ...preview, published: true }),
            input.previewId,
          );
      }
      return result;
    });
  }
  async overview(person, revalidate = async () => person) {
    this.require(person);
    const [health, diagnostics] = await Promise.allSettled([
      this.library.game("/health", {}),
      this.library.game("/diagnostics", {}),
    ]);
    const game =
      health.status === "fulfilled"
        ? health.value
        : { ok: false, callbackReady: false };
    this.require(await revalidate());
    return {
      catalogVersion: this.library.core.catalog().version,
      adminRevision: this.revision(),
      gameReady: game.ok === true && game.paymentProtocol === "cosmic-payments/1",
      leaseReady: game.leaseReady === true,
      callbackReady: game.callbackReady === true,
      gameDiagnostics:
        diagnostics.status === "fulfilled"
          ? diagnostics.value
          : { available: false },
      rewardsEnabled: this.rewardsEnabled,
      provider: this.rewardsEnabled ? "maplestory.v83 / Series One" : null,
      storage: this.library.readiness(),
      ...this.library.journal.diagnostics(),
      lastRecovery: this.library.lastRecovery ?? null,
      unmatchedPayments: this.library.journal.paymentDiscoveryPage({ limit: 25 }),
      recentActivity: this.library.journal.activityPage({ limit: 10 }).items,
    };
  }
  async orders(person, options, revalidate = async () => person) {
    this.require(person);
    const clean = pageOptions(options);
    if (
      clean.search &&
      !/^\d+$/.test(clean.search) &&
      !/^[a-f0-9]{64}$/.test(clean.search)
    ) {
      if (!/^[A-Za-z0-9]{3,13}$/.test(clean.search))
        return { items: [], next: null };
      try {
        const target = await this.library.game("/resolve-account", {
          name: clean.search,
        });
        clean.search = String(target.accountId);
      } catch (error) {
        if (error.code === "ACCOUNT_UNAVAILABLE")
          return { items: [], next: null };
        throw error;
      }
      this.require(await revalidate());
    }
    const page = this.library.journal.orderPage(clean);
    return {
      ...page,
      items: page.items.map((o) => ({
        id: o.id,
        accountId: o.accountId,
        accountName: o.accountName ?? null,
        state: o.state,
        productId: o.input.productId,
        quantity: o.input.quantity,
        cashType: o.input.cashType,
        amount: o.quote.price.amount,
        failureCode: o.failureCode ?? null,
        attempts: o.attempts ?? 0,
        lastError: o.lastError ?? null,
        lastAttemptAt: o.lastAttemptAt ?? null,
        nextAttemptAt: o.nextAttemptAt ?? null,
        createdAt: o.createdAt,
        updatedAt: o.updatedAt ?? o.createdAt,
      })),
    };
  }
  activity(person, options) {
    this.require(person);
    return this.library.journal.activityPage(pageOptions(options));
  }
  registrations(person, options = {}) {
    this.require(person);
    const limit = Number(options.limit ?? 25),
      after = String(options.after ?? "");
    if (
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100 ||
      (after && !/^[A-Za-z0-9_-]{1,100}$/.test(after))
    )
      fail(
        "INVALID_PAGE",
        "Use the supplied registration cursor and a page size from 1 to 100.",
      );
    return this.library.journal.registrationPage({ limit, after });
  }
  async retryRegistration(person, id, revalidate = async () => person) {
    this.require(person);
    const fresh = await revalidate();
    this.require(fresh);
    if (
      !this.library.journal.db
        .prepare("SELECT 1 FROM registration_diagnostics WHERE code_id=?")
        .get(id)
    )
      fail("NOT_FOUND", "Registration not found.", 404);
    this.library.journal.activity(
      person.accountId,
      "registration.retry_requested",
      { issuanceId: id },
    );
    await this.library.reconcile({ issuanceId: id, force: true });
    return { id, state: this.library.journal.registration(id) };
  }
  async retry(person, id, revalidate = async () => person) {
    this.require(person);
    const order = this.library.journal.order(id);
    if (!order) fail("NOT_FOUND", "Order not found.", 404);
    return this.library.retry(order, async () => {
      const fresh = await revalidate();
      this.require(fresh);
      this.library.journal.activity(person.accountId, "order.retry_requested", {
        orderId: id,
      });
    });
  }
}
