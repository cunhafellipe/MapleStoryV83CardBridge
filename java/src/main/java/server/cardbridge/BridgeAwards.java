package server.cardbridge;

import client.Character;
import client.Client;
import java.sql.*;
import java.util.*;
import server.maps.MapItem;
import tools.DatabaseConnection;
import tools.PacketCreator;

/** Source-bound awards retain their identity while database acknowledgements are uncertain. */
public final class BridgeAwards {
  private BridgeAwards() {}

  public static boolean credit(int account, int type, int amount, String source, String sourceId)
      throws SQLException {
    if (amount < 1
        || sourceId.isEmpty()
        || sourceId.length() > 128
        || !source.matches("[A-Z_]{1,24}")) throw new IllegalArgumentException("Invalid award");
    String id = BridgeCrypto.sha(source + "\n" + sourceId);
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        BridgePayments.lockAccount(con, account, type);
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT account_id,cash_type,amount FROM card_bridge_awards WHERE award_id=? FOR"
                    + " UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setString(1, id);
          try (ResultSet rs = ps.executeQuery()) {
            if (rs.next()) {
              if (rs.getInt(1) != account || rs.getInt(2) != type || rs.getInt(3) != amount)
                throw new IllegalArgumentException("Award identity conflict");
              con.commit();
              return true;
            }
          }
        }
        BridgePayments.credit(con, account, type, amount);
        try (PreparedStatement ps =
            con.prepareStatement(
                "INSERT INTO"
                    + " card_bridge_awards(award_id,account_id,cash_type,amount,source,source_id)"
                    + " VALUES(?,?,?,?,?,?)")) {
          ps.setString(1, id);
          ps.setInt(2, account);
          ps.setInt(3, type);
          ps.setInt(4, amount);
          ps.setString(5, source);
          ps.setString(6, sourceId);
          ps.executeUpdate();
        }
        con.commit();
        return true;
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  public static boolean loot(Character chr, MapItem drop) {
    int amount = drop.getItemId() == 4031865 ? 100 : 250;
    try {
      BridgeHttp.requireLease();
      return credit(chr.getAccountID(), 1, amount, "GROUND_NX", drop.getCardAwardId());
    } catch (BridgeHttp.Problem denied) {
      chr.message(
          "The credit cannot fit while pending refunds are reserved. The drop has not been"
              + " claimed.");
      return false;
    } catch (Exception unavailable) {
      chr.message("Credit confirmation is unavailable. Retry this same drop later.");
      return false;
    }
  }

  public static boolean grant(Character chr, int type, int amount) {
    try {
      BridgeHttp.requireLease();
    } catch (SQLException unavailable) {
      return false;
    }
    if (amount <= 0) {
      BridgeWallet.change(chr.getAccountID(), type, amount);
      return true;
    }
    String identity = UUID.randomUUID().toString();
    try {
      return credit(chr.getAccountID(), type, amount, "GM_AWARD", identity);
    } catch (BridgeHttp.Problem denied) {
      chr.message("The selected balance must retain room for pending refunds.");
      return false;
    } catch (Exception unknown) {
      chr.message("Award outcome is unknown. Do not repeat it; reference " + identity + ".");
      return false;
    }
  }

  public static boolean mts(Client client, int listing) {
    try {
      BridgeHttp.requireLease();
      sale(client.getAccID(), client.getPlayer().getId(), listing);
      client.sendPacket(PacketCreator.MTSConfirmBuy());
      return true;
    } catch (Exception denied) {
      client.sendPacket(PacketCreator.MTSFailBuy());
      return false;
    } finally {
      client.enableCSActions();
    }
  }

