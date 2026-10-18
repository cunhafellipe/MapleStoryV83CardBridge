package server.cardbridge;

import client.Character;
import client.Client;
import client.inventory.*;
import constants.inventory.ItemConstants;
import java.sql.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import server.CashShop;
import server.ItemInformationProvider;
import tools.DatabaseConnection;
import tools.PacketCreator;

/** A reward becomes USED in the same transaction that saves its inventory item. */
public final class BridgeRewards {
  private BridgeRewards() {}

  static final String SESSION = UUID.randomUUID().toString();

  record Pending(
      String issuance,
      String receipt,
      long usedAt,
      boolean delivered,
      Item cashItem,
      Character owner) {}

  private static final ConcurrentHashMap<Integer, Pending> pending = new ConcurrentHashMap<>();

  static boolean hasPending(Character chr) {
    return pending.containsKey(chr.getId())
        || BridgeCoupons.hasPending(chr)
        || BridgeExchanges.hasPending(chr);
  }

  public static Map<String, Object> diagnostics() throws SQLException {
    Map<String, Object> result = new LinkedHashMap<>();
    Timestamp oldest = null;
    long callbacks, attempts, lastAttempt;
    try (Connection con = DatabaseConnection.getConnection()) {
      try (Statement st = con.createStatement();
          ResultSet rs =
              st.executeQuery(
                  "SELECT COUNT(*),COALESCE(SUM(amount),0),COALESCE(SUM(CASE WHEN refund_owed=TRUE"
                      + " THEN 1 ELSE 0 END),0) FROM card_bridge_payments WHERE payment_state IN"
                      + " ('DEBITED','LEGACY_PAID')")) {
        rs.next();
        result.put("pendingPayments", rs.getLong(1));
        result.put("reservedRefundUnits", Long.toString(rs.getLong(2)));
        result.put("owedRefunds", rs.getLong(3));
      }
      try (Statement st = con.createStatement();
          ResultSet rs =
              st.executeQuery(
                  "SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='PENDING'")) {
        rs.next();
        result.put("pendingCouponAwards", rs.getLong(1));
      }
      result.put("pendingExchanges", BridgeExchanges.pendingCount());
      try (Statement st = con.createStatement();
          ResultSet rs =
              st.executeQuery(
                  "SELECT COUNT(*),MIN(created_at) FROM card_bridge_codes WHERE state='PENDING'")) {
        rs.next();
        result.put("pendingRewards", rs.getLong(1));
        oldest = rs.getTimestamp(2);
      }
      try (Statement st = con.createStatement();
          ResultSet rs =
              st.executeQuery(
                  "SELECT"
                      + " COUNT(*),MIN(created_at),COALESCE(MAX(last_attempt),0),COALESCE(SUM(attempts),0)"
                      + " FROM card_bridge_outbox WHERE delivered=FALSE")) {
        rs.next();
        callbacks = rs.getLong(1);
        Timestamp callbackOldest = rs.getTimestamp(2);
        if (callbackOldest != null && (oldest == null || callbackOldest.before(oldest)))
          oldest = callbackOldest;
        lastAttempt = rs.getLong(3);
        attempts = rs.getLong(4);
      }
      String lastError = null;
      try (Statement st = con.createStatement();
          ResultSet rs =
              st.executeQuery(
                  "SELECT last_error FROM card_bridge_outbox WHERE delivered=FALSE AND last_error"
                      + " IS NOT NULL ORDER BY last_attempt DESC LIMIT 1")) {
        if (rs.next())
          lastError =
              "CALLBACK_UNAVAILABLE".equals(rs.getString(1))
                  ? "CALLBACK_UNAVAILABLE"
                  : "OUTBOX_UNAVAILABLE";
      }
      result.put("pendingCallbacks", callbacks);
      result.put("oldestPendingAt", oldest == null ? null : oldest.toInstant().toString());
      result.put(
          "lastAttemptAt",
          lastAttempt == 0 ? null : java.time.Instant.ofEpochMilli(lastAttempt).toString());
      result.put("lastError", lastError);
      result.put("attempts", attempts);
      result.put("leaseReady", true);
      return result;
    }
  }

  static String fingerprint(String code) {
    return BridgeCrypto.hmac(BridgeHttp.codeKey(), SeriesOne.normalize(code));
  }

