package server.cardbridge;

import com.google.gson.*;
import java.sql.*;
import java.util.*;
import tools.DatabaseConnection;

/** Durable provider payment outcomes and original-source refund capacity. */
public final class BridgePayments {
  private BridgePayments() {}

  private static final long MAX = Integer.MAX_VALUE;

  static void fields(JsonObject value, String... names) {
    if (!value.keySet().equals(Set.of(names))) invalid();
  }

  static void invalid() {
    throw new IllegalArgumentException("Invalid payment request");
  }

  static String string(JsonObject value, String name) {
    JsonElement item = value.get(name);
    if (item == null || !item.isJsonPrimitive() || !item.getAsJsonPrimitive().isString()) invalid();
    String text = item.getAsString();
    if (text.isEmpty() || text.length() > 128 || text.chars().anyMatch(c -> c < 32 || c == 127))
      invalid();
    return text;
  }

  static long integer(JsonObject value, String name, long minimum, long maximum) {
    JsonElement item = value.get(name);
    if (item == null
        || !item.isJsonPrimitive()
        || !item.getAsJsonPrimitive().isNumber()
        || !item.getAsString().matches("0|[1-9][0-9]*")) invalid();
    long result = Long.parseLong(item.getAsString());
    if (result < minimum || result > maximum) invalid();
    return result;
  }

  public static String currency(int type) {
    return switch (type) {
      case 1 -> "NX_CREDIT";
      case 2 -> "MAPLE_POINT";
      case 4 -> "NX_PREPAID";
      default -> throw new IllegalArgumentException("Invalid cash source");
    };
  }

  public static String column(int type) {
    return switch (type) {
      case 1 -> "nxCredit";
      case 2 -> "maplePoint";
      case 4 -> "nxPrepaid";
      default -> throw new IllegalArgumentException("Invalid cash source");
    };
  }

  private static String quote(String value) {
    StringBuilder result = new StringBuilder("\"");
    for (int i = 0; i < value.length(); i++) {
      char c = value.charAt(i);
      switch (c) {
        case '"' -> result.append("\\\"");
        case '\\' -> result.append("\\\\");
        case '\n' -> result.append("\\n");
        case '\r' -> result.append("\\r");
        case '\t' -> result.append("\\t");
        case '\b' -> result.append("\\b");
        case '\f' -> result.append("\\f");
        default -> {
          if (c < 32
              || (Character.isSurrogate(c)
                  && !(Character.isHighSurrogate(c)
                      && i + 1 < value.length()
                      && Character.isLowSurrogate(value.charAt(i + 1)))))
            result.append(String.format("\\u%04x", (int) c));
          else {
            result.append(c);
            if (Character.isHighSurrogate(c)) result.append(value.charAt(++i));
          }
        }
      }
    }
    return result.append('"').toString();
  }

  public static String canonical(JsonElement value) {
    if (value.isJsonObject()) {
      List<String> keys = new ArrayList<>(value.getAsJsonObject().keySet());
      Collections.sort(keys);
      List<String> parts = new ArrayList<>();
      for (String key : keys)
        parts.add(quote(key) + ":" + canonical(value.getAsJsonObject().get(key)));
      return "{" + String.join(",", parts) + "}";
    }
    if (value.isJsonArray()) {
      List<String> parts = new ArrayList<>();
      for (JsonElement item : value.getAsJsonArray()) parts.add(canonical(item));
      return "[" + String.join(",", parts) + "]";
    }
    if (!value.isJsonPrimitive()) invalid();
    return value.getAsJsonPrimitive().isString() ? quote(value.getAsString()) : value.toString();
  }

  static String hash(JsonElement value) {
    return BridgeCrypto.sha(canonical(value));
  }

  static String compensation(String preparation, String fingerprint) {
    JsonObject terms = new JsonObject();
    terms.addProperty("preparationId", preparation);
    terms.addProperty("fingerprint", fingerprint);
    return "ec_" + hash(terms);
  }

  record Binding(
      int account,
      int type,
      int amount,
      JsonObject intent,
      String id,
      String preparation,
      String fingerprint) {}

