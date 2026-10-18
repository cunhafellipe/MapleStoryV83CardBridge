package server.cardbridge;

import client.Character;
import client.Client;
import client.Ring;
import client.inventory.Equip;
import client.inventory.InventoryType;
import client.inventory.Item;
import client.inventory.manipulator.CashIdGenerator;
import config.YamlConfig;
import constants.inventory.ItemConstants;
import java.sql.*;
import java.util.Set;
import java.util.UUID;
import server.CashShop;
import server.CashShop.CashItem;
import service.NoteService;
import tools.DatabaseConnection;
import tools.PacketCreator;
import tools.Pair;

/** Atomically saves native ring/request benefits and their authoritative cash charge. */
public final class BridgeNativePurchases {
  private BridgeNativePurchases() {}

  private static final Set<Integer> CRUSH_RING_ITEMS =
      Set.of(1112000, 1112001, 1112002, 1112003, 1112005, 1112006, 1112007, 1112012);
  private static final Set<Integer> FRIENDSHIP_RING_ITEMS =
      Set.of(1112800, 1112801, 1112802, 1112810, 1112811, 1112812);

  private static long lastFailureLog;

  private static synchronized void failureLog() {
    long now = System.currentTimeMillis();
    if (now - lastFailureLog >= 60000) {
      lastFailureLog = now;
      System.err.println("card_bridge: NATIVE_PURCHASE_REJECTED; no cash or benefit committed");
    }
  }

  public static boolean supportedCash(int type) {
    return type == 1 || type == 2 || type == 4;
  }

  public static boolean validRingOffer(CashItem offer, boolean friendship) {
    if (offer == null) return false;
    return (friendship ? FRIENDSHIP_RING_ITEMS : CRUSH_RING_ITEMS).contains(offer.getItemId());
  }

  public static boolean ring(
      Client c,
      int type,
      CashItem offer,
      Character partner,
      String message,
      boolean friendship,
      NoteService notes) {
    if (!BridgeHttp.enabled()) return false;
    boolean completed =
        purchase(c, type, offer, friendship ? "friendship" : "crush", partner, message, 0);
    // Gifts are durable with the purchase. A notification failure cannot undo delivery.
    try {
      if (completed) {
        notes.sendWithFame(message, c.getPlayer().getName(), partner.getName());
        notes.show(partner);
      }
    } catch (Exception ignored) {
      System.err.println("card_bridge: RING_NOTIFICATION_UNAVAILABLE");
    }
    return true;
  }

  public static boolean request(
      Client c, CashItem offer, String name, int world, boolean transfer) {
    if (!BridgeHttp.enabled()) return false;
    purchase(c, 4, offer, transfer ? "world" : "name", null, name, world);
    return true;
  }

  static void charge(Connection con, int account, int type, int amount) throws SQLException {
    if (!supportedCash(type) || amount < 1 || amount > 100000000)
      throw new IllegalArgumentException("Invalid native cash purchase");
    String column =
        switch (type) {
          case 1 -> "nxCredit";
          case 2 -> "maplePoint";
          default -> "nxPrepaid";
        };
    try (PreparedStatement ps =
        con.prepareStatement(
            "UPDATE accounts SET "
                + column
                + "=COALESCE("
                + column
                + ",0)-? WHERE id=? AND banned=0"
                + " AND tempban<=UTC_TIMESTAMP() AND COALESCE("
                + column
                + ",0)>=?")) {
      ps.setInt(1, amount);
      ps.setInt(2, account);
      ps.setInt(3, amount);
      if (ps.executeUpdate() != 1)
        throw new BridgeHttp.Problem(
            409, "INSUFFICIENT_FUNDS", "The selected cash balance is too low.");
    }
    BridgePayments.assertLiveIdentity(con, account);
  }

