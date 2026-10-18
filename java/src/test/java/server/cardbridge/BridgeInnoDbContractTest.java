package server.cardbridge;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import java.io.InputStream;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mockito.MockedStatic;
import tools.DatabaseConnection;

/** Optional MySQL contracts. Requires an empty, dedicated synthetic fixture schema. */
@EnabledIfEnvironmentVariable(named = "CARD_BRIDGE_MYSQL_TEST_URL", matches = ".+")
public class BridgeInnoDbContractTest {
  private static String url;
  private static final Properties credentials = new Properties();

  @BeforeAll
  static void fixtureOnly() throws Exception {
    url = System.getenv("CARD_BRIDGE_MYSQL_TEST_URL");
    if (!url.matches(
        "jdbc:mysql://(127\\.0\\.0\\.1|localhost|db|card-bridge-lock-db)(:[0-9]{1,5})?/card_bridge_lock_[a-z0-9_]{1,48}"))
      throw new IllegalArgumentException("Use a dedicated local synthetic fixture schema");
    for (String key : List.of("USER", "PASSWORD")) {
      String value = System.getenv("CARD_BRIDGE_MYSQL_TEST_" + key);
      if (value == null || value.isEmpty())
        throw new IllegalArgumentException("Fixture credentials required");
      credentials.setProperty(key.toLowerCase(Locale.ROOT), value);
    }
    credentials.setProperty("useSSL", "false");
    credentials.setProperty("allowPublicKeyRetrieval", "true");
    credentials.setProperty("serverTimezone", "UTC");
    credentials.setProperty("connectTimeout", "5000");
    credentials.setProperty("socketTimeout", "15000");
    try (Connection con = open(Connection.TRANSACTION_REPEATABLE_READ);
        ResultSet tables =
            con.getMetaData().getTables(con.getCatalog(), null, "%", new String[] {"TABLE"})) {
      if (tables.next())
        throw new IllegalArgumentException("Fixture schema must initially be empty");
      assertEquals("MySQL", con.getMetaData().getDatabaseProductName());
    }
  }

  private static Connection open(int isolation) throws SQLException {
    Connection con = DriverManager.getConnection(url, credentials);
    con.setTransactionIsolation(isolation);
    return con;
  }

  private static final List<String> tables = new ArrayList<>();