  static Binding binding(JsonObject input) {
    fields(input, "accountId", "cashType", "intent", "preparationId", "fingerprint");
    int account = (int) integer(input, "accountId", 1, Integer.MAX_VALUE),
        type = (int) integer(input, "cashType", 1, 4);
    currency(type);
    if (!input.get("intent").isJsonObject()) invalid();
    JsonObject intent = input.getAsJsonObject("intent");
    fields(
        intent,
        "version",
        "providerId",
        "transactionId",
        "userId",
        "externalCurrency",
        "externalUnits",
        "quote");
    integer(intent, "version", 1, 1);
    if (!"maplestory.v83".equals(string(intent, "providerId"))
        || !currency(type).equals(string(intent, "externalCurrency"))) invalid();
    String id = string(intent, "transactionId"), units = string(intent, "externalUnits");
    if (!id.matches("[a-f0-9]{64}") || !units.matches("[1-9][0-9]{0,8}")) invalid();
    int amount = Integer.parseInt(units);
    if (amount > 100000000) invalid();
    string(intent, "userId");
    if (!intent.get("quote").isJsonObject()) invalid();
    JsonObject q = intent.getAsJsonObject("quote");
    fields(
        q, "productId", "quantity", "productRevision", "catalogVersion", "adminRevision", "price");
    string(q, "productId");
    integer(q, "quantity", 1, 100);
    for (String revision : List.of("productRevision", "catalogVersion", "adminRevision"))
      integer(q, revision, 0, 9007199254740991L);
    if (!q.get("price").isJsonObject()) invalid();
    JsonObject price = q.getAsJsonObject("price");
    fields(price, "currencyId", "amount");
    string(price, "currencyId");
    integer(price, "amount", 1, 9007199254740991L);
    if (integer(price, "amount", 1, 9007199254740991L) != amount) invalid();
    String fingerprint = string(input, "fingerprint"), preparation = string(input, "preparationId");
    JsonObject identity = new JsonObject();
    identity.addProperty("providerId", "maplestory.v83");
    identity.addProperty("transactionId", id);
    if (!fingerprint.equals(hash(intent)) || !preparation.equals("ep_" + hash(identity))) invalid();
    return new Binding(account, type, amount, intent.deepCopy(), id, preparation, fingerprint);
  }

  public static void initialize(Connection con) throws SQLException {
    for (String[] item :
        new String[][] {
          {"payment_state", "VARCHAR(16) NOT NULL DEFAULT 'LEGACY_PAID'"},
          {"intent", "TEXT"},
          {"fingerprint", "CHAR(64)"},
          {"preparation_id", "CHAR(67)"},
          {"account_incarnation", "CHAR(36)"},
          {"purchase_id", "VARCHAR(128)"},
          {"compensation_id", "CHAR(67)"},
          {"refund_owed", "BOOLEAN NOT NULL DEFAULT FALSE"},
          {"terminal_at", "BIGINT"},
          {"cancellation_reason", "VARCHAR(40)"}
        }) {
      boolean found = false;
      for (String table : List.of("card_bridge_payments", "CARD_BRIDGE_PAYMENTS"))
        try (ResultSet columns =
            con.getMetaData().getColumns(con.getCatalog(), null, table, null)) {
          while (columns.next())
            if (item[0].equalsIgnoreCase(columns.getString("COLUMN_NAME"))) found = true;
        }
      if (!found)
        try (Statement st = con.createStatement()) {
          st.execute("ALTER TABLE card_bridge_payments ADD COLUMN " + item[0] + " " + item[1]);
        }
    }
    for (String[] index :
        new String[][] {
          {"card_bridge_payment_pending", "payment_state,order_id"},
          {"card_bridge_payment_headroom", "account_id,cash_type,payment_state"}
        }) {
      boolean found = false;
      for (String table : List.of("card_bridge_payments", "CARD_BRIDGE_PAYMENTS"))
        try (ResultSet rows =
            con.getMetaData().getIndexInfo(con.getCatalog(), null, table, false, false)) {
          while (rows.next())
            if (index[0].equalsIgnoreCase(rows.getString("INDEX_NAME"))) found = true;
        }
      if (!found)
        try (Statement st = con.createStatement()) {
          st.execute("CREATE INDEX " + index[0] + " ON card_bridge_payments(" + index[1] + ")");
        }
    }
  }

