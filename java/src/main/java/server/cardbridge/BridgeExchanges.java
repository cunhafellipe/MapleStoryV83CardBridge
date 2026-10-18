package server.cardbridge;

import client.Character;
import client.Client;
import client.inventory.*;
import client.inventory.manipulator.InventoryManipulator;
import java.sql.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import tools.DatabaseConnection;
import tools.PacketCreator;

/** Exchange consideration and cash awards commit together, including character inventory saves. */
public final class BridgeExchanges {
  private BridgeExchanges() {}

  private static final class Pending {
    final String key;
    final int account, amount;
    final Character owner;
    boolean denied;

    Pending(String key, int account, int amount, Character owner) {
      this.key = key;
      this.account = account;
      this.amount = amount;
      this.owner = owner;
    }
  }

  private static final ConcurrentHashMap<Integer, Pending> pending = new ConcurrentHashMap<>();
  private static final Object[] identityLocks = new Object[256];

  static {
    Arrays.setAll(identityLocks, i -> new Object());
  }

  static boolean hasPending(Character chr) {
    Pending p = pending.get(chr.getId());
    return p != null && p.owner == chr;
  }

  static void retire(Character chr) {
    pending.computeIfPresent(chr.getId(), (id, p) -> p.owner == chr ? null : p);
  }

  static int pendingCount() {
    return pending.size();
  }

  public static boolean exchange(Client client, boolean vote, int leaf, int amount, String key) {
    try {
      BridgeHttp.requireLease();
    } catch (SQLException unavailable) {
      return false;
    }
    if (amount < 1 || amount > Integer.MAX_VALUE || !key.matches("[a-fA-F0-9-]{36}"))
      throw new IllegalArgumentException("Invalid exchange");
    synchronized (identityLocks[Math.floorMod(key.hashCode(), identityLocks.length)]) {
      if (pending.values().stream().anyMatch(p -> p.key.equals(key))) {
        client.getPlayer().message("This exchange is still awaiting save confirmation.");
        return false;
      }
      if (vote) {
        try {
          vote(client.getAccID(), amount, key);
          client.getVotePoints();
          return true;
        } catch (Exception denied) {
          client
              .getPlayer()
              .message(
                  "The exchange was not confirmed. Keep its original request identity and retry"
                      + " later.");
          return false;
        }
      }
      Character chr = client.getPlayer();
      synchronized (chr) {
        try (BridgeCharacters.Guard guard = BridgeCharacters.save(chr)) {
          if (!BridgeCharacters.current(chr)) return false;
          if (BridgeRewards.hasPending(chr)) {
            chr.message("An inventory award is still awaiting a confirmed save.");
            return false;
          }
          try (Connection con = DatabaseConnection.getConnection()) {
            con.setAutoCommit(false);
            try {
              BridgePayments.lockAccount(con, chr.getAccountID(), 1);
              if (exists(con, key, chr.getAccountID(), amount, "LEAF_EXCHANGE")) {
                con.rollback();
                return true;
              }
              con.rollback();
            } catch (Exception error) {
              con.rollback();
              throw error;
            }
          } catch (Exception unavailable) {
            chr.message("Exchange status is unavailable. Your leaf has not been consumed.");
            return false;
          }
          Inventory inventory = chr.getInventory(InventoryType.ETC);
          inventory.lockInventory();
          try {
            List<Item> leaves = inventory.listById(leaf);
            if (leaves.isEmpty() || leaves.get(0).getQuantity() < 1) return false;
            Item backup = leaves.get(0).copy();
            Pending p = new Pending(key, chr.getAccountID(), amount, chr);
            pending.put(chr.getId(), p);
            InventoryManipulator.removeById(client, InventoryType.ETC, leaf, 1, false, false);
            chr.saveCharToDB(true);
            if (p.denied) {
              inventory.addItemFromDB(backup);
              client.sendPacket(
                  PacketCreator.modifyInventory(false, List.of(new ModifyInventory(0, backup))));
              pending.remove(chr.getId(), p);
              chr.message("The balance must retain room for refunds. Your leaf was returned.");
              return false;
            }
            if (pending.containsKey(chr.getId())) {
              chr.message(
                  "The exchange is awaiting inventory-save confirmation. Do not exchange another"
                      + " leaf yet.");
              return false;
            }
            return true;
          } finally {
            inventory.unlockInventory();
          }
        }
      }
    }
  }

  static void vote(int account, int amount, String key) throws SQLException {
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        BridgePayments.lockAccount(con, account, 1);
        if (!exists(con, key, account, amount, "VOTE_EXCHANGE")) {
          BridgePayments.credit(con, account, 1, amount);
          try (PreparedStatement ps =
              con.prepareStatement(
                  "UPDATE accounts SET votepoints=votepoints-1 WHERE id=? AND votepoints>=1")) {
            ps.setInt(1, account);
            if (ps.executeUpdate() != 1) throw new SQLException("Vote point unavailable");
          }
          insert(con, key, account, amount, "VOTE_EXCHANGE");
        }
        con.commit();
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  static boolean exists(Connection con, String key, int account, int amount, String source)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT account_id,cash_type,amount,source FROM card_bridge_awards WHERE award_id=? FOR"
                + " UPDATE")) {
      ps.setQueryTimeout(5);
      ps.setString(1, BridgeCrypto.sha("NPC_EXCHANGE\n" + key));
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) return false;
        if (rs.getInt(1) != account
            || rs.getInt(2) != 1
            || rs.getInt(3) != amount
            || !source.equals(rs.getString(4)))
          throw new IllegalArgumentException("Exchange identity conflict");
        return true;
      }
    }
  }

  static void insert(Connection con, String key, int account, int amount, String source)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "INSERT INTO card_bridge_awards(award_id,account_id,cash_type,amount,source,source_id)"
                + " VALUES(?,?,1,?,?,?)")) {
      ps.setString(1, BridgeCrypto.sha("NPC_EXCHANGE\n" + key));
      ps.setInt(2, account);
      ps.setInt(3, amount);
      ps.setString(4, source);
      ps.setString(5, key);
      ps.executeUpdate();
    }
  }

  static void beforeSave(Connection con, Character chr) throws SQLException {
    Pending p = pending.get(chr.getId());
    if (p == null) return;
    if (p.owner != chr || !BridgeCharacters.current(chr))
      throw new SQLException("Replaced exchange character");
    BridgeHttp.requireLease();
    BridgePayments.lockAccount(con, p.account, 1);
    if (exists(con, p.key, p.account, p.amount, "LEAF_EXCHANGE")) return;
    try {
      BridgePayments.credit(con, p.account, 1, p.amount);
    } catch (BridgeHttp.Problem denied) {
      p.denied = true;
      throw new SQLException("Exchange credit admission rejected", denied);
    }
    insert(con, p.key, p.account, p.amount, "LEAF_EXCHANGE");
  }

  static void afterSave(Character chr) {
    pending.computeIfPresent(chr.getId(), (id, p) -> p.owner == chr ? null : p);
  }
}
