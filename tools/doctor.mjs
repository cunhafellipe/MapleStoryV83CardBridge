import { createGameClient } from "../src/protocol.mjs";
const quiet = process.argv.includes("--quiet");
try {
  const origin =
    process.env.CARD_CHECK_URL ??
    "http://127.0.0.1:" + (process.env.PORT ?? 8487);
  const response = await fetch(new URL("/api/library/health", origin), {
    redirect: "error",
    signal: AbortSignal.timeout(12000),
  });
  const result = await response.json();
  if (
    !response.ok ||
    result.ok !== true ||
    result.gameReady !== true ||
    result.callbackReady !== true ||
    result.leaseReady !== true ||
    result.storage?.ok !== true
    || result.paymentProtocol !== "cosmic-payments/1"
  )
    throw Error(result.code ?? "NOT_READY");
  if (process.env.GAME_SHARED_KEY && process.env.GAME_URL) {
    const game = createGameClient({
      url: process.env.GAME_URL,
      secret: process.env.GAME_SHARED_KEY,
    });
    const health = await game("/health", {});
    if (!health.callbackReady || !health.leaseReady || health.paymentProtocol !== "cosmic-payments/1")
      throw Error("CALLBACK_OR_LEASE_UNAVAILABLE");
  }
  if (!quiet)
    console.log(
      "Ready: game adapter, database writer, authenticated callback, account mode, cash types and catalog.",
    );
} catch (error) {
  if (!quiet)
    console.error(
      "Not ready: " +
        (/^[A-Z_]+$/.test(error.message)
          ? error.message
          : "CONNECTION_FAILED") +
        ". Check the game and card service logs; no keys are printed.",
    );
  process.exitCode = 1;
}