  public static long reserved(Connection con, int account, int type) throws SQLException {
    // Financial callers hold the existing account row. Current locking reads must not
    // mix a fresh balance with an older repeatable-read snapshot of its obligations.
    boolean locked = !con.getAutoCommit() && !con.isReadOnly();
    long total = 0;
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT amount FROM card_bridge_payments WHERE account_id=? AND cash_type=? AND"
                + " payment_state IN ('DEBITED','LEGACY_PAID')"
                + (locked ? " FOR UPDATE" : ""))) {
      ps.setQueryTimeout(5);
      ps.setInt(1, account);
      ps.setInt(2, type);
      try (ResultSet rs = ps.executeQuery()) {
        while (rs.next()) total = Math.addExact(total, rs.getLong(1));
      }
    }
    return total;
  }

  public static long[] lockAccount(Connection con, int account, int type) throws SQLException {
    long[] result = lockExistingAccount(con, account, type);
    if (result == null)
      throw new BridgeHttp.Problem(
          409, "ACCOUNT_UNAVAILABLE", "The original account is unavailable.");
    return result;
  }

  private static long[] lockExistingAccount(Connection con, int account, int type)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT COALESCE("
                + column(type)
                + ",0),banned,tempban,name FROM accounts WHERE id=? FOR UPDATE")) {
      ps.setQueryTimeout(5);
      ps.setInt(1, account);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) return null;
        return new long[] {
          rs.getLong(1), rs.getInt(2), rs.getTimestamp(3) == null ? 0 : rs.getTimestamp(3).getTime()
        };
      }
    }
  }

  public static void credit(Connection con, int account, int type, int amount) throws SQLException {
    if (amount < 1) throw new IllegalArgumentException("Invalid credit amount");
    long[] row = lockAccount(con, account, type);
    long held = reserved(con, account, type);
    assertLiveIdentity(con, account);
    if (row[0] < 0 || row[0] + held > MAX - amount)
      throw new BridgeHttp.Problem(
          409, "CREDIT_HEADROOM_UNAVAILABLE", "This balance must retain room for pending refunds.");
    setBalance(con, account, type, row[0] + amount);
  }

  private static void setBalance(Connection con, int account, int type, long balance)
      throws SQLException {
    if (balance < 0 || balance > MAX) throw new IllegalArgumentException("Invalid balance");
    try (PreparedStatement ps =
        con.prepareStatement("UPDATE accounts SET " + column(type) + "=? WHERE id=?")) {
      ps.setLong(1, balance);
      ps.setInt(2, account);
      if (ps.executeUpdate() != 1) throw new SQLException("Account changed");
    }
  }

  private static String incarnation(Connection con, int account, boolean live) throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT incarnation,live_account_id FROM card_bridge_account_identity WHERE"
                + " account_id=? FOR UPDATE")) {
      ps.setInt(1, account);
      try (ResultSet rs = ps.executeQuery()) {
        if (rs.next()) {
          if (live && (rs.getObject(2) == null || rs.getInt(2) != account))
            throw new BridgeHttp.Problem(
                409,
                "ACCOUNT_INCARNATION_CONFLICT",
                "A removed account identity cannot be reassigned.");
          return rs.getString(1);
        }
      }
    }
    String identity = UUID.randomUUID().toString();
    if (live) {
      try (PreparedStatement ps =
          con.prepareStatement(
              "INSERT INTO"
                  + " card_bridge_account_identity(account_id,live_account_id,incarnation,account_name)"
                  + " SELECT id,id,?,name FROM accounts WHERE id=?")) {
        ps.setString(1, identity);
        ps.setInt(2, account);
        if (ps.executeUpdate() != 1) throw new SQLException("Account unavailable");
      }
    } else {
      try (PreparedStatement ps =
          con.prepareStatement(
              "INSERT INTO"
                  + " card_bridge_account_identity(account_id,live_account_id,incarnation,account_name)"
                  + " VALUES(?,NULL,?,'')")) {
        ps.setInt(1, account);
        ps.setString(2, identity);
        ps.executeUpdate();
      }
    }
    return identity;
  }

  public static String assertLiveIdentity(Connection con, int account) throws SQLException {
    return incarnation(con, account, true);
  }

  private static JsonObject record(Connection con, String id) throws SQLException {
    return record(con, id, true);
  }

  private static JsonObject record(Connection con, String id, boolean locked) throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT * FROM card_bridge_payments WHERE order_id=?"
                + (locked ? " FOR UPDATE" : ""))) {
      ps.setQueryTimeout(5);
      ps.setString(1, id);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) return null;
        JsonObject value = new JsonObject();
        for (String name :
            List.of(
                "order_id",
                "payment_state",
                "intent",
                "fingerprint",
                "preparation_id",
                "account_incarnation",
                "purchase_id",
                "compensation_id",
                "cancellation_reason")) {
          if (rs.getString(name) != null) value.addProperty(name, rs.getString(name));
        }
        for (String name : List.of("account_id", "cash_type", "amount", "balance_after"))
          value.addProperty(name, rs.getInt(name));
        value.addProperty("refund_owed", rs.getBoolean("refund_owed"));
        return value;
      }
    }
  }

  private static void matches(JsonObject row, Binding b) {
    if (row.get("account_id").getAsInt() != b.account
        || row.get("cash_type").getAsInt() != b.type
        || row.get("amount").getAsInt() != b.amount) conflict();
    if (row.has("fingerprint")
        && (!row.get("fingerprint").getAsString().equals(b.fingerprint)
            || !row.get("intent").getAsString().equals(canonical(b.intent)))) conflict();
  }

  private static void conflict() {
    throw new BridgeHttp.Problem(
        409, "PAYMENT_CONFLICT", "The payment identity has different immutable terms.");
  }

  private static void terminalConflict() {
    throw new BridgeHttp.Problem(
        409, "PAYMENT_TERMINAL_CONFLICT", "The payment already has a different outcome.");
  }

  private static void insert(
      Connection con, Binding b, String state, String incarnation, long balance)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "INSERT INTO"
                + " card_bridge_payments(order_id,account_id,cash_type,amount,balance_after,payment_state,intent,fingerprint,preparation_id,account_incarnation)"
                + " VALUES(?,?,?,?,?,?,?,?,?,?)")) {
      ps.setString(1, b.id);
      ps.setInt(2, b.account);
      ps.setInt(3, b.type);
      ps.setInt(4, b.amount);
      ps.setLong(5, balance);
      ps.setString(6, state);
      ps.setString(7, canonical(b.intent));
      ps.setString(8, b.fingerprint);
      ps.setString(9, b.preparation);
      ps.setString(10, incarnation);
      ps.executeUpdate();
    }
  }

  private static Map<String, Object> proof(JsonObject row, String kind) {
    JsonObject intent = JsonParser.parseString(row.get("intent").getAsString()).getAsJsonObject();
    Map<String, Object> value = new LinkedHashMap<>();
    value.put("kind", kind);
    value.put("reference", kind + ":" + row.get("order_id").getAsString());
    for (String name :
        List.of("providerId", "transactionId", "userId", "externalCurrency", "externalUnits"))
      value.put(name, intent.get(name).getAsString());
    value.put("fingerprint", row.get("fingerprint").getAsString());
    if (kind.equals("refund")) {
      value.put("compensationId", row.get("compensation_id").getAsString());
      value.put("debitReference", "debit:" + row.get("order_id").getAsString());
    }
    return value;
  }

  private static Map<String, Object> projection(Connection con, JsonObject row)
      throws SQLException {
    int account = row.get("account_id").getAsInt(), type = row.get("cash_type").getAsInt();
    String state = row.get("payment_state").getAsString();
    long held = reserved(con, account, type);
    Map<String, Object> value = new LinkedHashMap<>();
    long current;
    try (PreparedStatement ps =
        con.prepareStatement("SELECT " + column(type) + " FROM accounts WHERE id=?")) {
      ps.setInt(1, account);
      try (ResultSet rs = ps.executeQuery()) {
        current = rs.next() ? rs.getLong(1) : -1;
      }
    }
    value.put("transactionId", row.get("order_id").getAsString());
    value.put("state", state);
    value.put("accountId", account);
    value.put("cashType", type);
    value.put("amount", row.get("amount").getAsInt());
    value.put("balance", row.get("balance_after").getAsInt());
    value.put("headroomReserved", state.equals("DEBITED") && current >= 0 && current + held <= MAX);
    value.put("reservedRefundUnits", Long.toString(held));
    value.put(
        "owedAmount",
        row.get("refund_owed").getAsBoolean() ? row.get("amount").getAsString() : "0");
    if (row.has("account_incarnation"))
      value.put("accountIncarnation", row.get("account_incarnation").getAsString());
    if (row.has("intent")) {
      value.put("intent", JsonParser.parseString(row.get("intent").getAsString()));
      value.put("fingerprint", row.get("fingerprint").getAsString());
      value.put("preparationId", row.get("preparation_id").getAsString());
      if (state.equals("CANCELLED")) value.put("noDebitReceipt", proof(row, "no_debit"));
      else value.put("debitReceipt", proof(row, "debit"));
      if (state.equals("REFUNDED")) value.put("refundReceipt", proof(row, "refund"));
    }
    for (String field : List.of("purchase_id", "compensation_id"))
      if (row.has(field))
        value.put(
            field.equals("purchase_id") ? "purchaseId" : "compensationId",
            row.get(field).getAsString());
    if (row.has("cancellation_reason"))
      value.put("cancellationReason", row.get("cancellation_reason").getAsString());
    return value;
  }

  public static Map<String, Object> admit(JsonObject input, String action) throws SQLException {
    if (!Set.of("debit", "cancel", "adopt-legacy").contains(action)) invalid();
    Binding b = binding(input);
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        long[] account = lockExistingAccount(con, b.account, b.type);
        JsonObject row = record(con, b.id);
        if (row != null && "CANCELLED".equals(row.get("payment_state").getAsString())) {
          matches(row, b);
          Map<String, Object> result = projection(con, row);
          con.commit();
          return result;
        }
        String identity = incarnation(con, b.account, account != null);
        if (row != null) {
          matches(row, b);
          String state = row.get("payment_state").getAsString();
          if (row.has("account_incarnation")
              && !identity.equals(row.get("account_incarnation").getAsString())) conflict();
          if (action.equals("adopt-legacy") && state.equals("LEGACY_PAID")) {
            if (!row.has("account_incarnation"))
              throw new BridgeHttp.Problem(
                  409,
                  "LEGACY_IDENTITY_UNPROVEN",
                  "Historical account ownership requires verified migration; it cannot be inferred"
                      + " from a reused numeric ID.");
            try (PreparedStatement ps =
                con.prepareStatement(
                    "UPDATE card_bridge_payments SET"
                        + " payment_state='DEBITED',intent=?,fingerprint=?,preparation_id=?,account_incarnation=?"
                        + " WHERE order_id=? AND payment_state='LEGACY_PAID'")) {
              ps.setString(1, canonical(b.intent));
              ps.setString(2, b.fingerprint);
              ps.setString(3, b.preparation);
              ps.setString(4, identity);
              ps.setString(5, b.id);
              if (ps.executeUpdate() != 1) throw new SQLException("Payment changed");
            }
          } else if (action.equals("cancel") && !state.equals("CANCELLED")) terminalConflict();
          else if (state.equals("LEGACY_PAID"))
            throw new BridgeHttp.Problem(
                409,
                "LEGACY_RECONCILIATION_REQUIRED",
                "The original paid order needs reconciliation.");
        } else {
          if (action.equals("adopt-legacy"))
            throw new BridgeHttp.Problem(404, "NOT_FOUND", "The original debit does not exist.");
          boolean cancelled =
              action.equals("cancel")
                  || account == null
                  || account[1] != 0
                  || account[2] > System.currentTimeMillis()
                  || !BridgeWallet.acceptedTypes().contains(b.type)
                  || account[0] < b.amount;
          if (!cancelled && (account[0] < 0 || account[0] + reserved(con, b.account, b.type) > MAX))
            throw new BridgeHttp.Problem(
                409,
                "LEGACY_RECONCILIATION_REQUIRED",
                "Pending original-source obligations require reconciliation.");
          long balance = account == null ? 0 : cancelled ? account[0] : account[0] - b.amount;
          if (!cancelled) setBalance(con, b.account, b.type, balance);
          insert(con, b, cancelled ? "CANCELLED" : "DEBITED", identity, balance);
          if (cancelled) {
            String reason =
                action.equals("cancel")
                    ? "CANCELLED"
                    : account == null
                        ? "ACCOUNT_UNAVAILABLE"
                        : account[1] != 0 || account[2] > System.currentTimeMillis()
                            ? "ACCOUNT_UNAVAILABLE"
                            : !BridgeWallet.acceptedTypes().contains(b.type)
                                ? "UNSUPPORTED_CASH_TYPE"
                                : "INSUFFICIENT_FUNDS";
            try (PreparedStatement ps =
                con.prepareStatement(
                    "UPDATE card_bridge_payments SET cancellation_reason=? WHERE order_id=?")) {
              ps.setString(1, reason);
              ps.setString(2, b.id);
              ps.executeUpdate();
            }
          }
        }
        Map<String, Object> result = projection(con, record(con, b.id));
        con.commit();
        return result;
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  public static Map<String, Object> complete(JsonObject input, boolean refund) throws SQLException {
    fields(
        input,
        refund
            ? new String[] {"transactionId", "fingerprint", "compensationId", "debitReference"}
            : new String[] {"transactionId", "fingerprint", "purchaseId"});
    String id = string(input, "transactionId"), fingerprint = string(input, "fingerprint");
    if (!id.matches("[a-f0-9]{64}") || !fingerprint.matches("[a-f0-9]{64}")) invalid();
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        int account, type;
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT account_id,cash_type FROM card_bridge_payments WHERE order_id=?")) {
          ps.setString(1, id);
          try (ResultSet rs = ps.executeQuery()) {
            if (!rs.next()) throw new BridgeHttp.Problem(404, "NOT_FOUND", "Payment not found.");
            account = rs.getInt(1);
            type = rs.getInt(2);
          }
        }
        long[] bank = lockExistingAccount(con, account, type);
        String identity = incarnation(con, account, bank != null);
        JsonObject row = record(con, id);
        if (row == null
            || !row.has("fingerprint")
            || !fingerprint.equals(row.get("fingerprint").getAsString())
            || !identity.equals(row.get("account_incarnation").getAsString())) conflict();
        String state = row.get("payment_state").getAsString(),
            terminal = refund ? "REFUNDED" : "FULFILLED",
            reference = string(input, refund ? "compensationId" : "purchaseId");
        if (refund
            && (!reference.equals(
                    compensation(row.get("preparation_id").getAsString(), fingerprint))
                || !string(input, "debitReference").equals("debit:" + id))) conflict();
        if (state.equals(terminal)) {
          if (!reference.equals(row.get(refund ? "compensation_id" : "purchase_id").getAsString()))
            conflict();
        } else {
          if (!state.equals("DEBITED")) terminalConflict();
          if (!refund && row.get("refund_owed").getAsBoolean()) terminalConflict();
          long amount = row.get("amount").getAsInt();
          if (refund && (bank == null || bank[0] < 0 || bank[0] + amount > MAX)) {
            try (PreparedStatement ps =
                con.prepareStatement(
                    "UPDATE card_bridge_payments SET refund_owed=TRUE,compensation_id=? WHERE"
                        + " order_id=?")) {
              ps.setString(1, reference);
              ps.setString(2, id);
              ps.executeUpdate();
            }
          } else {
            if (refund) setBalance(con, account, type, bank[0] + amount);
            try (PreparedStatement ps =
                con.prepareStatement(
                    "UPDATE card_bridge_payments SET payment_state=?,"
                        + (refund ? "compensation_id" : "purchase_id")
                        + "=?,refund_owed=FALSE,terminal_at=? WHERE order_id=? AND"
                        + " payment_state='DEBITED'")) {
              ps.setString(1, terminal);
              ps.setString(2, reference);
              ps.setLong(3, System.currentTimeMillis());
              ps.setString(4, id);
              if (ps.executeUpdate() != 1) throw new SQLException("Payment changed");
            }
          }
        }
        Map<String, Object> result = projection(con, record(con, id));
        con.commit();
        return result;
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  private static String text(JsonObject input, String name, int maximum) {
    JsonElement value = input.get(name);
    if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
      invalid();
    String result = value.getAsString();
    if (result.isBlank()
        || result.length() > maximum
        || result.chars().anyMatch(c -> c < 32 || c == 127)) invalid();
    return result;
  }

  private static long timestamp(JsonObject input, String name) {
    String value = string(input, name);
    if (!value.matches("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z"))
      invalid();
    try {
      long result = java.time.Instant.parse(value).toEpochMilli();
      if (result < 1000
          || result > 2147483647000L
          || !new java.time.format.DateTimeFormatterBuilder()
              .appendInstant(3)
              .toFormatter()
              .format(java.time.Instant.ofEpochMilli(result))
              .equals(value)) invalid();
      return result;
    } catch (java.time.DateTimeException bad) {
      invalid();
      return 0;
    }
  }

  private static void unproven() {
    throw new BridgeHttp.Problem(
        409,
        "LEGACY_IDENTITY_UNPROVEN",
        "The retained snapshot cannot prove original account ownership.");
  }

  private static void timestampSchema(Connection con, String table, String column)
      throws SQLException {
    boolean found = false;
    for (String name : List.of(table, table.toUpperCase(Locale.ROOT)))
      try (ResultSet fields = con.getMetaData().getColumns(con.getCatalog(), null, name, null)) {
        while (fields.next())
          if (column.equalsIgnoreCase(fields.getString("COLUMN_NAME"))) {
            found = true;
            String definition = fields.getString("COLUMN_DEF");
            String physicalType = fields.getString("TYPE_NAME");
            int precision = fields.getInt("DECIMAL_DIGITS");
            boolean unknownPrecision = fields.wasNull();
            int jdbcType = fields.getInt("DATA_TYPE");
            int nullable = fields.getInt("NULLABLE");
            boolean unknownNullable = fields.wasNull();
            if (!"TIMESTAMP".equalsIgnoreCase(physicalType)
                || jdbcType != Types.TIMESTAMP
                || unknownNullable
                || nullable != DatabaseMetaData.columnNoNulls
                || definition == null
                || !definition.toUpperCase(Locale.ROOT).matches("CURRENT_TIMESTAMP(?:\\(0?\\))?"))
              unproven();
            if (unknownPrecision) authoritativeTimestampPrecision(con, table, column);
            else if (precision != 0) unproven();
          }
      }
    if (!found) unproven();
  }

  private static void authoritativeTimestampPrecision(Connection con, String table, String column)
      throws SQLException {
    String catalog = con.getCatalog();
    if (!"MySQL".equals(con.getMetaData().getDatabaseProductName())
        || catalog == null
        || catalog.isBlank()) unproven();
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT DATA_TYPE,COLUMN_TYPE,DATETIME_PRECISION,IS_NULLABLE,COLUMN_DEFAULT FROM"
                + " information_schema.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND"
                + " COLUMN_NAME=?")) {
      ps.setQueryTimeout(5);
      ps.setString(1, catalog);
      ps.setString(2, table);
      ps.setString(3, column);
      try (ResultSet schema = ps.executeQuery()) {
        if (!schema.next()) unproven();
        String type = schema.getString(1), physicalType = schema.getString(2);
        int precision = schema.getInt(3);
        boolean unknownPrecision = schema.wasNull();
        String nullable = schema.getString(4), definition = schema.getString(5);
        if (!"timestamp".equalsIgnoreCase(type)
            || physicalType == null
            || !physicalType.toLowerCase(Locale.ROOT).matches("timestamp(?:\\(0\\))?")
            || unknownPrecision
            || precision != 0
            || !"NO".equals(nullable)
            || definition == null
            || !definition.toUpperCase(Locale.ROOT).matches("CURRENT_TIMESTAMP(?:\\(0?\\))?")
            || schema.next()) unproven();
      }
    }
  }

  private static long savedTimestamp(
      Connection con, String table, String column, String identity, Object key)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT " + column + " FROM " + table + " WHERE " + identity + "=? FOR UPDATE")) {
      ps.setObject(1, key);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) {
          unproven();
          return 0;
        }
        Timestamp value = rs.getTimestamp(1, Calendar.getInstance(TimeZone.getTimeZone("UTC")));
        if (value == null
            || value.getTime() < 1000
            || value.getTime() > 2147483647000L
            || value.getNanos() % 1000000 != 0) unproven();
        return value.getTime();
      }
    }
  }

  private static void utcEvidence(Connection con) throws SQLException {
    if ("MySQL".equals(con.getMetaData().getDatabaseProductName()))
      try (Statement st = con.createStatement();
          ResultSet rs = st.executeQuery("SELECT @@session.time_zone,@@system_time_zone")) {
        if (!rs.next()) unproven();
        String session = rs.getString(1), system = rs.getString(2);
        Set<String> zero = Set.of("UTC", "Etc/UTC", "GMT", "+00:00");
        if (!(zero.contains(session) || ("SYSTEM".equals(session) && zero.contains(system))))
          unproven();
      }
  }

  private static Map<String, Object> attributionResult(JsonObject original, String incarnation) {
    JsonObject evidence = original.getAsJsonObject("evidence");
    Map<String, Object> result = new LinkedHashMap<>();
    result.put("version", 1);
    result.put("state", "ATTRIBUTED");
    for (String name :
        List.of(
            "transactionId",
            "accountId",
            "cashType",
            "amount",
            "decisionKey",
            "operatorId",
            "reason"))
      result.put(
          name,
          name.equals("accountId") || name.equals("cashType") || name.equals("amount")
              ? original.get(name).getAsInt()
              : original.get(name).getAsString());
    result.put("accountIncarnation", incarnation);
    result.put("evidenceFingerprint", hash(evidence));
    result.put("backupDigest", evidence.get("backupDigest").getAsString());
    result.put("databaseDigest", evidence.get("databaseDigest").getAsString());
    result.put(
        "accountCreatedAt", evidence.getAsJsonObject("account").get("createdAt").getAsString());
    result.put(
        "paymentCreatedAt", evidence.getAsJsonObject("payment").get("createdAt").getAsString());
    return result;
  }

  /** The signed operator caller verifies the immutable backup and its closed migration binding. */
  public static Map<String, Object> attributeLegacy(JsonObject input) throws SQLException {
    fields(
        input,
        "transactionId",
        "accountId",
        "cashType",
        "amount",
        "decisionKey",
        "operatorId",
        "reason",
        "evidence");
    String id = string(input, "transactionId"),
        decision = string(input, "decisionKey"),
        operator = string(input, "operatorId");
    text(input, "reason", 512);
    if (!id.matches("[a-f0-9]{64}")
        || !decision.matches("[A-Za-z0-9._:-]{1,128}")
        || !operator.matches("[A-Za-z0-9._:@-]{1,128}")) invalid();
    int account = (int) integer(input, "accountId", 1, Integer.MAX_VALUE),
        type = (int) integer(input, "cashType", 1, 4),
        amount = (int) integer(input, "amount", 1, 100000000);
    currency(type);
    if (!input.get("evidence").isJsonObject()) invalid();
    JsonObject evidence = input.getAsJsonObject("evidence");
    fields(
        evidence,
        "version",
        "kind",
        "schema",
        "backupDigest",
        "databaseDigest",
        "account",
        "payment");
    integer(evidence, "version", 1, 1);
    if (!string(evidence, "kind").equals("managed-cosmic-backup/1")
        || !string(evidence, "schema").equals("cosmic-createdat-timestamp/1")
        || !string(evidence, "backupDigest").matches("[a-f0-9]{64}")
        || !string(evidence, "databaseDigest").matches("[a-f0-9]{64}")
        || !evidence.get("account").isJsonObject()
        || !evidence.get("payment").isJsonObject()) invalid();
    JsonObject originalAccount = evidence.getAsJsonObject("account"),
        originalPayment = evidence.getAsJsonObject("payment");
    fields(originalAccount, "accountId", "createdAt");
    fields(
        originalPayment,
        "transactionId",
        "accountId",
        "cashType",
        "amount",
        "balanceAfter",
        "createdAt");
    long accountCreated = timestamp(originalAccount, "createdAt"),
        paymentCreated = timestamp(originalPayment, "createdAt");
    int balance = (int) integer(originalPayment, "balanceAfter", 0, Integer.MAX_VALUE);
    if (integer(originalAccount, "accountId", 1, Integer.MAX_VALUE) != account
        || !string(originalPayment, "transactionId").equals(id)
        || integer(originalPayment, "accountId", 1, Integer.MAX_VALUE) != account
        || integer(originalPayment, "cashType", 1, 4) != type
        || integer(originalPayment, "amount", 1, 100000000) != amount
        || accountCreated >= paymentCreated) unproven();
    String payload = canonical(input);
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        long[] live = lockExistingAccount(con, account, type);
        // Exact prior attribution is historical evidence, including after settlement.
        // Replaying it cannot move funds or authorize payment settlement by itself.
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT order_id,decision_key,payload,account_incarnation FROM"
                    + " card_bridge_payment_attribution WHERE order_id=? OR decision_key=? FOR"
                    + " UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setString(1, id);
          ps.setString(2, decision);
          try (ResultSet rs = ps.executeQuery()) {
            if (rs.next()) {
              String storedId = rs.getString(1),
                  storedDecision = rs.getString(2),
                  storedPayload = rs.getString(3),
                  incarnation = rs.getString(4);
              if (!id.equals(storedId)
                  || !decision.equals(storedDecision)
                  || !payload.equals(storedPayload)
                  || rs.next())
                throw new BridgeHttp.Problem(
                    409,
                    "PAYMENT_ATTRIBUTION_CONFLICT",
                    "This operator decision has different immutable evidence.");
              Map<String, Object> result = attributionResult(input, incarnation);
              con.commit();
              return result;
            }
          }
        }
        if (live == null) unproven();
        utcEvidence(con);
        timestampSchema(con, "accounts", "createdat");
        timestampSchema(con, "card_bridge_payments", "created_at");
        JsonObject row = record(con, id);
        if (row == null
            || !"LEGACY_PAID".equals(row.get("payment_state").getAsString())
            || row.has("account_incarnation")
            || row.has("intent")
            || row.get("account_id").getAsInt() != account
            || row.get("cash_type").getAsInt() != type
            || row.get("amount").getAsInt() != amount
            || row.get("balance_after").getAsInt() != balance) unproven();
        if (savedTimestamp(con, "accounts", "createdat", "id", account) != accountCreated
            || savedTimestamp(con, "card_bridge_payments", "created_at", "order_id", id)
                != paymentCreated) unproven();
        String incarnation = assertLiveIdentity(con, account);
        try (PreparedStatement ps =
            con.prepareStatement(
                "UPDATE card_bridge_payments SET account_incarnation=? WHERE order_id=? AND"
                    + " payment_state='LEGACY_PAID' AND account_incarnation IS NULL")) {
          ps.setString(1, incarnation);
          ps.setString(2, id);
          if (ps.executeUpdate() != 1) throw new SQLException("Legacy payment changed");
        }
        try (PreparedStatement ps =
            con.prepareStatement(
                "INSERT INTO"
                    + " card_bridge_payment_attribution(order_id,decision_key,payload,evidence_sha,account_incarnation)"
                    + " VALUES(?,?,?,?,?)")) {
          ps.setString(1, id);
          ps.setString(2, decision);
          ps.setString(3, payload);
          ps.setString(4, hash(evidence));
          ps.setString(5, incarnation);
          ps.executeUpdate();
        }
        Map<String, Object> result = attributionResult(input, incarnation);
        con.commit();
        return result;
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  public static Map<String, Object> status(JsonObject input) throws SQLException {
    fields(input, "transactionId");
    String id = string(input, "transactionId");
    if (!id.matches("[a-f0-9]{64}")) invalid();
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setReadOnly(true);
      JsonObject row = record(con, id, false);
      if (row == null) return Map.of("transactionId", id, "state", "UNSEEN");
      return projection(con, row);
    }
  }

  public static Map<String, Object> pending(JsonObject input) throws SQLException {
    fields(input, "after", "limit");
    int limit = (int) integer(input, "limit", 1, 100);
    String after = input.get("after").isJsonNull() ? "" : string(input, "after");
    if (!after.isEmpty() && !after.matches("[a-f0-9]{64}")) invalid();
    List<Map<String, Object>> result = new ArrayList<>();
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT order_id FROM card_bridge_payments WHERE payment_state IN"
                    + " ('DEBITED','LEGACY_PAID') AND order_id>? ORDER BY order_id LIMIT ?")) {
      ps.setString(1, after);
      ps.setInt(2, limit + 1);
      try (ResultSet rs = ps.executeQuery()) {
        while (rs.next()) result.add(projection(con, record(con, rs.getString(1), false)));
      }
    }
    String cursor =
        result.size() > limit ? (String) result.get(limit - 1).get("transactionId") : null;
    if (result.size() > limit) result.remove(limit);
    Map<String, Object> value = new LinkedHashMap<>();
    value.put("items", result);
    value.put("nextCursor", cursor);
    return value;
  }
}