  static void sale(int buyer, int buyerCharacter, int listing) throws SQLException {
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        int seller;
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT c.accountid FROM mts_items m JOIN characters c ON c.id=m.seller WHERE"
                    + " m.id=?")) {
          ps.setInt(1, listing);
          try (ResultSet rs = ps.executeQuery()) {
            if (!rs.next()) throw new SQLException("Listing unavailable");
            seller = rs.getInt(1);
          }
        }
        BridgePayments.lockAccount(con, Math.min(buyer, seller), 4);
        if (buyer != seller) BridgePayments.lockAccount(con, Math.max(buyer, seller), 4);
        BridgePayments.assertLiveIdentity(con, buyer);
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT payer_account,payer_character FROM card_bridge_awards WHERE award_id=? FOR"
                    + " UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setString(1, BridgeCrypto.sha("MTS_SALE\n" + listing));
          try (ResultSet rs = ps.executeQuery()) {
            if (rs.next()) {
              if (rs.getInt(1) != buyer || rs.getInt(2) != buyerCharacter)
                throw new SQLException("Sale identity conflict");
              con.commit();
              return;
            }
          }
        }
        int amount, owner, transferred;
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT m.price,c.accountid,m.transfer FROM mts_items m JOIN characters c ON"
                    + " c.id=m.seller WHERE m.id=? FOR UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setInt(1, listing);
          try (ResultSet rs = ps.executeQuery()) {
            if (!rs.next()) throw new SQLException("Listing unavailable");
            amount = rs.getInt(1);
            owner = rs.getInt(2);
            transferred = rs.getInt(3);
          }
        }
        if (owner != seller || transferred != 0 || amount < 1)
          throw new SQLException("Listing changed");
        long total = (long) amount + 100 + (long) (amount * 0.1);
        if (total > Integer.MAX_VALUE) throw new SQLException("Listing amount unavailable");
        try (PreparedStatement ps =
            con.prepareStatement(
                "UPDATE accounts SET nxPrepaid=nxPrepaid-? WHERE id=? AND nxPrepaid>=?")) {
          ps.setLong(1, total);
          ps.setInt(2, buyer);
          ps.setLong(3, total);
          if (ps.executeUpdate() != 1) throw new SQLException("Buyer balance unavailable");
        }
        BridgePayments.credit(con, seller, 4, amount);
        try (PreparedStatement ps =
            con.prepareStatement(
                "INSERT INTO"
                    + " card_bridge_awards(award_id,account_id,cash_type,amount,source,source_id,payer_account,payer_character,payment_amount)"
                    + " VALUES(?,?,4,?,'MTS_SALE',?,?,?,?)")) {
          ps.setString(1, BridgeCrypto.sha("MTS_SALE\n" + listing));
          ps.setInt(2, seller);
          ps.setInt(3, amount);
          ps.setString(4, Integer.toString(listing));
          ps.setInt(5, buyer);
          ps.setInt(6, buyerCharacter);
          ps.setLong(7, total);
          ps.executeUpdate();
        }
        try (PreparedStatement ps =
            con.prepareStatement(
                "UPDATE mts_items SET seller=?,transfer=1 WHERE id=? AND transfer=0")) {
          ps.setInt(1, buyerCharacter);
          ps.setInt(2, listing);
          if (ps.executeUpdate() != 1) throw new SQLException("Listing changed");
        }
        try (PreparedStatement ps = con.prepareStatement("DELETE FROM mts_cart WHERE itemid=?")) {
          ps.setInt(1, listing);
          ps.executeUpdate();
        }
        con.commit();
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  public static void operatorFund(Connection con, String name, int type, int amount, String key)
      throws SQLException {
    String field = BridgePayments.column(type);
    if (amount < 1 || amount > 100000000 || key == null || !key.matches("[A-Za-z0-9._:-]{1,128}"))
      throw new IllegalArgumentException("Invalid funding request");
    try (Statement st = con.createStatement()) {
      st.execute(
          "CREATE TABLE IF NOT EXISTS card_bridge_operator_funding(request_id CHAR(64) PRIMARY"
              + " KEY,account_id INT NOT NULL,cash_type INT NOT NULL,amount INT NOT"
              + " NULL,balance_after INT NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT"
              + " CURRENT_TIMESTAMP) ENGINE=InnoDB");
    }
    con.setAutoCommit(false);
    try {
      int account;
      long balance;
      try (PreparedStatement ps =
          con.prepareStatement(
              "SELECT id,COALESCE(" + field + ",0) FROM accounts WHERE name=? FOR UPDATE")) {
        ps.setQueryTimeout(5);
        ps.setString(1, name);
        try (ResultSet rs = ps.executeQuery()) {
          if (!rs.next()) throw new SQLException("Account unavailable");
          account = rs.getInt(1);
          balance = rs.getLong(2);
        }
      }
      String identity = BridgeCrypto.sha("operator-fund\n" + account + "\n" + key);
      BridgePayments.assertLiveIdentity(con, account);
      try (PreparedStatement ps =
          con.prepareStatement(
              "SELECT account_id,cash_type,amount FROM card_bridge_operator_funding WHERE"
                  + " request_id=? FOR UPDATE")) {
        ps.setString(1, identity);
        try (ResultSet rs = ps.executeQuery()) {
          if (rs.next()) {
            if (rs.getInt(1) != account || rs.getInt(2) != type || rs.getInt(3) != amount)
              throw new IllegalArgumentException("Funding identity conflict");
            con.commit();
            return;
          }
        }
      }
      BridgePayments.credit(con, account, type, amount);
      try (PreparedStatement ps =
          con.prepareStatement(
              "INSERT INTO"
                  + " card_bridge_operator_funding(request_id,account_id,cash_type,amount,balance_after)"
                  + " VALUES(?,?,?,?,?)")) {
        ps.setString(1, identity);
        ps.setInt(2, account);
        ps.setInt(3, type);
        ps.setInt(4, amount);
        ps.setLong(5, balance + amount);
        ps.executeUpdate();
      }
      con.commit();
    } catch (Exception error) {
      con.rollback();
      throw error;
    } finally {
      con.setAutoCommit(true);
    }
  }
}