  @AfterEach
  void clearFixture() throws Exception {
    try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
        Statement st = con.createStatement()) {
      // Only tables created by this class in its previously verified empty schema.
      for (int i = tables.size() - 1; i >= 0; i--) st.execute("DROP TABLE " + tables.get(i));
      tables.clear();
    }
  }

  private static void initialize(int isolation) throws Exception {
    try (Connection con = open(isolation);
        Statement st = con.createStatement()) {
      st.execute(
          "CREATE TABLE accounts(id INT PRIMARY KEY,name VARCHAR(13),password VARCHAR(128),nxCredit"
              + " INT,maplePoint INT,nxPrepaid INT,banned INT,tempban TIMESTAMP) ENGINE=InnoDB");
      tables.add("accounts");
      st.execute(
          "INSERT INTO accounts VALUES(1,'Buyer','fixture',10000,0,5000,0,'1970-01-01"
              + " 00:00:01'),(2,'Seller','fixture',0,0,2147483647,0,'1970-01-01 00:00:01')");
      try (InputStream source =
          BridgeInnoDbContractTest.class.getResourceAsStream("/card-bridge/schema.sql")) {
        for (String sql : new String(source.readAllBytes(), StandardCharsets.UTF_8).split(";"))
          if (!sql.isBlank()) {
            st.execute(sql);
            String name = sql.trim().split("\\s+")[5];
            tables.add(name);
          }
      }
      st.execute("CREATE TABLE characters(id INT PRIMARY KEY,accountid INT) ENGINE=InnoDB");
      tables.add("characters");
      st.execute("INSERT INTO characters VALUES(7,1),(22,2)");
      st.execute(
          "CREATE TABLE mts_items(id INT PRIMARY KEY,seller INT,price INT,transfer INT)"
              + " ENGINE=InnoDB");
      tables.add("mts_items");
      st.execute("INSERT INTO mts_items VALUES(77,22,1000,0)");
      st.execute("CREATE TABLE mts_cart(itemid INT,cid INT) ENGINE=InnoDB");
      tables.add("mts_cart");
    }
  }

  private static int scalar(String sql) throws SQLException {
    try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
        Statement st = con.createStatement();
        ResultSet rs = st.executeQuery(sql)) {
      rs.next();
      return rs.getInt(1);
    }
  }

  private static Object proxy(Class<?> type, Object delegate, InvocationHandler handler) {
    return Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, handler);
  }

  private static Object invoke(Object delegate, Method method, Object[] args) throws Throwable {
    try {
      return method.invoke(delegate, args);
    } catch (InvocationTargetException error) {
      throw error.getCause();
    }
  }

  private static Connection pausedSnapshot(
      Connection con, CountDownLatch snapshot, CountDownLatch resume) {
    AtomicBoolean paused = new AtomicBoolean();
    return (Connection)
        proxy(
            Connection.class,
            con,
            (object, method, args) -> {
              Object result = invoke(con, method, args);
              if (method.getName().equals("prepareStatement")
                  && args[0] instanceof String sql
                  && sql.startsWith("SELECT c.accountid FROM mts_items")) {
                PreparedStatement statement = (PreparedStatement) result;
                return proxy(
                    PreparedStatement.class,
                    statement,
                    (ignored, operation, parameters) -> {
                      Object value = invoke(statement, operation, parameters);
                      if (operation.getName().equals("executeQuery")
                          && paused.compareAndSet(false, true)) {
                        snapshot.countDown();
                        if (!resume.await(10, TimeUnit.SECONDS))
                          throw new SQLException("Fixture interleaving deadline");
                      }
                      return value;
                    });
              }
              return result;
            });
  }

  private static void staleSnapshotSale(int isolation) throws Exception {
    initialize(isolation);
    CountDownLatch snapshot = new CountDownLatch(1), resume = new CountDownLatch(1);
    ExecutorService worker = Executors.newSingleThreadExecutor();
    try {
      Future<Object> sale =
          worker.submit(
              () -> {
                try (MockedStatic<DatabaseConnection> database =
                    mockStatic(DatabaseConnection.class)) {
                  database
                      .when(DatabaseConnection::getConnection)
                      .thenAnswer(ignored -> pausedSnapshot(open(isolation), snapshot, resume));
                  try {
                    BridgeAwards.sale(1, 7, 77);
                    return "UNEXPECTED_CREDIT";
                  } catch (BridgeHttp.Problem rejected) {
                    return rejected.code;
                  }
                }
              });
      assertTrue(snapshot.await(10, TimeUnit.SECONDS));
      try (MockedStatic<DatabaseConnection> database = mockStatic(DatabaseConnection.class)) {
        database.when(DatabaseConnection::getConnection).thenAnswer(ignored -> open(isolation));
        var intent = BridgeContractTest.paymentInput(4, 1000, "a".repeat(64));
        intent.addProperty("accountId", 2);
        assertEquals("DEBITED", BridgePayments.admit(intent, "debit").get("state"));
      }
      resume.countDown();
      assertEquals("CREDIT_HEADROOM_UNAVAILABLE", sale.get(15, TimeUnit.SECONDS));
      assertEquals(5000, scalar("SELECT nxPrepaid FROM accounts WHERE id=1"));
      assertEquals(Integer.MAX_VALUE - 1000, scalar("SELECT nxPrepaid FROM accounts WHERE id=2"));
      assertEquals(0, scalar("SELECT COUNT(*) FROM card_bridge_awards"));
      assertEquals(0, scalar("SELECT transfer FROM mts_items WHERE id=77"));
      assertEquals(
          1, scalar("SELECT COUNT(*) FROM card_bridge_payments WHERE payment_state='DEBITED'"));
    } finally {
      resume.countDown();
      worker.shutdownNow();
      assertTrue(worker.awaitTermination(20, TimeUnit.SECONDS));
    }
  }

  @Test
  void repeatableReadSellerCreditObservesNewRefundReservation() throws Exception {
    staleSnapshotSale(Connection.TRANSACTION_REPEATABLE_READ);
  }

  @Test
  void readCommittedSellerCreditObservesNewRefundReservation() throws Exception {
    staleSnapshotSale(Connection.TRANSACTION_READ_COMMITTED);
  }

  private static void setFixtureBalances(int isolation, int amount) throws SQLException {
    try (Connection con = open(isolation); Statement st = con.createStatement()) {
      st.execute("UPDATE accounts SET nxCredit=" + amount + ",maplePoint=" + amount
          + ",nxPrepaid=" + amount + " WHERE id=1");
    }
  }

  private static com.google.gson.JsonObject completion(
      com.google.gson.JsonObject input, String id, boolean refund) {
    var request = new com.google.gson.JsonObject();
    request.addProperty("transactionId", id);
    request.addProperty("fingerprint", input.get("fingerprint").getAsString());
    if (refund) {
      request.addProperty("compensationId", BridgePayments.compensation(
          input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      request.addProperty("debitReference", "debit:" + id);
    } else request.addProperty("purchaseId", "original-purchase-" + id);
    return request;
  }

  private static Map<String, Object> paymentState(String id) throws SQLException {
    var request = new com.google.gson.JsonObject();
    request.addProperty("transactionId", id);
    return BridgePayments.status(request);
  }

  private static List<Object> race(int isolation, Callable<?> first, Callable<?> second)
      throws Exception {
    CountDownLatch ready = new CountDownLatch(2), start = new CountDownLatch(1);
    ExecutorService pool = Executors.newFixedThreadPool(2);
    try {
      List<Future<Object>> futures = new ArrayList<>();
      for (Callable<?> action : List.of(first, second)) futures.add(pool.submit(() -> {
        try (MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class)) {
          db.when(DatabaseConnection::getConnection).thenAnswer(ignored -> open(isolation));
          ready.countDown();
          if (!start.await(10, TimeUnit.SECONDS)) throw new SQLException("Race start deadline");
          try { return action.call(); }
          catch (BridgeHttp.Problem rejected) { return rejected.code; }
        }
      }));
      assertTrue(ready.await(10, TimeUnit.SECONDS));
      start.countDown();
      List<Object> results = new ArrayList<>();
      for (Future<Object> future : futures) results.add(future.get(20, TimeUnit.SECONDS));
      return results;
    } finally {
      start.countDown(); pool.shutdownNow();
      assertTrue(pool.awaitTermination(20, TimeUnit.SECONDS));
    }
  }

  private static void assertBalances(int type, int expected, int other) {
    for (int source : List.of(1, 2, 4))
      assertEquals(source == type ? expected : other, BridgeWallet.read(1, source));
    assertEquals(0, scalarUnchecked("SELECT COUNT(*) FROM card_bridge_codes"));
  }

  private static int scalarUnchecked(String sql) {
    try { return scalar(sql); } catch (SQLException error) { throw new AssertionError(error); }
  }

  private static void debitCancellation(int isolation) throws Exception {
    initialize(isolation);
    try (MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class)) {
      db.when(DatabaseConnection::getConnection).thenAnswer(ignored -> open(isolation));
      int count = 0;
      for (int type : List.of(1, 2, 4)) {
        setFixtureBalances(isolation, 10000);
        String id = String.format("%064x", type);
        var input = BridgeContractTest.paymentInput(type, 1000, id);
        var results = race(isolation, () -> BridgePayments.admit(input, "debit"),
            () -> BridgePayments.admit(input, "cancel"));
        var state = paymentState(id);
        assertTrue(Set.of("DEBITED", "CANCELLED").contains(state.get("state")));
        assertEquals(state, results.get(0));
        assertEquals(state, BridgePayments.admit(input, "debit"));
        if (state.get("state").equals("DEBITED")) {
          assertEquals("PAYMENT_TERMINAL_CONFLICT", results.get(1));
          assertEquals("PAYMENT_TERMINAL_CONFLICT", assertThrows(BridgeHttp.Problem.class,
              () -> BridgePayments.admit(input, "cancel")).code);
        } else {
          assertEquals(state, results.get(1));
          assertEquals(state, BridgePayments.admit(input, "cancel"));
        }
        assertEquals(input.get("intent"), state.get("intent"));
        assertEquals(1, state.get("accountId")); assertEquals(type, state.get("cashType"));
        boolean debited = state.get("state").equals("DEBITED");
        assertBalances(type, debited ? 9000 : 10000, 10000);
        try (Connection con = open(isolation)) {
          assertEquals(debited ? 1000 : 0, BridgePayments.reserved(con, 1, type));
        }
        assertEquals(++count, scalar("SELECT COUNT(*) FROM card_bridge_payments"));
        if (debited) {
          var refund = completion(input, id, true);
          var done = BridgePayments.complete(refund, true);
          assertEquals("REFUNDED", done.get("state"));
          assertEquals(done, BridgePayments.complete(refund, true));
          assertBalances(type, 10000, 10000);
        }
      }
    }
  }

  private static void finalizeRefund(int isolation) throws Exception {
    initialize(isolation);
    try (MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class)) {
      db.when(DatabaseConnection::getConnection).thenAnswer(ignored -> open(isolation));
      int count = 0;
      for (int type : List.of(1, 2, 4)) {
        setFixtureBalances(isolation, Integer.MAX_VALUE);
        String id = String.format("%064x", 16 + type);
        var input = BridgeContractTest.paymentInput(type, 1000, id);
        var paid = BridgePayments.admit(input, "debit");
        assertEquals("DEBITED", paid.get("state"));
        assertEquals(true, paid.get("headroomReserved"));
        assertEquals("CREDIT_HEADROOM_UNAVAILABLE", assertThrows(BridgeHttp.Problem.class,
            () -> BridgeWallet.change(1, type, 1)).code);
        var finalize = completion(input, id, false); var refund = completion(input, id, true);
        var results = race(isolation, () -> BridgePayments.complete(finalize, false),
            () -> BridgePayments.complete(refund, true));
        assertEquals(1, results.stream().filter(x -> x instanceof Map).count());
        assertEquals(1, results.stream().filter(x -> x.equals("PAYMENT_TERMINAL_CONFLICT")).count());
        var state = paymentState(id);
        assertTrue(Set.of("FULFILLED", "REFUNDED").contains(state.get("state")));
        boolean refunded = state.get("state").equals("REFUNDED");
        assertEquals(state, BridgePayments.complete(refunded ? refund : finalize, refunded));
        assertEquals("PAYMENT_TERMINAL_CONFLICT", assertThrows(BridgeHttp.Problem.class,
            () -> BridgePayments.complete(refunded ? finalize : refund, !refunded)).code);
        assertEquals(paid.get("accountIncarnation"), state.get("accountIncarnation"));
        assertEquals(input.get("intent"), state.get("intent"));
        assertEquals(false, state.get("headroomReserved")); assertEquals("0", state.get("owedAmount"));
        assertBalances(type, refunded ? Integer.MAX_VALUE : Integer.MAX_VALUE - 1000, Integer.MAX_VALUE);
        try (Connection con = open(isolation)) { assertEquals(0, BridgePayments.reserved(con, 1, type)); }
        assertEquals(++count, scalar("SELECT COUNT(*) FROM card_bridge_payments"));
      }
    }
  }

  private static Connection loseCommitAck(Connection con, AtomicBoolean lose) {
    return (Connection) proxy(Connection.class, con, (object, method, args) -> {
      Object result = invoke(con, method, args);
      // Commit really reaches MySQL before the client observes an ambiguous result.
      if (method.getName().equals("commit") && lose.compareAndSet(true, false))
        throw new SQLException("Synthetic lost commit acknowledgement", "08007");
      return result;
    });
  }

  private static void transitionReplay(int isolation) throws Exception {
    initialize(isolation);
    AtomicBoolean lose = new AtomicBoolean();
    try (MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class)) {
      db.when(DatabaseConnection::getConnection).thenAnswer(ignored -> loseCommitAck(open(isolation), lose));
      int count = 0;
      for (int type : List.of(1, 2, 4)) {
        setFixtureBalances(isolation, Integer.MAX_VALUE);
        String debitId = String.format("%064x", 32 + type);
        String cancelId = String.format("%064x", 48 + type);
        String fulfilledId = String.format("%064x", 64 + type);
        var debit = BridgeContractTest.paymentInput(type, 1000, debitId);
        var cancel = BridgeContractTest.paymentInput(type, 1000, cancelId);
        lose.set(true);
        assertThrows(SQLException.class, () -> BridgePayments.admit(debit, "debit"));
        assertFalse(lose.get());
        var paid = BridgePayments.admit(debit, "debit");
        assertEquals("DEBITED", paid.get("state"));
        assertEquals(paid, BridgePayments.admit(debit, "debit"));
        assertBalances(type, Integer.MAX_VALUE - 1000, Integer.MAX_VALUE);
        assertEquals("CREDIT_HEADROOM_UNAVAILABLE", assertThrows(BridgeHttp.Problem.class,
            () -> BridgeWallet.change(1, type, 1)).code);
        lose.set(true);
        assertThrows(SQLException.class, () -> BridgePayments.admit(cancel, "cancel"));
        assertFalse(lose.get());
        var cancelled = BridgePayments.admit(cancel, "cancel");
        assertEquals("CANCELLED", cancelled.get("state"));
        assertEquals(cancelled, BridgePayments.admit(cancel, "debit"));
        assertBalances(type, Integer.MAX_VALUE - 1000, Integer.MAX_VALUE);
        var delivered = BridgeContractTest.paymentInput(type, 1000, fulfilledId);
        BridgePayments.admit(delivered, "debit");
        var finalize = completion(delivered, fulfilledId, false);
        lose.set(true);
        assertThrows(SQLException.class, () -> BridgePayments.complete(finalize, false));
        assertFalse(lose.get());
        var fulfilled = BridgePayments.complete(finalize, false);
        assertEquals("FULFILLED", fulfilled.get("state"));
        assertEquals(fulfilled, BridgePayments.complete(finalize, false));
        assertBalances(type, Integer.MAX_VALUE - 2000, Integer.MAX_VALUE);
        var refund = completion(debit, debitId, true);
        lose.set(true);
        assertThrows(SQLException.class, () -> BridgePayments.complete(refund, true));
        assertFalse(lose.get());
        var refunded = BridgePayments.complete(refund, true);
        assertEquals("REFUNDED", refunded.get("state"));
        assertEquals(refunded, BridgePayments.complete(refund, true));
        assertEquals(paid.get("accountIncarnation"), refunded.get("accountIncarnation"));
        assertEquals(debit.get("intent"), refunded.get("intent"));
        assertEquals(false, refunded.get("headroomReserved")); assertEquals("0", refunded.get("owedAmount"));
        assertEquals("PAYMENT_TERMINAL_CONFLICT", assertThrows(BridgeHttp.Problem.class,
            () -> BridgePayments.complete(completion(delivered, fulfilledId, true), true)).code);
        assertEquals("PAYMENT_TERMINAL_CONFLICT", assertThrows(BridgeHttp.Problem.class,
            () -> BridgePayments.complete(completion(debit, debitId, false), false)).code);
        assertBalances(type, Integer.MAX_VALUE - 1000, Integer.MAX_VALUE);
        try (Connection con = open(isolation)) { assertEquals(0, BridgePayments.reserved(con, 1, type)); }
        count += 3; assertEquals(count, scalar("SELECT COUNT(*) FROM card_bridge_payments"));
      }
    }
  }

  @Test void repeatableReadDebitCancellation() throws Exception { debitCancellation(Connection.TRANSACTION_REPEATABLE_READ); }
  @Test void readCommittedDebitCancellation() throws Exception { debitCancellation(Connection.TRANSACTION_READ_COMMITTED); }
  @Test void repeatableReadFinalizeRefund() throws Exception { finalizeRefund(Connection.TRANSACTION_REPEATABLE_READ); }
  @Test void readCommittedFinalizeRefund() throws Exception { finalizeRefund(Connection.TRANSACTION_READ_COMMITTED); }
  @Test void repeatableReadTransitionReplay() throws Exception { transitionReplay(Connection.TRANSACTION_REPEATABLE_READ); }
  @Test void readCommittedTransitionReplay() throws Exception { transitionReplay(Connection.TRANSACTION_READ_COMMITTED); }


  @Test
  void nativeTimestampMetadataAttributionPreservesFundsAndReplays() throws Exception {
    initialize(Connection.TRANSACTION_READ_COMMITTED);
    String id = "f".repeat(64);
    try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
        Statement st = con.createStatement()) {
      st.execute("ALTER TABLE accounts ADD createdat TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP");
      st.execute("UPDATE accounts SET createdat='2005-01-01 00:00:00',name='Renamed' WHERE id=1");
      st.execute(
          "INSERT INTO"
              + " card_bridge_payments(order_id,account_id,cash_type,amount,balance_after,created_at)"
              + " VALUES('"
              + id
              + "',1,1,1000,9000,'2026-01-01 00:00:00')");
      timestampProjection(con, "accounts", "createdat");
      timestampProjection(con, "card_bridge_payments", "created_at");
    }
    var input =
        com.google.gson.JsonParser.parseString(
                """
{"transactionId":"%s","accountId":1,"cashType":1,"amount":1000,"decisionKey":"mysql-legacy-1","operatorId":"fixture-operator","reason":"Verified synthetic pre-upgrade snapshot", "evidence":{"version":1,"kind":"managed-cosmic-backup/1","schema":"cosmic-createdat-timestamp/1","backupDigest":"%s","databaseDigest":"%s","account":{"accountId":1,"createdAt":"2005-01-01T00:00:00.000Z"},"payment":{"transactionId":"%s","accountId":1,"cashType":1,"amount":1000,"balanceAfter":9000,"createdAt":"2026-01-01T00:00:00.000Z"}}}
"""
                    .formatted(id, "a".repeat(64), "b".repeat(64), id))
            .getAsJsonObject();
    try (MockedStatic<DatabaseConnection> database = mockStatic(DatabaseConnection.class)) {
      database
          .when(DatabaseConnection::getConnection)
          .thenAnswer(ignored -> open(Connection.TRANSACTION_READ_COMMITTED));
      var result = BridgePayments.attributeLegacy(input);
      assertEquals("ATTRIBUTED", result.get("state"));
      assertEquals(result, BridgePayments.attributeLegacy(input));
      assertEquals(10000, scalar("SELECT nxCredit FROM accounts WHERE id=1"));
      assertEquals(1, scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
      assertEquals(
          1,
          scalar(
              "SELECT COUNT(*) FROM card_bridge_payments WHERE payment_state='LEGACY_PAID' AND"
                  + " intent IS NULL"));
      assertEquals(0, scalar("SELECT COUNT(*) FROM card_bridge_codes"));
    }
  }

  @Test
  void incompatiblePhysicalTimestampRefusesAttributionWithoutSideEffects() throws Exception {
    initialize(Connection.TRANSACTION_READ_COMMITTED);
    String id = "e".repeat(64);
    try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
        Statement st = con.createStatement()) {
      st.execute("ALTER TABLE accounts ADD createdat TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP");
      st.execute("UPDATE accounts SET createdat='2005-01-01 00:00:00' WHERE id=1");
      st.execute(
          "INSERT INTO card_bridge_payments(order_id,account_id,cash_type,amount,balance_after,created_at)"
              + " VALUES('" + id + "',1,1,1000,9000,'2026-01-01 00:00:00')");
    }
    var input = com.google.gson.JsonParser.parseString(
        """
{"transactionId":"%s","accountId":1,"cashType":1,"amount":1000,"decisionKey":"mysql-incompatible-1","operatorId":"fixture-operator","reason":"Verify incompatible physical timestamp refusal","evidence":{"version":1,"kind":"managed-cosmic-backup/1","schema":"cosmic-createdat-timestamp/1","backupDigest":"%s","databaseDigest":"%s","account":{"accountId":1,"createdAt":"2005-01-01T00:00:00.000Z"},"payment":{"transactionId":"%s","accountId":1,"cashType":1,"amount":1000,"balanceAfter":9000,"createdAt":"2026-01-01T00:00:00.000Z"}}}
""".formatted(id, "a".repeat(64), "b".repeat(64), id)).getAsJsonObject();
    for (String table : List.of("accounts", "card_bridge_payments")) {
      String column = table.equals("accounts") ? "createdat" : "created_at";
      try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
          Statement st = con.createStatement()) {
        st.execute("ALTER TABLE " + table + " MODIFY " + column
            + " TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)");
        try (PreparedStatement ps = con.prepareStatement(
            "SELECT DATA_TYPE,COLUMN_TYPE,DATETIME_PRECISION FROM information_schema.COLUMNS"
                + " WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND COLUMN_NAME=?")) {
          ps.setString(1, con.getCatalog());
          ps.setString(2, table);
          ps.setString(3, column);
          try (ResultSet rs = ps.executeQuery()) {
            assertTrue(rs.next());
            assertEquals("timestamp", rs.getString(1));
            assertEquals("timestamp(6)", rs.getString(2));
            assertEquals(6, rs.getInt(3));
            assertFalse(rs.wasNull());
            assertFalse(rs.next());
          }
        }
        timestampProjection(con, table, column, 6);
      }
      try (MockedStatic<DatabaseConnection> database = mockStatic(DatabaseConnection.class)) {
        database.when(DatabaseConnection::getConnection)
            .thenAnswer(ignored -> open(Connection.TRANSACTION_READ_COMMITTED));
        for (int attempt = 0; attempt < 2; attempt++) {
          var refused = assertThrows(BridgeHttp.Problem.class,
              () -> BridgePayments.attributeLegacy(input));
          assertEquals("LEGACY_IDENTITY_UNPROVEN", refused.code);
          assertEquals(10000, scalar("SELECT nxCredit FROM accounts WHERE id=1"));
          assertEquals(0, scalar("SELECT maplePoint FROM accounts WHERE id=1"));
          assertEquals(5000, scalar("SELECT nxPrepaid FROM accounts WHERE id=1"));
          assertEquals(0, scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
          assertEquals(0, scalar("SELECT COUNT(*) FROM card_bridge_account_identity"));
          assertEquals(0, scalar("SELECT COUNT(*) FROM card_bridge_codes"));
          assertEquals(1, scalar("SELECT COUNT(*) FROM card_bridge_payments"));
          assertEquals(1, scalar(
              "SELECT COUNT(*) FROM card_bridge_payments WHERE account_id=1 AND cash_type=1"
                  + " AND amount=1000 AND balance_after=9000 AND created_at='2026-01-01 00:00:00'"
                  + " AND payment_state='LEGACY_PAID' AND intent IS NULL AND account_incarnation IS NULL"));
        }
      }
      try (Connection con = open(Connection.TRANSACTION_READ_COMMITTED);
          Statement st = con.createStatement()) {
        st.execute("ALTER TABLE " + table + " MODIFY " + column
            + " TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP");
      }
    }
  }

  private static void timestampProjection(Connection con, String table, String column)
      throws SQLException {
    timestampProjection(con, table, column, 0);
  }

  private static void timestampProjection(
      Connection con, String table, String column, int expectedPrecision) throws SQLException {
    Map<String, Object> projection = new LinkedHashMap<>();
    projection.put("table", table);
    projection.put("column", column);
    try (ResultSet fields = con.getMetaData().getColumns(con.getCatalog(), null, table, column)) {
      assertTrue(fields.next());
      projection.put("jdbcTypeName", fields.getString("TYPE_NAME"));
      projection.put("jdbcDataType", fields.getInt("DATA_TYPE"));
      projection.put("jdbcDefault", fields.getString("COLUMN_DEF"));
      projection.put("jdbcNullable", fields.getInt("NULLABLE"));
      int precision = fields.getInt("DECIMAL_DIGITS");
      boolean unknownPrecision = fields.wasNull();
      projection.put("jdbcPrecisionWasNull", unknownPrecision);
      projection.put("jdbcPrecision", unknownPrecision ? null : precision);
      assertFalse(fields.next());
    }
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT DATA_TYPE,COLUMN_TYPE,DATETIME_PRECISION,IS_NULLABLE,COLUMN_DEFAULT FROM"
                + " information_schema.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND"
                + " COLUMN_NAME=?")) {
      ps.setString(1, con.getCatalog());
      ps.setString(2, table);
      ps.setString(3, column);
      try (ResultSet fields = ps.executeQuery()) {
        assertTrue(fields.next());
        projection.put("schemaDataType", fields.getString(1));
        projection.put("schemaColumnType", fields.getString(2));
        int precision = fields.getInt(3);
        boolean unknownPrecision = fields.wasNull();
        projection.put("schemaPrecisionWasNull", unknownPrecision);
        projection.put("schemaPrecision", unknownPrecision ? null : precision);
        projection.put("schemaNullable", fields.getString(4));
        projection.put("schemaDefault", fields.getString(5));
        assertFalse(fields.next());
      }
    }
    System.out.println("card_bridge_fixture_timestamp " + BridgeHttp.JSON.toJson(projection));
    assertEquals("TIMESTAMP", projection.get("jdbcTypeName"));
    assertEquals(Types.TIMESTAMP, projection.get("jdbcDataType"));
    assertEquals(DatabaseMetaData.columnNoNulls, projection.get("jdbcNullable"));
    assertEquals("timestamp", projection.get("schemaDataType"));
    assertEquals(expectedPrecision == 0 ? "timestamp" : "timestamp(" + expectedPrecision + ")",
        projection.get("schemaColumnType"));
    assertEquals(expectedPrecision, projection.get("schemaPrecision"));
    assertEquals("NO", projection.get("schemaNullable"));
  }
}