  static boolean receiptExists(
      String receipt, int account, int character, int cashType, int amount, String kind)
      throws SQLException {
    try (Connection con = DatabaseConnection.getConnection()) {
      con.setNetworkTimeout(Runnable::run, 5000);
      con.setAutoCommit(false);
      try {
        // The original debit holds this existing row until its complete transaction
        // resolves. A lookup of a newly inserted receipt alone cannot prove absence.
        try (PreparedStatement accountLock =
            con.prepareStatement("SELECT id FROM accounts WHERE id=? FOR UPDATE")) {
          accountLock.setQueryTimeout(5);
          accountLock.setInt(1, account);
          try (ResultSet rs = accountLock.executeQuery()) {
            if (!rs.next()) throw new SQLException("Native purchase account cannot be reconciled");
          }
        }
        try (PreparedStatement ps =
            con.prepareStatement(
                "SELECT account_id,character_id,cash_type,amount,kind FROM"
                    + " card_bridge_native_purchases WHERE receipt_id=? FOR UPDATE")) {
          ps.setQueryTimeout(5);
          ps.setString(1, receipt);
          try (ResultSet rs = ps.executeQuery()) {
            if (!rs.next()) return false;
            if (rs.getInt(1) != account
                || rs.getInt(2) != character
                || rs.getInt(3) != cashType
                || rs.getInt(4) != amount
                || !kind.equals(rs.getString(5)))
              throw new SQLException("Native purchase receipt identity conflict");
            return true;
          }
        }
      } finally {
        // Reconciliation reads only; release the account lock without another commit.
        con.rollback();
      }
    }
  }

  private static boolean purchase(
      Client c, int type, CashItem offer, String kind, Character partner, String text, int world) {
    return purchase(c, type, offer, kind, partner, text, world, 0);
  }

