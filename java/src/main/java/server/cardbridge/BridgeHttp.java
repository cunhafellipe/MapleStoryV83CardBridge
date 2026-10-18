package server.cardbridge;

import com.google.gson.*;
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import tools.DatabaseConnection;

public final class BridgeHttp {
  public static final Gson JSON = new GsonBuilder().serializeNulls().create();
  private static final boolean ENABLED = "1".equals(System.getenv("CARD_BRIDGE_ENABLED"));
  private static final ConcurrentHashMap<String, Long> NONCES = new ConcurrentHashMap<>();
  private static Connection lease;
  private static HttpServer listener;
  private static boolean leaseLost;
  private static long lastOutboxLog;
  static java.util.function.IntConsumer terminate = code -> Runtime.getRuntime().halt(code);
  private static final ScheduledExecutorService worker =
      Executors.newSingleThreadScheduledExecutor(
          r -> {
            Thread t = new Thread(r, "card-bridge-outbox");
            t.setDaemon(true);
            return t;
          });

  private BridgeHttp() {}

  public static boolean enabled() {
    return ENABLED;
  }

  static String env(String name) {
    String value = System.getenv(name);
    if (value == null || value.length() < 32)
      throw new IllegalStateException(name + " must contain at least 32 characters");
    return value;
  }

  static String codeKey() {
    return env("CARD_BRIDGE_CODE_KEY");
  }

  public static class Problem extends RuntimeException {
    final int status;
    final String code;

