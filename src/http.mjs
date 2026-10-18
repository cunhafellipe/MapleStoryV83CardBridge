import { randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { seriesOne } from "./series-one.mjs";
import { readFile, realpath, stat } from "node:fs/promises";
import { join, relative, extname, sep, isAbsolute } from "node:path";
import { hash, mac, equal, fail, verifyRequest } from "./protocol.mjs";
const types = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".mp3": "audio/mpeg",
  ".woff2": "font/woff2",
};
export async function body(req, maximum = 16384) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maximum) fail("PAYLOAD_TOO_LARGE", "Request is too large.", 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}
export function createLibraryHttp({
  library,
  game,
  origin,
  secret,
  csrfSecret,
  webRoot,
  assetRoot,
  trustProxy = false,
  authMode = "grove",
  cookieName = authMode === "bridge" ? "card_library" : "qg_account",
}) {
  if (
    new URL(origin).origin !== origin ||
    secret.length < 32 ||
    csrfSecret.length < 32
  )
    throw Error("Invalid HTTP security configuration");
  if (!["bridge", "grove"].includes(authMode))
    throw Error("Invalid authentication mode");
  const nonces = new Map(),
    rates = new Map(),
    logins = new Map();
  const cookies = (req) =>
    Object.fromEntries(
      (req.headers.cookie ?? "").split(";").map((p) => p.trim().split("=")),
    );
  const setCookie = (res, name, value, age) =>
    res.setHeader(
      "Set-Cookie",
      name +
        "=" +
        value +
        "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" +
        age +
        (origin.startsWith("https:") ? "; Secure" : ""),
    );
  function response(res, status, value) {
    res.writeHead(status, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(JSON.stringify(value));
  }
  function rate(req) {
    const forwarded =
      trustProxy &&
      ["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(
        req.socket.remoteAddress,
      )
        ? req.headers["x-real-ip"]
        : null;
    const id =
        typeof forwarded === "string" && /^[0-9a-fA-F:.]{2,50}$/.test(forwarded)
          ? forwarded
          : req.socket.remoteAddress,
      time = Date.now();
    let row = rates.get(id);
    if (!row || row.until < time) {
      if (rates.size > 10000) rates.clear();
      row = { until: time + 60000, count: 0 };
      rates.set(id, row);
    }
    if (++row.count > 240)
      fail("RATE_LIMIT", "Please wait a moment before trying again.", 429);
  }
  async function identity(req) {
    const token = cookies(req)[cookieName];
    if (!/^[A-Za-z0-9_-]{43}$/.test(token ?? "")) return null;
    try {
      return {
        ...(await game("/session", { tokenHash: hash(token) })),
        csrf: mac(csrfSecret, token),
      };
    } catch (e) {
      if (e.status === 401) return null;
      throw e;
    }
  }
  async function staticFile(res, root, path) {
    if (!root) fail("NOT_FOUND", "Page not found.", 404);
    const base = await realpath(root);
    let file;
    try {
      file = await realpath(join(base, path));
    } catch {
      fail("NOT_FOUND", "Page not found.", 404);
    }
    const rel = relative(base, file);
    if (
      isAbsolute(rel) ||
      rel === ".." ||
      rel.startsWith(".." + sep) ||
      extname(file) === "" ||
      !Object.hasOwn(types, extname(file))
    )
      fail("NOT_FOUND", "Page not found.", 404);
    const info = await stat(file);
    if (!info.isFile() || info.size > 32 * 1024 * 1024)
      fail("NOT_FOUND", "Page not found.", 404);
    res.writeHead(200, {
      "Content-Type": types[extname(file)],
      "Cache-Control": [".html", ".json", ".css", ".js", ".mjs"].includes(
        extname(file),
      )
        ? "no-cache"
        : "public, max-age=86400",
    });
    res.end(await readFile(file));
  }
  return createServer(async (req, res) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "SAMEORIGIN");
    res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://maplestoryitcg.weebly.com; media-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
    );
    try {
      const url = new URL(req.url, "http://local"),
        path = url.pathname;
      if (
        req.method === "POST" &&
        ["/api/library/provider/used", "/api/library/provider/health"].includes(
          path,
        )
      ) {
        const raw = await body(req);
        if (
          !verifyRequest({
            secret,
            method: "POST",
            path,
            headers: req.headers,
            body: raw,
            nonces,
          })
        )
          fail("FORBIDDEN", "Provider authentication failed.", 403);
        const value = JSON.parse(raw);
        if (!value || Array.isArray(value) || typeof value !== "object")
          fail("INVALID_REQUEST", "Submit a JSON object.");
        if (path.endsWith("/health")) {
          response(res, 200, { ok: true, protocol: "v83-card-bridge/1" });
          return;
        }
        library.used(value);
        response(res, 200, { ok: true });
        return;
      }
      if (path.startsWith("/api/library/")) {
        rate(req);
        const route = path.slice("/api/library/".length);
        if (
          url.search &&
          !["collection", "admin/orders", "admin/activity", "admin/registrations"].includes(
            route,
          )
        )
          fail("INVALID_REQUEST", "Unexpected query parameters.");
        for (const field of url.searchParams.keys())
          if (!["after", "limit", "search"].includes(field))
            fail("INVALID_REQUEST", "Unexpected query parameter.");
        if (req.method === "GET" && route === "live") {
          response(res, 200, { ok: true });
          return;
        }
        if (req.method === "GET" && route === "rewards") {
          response(res, 200, {
            ...seriesOne,
            rewards: library.core
              .catalog()
              .variants.some((v) => v.codes?.length)
              ? seriesOne.rewards
              : [],
          });
          return;
        }
        if (req.method === "GET" && route === "catalog") {
          response(res, 200, {
            ...library.catalog(),
            acceptedCashTypes: library.acceptedCashTypes,
          });
          return;
        }
        if (req.method === "GET" && route === "health") {
          const ready = await game("/health", {});
          if (
            !ready ||
            ready.protocol !== "v83-card-bridge/1" ||
            ready.paymentProtocol !== "cosmic-payments/1" ||
            ready.ok !== true ||
            ready.leaseReady !== true ||
            ready.callbackReady !== true ||
            ready.sessionSource !== authMode ||
            !Array.isArray(ready.acceptedCashTypes) ||
            JSON.stringify([...ready.acceptedCashTypes].sort()) !==
              JSON.stringify([...library.acceptedCashTypes].sort())
          )
            fail(
              "CONFIGURATION_MISMATCH",
              "The game and card service configuration do not agree.",
              503,
            );
          const storage = library.readiness();
          if (!storage.ok)
            fail(
              "STORAGE_UNAVAILABLE",
              "Storage is not ready. Browsing remains available.",
              503,
            );
          response(res, 200, {
            ok: true,
            gameReady: true,
            callbackReady: true,
            paymentProtocol: ready.paymentProtocol,
            authMode,
            acceptedCashTypes: library.acceptedCashTypes,
            catalogVersion: library.core.catalog().version,
            leaseReady: ready.leaseReady === true,
            storage,
          });
          return;
        }
        if (route === "session" && req.method === "GET") {
          const person = await identity(req),
            value = person
              ? {
                  signedIn: true,
                  username: person.name,
                  csrf: person.csrf,
                  authMode,
                  role: library.journal.administrator(person.accountId)
                    ? "administrator"
                    : "collector",
                  capabilities: library.journal.administrator(person.accountId)
                    ? ["administration"]
                    : [],
                }
              : { signedIn: false, authMode };
          if (!person && authMode === "bridge") {
            let token = cookies(req).library_guest;
            if (!/^[A-Za-z0-9_-]{43}$/.test(token ?? "")) {
              token = randomBytes(32).toString("base64url");
              setCookie(res, "library_guest", token, 86400);
            }
            value.csrf = mac(csrfSecret, token);
          }
          response(res, 200, value);
          return;
        }
        if (
          route === "login" &&
          req.method === "POST" &&
          authMode === "bridge"
        ) {
          const token = cookies(req).library_guest;
          if (
            req.headers.origin !== origin ||
            !token ||
            !equal(req.headers["x-csrf-token"], mac(csrfSecret, token))
          )
            fail("FORBIDDEN", "Refresh this sign-in form and try again.", 403);
          if (
            !(req.headers["content-type"] ?? "").startsWith("application/json")
          )
            fail("INVALID_REQUEST", "Submit a JSON request.");
          const value = JSON.parse(await body(req));
          if (
            !value ||
            Array.isArray(value) ||
            typeof value.username !== "string" ||
            typeof value.password !== "string" ||
            value.username.length > 13 ||
            value.password.length > 128
          )
            fail("INVALID_REQUEST", "Please check your account details.");
          const name = value.username.toLowerCase(),
            time = Date.now(),
            key = hash(name);
          let attempts = logins.get(key);
          if (!attempts || attempts.until < time) {
            if (logins.size > 10000) logins.clear();
            attempts = { count: 0, until: time + 60000 };
            logins.set(key, attempts);
          }
          if (++attempts.count > 8)
            fail(
              "RATE_LIMIT",
              "Please wait a minute before signing in again.",
              429,
            );
          const accountToken = randomBytes(32).toString("base64url");
          const person = await game("/login", {
            username: value.username,
            password: value.password,
            tokenHash: hash(accountToken),
          });
          value.password = "";
          setCookie(res, cookieName, accountToken, 86400);
          logins.delete(key);
          response(res, 200, {
            signedIn: true,
            username: person.name,
            authMode,
            csrf: mac(csrfSecret, accountToken),
            role: library.journal.administrator(person.accountId)
              ? "administrator"
              : "collector",
            capabilities: library.journal.administrator(person.accountId)
              ? ["administration"]
              : [],
          });
          return;
        }
        const person = await identity(req);
        if (!person)
          fail(
            "UNAUTHENTICATED",
            "Sign in to buy packs and keep your collection.",
            401,
          );
        if (req.method === "GET" && route === "state") {
          response(res, 200, await library.state(person));
          return;
        }
        if (req.method === "GET" && route === "collection") {
          for (const field of url.searchParams.keys())
            if (!["after", "limit"].includes(field)) fail("INVALID_PAGE", "Unsupported collection page option.");
          response(res, 200, library.collectionPage(person, {
            after: url.searchParams.get("after") ?? "", limit: Number(url.searchParams.get("limit") ?? 200),
          }));
          return;
        }
        if (route.startsWith("admin/")) {
          library.admin.require(person);
          if (req.method === "GET") {
            const options = Object.fromEntries(url.searchParams);
            const get = {
              "admin/catalog": () => library.admin.catalog(person),
              "admin/export": () => library.admin.catalog(person),
              "admin/overview": () =>
                library.admin.overview(person, () => identity(req)),
              "admin/orders": () =>
                library.admin.orders(person, options, () => identity(req)),
              "admin/activity": () => library.admin.activity(person, options),
              "admin/registrations": () =>
                library.admin.registrations(person, options),
            };
            if (!Object.hasOwn(get, route))
              fail("NOT_FOUND", "Method not found.", 404);
            response(res, 200, await get[route]());
            return;
          }
        }
        if (req.method !== "POST") fail("NOT_FOUND", "Method not found.", 404);
        if (
          req.headers.origin !== origin ||
          !equal(req.headers["x-csrf-token"], person.csrf)
        )
          fail(
            "FORBIDDEN",
            "This form expired. Refresh the library and try again.",
            403,
          );
        if (!(req.headers["content-type"] ?? "").startsWith("application/json"))
          fail("INVALID_REQUEST", "Submit a JSON request.");
        if (route === "logout" && authMode === "bridge") {
          await game("/logout", { tokenHash: hash(cookies(req)[cookieName]) });
          setCookie(res, cookieName, "", 0);
          response(res, 200, { ok: true });
          return;
        }
        const value = JSON.parse(
          await body(req, route === "admin/preview" ? 2 * 1024 * 1024 : 16384),
        );
        if (!value || Array.isArray(value) || typeof value !== "object")
          fail("INVALID_REQUEST", "Submit a JSON object.");
        if (route.startsWith("admin/")) {
          const revalidate = () => identity(req);
          let result;
          if (route === "admin/preview")
            result = library.admin.preview(person, value);
          else if (route === "admin/publish")
            result = await library.admin.publish(person, value, revalidate);
          else if (/^admin\/orders\/[a-f0-9]{64}\/retry$/.test(route))
            result = await library.admin.retry(
              person,
              route.split("/")[2],
              revalidate,
            );
          else if (
            /^admin\/registrations\/[A-Za-z0-9_-]{1,100}\/retry$/.test(route)
          )
            result = await library.admin.retryRegistration(
              person,
              route.split("/")[2],
              revalidate,
            );
          else fail("NOT_FOUND", "Method not found.", 404);
          response(res, 200, result);
          return;
        }
        const actions = {
          codes: () => library.codePage(person, value),
          inventory: () => library.inventoryPage(person, value),
          collection: () => library.collectionPage(person, value),
          packs: () => library.packPage(person, value),
          quote: () => library.quote(person, value),
          buy: () => library.buy(person, value),
          open: () => library.open(person, value),
          reveal: () => library.reveal(person, value),
          album: () => library.album(person, value),
          refresh: async () => {
            await library.reconcile({
              accountId: person.accountId,
              force: true,
            });
            return library.state(person);
          },
        };
        if (!Object.hasOwn(actions, route))
          fail("NOT_FOUND", "Method not found.", 404);
        response(res, 200, await actions[route]());
        return;
      }
      if (req.method !== "GET" && req.method !== "HEAD")
        fail("NOT_FOUND", "Page not found.", 404);
      if (path.startsWith("/assets/library/")) {
        await staticFile(res, assetRoot, decodeURIComponent(path.slice(16)));
        return;
      }
      if (path === "/" || path === "/library" || path === "/library/") {
        await staticFile(res, webRoot, "index.html");
        return;
      }
      if (path.startsWith("/library/")) {
        await staticFile(res, webRoot, decodeURIComponent(path.slice(9)));
        return;
      }
      fail("NOT_FOUND", "Page not found.", 404);
    } catch (e) {
      if (res.headersSent) {
        res.destroy();
        return;
      }
      const known = e.status || e.statusCode;
      response(res, known ?? (e instanceof SyntaxError ? 400 : 503), {
        error: known
          ? e.message
          : e instanceof SyntaxError
            ? "Invalid request."
            : "The library is temporarily unavailable. Please try shortly.",
        code: known ? e.code : "UNAVAILABLE",
      });
    }
  });
}