  private static boolean purchase(
      Client c,
      int type,
      CashItem offer,
      String kind,
      Character partner,
      String text,
      int world,
      int quantity) {
    Character chr = c.getPlayer();
    synchronized (chr) {
      try (BridgeCharacters.Guard guard = BridgeCharacters.save(chr)) {
        if (!BridgeCharacters.current(chr)) {
          c.enableCSActions();
          return false;
        }
        CashShop shop = chr.getCashShop();
        Item delivered = null;
        java.util.List<Item> deliveredItems = new java.util.ArrayList<>();
        int left = -1, right = -1;
        boolean committed = false;
        int newSlots = -1;
        boolean entitlement = kind.equals("gift") || kind.endsWith("_slots");
        String receipt = UUID.randomUUID().toString();
        try {
          BridgeHttp.requireLease();
          if (!supportedCash(type)
              || offer == null
              || !offer.isOnSale()
              || offer.getPrice() < 1
              || offer.getPrice() > 100000000
              || (!entitlement && shop.getItemsSize() >= 100)
              || BridgeRewards.hasPending(chr))
            throw new IllegalArgumentException("Invalid native purchase");
          boolean ring = kind.equals("crush") || kind.equals("friendship");
          boolean ordinary = kind.equals("item") || kind.equals("package");
          if (ring && !validRingOffer(offer, kind.equals("friendship")))
            throw new IllegalArgumentException("Invalid ring offer family");
          if (!ordinary && !entitlement) {
            delivered = offer.toItem();
            deliveredItems.add(delivered);
          }
          if (ring
              && (partner == null
                  || partner.getId() == chr.getId()
                  || !(delivered instanceof Equip)))
            throw new IllegalArgumentException("Invalid ring recipient or item");
          try (Connection con = DatabaseConnection.getConnection()) {
            con.setAutoCommit(false);
            try {
              // Lock the account before issuing any persisted benefit. Website and native
              // charges contend on this same row; a stale balance preview grants nothing.
              charge(con, c.getAccID(), type, offer.getPrice());
              if (ordinary) {
                if (kind.equals("package")) {
                  java.util.List<Integer> serials =
                      CashShop.CashItemFactory.getPackageSerials(offer.getItemId());
                  if (serials.isEmpty() || shop.getItemsSize() + serials.size() > 100)
                    throw new IllegalArgumentException("Invalid cash package capacity");
                  java.util.List<CashItem> contents = new java.util.ArrayList<>();
                  for (int serial : serials) {
                    CashItem member = CashShop.CashItemFactory.getItem(serial);
                    if (member == null)
                      throw new IllegalArgumentException("Cash package item unavailable");
                    contents.add(member);
                  }
                  for (CashItem member : contents) deliveredItems.add(member.toItem());
                } else deliveredItems.add(offer.toItem());
                if (shop.getItemsSize() + deliveredItems.size() > 100
                    || deliveredItems.stream()
                        .anyMatch(
                            item ->
                                item == null
                                    || (ItemConstants.isPet(item.getItemId())
                                        && item.getPetId() < 0)))
                  throw new IllegalArgumentException("Cash inventory delivery unavailable");
                delivered = deliveredItems.get(0);
              }
              if (ring) {
                left = CashIdGenerator.generateCashId();
                right = CashIdGenerator.generateCashId();
                insertRing(con, left, right, offer.getItemId(), partner);
                insertRing(con, right, left, offer.getItemId(), chr);
                ((Equip) delivered).setRingId(left);
                try (PreparedStatement ps =
                    con.prepareStatement("INSERT INTO gifts VALUES(DEFAULT,?,?,?,?,?)")) {
                  ps.setInt(1, partner.getId());
                  ps.setString(2, chr.getName());
                  ps.setString(3, text);
                  ps.setInt(4, offer.getSN());
                  ps.setInt(5, right);
                  ps.executeUpdate();
                }
              } else if (kind.equals("gift")) {
                try (PreparedStatement ps =
                    con.prepareStatement(
                        "SELECT accountid FROM characters WHERE id=? FOR UPDATE")) {
                  ps.setInt(1, world);
                  try (ResultSet rs = ps.executeQuery()) {
                    if (!rs.next() || rs.getInt(1) == c.getAccID())
                      throw new IllegalArgumentException("Gift recipient unavailable");
                  }
                }
                try (PreparedStatement ps =
                    con.prepareStatement("INSERT INTO gifts VALUES(DEFAULT,?,?,?,?,?)")) {
                  ps.setInt(1, world);
                  ps.setString(2, chr.getName());
                  ps.setString(3, text);
                  ps.setInt(4, offer.getSN());
                  ps.setInt(5, -1);
                  ps.executeUpdate();
                }
              } else if (kind.endsWith("_slots")) {
                newSlots = expand(con, c, kind, world, quantity);
              } else if (!ordinary) {
                insertRequest(con, chr, kind, text, world);
              }
              for (Item item : deliveredItems) shop.addToInventory(item);
              if (!entitlement) shop.save(con);
              try (PreparedStatement ps =
                  con.prepareStatement(
                      "INSERT INTO card_bridge_native_purchases"
                          + " VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)")) {
                ps.setString(1, receipt);
                ps.setInt(2, c.getAccID());
                ps.setInt(3, chr.getId());
                ps.setInt(4, type);
                ps.setInt(5, offer.getPrice());
                ps.setString(6, kind);
                ps.executeUpdate();
              }
              BridgeHttp.requireLease();
              try {
                con.commit();
                committed = true;
              } catch (SQLException uncertain) {
                // Close the failed connection before querying the independent durable receipt.
                try {
                  con.close();
                } catch (SQLException ignored) {
                  /* Verify the durable receipt independently. */
                }
                try {
                  committed =
                      receiptExists(
                          receipt, c.getAccID(), chr.getId(), type, offer.getPrice(), kind);
                } catch (SQLException unavailable) {
                  BridgeHttp.uncertainCommit();
                }
                if (!committed) throw uncertain;
              }
            } catch (Exception error) {
              if (!committed && !con.isClosed()) con.rollback();
              throw error;
            }
          }
        } catch (Exception error) {
          if (!committed) {
            for (Item item : deliveredItems) if (item != null) shop.removeFromInventory(item);
            // Native factories persist pet identities separately. Once noncommit is
            // proven, remove only the newly materialized, unreferenced identities.
            cleanupPets(deliveredItems);
            if (left >= 0) CashIdGenerator.freeCashId(left);
            if (right >= 0) CashIdGenerator.freeCashId(right);
            failureLog();
            c.sendPacket(PacketCreator.showCashShopMessage((byte) 0));
            c.enableCSActions();
            return false;
          }
        }
        // Notifications occur only after the durable inventory, request and debit commit.
        if (kind.equals("inventory_slots")) {
          chr.getInventory(InventoryType.getByType((byte) world)).setSlotLimit(newSlots);
          c.sendPacket(PacketCreator.showBoughtInventorySlots((byte) world, (byte) newSlots));
        } else if (kind.equals("storage_slots")) {
          chr.getStorage().bridgeSetSlots(newSlots);
          chr.setUsedStorage();
          c.sendPacket(PacketCreator.showBoughtStorageSlots((byte) newSlots));
        } else if (kind.equals("character_slots")) {
          c.setCharacterSlots((byte) newSlots);
          c.sendPacket(PacketCreator.showBoughtCharacterSlot((short) newSlots));
        } else if (kind.equals("gift")) {
          // The caller sends recipient notifications after the durable entitlement.
        } else if (kind.equals("item")) {
          c.sendPacket(PacketCreator.showBoughtCashItem(delivered, c.getAccID()));
        } else if (kind.equals("package")) {
          c.sendPacket(PacketCreator.showBoughtCashPackage(deliveredItems, c.getAccID()));
        } else if (kind.equals("name")) {
          chr.bridgeNameChangePending();
          c.sendPacket(PacketCreator.showNameChangeSuccess(delivered, c.getAccID()));
        } else if (kind.equals("world")) {
          c.sendPacket(PacketCreator.showWorldTransferSuccess(delivered, c.getAccID()));
        } else {
          if (kind.equals("friendship")) {
            chr.addFriendshipRing(Ring.loadFromDb(left));
            c.sendPacket(
                PacketCreator.showBoughtCashRing(delivered, partner.getName(), c.getAccID()));
          } else {
            chr.addCrushRing(Ring.loadFromDb(left));
            c.sendPacket(PacketCreator.showBoughtCashItem(delivered, c.getAccID()));
          }
        }
        c.sendPacket(PacketCreator.showCash(chr));
        c.enableCSActions();
        return true;
      }
    }
  }