  public static Map<String, Object> register(
      String id, int account, String code, int item, int qty, int petDays, int series)
      throws SQLException {
    if (!id.matches("[a-f0-9-]{36}") || account < 1)
      throw new IllegalArgumentException("Invalid registration");
    SeriesOne.validate(code, item, qty, petDays, series);
    if (ItemInformationProvider.getInstance().getName(item) == null)
      throw new IllegalArgumentException("Reward is absent from this game data");
    String hash = fingerprint(code);
    try (Connection con = DatabaseConnection.getConnection()) {
      try (PreparedStatement ps =
          con.prepareStatement(
              "INSERT IGNORE INTO"
                  + " card_bridge_codes(issuance_id,code_hash,account_id,item_id,quantity,pet_days)"
                  + " VALUES(?,?,?,?,?,?)")) {
        ps.setString(1, id);
        ps.setString(2, hash);
        ps.setInt(3, account);
        ps.setInt(4, item);
        ps.setInt(5, qty);
        ps.setInt(6, petDays);
        ps.executeUpdate();
      }
      try (PreparedStatement ps =
          con.prepareStatement("SELECT * FROM card_bridge_codes WHERE issuance_id=?")) {
        ps.setString(1, id);
        try (ResultSet rs = ps.executeQuery()) {
          if (!rs.next()
              || !rs.getString("code_hash").equals(hash)
              || rs.getInt("account_id") != account
              || rs.getInt("item_id") != item
              || rs.getInt("quantity") != qty
              || rs.getInt("pet_days") != petDays)
            throw new IllegalArgumentException("Registration identity conflict");
        }
      }
    }
    return Map.of("issuanceId", id, "status", "READY");
  }