    public Problem(int status, String code, String message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  public static synchronized void requireLease() throws SQLException {
    if (leaseLost) throw new SQLException("Bridge writer lease unavailable");
    try {
      if (lease == null || lease.isClosed())
        throw new SQLException("Bridge writer lease unavailable");
      try (Statement st = lease.createStatement();
          ResultSet rs =
              st.executeQuery("SELECT IS_USED_LOCK('card_bridge_writer')=CONNECTION_ID()")) {
        rs.next();
        if (rs.getInt(1) != 1) throw new SQLException("Bridge writer lease lost");
      }
    } catch (SQLException e) {
      leaseLost = true;
      System.err.println(
          "card_bridge: WRITER_LEASE_LOST; restarting game to fence old reward claims");
      terminate.accept(75);
      throw e;
    }
  }

  static void uncertainCommit() {
    System.err.println(
        "card_bridge: COMMIT_UNCERTAIN; restarting game before further inventory saves");
    terminate.accept(75);
    throw new IllegalStateException("Commit outcome requires process recovery");
  }

  public static synchronized void acquireLease() {
    if (!ENABLED || lease != null) return;
    try {
      env("CARD_BRIDGE_SHARED_KEY");
      codeKey();
      Connection candidate = DatabaseConnection.getConnection();
      try {
        candidate.setNetworkTimeout(Runnable::run, 5000);
        try (Statement st = candidate.createStatement();
            ResultSet rs = st.executeQuery("SELECT GET_LOCK('card_bridge_writer',0)")) {
          if (!rs.next() || rs.getInt(1) != 1)
            throw new SQLException("Another bridge game writer is running");
        }
        lease = candidate;
      } catch (Exception error) {
        candidate.close();
        throw error;
      }
    } catch (Exception error) {
      System.err.println("card_bridge: WRITER_LEASE_UNAVAILABLE; game startup stopped");
      terminate.accept(75);
      throw new IllegalStateException("Bridge writer lease acquisition failed", error);
    }
  }

  public static void start() {
    if (!ENABLED || listener != null) return;
    try {
      env("CARD_BRIDGE_SHARED_KEY");
      codeKey();
      if (config.YamlConfig.config.server.USE_MTS)
        throw new IllegalStateException(
            "MTS requires a qualified native inventory integration and is unsupported by this"
                + " bridge profile");
      acquireLease();
      try (InputStream stream = BridgeHttp.class.getResourceAsStream("/card-bridge/schema.sql")) {
        if (stream == null) throw new IllegalStateException("Card bridge schema resource missing");
        String sql = new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        try (Statement st = lease.createStatement()) {
          for (String part : sql.split(";")) if (!part.isBlank()) st.execute(part);
        }
      }
      try (ResultSet columns =
          lease
              .getMetaData()
              .getColumns(lease.getCatalog(), null, "card_bridge_payments", "cash_type")) {
        if (!columns.next())
          try (Statement st = lease.createStatement()) {
            st.execute(
                "ALTER TABLE card_bridge_payments ADD COLUMN cash_type INT NOT NULL DEFAULT 1");
          }
      }
      BridgeWallet.acceptedTypes();
      BridgePayments.initialize(lease);
      for (String[] column :
          new String[][] {
            {"attempts", "INT NOT NULL DEFAULT 0"}, {"last_attempt", "BIGINT"},
            {"next_attempt", "BIGINT NOT NULL DEFAULT 0"}, {"last_error", "VARCHAR(48)"}
          }) {
        try (ResultSet columns =
            lease
                .getMetaData()
                .getColumns(lease.getCatalog(), null, "card_bridge_outbox", column[0])) {
          if (!columns.next())
            try (Statement st = lease.createStatement()) {
              st.execute(
                  "ALTER TABLE card_bridge_outbox ADD COLUMN " + column[0] + " " + column[1]);
            }
        }
      }
      if (!Set.of("bridge", "grove")
          .contains(System.getenv().getOrDefault("CARD_BRIDGE_SESSION_SOURCE", "bridge")))
        throw new IllegalStateException("Invalid session source");
      int port = Integer.parseInt(System.getenv().getOrDefault("CARD_BRIDGE_PORT", "8486"));
      listener =
          HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), port), 32);
      listener.setExecutor(
          new ThreadPoolExecutor(
              2,
              8,
              60,
              TimeUnit.SECONDS,
              new ArrayBlockingQueue<>(64),
              r -> {
                Thread t = new Thread(r, "card-bridge-http");
                t.setDaemon(true);
                return t;
              },
              new ThreadPoolExecutor.AbortPolicy()));
      listener.createContext("/", BridgeHttp::handle);
      listener.start();
      worker.scheduleWithFixedDelay(BridgeHttp::deliver, 2, 5, TimeUnit.SECONDS);
      Runtime.getRuntime()
          .addShutdownHook(
              new Thread(
                  () -> {
                    if (listener != null) listener.stop(1);
                    worker.shutdownNow();
                    try {
                      if (lease != null) lease.close();
                    } catch (SQLException ignored) {
                    }
                  }));
    } catch (Exception e) {
      throw new IllegalStateException("Card bridge startup failed", e);
    }
  }

  private static int number(JsonObject value, String name) {
    JsonElement e = value.get(name);
    if (e == null
        || !e.isJsonPrimitive()
        || !e.getAsJsonPrimitive().isNumber()
        || !e.toString().matches("[0-9]{1,10}"))
      throw new IllegalArgumentException("Invalid " + name);
    return Integer.parseInt(e.toString());
  }

  private static String text(JsonObject value, String name, int limit) {
    JsonElement e = value.get(name);
    if (e == null
        || !e.isJsonPrimitive()
        || !e.getAsJsonPrimitive().isString()
        || e.getAsString().length() > limit) throw new IllegalArgumentException("Invalid " + name);
    return e.getAsString();
  }

  private static void handle(HttpExchange exchange) throws IOException {
    int status = 200;
    Object result;
    try {
      String path = exchange.getRequestURI().getRawPath();
      byte[] bytes = exchange.getRequestBody().readNBytes(16385);
      if (bytes.length > 16384)
        throw new Problem(413, "PAYLOAD_TOO_LARGE", "Request exceeds the supported size.");
      String body = new String(bytes, StandardCharsets.UTF_8);
      Headers h = exchange.getRequestHeaders();
      if (!exchange.getRequestMethod().equals("POST")
          || exchange.getRequestURI().getRawQuery() != null
          || !BridgeCrypto.verify(
              env("CARD_BRIDGE_SHARED_KEY"),
              "POST",
              path,
              h.getFirst("X-Bridge-Time"),
              h.getFirst("X-Bridge-Nonce"),
              h.getFirst("X-Bridge-Signature"),
              body,
              NONCES,
              System.currentTimeMillis()))
        throw new Problem(403, "FORBIDDEN", "Bridge authentication failed.");
      requireLease();
      JsonObject value = JsonParser.parseString(body).getAsJsonObject();
      result =
          switch (path) {
            case "/health" -> health();
            case "/diagnostics" -> BridgeRewards.diagnostics();
            case "/resolve-account" ->
                BridgeSessions.resolve(
                    value.has("name") ? text(value, "name", 13) : null,
                    value.has("accountId") ? number(value, "accountId") : null);
            case "/session" -> BridgeSessions.session(text(value, "tokenHash", 64));
            case "/login" ->
                BridgeSessions.login(
                    text(value, "username", 13),
                    text(value, "password", 128),
                    text(value, "tokenHash", 64));
            case "/logout" -> BridgeSessions.logout(text(value, "tokenHash", 64));
            case "/wallet" -> BridgeWallet.wallet(number(value, "accountId"));
            case "/payment/debit" -> BridgePayments.admit(value, "debit");
            case "/payment/cancel" -> BridgePayments.admit(value, "cancel");
            case "/payment/adopt-legacy" -> BridgePayments.admit(value, "adopt-legacy");
            case "/payment/attribute-legacy" -> BridgePayments.attributeLegacy(value);
            case "/payment/status" -> BridgePayments.status(value);
            case "/payment/pending" -> BridgePayments.pending(value);
            case "/payment/finalize" -> BridgePayments.complete(value, false);
            case "/payment/refund" -> BridgePayments.complete(value, true);
            case "/debit" ->
                BridgeWallet.debit(
                    text(value, "orderId", 64),
                    number(value, "accountId"),
                    number(value, "amount"),
                    value.has("cashType") ? number(value, "cashType") : 1);
            case "/codes/register" ->
                BridgeRewards.register(
                    text(value, "issuanceId", 36),
                    number(value, "accountId"),
                    text(value, "code", 40),
                    number(value, "itemId"),
                    number(value, "quantity"),
                    number(value, "petDays"),
                    number(value, "series"));
            case "/codes/status" -> BridgeRewards.status(text(value, "issuanceId", 36));
            default -> throw new Problem(404, "NOT_FOUND", "Bridge method not found.");
          };
    } catch (Problem e) {
      status = e.status;
      result = Map.of("code", e.code, "message", e.getMessage());
    } catch (IllegalArgumentException | IllegalStateException e) {
      status = 400;
      result = Map.of("code", "INVALID_REQUEST", "message", "Invalid bridge request.");
    } catch (Exception e) {
      status = 503;
      result =
          Map.of(
              "code",
              "GAME_UNAVAILABLE",
              "message",
              "The game service is temporarily unavailable.");
    }
    byte[] output = JSON.toJson(result).getBytes(StandardCharsets.UTF_8);
    exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
    exchange.getResponseHeaders().set("Cache-Control", "no-store");
    exchange.sendResponseHeaders(status, output.length);
    try (OutputStream os = exchange.getResponseBody()) {
      os.write(output);
    } finally {
      exchange.close();
    }
  }

  private static Map<String, Object> health() throws Exception {
    String target = System.getenv("CARD_BRIDGE_CALLBACK_URL");
    if (target == null || !target.endsWith("/api/library/provider/used"))
      throw new Problem(503, "CALLBACK_UNAVAILABLE", "Configure the provider callback URL.");
    URI uri = URI.create(target.substring(0, target.length() - 4) + "health");
    if (!Set.of("http", "https").contains(uri.getScheme())
        || uri.getUserInfo() != null
        || uri.getRawQuery() != null
        || ("http".equals(uri.getScheme())
            && !Set.of("127.0.0.1", "localhost", "[::1]", "::1").contains(uri.getHost())))
      throw new Problem(503, "CALLBACK_UNAVAILABLE", "Use a loopback or HTTPS provider callback.");
    String body = "{}",
        time = Long.toString(System.currentTimeMillis()),
        nonce = UUID.randomUUID().toString().replace("-", "");
    HttpRequest request =
        HttpRequest.newBuilder(uri)
            .timeout(Duration.ofSeconds(5))
            .header("Content-Type", "application/json")
            .header("X-Bridge-Time", time)
            .header("X-Bridge-Nonce", nonce)
            .header(
                "X-Bridge-Signature",
                BridgeCrypto.signature(
                    env("CARD_BRIDGE_SHARED_KEY"), "POST", uri.getRawPath(), time, nonce, body))
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
    try {
      HttpResponse<String> response =
          HttpClient.newBuilder()
              .connectTimeout(Duration.ofSeconds(3))
              .followRedirects(HttpClient.Redirect.NEVER)
              .build()
              .send(request, HttpResponse.BodyHandlers.ofString());
      if (response.statusCode() != 200 || response.body().length() > 1024)
        throw new IllegalStateException();
      JsonObject data = JsonParser.parseString(response.body()).getAsJsonObject();
      if (!data.get("ok").getAsBoolean()
          || !"v83-card-bridge/1".equals(data.get("protocol").getAsString()))
        throw new IllegalStateException();
    } catch (Exception error) {
      throw new Problem(
          503, "CALLBACK_UNAVAILABLE", "The authenticated provider callback is unavailable.");
    }
    return Map.of(
        "ok",
        true,
        "protocol",
        "v83-card-bridge/1",
        "paymentProtocol",
        "cosmic-payments/1",
        "legacyAttributionProtocol",
        "cosmic-legacy-attribution/1",
        "sessionSource",
        System.getenv().getOrDefault("CARD_BRIDGE_SESSION_SOURCE", "bridge"),
        "acceptedCashTypes",
        BridgeWallet.acceptedTypes(),
        "callbackReady",
        true,
        "leaseReady",
        true);
  }

  private static void deliver() {
    try {
      requireLease();
      String target = System.getenv("CARD_BRIDGE_CALLBACK_URL");
      if (target == null || target.isBlank()) return;
      URI uri = URI.create(target);
      if (!Set.of("http", "https").contains(uri.getScheme())
          || uri.getRawQuery() != null
          || uri.getUserInfo() != null
          || ("http".equals(uri.getScheme())
              && !Set.of("127.0.0.1", "localhost", "[::1]", "::1").contains(uri.getHost())))
        throw new IllegalStateException("Invalid bridge callback URL");
      HttpClient client =
          HttpClient.newBuilder()
              .connectTimeout(Duration.ofSeconds(3))
              .followRedirects(HttpClient.Redirect.NEVER)
              .build();
      try (Connection con = DatabaseConnection.getConnection();
          PreparedStatement ps =
              con.prepareStatement(
                  "SELECT receipt_id,payload,attempts FROM card_bridge_outbox WHERE delivered=FALSE"
                      + " AND next_attempt<="
                      + System.currentTimeMillis()
                      + " ORDER BY"
                      + " created_at LIMIT 20");
          ResultSet rs = ps.executeQuery()) {
        while (rs.next()) {
          String receipt = rs.getString(1),
              body = rs.getString(2),
              time = Long.toString(System.currentTimeMillis()),
              nonce = UUID.randomUUID().toString().replace("-", "");
          HttpRequest request =
              HttpRequest.newBuilder(uri)
                  .timeout(Duration.ofSeconds(5))
                  .header("Content-Type", "application/json")
                  .header("X-Bridge-Time", time)
                  .header("X-Bridge-Nonce", nonce)
                  .header(
                      "X-Bridge-Signature",
                      BridgeCrypto.signature(
                          env("CARD_BRIDGE_SHARED_KEY"),
                          "POST",
                          uri.getRawPath(),
                          time,
                          nonce,
                          body))
                  .POST(HttpRequest.BodyPublishers.ofString(body))
                  .build();
          boolean delivered = false;
          try {
            HttpResponse<String> response =
                client.send(request, HttpResponse.BodyHandlers.ofString());
            delivered = response.statusCode() >= 200 && response.statusCode() < 300;
          } catch (Exception unavailable) {
            /* Retry the same durable receipt. */
          }
          requireLease();
          if (delivered) {
            try (PreparedStatement done =
                con.prepareStatement(
                    "UPDATE card_bridge_outbox SET"
                        + " delivered=TRUE,last_error=NULL,attempts=attempts+1,last_attempt="
                        + System.currentTimeMillis()
                        + " WHERE receipt_id=?")) {
              done.setString(1, receipt);
              done.executeUpdate();
            }
          } else {
            long now = System.currentTimeMillis();
            long delay = Math.min(300000L, 5000L << Math.min(6, rs.getInt(3)));
            try (PreparedStatement retry =
                con.prepareStatement(
                    "UPDATE card_bridge_outbox SET"
                        + " attempts=attempts+1,last_attempt=?,next_attempt=?,last_error='CALLBACK_UNAVAILABLE'"
                        + " WHERE receipt_id=?")) {
              retry.setLong(1, now);
              retry.setLong(2, now + delay);
              retry.setString(3, receipt);
              retry.executeUpdate();
            }
            outboxFailure();
          }
        }
      }
    } catch (Exception ignored) {
      outboxFailure();
    }
  }

  private static void outboxFailure() {
    long now = System.currentTimeMillis();
    if (now - lastOutboxLog >= 60000) {
      lastOutboxLog = now;
      System.err.println(
          "card_bridge: OUTBOX_RETRY; inspect callback availability and game readiness");
    }
  }
}
