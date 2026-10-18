package server.cardbridge;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.*;
import java.util.*;
import tools.BCrypt;
import tools.DatabaseConnection;

/** Account sessions for the standalone starter, or an existing Grove account website. */
public final class BridgeSessions {
  private BridgeSessions() {}

  static Map<String, Object> resolve(String name, Integer accountId) throws SQLException {
    if ((name == null) == (accountId == null)
        || (name != null && !name.matches("[a-zA-Z0-9]{3,13}"))
        || (accountId != null && accountId < 1))
      throw new IllegalArgumentException("Supply one account identity");
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT id,name FROM accounts WHERE "
                    + (name != null ? "name=?" : "id=?")
                    + " AND banned=0 AND tempban<=UTC_TIMESTAMP() FOR UPDATE")) {
      con.setAutoCommit(false);
      if (name != null) ps.setString(1, name);
      else ps.setInt(1, accountId);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next())
          throw new BridgeHttp.Problem(404, "ACCOUNT_UNAVAILABLE", "The account is unavailable.");
        int id = rs.getInt(1);
        String savedName = rs.getString(2);
        BridgePayments.assertLiveIdentity(con, id);
        con.commit();
        return Map.of("accountId", id, "name", savedName);
      }
    }
  }

  static boolean nativeSessions() {
    return !"grove".equals(System.getenv("CARD_BRIDGE_SESSION_SOURCE"));
  }

  static boolean passwordMatches(String password, String stored) {
    if (password == null || stored == null || stored.isEmpty()) return false;
    try {
      if (stored.startsWith("$2")) return BCrypt.checkpw(password, stored);
      if (MessageDigest.isEqual(
          password.getBytes(StandardCharsets.UTF_8), stored.getBytes(StandardCharsets.UTF_8)))
        return true;
      for (String algorithm : List.of("SHA-1", "SHA-512")) {
        String digest =
            HexFormat.of()
                .formatHex(
                    MessageDigest.getInstance(algorithm)
                        .digest(password.getBytes(StandardCharsets.UTF_8)));
        if (MessageDigest.isEqual(
            digest.getBytes(StandardCharsets.US_ASCII),
            stored.toLowerCase(Locale.ROOT).getBytes(StandardCharsets.US_ASCII))) return true;
      }
    } catch (Exception ignored) {
      return false;
    }
    return false;
  }

  static Map<String, Object> login(String name, String password, String hash) throws SQLException {
    if (!nativeSessions())
      throw new BridgeHttp.Problem(404, "NOT_FOUND", "Sign in through the account website.");
    if (!name.matches("[a-zA-Z0-9]{3,13}")
        || password.isEmpty()
        || password.length() > 128
        || !hash.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Invalid login");
    int account;
    String canonical;
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT id,name,password FROM accounts WHERE name=? AND banned=0 AND"
                    + " tempban<=UTC_TIMESTAMP() FOR UPDATE")) {
      con.setAutoCommit(false);
      ps.setString(1, name);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next() || !passwordMatches(password, rs.getString(3)))
          throw new BridgeHttp.Problem(
              401, "UNAUTHENTICATED", "Please check your account details.");
        account = rs.getInt(1);
        canonical = rs.getString(2);
      }
      BridgePayments.assertLiveIdentity(con, account);
      try (PreparedStatement insert =
          con.prepareStatement(
              "INSERT INTO card_bridge_sessions(token_hash,account_id,expires_at) VALUES(?,?,?)")) {
        insert.setString(1, hash);
        insert.setInt(2, account);
        insert.setLong(
            3, System.currentTimeMillis() + java.util.concurrent.TimeUnit.DAYS.toMillis(1));
        insert.executeUpdate();
      }
      try (PreparedStatement cleanup =
          con.prepareStatement("DELETE FROM card_bridge_sessions WHERE expires_at<?")) {
        cleanup.setLong(1, System.currentTimeMillis());
        cleanup.executeUpdate();
      }
      con.commit();
    }
    return Map.of("accountId", account, "name", canonical);
  }

  static Map<String, Object> session(String hash) throws SQLException {
    if (!hash.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Invalid session hash");
    String sql =
        nativeSessions()
            ? "SELECT a.id,a.name FROM card_bridge_sessions s JOIN accounts a ON a.id=s.account_id"
                + " WHERE s.token_hash=? AND s.expires_at>? AND a.banned=0 AND"
                + " a.tempban<=UTC_TIMESTAMP()"
            : "SELECT a.id,a.name FROM web_sessions s JOIN accounts a ON a.id=s.account_id WHERE"
                + " s.token_hash=? AND s.expires_at>UTC_TIMESTAMP() AND a.banned=0 AND"
                + " a.tempban<=UTC_TIMESTAMP()";
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps = con.prepareStatement(sql + " FOR UPDATE")) {
      con.setAutoCommit(false);
      ps.setString(1, hash);
      if (nativeSessions()) ps.setLong(2, System.currentTimeMillis());
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next())
          throw new BridgeHttp.Problem(
              401, "UNAUTHENTICATED", "Please sign in to your game account.");
        int id = rs.getInt(1);
        String savedName = rs.getString(2);
        BridgePayments.assertLiveIdentity(con, id);
        con.commit();
        return Map.of("accountId", id, "name", savedName);
      }
    }
  }

  static Map<String, Object> logout(String hash) throws SQLException {
    if (!nativeSessions() || !hash.matches("[a-f0-9]{64}"))
      throw new IllegalArgumentException("Invalid logout");
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement("DELETE FROM card_bridge_sessions WHERE token_hash=?")) {
      ps.setString(1, hash);
      ps.executeUpdate();
    }
    return Map.of("ok", true);
  }
}
