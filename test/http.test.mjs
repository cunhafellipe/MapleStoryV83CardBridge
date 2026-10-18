import test from "node:test";
import assert from "node:assert/strict";
import { fixture } from "./fixture.mjs";
import { createLibraryHttp } from "../src/http.mjs";
import { fail, hash, mac } from "../src/protocol.mjs";

async function httpFixture(t) {
  const x = fixture(),
    sessions = new Map(),
    secret = "shared-contract-key-".repeat(3),
    csrfSecret = "csrf-contract-key-".repeat(3);
  let attempts = 0;
  const game = async (path, value) => {
    if (path === "/login") {
      attempts++;
      if (value.username !== "Collector" || value.password !== "fixture")
        fail("UNAUTHENTICATED", "Invalid account", 401);
      sessions.set(value.tokenHash, x.person);
      return x.person;
    }
    if (path === "/session") {
      if (!sessions.has(value.tokenHash))
        fail("UNAUTHENTICATED", "Expired session", 401);
      return sessions.get(value.tokenHash);
    }
    if (path === "/logout") {
      sessions.delete(value.tokenHash);
      return { ok: true };
    }
    return x.game(path, value);
  };
  const server = createLibraryHttp({
    library: x.library,
    game,
    origin: "http://127.0.0.1:8487",
    secret,
    csrfSecret,
    authMode: "bridge",
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    await new Promise((r) => server.close(r));
    x.close();
  });
  const base = "http://127.0.0.1:" + server.address().port;
  const guestResponse = await fetch(base + "/api/library/session"),
    guest = await guestResponse.json(),
    cookie = guestResponse.headers.get("set-cookie").split(";")[0];
  const headers = {
    Origin: "http://127.0.0.1:8487",
    "Content-Type": "application/json",
    Cookie: cookie,
    "X-CSRF-Token": guest.csrf,
  };
  const post = (route, value, h = headers) =>
    fetch(base + "/api/library/" + route, {
      method: "POST",
      headers: h,
      body: JSON.stringify(value),
    });
  return {
    x,
    sessions,
    post,
    headers,
    base,
    secret,
    get attempts() {
      return attempts;
    },
  };
}
test("readiness requires the game, matching account mode and a verified callback", async (t) => {
  const h = await httpFixture(t);
  const ready = await fetch(h.base + "/api/library/health");
  assert.equal(ready.status, 200);
  assert.equal((await ready.json()).callbackReady, true);
  h.x.faults.health = true;
  assert.equal((await fetch(h.base + "/api/library/health")).status, 503);
  h.x.faults.health = false;
  h.x.faults.sessionSource = "grove";
  assert.equal((await fetch(h.base + "/api/library/health")).status, 503);
  h.x.faults.sessionSource = "bridge";
  const original = h.x.game;
  h.x.game = async (path, input) => {
    const result = await original(path, input);
    if (path === "/health") delete result.paymentProtocol;
    return result;
  };
  assert.equal((await fetch(h.base + "/api/library/health")).status, 503);
});
test("callback probes require the exact signed body and do not change code state", async (t) => {
  const h = await httpFixture(t),
    path = "/api/library/provider/health",
    body = "{}",
    time = String(Date.now()),
    nonce = "1".repeat(32);
  assert.equal(
    (await fetch(h.base + path, { method: "POST", body })).status,
    403,
  );
  const signature = mac(
    h.secret,
    ["POST", path, time, nonce, hash(body)].join("\n"),
  );
  const headers = {
    "X-Bridge-Time": time,
    "X-Bridge-Nonce": nonce,
    "X-Bridge-Signature": signature,
  };
  const response = await fetch(h.base + path, {
    method: "POST",
    body,
    headers,
  });
  assert.deepEqual(await response.json(), {
    ok: true,
    protocol: "v83-card-bridge/1",
  });
  assert.equal(
    (await fetch(h.base + path, { method: "POST", body, headers })).status,
    403,
  );
  assert.equal(h.x.registrations.size, 0);
});
test("native login verifies CSRF and credentials, stores only a token hash, and logout revokes ownership", async (t) => {
  const h = await httpFixture(t),
    account = { username: "Collector", password: "fixture" };
  assert.equal(
    (
      await h.post("login", account, {
        ...h.headers,
        Origin: "https://other.example",
      })
    ).status,
    403,
  );
  assert.equal(
    (
      await h.post("login", account, {
        ...h.headers,
        "X-CSRF-Token": "invalid",
      })
    ).status,
    403,
  );
  assert.equal(h.attempts, 0);
  assert.equal(
    (await h.post("login", { ...account, password: "incorrect" })).status,
    401,
  );
  assert.equal((await h.post("login", null)).status, 400);
  const response = await h.post("login", account),
    session = await response.json();
  assert.equal(response.status, 200);
  assert.equal(session.signedIn, true);
  assert.equal(session.username, "Collector");
  const cookie = response.headers.get("set-cookie");
  assert.match(cookie, /HttpOnly; SameSite=Strict/);
  const token = cookie.split(";")[0].split("=")[1];
  assert.equal(token.length, 43);
  assert(h.sessions.has(hash(token)));
  assert(!h.sessions.has(token));
  const headers = {
    ...h.headers,
    Cookie: cookie.split(";")[0],
    "X-CSRF-Token": session.csrf,
  };
  const quote = await h
    .post("quote", { productId: "first-light", cashType: 2 }, headers)
    .then((r) => r.json());
  assert.equal(
    (await h.post("buy", { key: "native-purchase", ...quote }, headers)).status,
    200,
  );
  assert.equal(h.x.maplePoints.get(1), 9000);
  assert.equal(h.x.balances.get(1), 10000);
  assert.equal((await h.post("logout", {}, headers)).status, 200);
  assert.equal(h.sessions.size, 0);
  assert.equal(
    (await fetch(h.base + "/api/library/state", { headers })).status,
    401,
  );
  assert.equal(
    (await h.post("buy", { key: "after-logout", ...quote }, headers)).status,
    401,
  );
});
test("native sign-in throttles repeated failed credentials before the private game request", async (t) => {
  const h = await httpFixture(t);
  for (let i = 0; i < 8; i++)
    assert.equal(
      (await h.post("login", { username: "Collector", password: "incorrect" }))
        .status,
      401,
    );
  assert.equal(
    (await h.post("login", { username: "Collector", password: "incorrect" }))
      .status,
    429,
  );
  assert.equal(h.attempts, 8);
});