  public static boolean buy(Client client, int type, CashItem offer, boolean pack) {
    return purchase(client, type, offer, pack ? "package" : "item", null, null, 0);
  }

  public static boolean gift(Client client, CashItem offer, int recipient, String message) {
    if (message == null || message.isEmpty() || message.length() > 73 || recipient < 1)
      return false;
    return purchase(client, 4, offer, "gift", null, message, recipient, 0);
  }

  public static boolean slots(
      Client client, int cashType, CashItem offer, int category, int inventoryType, int quantity) {
    if (category < 1
        || category > 3
        || (category == 1 && (inventoryType < 1 || inventoryType > 4))
        || (category < 3 && quantity != 4 && quantity != 8)
        || (category == 3 && quantity != 1)) return rejectSlots(client);
    if (offer == null) {
      if (category == 3 || quantity != 4) return rejectSlots(client);
      offer = CashShop.CashItemFactory.bridgeFixedSlotOffer();
    } else if ((category == 1 && offer.getItemId() != 9110000 + inventoryType * 1000)
        || (category == 2 && offer.getItemId() != 9110000)
        || (category == 3 && offer.getItemId() != 5430000)) return rejectSlots(client);
    return purchase(
        client,
        cashType,
        offer,
        category == 1 ? "inventory_slots" : category == 2 ? "storage_slots" : "character_slots",
        null,
        null,
        inventoryType,
        quantity);
  }

  private static boolean rejectSlots(Client client) {
    client.sendPacket(PacketCreator.showCashShopMessage((byte) 0));
    client.enableCSActions();
    return false;
  }

