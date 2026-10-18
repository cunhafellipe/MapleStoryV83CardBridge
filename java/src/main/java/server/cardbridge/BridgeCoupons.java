package server.cardbridge;

import client.Character;
import client.Client;
import client.inventory.*;
import client.inventory.manipulator.InventoryManipulator;
import com.google.gson.*;
import constants.inventory.ItemConstants;
import java.sql.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import server.CashShop;
import server.ItemInformationProvider;
import tools.DatabaseConnection;
import tools.PacketCreator;
import tools.Pair;

/** Coupon cash claims and recoverable mixed inventory awards share one durable identity. */
public final class BridgeCoupons {
  private BridgeCoupons() {}

  private record Pending(
      int code, int account, String receipt, boolean delivered, Character owner) {}

  private static final ConcurrentHashMap<Integer, Pending> pending = new ConcurrentHashMap<>();
  private static final Object[] accountLocks = new Object[256];

  static {
    Arrays.setAll(accountLocks, i -> new Object());
  }

  static boolean hasPending(Character chr) {
    Pending p = pending.get(chr.getId());
    return p != null && p.owner == chr;
  }

  static void retire(Character chr) {
    pending.computeIfPresent(chr.getId(), (id, p) -> p.owner == chr ? null : p);
  }

  record Delivery(int item, short quantity, boolean cash) {}

  record Claim(int code, String receipt, JsonArray awards, List<Delivery> delivery) {}

