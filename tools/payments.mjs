import { resolve, join } from "node:path";
import { existsSync } from "node:fs";
import { CardFramework, createCodeVault } from "@digital-card/framework";
import { SQLiteStore } from "@digital-card/framework/sqlite";
import { Journal } from "../src/journal.mjs";
import { Library } from "../src/library.mjs";
import { PaymentAuthority, paymentProviderId } from "../src/payment-authority.mjs";
import { createGameClient } from "../src/protocol.mjs";
import { resolveLegacyPayment } from "../src/payment-resolution.mjs";
import { attributeLegacyPayment, requireManagedEnvironment, retainedJournalOrder } from "../src/legacy-backup.mjs";

const args = process.argv.slice(2), options = {}, action = args.shift();
const names = action === "attribute-legacy" ? ["--order-id", "--deployment-directory", "--decision-key", "--reason", "--operator-id"] : ["--order-id", "--purchase-key", "--decision-key", "--reason", "--operator-id"];
let valid = ["resolve-legacy", "attribute-legacy"].includes(action) && args.pop() === "--confirm-bridge-stopped";
for (let i = 0; i < args.length; i += 2) {
  const name = args[i];
  if (!names.includes(name) ||
      !args[i + 1] || Object.hasOwn(options, name)) { valid = false; break; }
  options[name] = args[i + 1];
}
if (!valid || Object.keys(options).length !== 5) {
  console.error("Usage: node tools/payments.mjs resolve-legacy --order-id ORDER_ID --purchase-key ORIGINAL_PURCHASE_KEY --decision-key DECISION_KEY --reason REASON --operator-id OPERATOR_ID --confirm-bridge-stopped. For retained account attribution, use attribute-legacy with --deployment-directory DIRECTORY instead of --purchase-key. Back up and stop the bridge writer; keep the game adapter running.");
  process.exit(2);
}
let core, journal, store;
try {
  if (!process.env.STATE_DIRECTORY) throw Error("Missing state directory");
  const directory = resolve(process.env.STATE_DIRECTORY);
  for (const name of ["framework.sqlite", "bridge.sqlite"])
    if (!existsSync(join(directory, name))) throw Error("Existing paired state required");
  if (action === "attribute-legacy") {
    requireManagedEnvironment(options["--deployment-directory"], process.env);
    const order = retainedJournalOrder(directory, options["--order-id"]);
    const result = await attributeLegacyPayment({
      library: { journal: { order: () => order }, game: createGameClient({ url: process.env.GAME_URL, secret: process.env.GAME_SHARED_KEY }) },
      deploymentDirectory: options["--deployment-directory"], orderId: options["--order-id"],
      decisionKey: options["--decision-key"], operatorId: options["--operator-id"], reason: options["--reason"],
    });
    console.log(JSON.stringify(result));
  } else {
  const key = (name) => {
    const value = Buffer.from(process.env[name] ?? "", "base64");
    if (value.length !== 32) throw Error("Invalid installation key");
    return value;
  };
  const authority = new PaymentAuthority();
  store = new SQLiteStore(join(directory, "framework.sqlite"), { encryptionKey: key("STATE_KEY") });
  core = new CardFramework({ store,
    codeVault: createCodeVault({ activeKeyId: "v1", keys: { v1: key("CODE_ENCRYPTION_KEY") }, indexKey: key("CODE_INDEX_KEY") }),
    externalPurchaseProviders: { [paymentProviderId]: authority },
  });
  journal = new Journal(join(directory, "bridge.sqlite"));
  const library = new Library({ framework: core, store, journal, paymentAuthority: authority,
    game: createGameClient({ url: process.env.GAME_URL, secret: process.env.GAME_SHARED_KEY ?? "" }),
  });
  console.log(JSON.stringify(await resolveLegacyPayment({ library,
    orderId: options["--order-id"], purchaseKey: options["--purchase-key"],
    decisionKey: options["--decision-key"], reason: options["--reason"], operatorId: options["--operator-id"],
  })));
  }
} catch (error) {
  console.error(JSON.stringify({ code: /^[A-Z][A-Z0-9_]{0,80}$/.test(error.code ?? "") ? error.code : "PAYMENT_RECOVERY_FAILED",
    message: "The original payment remains recoverable. Check authoritative delivery evidence, the exact decision key and installation keys. Retry the same decision; never replace or refund an unverified obligation." }));
  process.exitCode = 1;
} finally { journal?.close(); if (core) core.close(); else store?.close(); }