  private static int expand(
      Connection con, Client client, String kind, int inventoryType, int quantity)
      throws SQLException {
    Character chr = client.getPlayer();
    String table, field, where;
    int id, limit, observed;
    if (kind.equals("inventory_slots")) {
      table = "characters";
      field =
          switch (inventoryType) {
            case 1 -> "equipslots";
            case 2 -> "useslots";
            case 3 -> "setupslots";
            case 4 -> "etcslots";
            default -> throw new IllegalArgumentException("Invalid inventory");
          };
      where = "id=? AND accountid=" + client.getAccID();
      id = chr.getId();
      limit = 96;
      observed = chr.getInventory(InventoryType.getByType((byte) inventoryType)).getSlotLimit();
    } else if (kind.equals("storage_slots")) {
      table = "storages";
      field = "slots";
      where = "accountid=? AND world=" + chr.getWorld();
      id = client.getAccID();
      limit = 48;
      observed = chr.getStorage().getSlots();
    } else {
      table = "accounts";
      field = "characterslots";
      where = "id=?";
      id = client.getAccID();
      limit = 15;
      observed = client.getCharacterSlots();
    }
    int current;
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT " + field + " FROM " + table + " WHERE " + where + " FOR UPDATE")) {
      ps.setQueryTimeout(5);
      ps.setInt(1, id);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) throw new SQLException("Slot owner unavailable");
        current = rs.getInt(1);
        if (rs.next()) throw new SQLException("Ambiguous slot owner");
      }
    }
    if (current < 0 || current != observed || current > limit - quantity)
      throw new IllegalArgumentException("Slot expansion unavailable");
    try (PreparedStatement ps =
        con.prepareStatement("UPDATE " + table + " SET " + field + "=? WHERE " + where)) {
      ps.setInt(1, current + quantity);
      ps.setInt(2, id);
      if (ps.executeUpdate() != 1) throw new SQLException("Slot expansion changed");
    }
    return current + quantity;
  }

  public static java.util.List<Pair<Item, String>> receiveGifts(Client client) {
    Character chr = client.getPlayer();
    synchronized (chr) {
      try (BridgeCharacters.Guard guard = BridgeCharacters.save(chr)) {
        java.util.List<Item> staged = new java.util.ArrayList<>();
        java.util.List<Pair<Item, String>> notifications = new java.util.ArrayList<>();
        String receipt = UUID.randomUUID().toString();
        boolean committed = false;
        int count = 0;
        try {
          BridgeHttp.requireLease();
          if (!BridgeCharacters.current(chr) || BridgeRewards.hasPending(chr)) return notifications;
          try (Connection con = DatabaseConnection.getConnection()) {
            con.setAutoCommit(false);
            try {
              BridgePayments.lockAccount(con, client.getAccID(), 4);
              BridgePayments.assertLiveIdentity(con, client.getAccID());
              record Gift(int id, CashItem offer, String sender, String message, int ring) {}
              java.util.List<Gift> gifts = new java.util.ArrayList<>();
              int spaces = 0;
              try (PreparedStatement ps =
                  con.prepareStatement(
                      "SELECT id,sn,`from`,message,ringid FROM gifts WHERE `to`=? ORDER BY id FOR"
                          + " UPDATE")) {
                ps.setQueryTimeout(5);
                ps.setInt(1, chr.getId());
                try (ResultSet rs = ps.executeQuery()) {
                  while (rs.next()) {
                    CashItem offer = CashShop.CashItemFactory.getItem(rs.getInt(2));
                    if (offer == null) throw new IllegalArgumentException("Gift item unavailable");
                    if (CashShop.CashItemFactory.isPackage(offer.getItemId())) {
                      java.util.List<Integer> serials =
                          CashShop.CashItemFactory.getPackageSerials(offer.getItemId());
                      if (serials.isEmpty())
                        throw new IllegalArgumentException("Gift package unavailable");
                      for (int sn : serials)
                        if (CashShop.CashItemFactory.getItem(sn) == null)
                          throw new IllegalArgumentException("Gift package item unavailable");
                      spaces += serials.size();
                    } else spaces++;
                    if (chr.getCashShop().getItemsSize() + spaces > 100)
                      throw new IllegalArgumentException("Gift inventory capacity");
                    gifts.add(
                        new Gift(
                            rs.getInt(1), offer, rs.getString(3), rs.getString(4), rs.getInt(5)));
                  }
                }
              }
              if (gifts.isEmpty()) {
                con.rollback();
                return notifications;
              }
              count = gifts.size();
              for (Gift gift : gifts) {
                Item display = gift.offer.toItem();
                if (display == null) throw new IllegalArgumentException("Gift item unavailable");
                display.setGiftFrom(gift.sender);
                if (display instanceof Equip equip) equip.setRingId(gift.ring);
                notifications.add(new Pair<>(display, gift.message));
                if (CashShop.CashItemFactory.isPackage(gift.offer.getItemId()))
                  for (int sn :
                      CashShop.CashItemFactory.getPackageSerials(gift.offer.getItemId())) {
                    Item item = CashShop.CashItemFactory.getItem(sn).toItem();
                    if (item == null
                        || (ItemConstants.isPet(item.getItemId()) && item.getPetId() < 0))
                      throw new IllegalArgumentException("Gift package item unavailable");
                    item.setGiftFrom(gift.sender);
                    staged.add(item);
                  }
                else {
                  if (ItemConstants.isPet(display.getItemId()) && display.getPetId() < 0)
                    throw new IllegalArgumentException("Gift pet unavailable");
                  staged.add(display);
                }
              }
              for (Item item : staged) chr.getCashShop().addToInventory(item);
              chr.getCashShop().save(con);
              try (PreparedStatement ps =
                  con.prepareStatement("DELETE FROM gifts WHERE id=? AND `to`=?")) {
                for (Gift gift : gifts) {
                  ps.setInt(1, gift.id);
                  ps.setInt(2, chr.getId());
                  if (ps.executeUpdate() != 1) throw new SQLException("Gift changed");
                }
              }
              try (PreparedStatement ps =
                  con.prepareStatement(
                      "INSERT INTO card_bridge_native_purchases"
                          + " VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)")) {
                ps.setString(1, receipt);
                ps.setInt(2, client.getAccID());
                ps.setInt(3, chr.getId());
                ps.setInt(4, 4);
                ps.setInt(5, 0);
                ps.setString(6, "gift_delivery");
                ps.executeUpdate();
              }
              BridgeHttp.requireLease();
              try {
                con.commit();
                committed = true;
              } catch (SQLException uncertain) {
                try {
                  con.close();
                } catch (SQLException ignored) {
                }
                try {
                  committed =
                      receiptExists(receipt, client.getAccID(), chr.getId(), 4, 0, "gift_delivery");
                } catch (SQLException unknown) {
                  BridgeHttp.uncertainCommit();
                }
                if (!committed) throw uncertain;
              }
            } catch (Exception error) {
              if (!committed && !con.isClosed()) con.rollback();
              throw error;
            }
          }
        } catch (Exception unavailable) {
          if (!committed) {
            for (Item item : staged) chr.getCashShop().removeFromInventory(item);
            cleanupPets(staged);
            chr.message(
                "Gifts are awaiting delivery. Leave enough cash inventory space and retry later.");
            return java.util.List.of();
          }
        }
        chr.getCashShop().bridgeAddGiftNotes(count);
        return notifications;
      }
    }
  }

  private static void cleanupPets(java.util.List<Item> items) {
    for (Item item : items)
      if (item != null && item.getPetId() >= 0) {
        try (Connection cleanup = DatabaseConnection.getConnection();
            PreparedStatement ps = cleanup.prepareStatement("DELETE FROM pets WHERE petid=?")) {
          ps.setInt(1, item.getPetId());
          ps.executeUpdate();
          CashIdGenerator.freeCashId(item.getPetId());
        } catch (SQLException unavailable) {
          System.err.println("card_bridge: UNREFERENCED_PET_CLEANUP_PENDING");
        }
      }
  }

  private static void insertRing(Connection con, int id, int other, int item, Character partner)
      throws SQLException {
    try (PreparedStatement ps =
        con.prepareStatement(
            "INSERT INTO rings(id,itemid,partnerRingId,partnerChrId,partnername)"
                + " VALUES(?,?,?,?,?)")) {
      ps.setInt(1, id);
      ps.setInt(2, item);
      ps.setInt(3, other);
      ps.setInt(4, partner.getId());
      ps.setString(5, partner.getName());
      ps.executeUpdate();
    }
  }

  private static void insertRequest(
      Connection con, Character chr, String kind, String name, int world) throws SQLException {
    boolean transfer = kind.equals("world");
    String table = transfer ? "worldtransfers" : "namechanges";
    long cooldown =
        transfer
            ? YamlConfig.config.server.WORLD_TRANSFER_COOLDOWN
            : YamlConfig.config.server.NAME_CHANGE_COOLDOWN;
    try (PreparedStatement ps =
        con.prepareStatement(
            "SELECT completionTime FROM " + table + " WHERE characterid=? FOR UPDATE")) {
      ps.setInt(1, chr.getId());
      try (ResultSet rs = ps.executeQuery()) {
        while (rs.next()) {
          Timestamp completed = rs.getTimestamp(1);
          if (completed == null || completed.getTime() + cooldown > System.currentTimeMillis())
            throw new IllegalArgumentException("Request already pending or on cooldown");
        }
      }
    }
    try (PreparedStatement ps =
        con.prepareStatement(
            transfer
                ? "INSERT INTO worldtransfers(characterid,`from`,`to`) VALUES(?,?,?)"
                : "INSERT INTO namechanges(characterid,old,new) VALUES(?,?,?)")) {
      ps.setInt(1, chr.getId());
      if (transfer) {
        ps.setInt(2, chr.getWorld());
        ps.setInt(3, world);
      } else {
        ps.setString(2, chr.getName());
        ps.setString(3, name);
      }
      ps.executeUpdate();
    }
  }
}