  public static Map<String, Object> status(String id) throws SQLException {
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT state,receipt_id,used_at FROM card_bridge_codes WHERE issuance_id=?")) {
      ps.setString(1, id);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next())
          throw new BridgeHttp.Problem(404, "NOT_FOUND", "Code registration is pending.");
        Map<String, Object> result = new HashMap<>();
        result.put("issuanceId", id);
        result.put("status", rs.getString(1));
        if ("USED".equals(rs.getString(1))) {
          result.put("receiptId", rs.getString(2));
          result.put("usedAt", java.time.Instant.ofEpochMilli(rs.getLong(3)).toString());
        }
        return result;
      }
    }
  }

  public static boolean handle(Client c, String raw) {
    if (!BridgeHttp.enabled()) return false;
    String hash;
    try {
      hash = fingerprint(raw);
    } catch (IllegalArgumentException e) {
      return false;
    }
    Character chr = c.getPlayer();
    synchronized (chr) {
      try (BridgeCharacters.Guard guard = BridgeCharacters.save(chr)) {
        if (!BridgeCharacters.current(chr)) {
          c.enableCSActions();
          return true;
        }
        String id = null;
        int item = 0, qty = 0;
        Item cashReward = null;
        boolean grant = false;
        Pending claimRecord = null;
        try {
          BridgeHttp.requireLease();
          try (Connection con = DatabaseConnection.getConnection()) {
            con.setAutoCommit(false);
            try {
              try (PreparedStatement ps =
                  con.prepareStatement(
                      "SELECT * FROM card_bridge_codes WHERE code_hash=? FOR UPDATE")) {
                ps.setString(1, hash);
                try (ResultSet rs = ps.executeQuery()) {
                  if (!rs.next()) {
                    con.rollback();
                    return false;
                  }
                  if (rs.getInt("account_id") != c.getAccID())
                    throw new BridgeHttp.Problem(
                        403, "WRONG_ACCOUNT", "This code belongs to another account.");
                  id = rs.getString("issuance_id");
                  item = rs.getInt("item_id");
                  qty = rs.getInt("quantity");
                  if ("USED".equals(rs.getString("state")))
                    throw new BridgeHttp.Problem(409, "USED", "This code has already been used.");
                  if ("PENDING".equals(rs.getString("state"))
                      && SESSION.equals(rs.getString("claim_session"))) {
                    Pending old = pending.get(chr.getId());
                    if (old == null
                        || !old.issuance().equals(id)
                        || rs.getInt("claim_character") != chr.getId())
                      throw new BridgeHttp.Problem(
                          409, "PENDING", "This reward is being saved. Please try shortly.");
                  } else {
                    if (pending.containsKey(chr.getId()))
                      throw new BridgeHttp.Problem(
                          409, "PENDING", "Finish saving the previous reward first.");
                    boolean cash = ItemInformationProvider.getInstance().isCash(item);
                    if (cash
                        ? chr.getCashShop().getItemsSize() >= 100
                        : chr.getInventory(ItemConstants.getInventoryType(item)).getNextFreeSlot()
                            == -1)
                      throw new BridgeHttp.Problem(
                          409, "INVENTORY_FULL", "Please leave one free slot for your reward.");
                    String receipt = UUID.randomUUID().toString();
                    try (PreparedStatement claim =
                        con.prepareStatement(
                            "UPDATE card_bridge_codes SET"
                                + " state='PENDING',claim_session=?,claim_character=?,receipt_id=?"
                                + " WHERE issuance_id=?")) {
                      claim.setString(1, SESSION);
                      claim.setInt(2, chr.getId());
                      claim.setString(3, receipt);
                      claim.setString(4, id);
                      claim.executeUpdate();
                    }
                    claimRecord =
                        new Pending(id, receipt, System.currentTimeMillis(), false, null, chr);
                    grant = true;
                  }
                }
              }
              BridgeHttp.requireLease();
              con.commit();
            } catch (Exception e) {
              try {
                con.rollback();
              } catch (SQLException rollbackError) {
                e.addSuppressed(rollbackError);
              }
              throw e;
            }
          } catch (SQLException uncertain) {
            // Includes commit acknowledgement and connection-close failures. The old
            // transaction is closed before a fresh connection verifies its exact claim.
            if (!grant || claimRecord == null) throw uncertain;
            boolean committed;
            try {
              committed = claimCommitted(claimRecord, chr.getId(), c.getAccID());
            } catch (SQLException unavailable) {
              BridgeHttp.uncertainCommit();
              throw unavailable;
            }
            if (!committed) throw uncertain;
          }
          if (grant) {
            pending.put(chr.getId(), claimRecord);
            // One new stack avoids partial merges and makes the delivery boundary explicit.
            if (ItemInformationProvider.getInstance().isCash(item)) {
              Item reward = CashShop.generateCouponItem(item, (short) qty);
              if (ItemConstants.isPet(item))
                reward.setExpiration(
                    System.currentTimeMillis() + java.util.concurrent.TimeUnit.DAYS.toMillis(30));
              chr.getCashShop().addToInventory(reward);
              cashReward = reward;
              pending.put(
                  chr.getId(),
                  new Pending(
                      claimRecord.issuance(),
                      claimRecord.receipt(),
                      claimRecord.usedAt(),
                      true,
                      reward,
                      chr));
            } else {
              Inventory inv = chr.getInventory(ItemConstants.getInventoryType(item));
              inv.lockInventory();
              try {
                Item reward = new Item(item, (short) 0, (short) qty, -1);
                if (inv.addItem(reward) == -1)
                  throw new IllegalStateException("Reserved inventory slot is unavailable");
                pending.put(
                    chr.getId(),
                    new Pending(
                        claimRecord.issuance(),
                        claimRecord.receipt(),
                        claimRecord.usedAt(),
                        true,
                        null,
                        chr));
                c.sendPacket(
                    PacketCreator.modifyInventory(true, List.of(new ModifyInventory(0, reward))));
              } finally {
                inv.unlockInventory();
              }
            }
          }
          Pending delivery = pending.get(chr.getId());
          if (delivery != null) cashReward = delivery.cashItem();
          chr.saveCharToDB(true);
          if (!"USED".equals(status(id).get("status")))
            throw new IllegalStateException("Reward save is pending");
          c.sendPacket(
              PacketCreator.showCouponRedeemedItems(
                  c.getAccID(),
                  0,
                  0,
                  cashReward == null ? List.of() : List.of(cashReward),
                  cashReward == null ? List.of(new tools.Pair<>(item, qty)) : List.of()));
          chr.dropMessage(
              5,
              "Card code granted "
                  + qty
                  + " "
                  + ItemInformationProvider.getInstance().getName(item)
                  + ".");
          c.enableCSActions();
          return true;
        } catch (Exception e) {
          Pending unfinished = pending.get(chr.getId());
          if (unfinished != null && !unfinished.delivered()) {
            try (Connection con = DatabaseConnection.getConnection();
                PreparedStatement ps =
                    con.prepareStatement(
                        "UPDATE card_bridge_codes SET"
                            + " state='READY',claim_session=NULL,claim_character=NULL,receipt_id=NULL"
                            + " WHERE issuance_id=? AND state='PENDING' AND claim_session=?")) {
              ps.setString(1, unfinished.issuance());
              ps.setString(2, SESSION);
              ps.executeUpdate();
              pending.remove(chr.getId(), unfinished);
            } catch (SQLException ignored) {
              /* Keep the unsaved claim blocked until storage recovers. */
            }
          }
          c.sendPacket(PacketCreator.showCashShopMessage((byte) 0xB1));
          chr.dropMessage(
              5,
              e instanceof BridgeHttp.Problem
                  ? e.getMessage()
                  : "The reward could not finish saving. Please try this code again.");
          c.enableCSActions();
          return true;
        }
      }
    }
  }

  static void replace(Character chr) throws SQLException {
    Pending old = pending.get(chr.getId());
    if (old == null) return;
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "UPDATE card_bridge_codes SET"
                    + " state='READY',claim_session=NULL,claim_character=NULL,receipt_id=NULL WHERE"
                    + " issuance_id=? AND state='PENDING' AND claim_session=? AND claim_character=?"
                    + " AND receipt_id=?")) {
      ps.setString(1, old.issuance());
      ps.setString(2, SESSION);
      ps.setInt(3, chr.getId());
      ps.setString(4, old.receipt());
      ps.executeUpdate();
    }
    pending.remove(chr.getId(), old);
  }

  public static void beforeSave(Connection con, Character chr) throws SQLException {
    if (!BridgeHttp.enabled()) return;
    BridgeHttp.requireLease();
    if (!BridgeCharacters.current(chr)) throw new SQLException("Replaced inventory character");
    BridgeCoupons.beforeSave(con, chr);
    BridgeExchanges.beforeSave(con, chr);
    Pending p = pending.get(chr.getId());
    if (p == null) return;
    if (p.owner() != chr) throw new SQLException("Replaced code reward character");
    BridgeHttp.requireLease();
    if (!p.delivered()) throw new SQLException("Reward delivery is incomplete");
    try (PreparedStatement ps =
        con.prepareStatement(
            "UPDATE card_bridge_codes SET state='USED',used_at=? WHERE issuance_id=? AND"
                + " state='PENDING' AND claim_session=? AND claim_character=? AND receipt_id=? AND"
                + " account_id=?")) {
      ps.setLong(1, p.usedAt());
      ps.setString(2, p.issuance());
      ps.setString(3, SESSION);
      ps.setInt(4, chr.getId());
      ps.setString(5, p.receipt());
      ps.setInt(6, chr.getAccountID());
      if (ps.executeUpdate() != 1) {
        // A previous commit may have succeeded while its acknowledgement was lost.
        // Reconcile only the identical claim and its atomic outbox receipt.
        try (PreparedStatement done =
            con.prepareStatement(
                "SELECT c.receipt_id,c.used_at FROM card_bridge_codes c JOIN card_bridge_outbox o"
                    + " ON o.receipt_id=c.receipt_id WHERE c.issuance_id=? AND c.state='USED'"
                    + " AND c.claim_session=? AND c.claim_character=? AND c.account_id=?")) {
          done.setString(1, p.issuance());
          done.setString(2, SESSION);
          done.setInt(3, chr.getId());
          done.setInt(4, chr.getAccountID());
          try (ResultSet rs = done.executeQuery()) {
            if (rs.next() && p.receipt().equals(rs.getString(1)) && p.usedAt() == rs.getLong(2))
              return;
          }
        }
        throw new SQLException("Reward claim changed before inventory commit");
      }
    }
    String body =
        BridgeHttp.JSON.toJson(
            Map.of(
                "issuanceId",
                p.issuance(),
                "receiptId",
                p.receipt(),
                "occurredAt",
                java.time.Instant.ofEpochMilli(p.usedAt()).toString()));
    try (PreparedStatement ps =
        con.prepareStatement("INSERT INTO card_bridge_outbox(receipt_id,payload) VALUES(?,?)")) {
      ps.setString(1, p.receipt());
      ps.setString(2, body);
      ps.executeUpdate();
    }
  }

  private static boolean claimCommitted(Pending p, int character, int account) throws SQLException {
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT state,receipt_id,claim_session,claim_character,account_id FROM"
                    + " card_bridge_codes WHERE issuance_id=? FOR UPDATE")) {
      con.setNetworkTimeout(Runnable::run, 5000);
      ps.setQueryTimeout(5);
      ps.setString(1, p.issuance());
      try (ResultSet rs = ps.executeQuery()) {
        return rs.next()
            && "PENDING".equals(rs.getString(1))
            && p.receipt().equals(rs.getString(2))
            && SESSION.equals(rs.getString(3))
            && character == rs.getInt(4)
            && account == rs.getInt(5);
      }
    }
  }

  public static void afterSave(Character chr) {
    if (BridgeHttp.enabled()) {
      pending.computeIfPresent(chr.getId(), (id, p) -> p.owner() == chr ? null : p);
      BridgeCoupons.afterSave(chr);
      BridgeExchanges.afterSave(chr);
    }
  }
}