  static Claim claim(Character chr, String code) throws SQLException {
    BridgeHttp.requireLease();
    if (!BridgeCharacters.current(chr)) throw new SQLException("Replaced coupon character");
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        BridgePayments.lockAccount(con, chr.getAccountID(), 1);
        BridgePayments.assertLiveIdentity(con, chr.getAccountID());
        int codeId;
        long expiration;
        String retriever;
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT id,expiration,retriever FROM nxcode WHERE code=? FOR UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setString(1, code.toUpperCase(Locale.ROOT));
          try (ResultSet rs = ps.executeQuery()) {
            if (!rs.next())
              throw new BridgeHttp.Problem(409, "COUPON_NOT_FOUND", "Coupon unavailable.");
            codeId = rs.getInt(1);
            expiration = rs.getLong(2);
            retriever = rs.getString(3);
          }
        }
        JsonArray awards;
        String receipt = UUID.randomUUID().toString();
        boolean existing = false;
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT account_id,state,receipt_id,payload,claim_session,claim_character FROM"
                    + " card_bridge_coupon_claims WHERE code_id=? FOR UPDATE")) {
          ps.setInt(1, codeId);
          try (ResultSet rs = ps.executeQuery()) {
            if (rs.next()) {
              if (rs.getInt(1) != chr.getAccountID() || rs.getString(2).equals("DELIVERED"))
                throw new BridgeHttp.Problem(409, "COUPON_USED", "Coupon already used.");
              if (pending.values().stream().anyMatch(p -> p.account == chr.getAccountID()))
                throw new BridgeHttp.Problem(
                    409, "COUPON_PENDING", "Coupon delivery awaits inventory recovery.");
              receipt = rs.getString(3);
              awards = JsonParser.parseString(rs.getString(4)).getAsJsonArray();
              existing = true;
            } else awards = null;
          }
        }
        if (!existing) {
          if (retriever != null || expiration < System.currentTimeMillis())
            throw new BridgeHttp.Problem(409, "COUPON_USED", "Coupon unavailable.");
          awards = new JsonArray();
          try (PreparedStatement ps =
              con.prepareStatement(
                  "SELECT type,item,quantity FROM nxcode_items WHERE codeid=? ORDER BY"
                      + " type,item")) {
            ps.setInt(1, codeId);
            try (ResultSet rs = ps.executeQuery()) {
              while (rs.next()) {
                int kind = rs.getInt(1), item = rs.getInt(2), quantity = rs.getInt(3);
                if (kind < 0 || kind > 5 || quantity < 1)
                  throw new SQLException("Invalid coupon award");
                JsonObject award = new JsonObject();
                award.addProperty("type", kind);
                award.addProperty("item", item);
                award.addProperty("quantity", quantity);
                awards.add(award);
              }
            }
          }
          if (awards.isEmpty() || awards.size() > 100)
            throw new SQLException("Invalid coupon award capacity");
        }
        List<Delivery> delivery = checkCapacity(chr, awards);
        if (!existing) {
          Map<Integer, Long> cash = new TreeMap<>();
          for (JsonElement element : awards) {
            JsonObject a = element.getAsJsonObject();
            int kind = a.get("type").getAsInt(), quantity = a.get("quantity").getAsInt();
            if (kind == 4 || kind == 3) cash.merge(1, (long) quantity, Long::sum);
            if (kind == 1) cash.merge(2, (long) quantity, Long::sum);
            if (kind == 2) cash.merge(4, (long) quantity, Long::sum);
            if (kind == 3 && quantity / 5000 > 0)
              cash.merge(4, (long) (quantity / 5000), Long::sum);
          }
          for (var award : cash.entrySet()) {
            if (award.getValue() > Integer.MAX_VALUE)
              throw new SQLException("Coupon credit out of range");
            BridgePayments.credit(
                con, chr.getAccountID(), award.getKey(), award.getValue().intValue());
          }
          try (PreparedStatement ps =
              con.prepareStatement(
                  "UPDATE nxcode SET retriever=? WHERE id=? AND retriever IS NULL")) {
            ps.setString(1, chr.getName());
            ps.setInt(2, codeId);
            if (ps.executeUpdate() != 1) throw new SQLException("Coupon changed");
          }
          try (PreparedStatement ps =
              con.prepareStatement(
                  "INSERT INTO"
                      + " card_bridge_coupon_claims(code_id,account_id,receipt_id,payload,state)"
                      + " VALUES(?,?,?,?,'PENDING')")) {
            ps.setInt(1, codeId);
            ps.setInt(2, chr.getAccountID());
            ps.setString(3, receipt);
            ps.setString(4, awards.toString());
            ps.executeUpdate();
          }
        }
        try (PreparedStatement ps =
            con.prepareStatement(
                "UPDATE card_bridge_coupon_claims SET claim_session=?,claim_character=? WHERE"
                    + " code_id=? AND state='PENDING'")) {
          ps.setString(1, BridgeRewards.SESSION);
          ps.setInt(2, chr.getId());
          ps.setInt(3, codeId);
          if (ps.executeUpdate() != 1) throw new SQLException("Coupon changed");
        }
        con.commit();
        return new Claim(codeId, receipt, awards, delivery);
      } catch (Exception error) {
        con.rollback();
        throw error;
      }
    }
  }

  private static List<Delivery> checkCapacity(Character chr, JsonArray awards) throws SQLException {
    long mesos = 0;
    int cashSlots = 0;
    List<Delivery> plan = new ArrayList<>();
    List<Pair<Item, InventoryType>> inventory = new ArrayList<>();
    for (JsonElement element : awards) {
      JsonObject award = element.getAsJsonObject();
      int kind = award.get("type").getAsInt(), quantity = award.get("quantity").getAsInt();
      if (kind == 0) mesos += quantity;
      if (kind != 5) continue;
      int item = award.get("item").getAsInt();
      InventoryType type = ItemConstants.getInventoryType(item);
      if (quantity < 1
          || ItemInformationProvider.getInstance().getName(item) == null
          || type == InventoryType.UNDEFINED
          || type == InventoryType.EQUIPPED) capacity();
      boolean cash = ItemInformationProvider.getInstance().isCash(item);
      if (!cash
          && type != InventoryType.EQUIP
          && ItemInformationProvider.getInstance().getSlotMax(chr.getClient(), item) < 1)
        capacity();
      // Preserve source-row stack boundaries. Rechargeables and cash objects do not
      // merge during native delivery, so admission and delivery use this same plan.
      while (quantity > 0) {
        short count =
            (short)
                Math.min(
                    type == InventoryType.EQUIP || ItemConstants.isPet(item) ? 1 : Short.MAX_VALUE,
                    quantity);
        quantity -= count;
        plan.add(new Delivery(item, count, cash));
        if (plan.size() > 100) capacity();
        if (cash) cashSlots++;
        else inventory.add(new Pair<>(new Item(item, (short) 0, count), type));
      }
    }
    if (mesos + chr.getMeso() > Integer.MAX_VALUE
        || chr.getCashShop().getItemsSize() + cashSlots > 100
        || !Inventory.checkSpots(chr, inventory)) capacity();
    return List.copyOf(plan);
  }

  private static void capacity() {
    throw new BridgeHttp.Problem(
        409, "COUPON_CAPACITY", "Coupon delivery needs balance or inventory space.");
  }

  public static void clean(Connection con, long cutoff) throws SQLException {
    boolean exists = false;
    for (String table : List.of("card_bridge_coupon_claims", "CARD_BRIDGE_COUPON_CLAIMS"))
      try (ResultSet rs = con.getMetaData().getTables(con.getCatalog(), null, table, null)) {
        if (rs.next()) exists = true;
      }
    // Startup cleanup precedes adapter initialization on a new installation.
    if (!exists) return;
    try (PreparedStatement ps =
        con.prepareStatement(
            "DELETE FROM nxcode_items WHERE codeid IN (SELECT id FROM nxcode WHERE expiration<=?"
                + " AND NOT EXISTS (SELECT 1 FROM card_bridge_coupon_claims c WHERE"
                + " c.code_id=nxcode.id AND c.state='PENDING'))")) {
      ps.setLong(1, cutoff);
      ps.executeUpdate();
    }
    try (PreparedStatement ps =
        con.prepareStatement(
            "DELETE FROM nxcode WHERE expiration<=? AND NOT EXISTS (SELECT 1 FROM"
                + " card_bridge_coupon_claims c WHERE c.code_id=nxcode.id AND"
                + " c.state='PENDING')")) {
      ps.setLong(1, cutoff);
      ps.executeUpdate();
    }
  }

  public static void handle(Client client, String code) {
    Character chr = client.getPlayer();
    synchronized (accountLocks[Math.floorMod(chr.getAccountID(), accountLocks.length)]) {
      synchronized (chr) {
        try (BridgeCharacters.Guard guard = BridgeCharacters.save(chr)) {
          if (!BridgeCharacters.current(chr)) {
            client.enableCSActions();
            return;
          }
          if (BridgeRewards.hasPending(chr)) {
            chr.message("A previous inventory award is still being saved.");
            client.enableCSActions();
            return;
          }
          Claim claim;
          try {
            claim = claim(chr, code);
          } catch (Exception denied) {
            chr.message(
                "The coupon has not been delivered. Check its status, balance capacity and"
                    + " inventory space, then retry.");
            client.sendPacket(PacketCreator.showCashShopMessage((byte) 0xBB));
            client.enableCSActions();
            return;
          }
          pending.put(
              chr.getId(), new Pending(claim.code, chr.getAccountID(), claim.receipt, false, chr));
          try {
            for (JsonElement element : claim.awards) {
              JsonObject award = element.getAsJsonObject();
              if (award.get("type").getAsInt() == 0)
                chr.gainMeso(award.get("quantity").getAsInt(), false);
            }
            for (Delivery delivery : claim.delivery) {
              if (delivery.cash) {
                Item item = CashShop.generateCouponItem(delivery.item, delivery.quantity);
                if (item == null || (ItemConstants.isPet(item.getItemId()) && item.getPetId() < 0))
                  throw new IllegalStateException("Coupon item unavailable");
                chr.getCashShop().addToInventory(item);
              } else if (!InventoryManipulator.addById(
                  client, delivery.item, delivery.quantity, "", -1))
                throw new IllegalStateException("Coupon inventory changed");
            }
            pending.put(
                chr.getId(), new Pending(claim.code, chr.getAccountID(), claim.receipt, true, chr));
            chr.saveCharToDB(true);
            if (pending.containsKey(chr.getId()))
              chr.message(
                  "Coupon awards are awaiting a confirmed inventory save. Do not redeem another"
                      + " coupon yet.");
            else {
              client.sendPacket(PacketCreator.showBoughtQuestItem(0));
              chr.message("Coupon rewards granted.");
            }
          } catch (RuntimeException incomplete) {
            BridgeHttp.uncertainCommit();
          } finally {
            client.enableCSActions();
          }
        }
      }
    }
  }

  static void beforeSave(Connection con, Character chr) throws SQLException {
    Pending p = pending.get(chr.getId());
    if (p == null) return;
    if (p.owner != chr || !BridgeCharacters.current(chr))
      throw new SQLException("Replaced coupon character");
    BridgeHttp.requireLease();
    if (!p.delivered) throw new SQLException("Coupon inventory delivery incomplete");
    try (PreparedStatement ps =
        con.prepareStatement(
            "UPDATE card_bridge_coupon_claims SET state='DELIVERED' WHERE code_id=? AND"
                + " account_id=? AND receipt_id=? AND claim_session=? AND claim_character=? AND"
                + " state='PENDING'")) {
      ps.setInt(1, p.code);
      ps.setInt(2, p.account);
      ps.setString(3, p.receipt);
      ps.setString(4, BridgeRewards.SESSION);
      ps.setInt(5, chr.getId());
      if (ps.executeUpdate() == 1) return;
    }
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT state,receipt_id,claim_session,claim_character,account_id FROM"
                + " card_bridge_coupon_claims WHERE code_id=? FOR UPDATE")) {
      ps.setInt(1, p.code);
      try (ResultSet rs = ps.executeQuery()) {
        if (rs.next()
            && rs.getString(1).equals("DELIVERED")
            && p.receipt.equals(rs.getString(2))
            && BridgeRewards.SESSION.equals(rs.getString(3))
            && rs.getInt(4) == chr.getId()
            && rs.getInt(5) == p.account) return;
      }
    }
    throw new SQLException("Coupon claim changed before inventory commit");
  }

  static void afterSave(Character chr) {
    pending.computeIfPresent(chr.getId(), (id, p) -> p.owner == chr ? null : p);
  }

  public static void recover(Client client) {
    if (BridgeRewards.hasPending(client.getPlayer())) return;
    String code = null;
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement(
                "SELECT n.code FROM card_bridge_coupon_claims c JOIN nxcode n ON n.id=c.code_id"
                    + " WHERE c.account_id=? AND c.state='PENDING' ORDER BY c.created_at,c.code_id"
                    + " LIMIT 1")) {
      ps.setInt(1, client.getAccID());
      try (ResultSet rs = ps.executeQuery()) {
        if (rs.next()) code = rs.getString(1);
      }
    } catch (SQLException unavailable) {
      client.getPlayer().message("Pending coupon recovery is temporarily unavailable.");
      return;
    }
    if (code != null) handle(client, code);
  }
}
