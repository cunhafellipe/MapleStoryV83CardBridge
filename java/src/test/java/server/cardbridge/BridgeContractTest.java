package server.cardbridge;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import client.Character;
import client.Client;
import client.Ring;
import client.inventory.*;
import client.inventory.manipulator.CashIdGenerator;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import net.packet.InPacket;
import net.server.PlayerStorage;
import net.server.channel.Channel;
import net.server.channel.handlers.CashOperationHandler;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import server.CashShop;
import server.ItemInformationProvider;
import service.NoteService;
import tools.DatabaseConnection;
import tools.PacketCreator;

public class BridgeContractTest {
  static com.google.gson.JsonObject paymentInput(int type, int amount, String id) {
    var intent = new com.google.gson.JsonObject();
    intent.addProperty("version", 1);
    intent.addProperty("providerId", "maplestory.v83");
    intent.addProperty("transactionId", id);
    intent.addProperty("userId", "collector-user");
    intent.addProperty("externalCurrency", BridgePayments.currency(type));
    intent.addProperty("externalUnits", Integer.toString(amount));
    var q = new com.google.gson.JsonObject();
    q.addProperty("productId", "series-one");
    q.addProperty("quantity", 1);
    q.addProperty("productRevision", 1);
    q.addProperty("catalogVersion", 2);
    q.addProperty("adminRevision", 3);
    var price = new com.google.gson.JsonObject();
    price.addProperty("currencyId", "nx");
    price.addProperty("amount", amount);
    q.add("price", price);
    intent.add("quote", q);
    var identity = new com.google.gson.JsonObject();
    identity.addProperty("providerId", "maplestory.v83");
    identity.addProperty("transactionId", id);
    var input = new com.google.gson.JsonObject();
    input.addProperty("accountId", 1);
    input.addProperty("cashType", type);
    input.add("intent", intent);
    input.addProperty("fingerprint", BridgePayments.hash(intent));
    input.addProperty("preparationId", "ep_" + BridgePayments.hash(identity));
    return input;
  }

  @Test
  void emptyPendingPageRetainsExplicitTerminalCursorOnTheWire() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = new com.google.gson.JsonObject();
      input.add("after", com.google.gson.JsonNull.INSTANCE);
      input.addProperty("limit", 1);
      var wire = com.google.gson.JsonParser.parseString(
          BridgeHttp.JSON.toJson(BridgePayments.pending(input))).getAsJsonObject();
      assertEquals(0, wire.getAsJsonArray("items").size());
      assertTrue(wire.has("nextCursor"));
      assertTrue(wire.get("nextCursor").isJsonNull());
    }
  }

  @Test
  void boundPaymentRefundRetainsCapacityAndOriginalProofAcrossBans() throws Exception {
    try (Fixture f = new Fixture()) {
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET nxCredit=2147483647");
      }
      var input = paymentInput(1, 1000, "a".repeat(64));
      var paid = BridgePayments.admit(input, "debit");
      assertEquals("DEBITED", paid.get("state"));
      assertEquals(true, paid.get("headroomReserved"));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.change(1, 1, 1));
      assertEquals(Integer.MAX_VALUE - 1000, BridgeWallet.read(1, 1));
      var request = new com.google.gson.JsonObject();
      request.addProperty("transactionId", "a".repeat(64));
      request.addProperty("fingerprint", input.get("fingerprint").getAsString());
      request.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      request.addProperty("debitReference", "debit:" + "a".repeat(64));
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET banned=1");
      }
      var refund = BridgePayments.complete(request, true);
      assertEquals("REFUNDED", refund.get("state"));
      assertEquals("0", refund.get("owedAmount"));
      assertEquals(false, refund.get("headroomReserved"));
      assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, 1));
      assertEquals(refund, BridgePayments.complete(request, true));
      assertEquals(10, ((Map<?, ?>) refund.get("refundReceipt")).size());
      assertEquals(input.get("intent"), refund.get("intent"));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.debit("a".repeat(64), 1, 1000, 1));
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        assertThrows(SQLException.class, () -> s.execute("DELETE FROM accounts WHERE id=1"));
      }
    }
  }

  @Test
  void paymentCancellationFencesLateDebitAndRejectsChangedIntent() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "b".repeat(64));
      var cancelled = BridgePayments.admit(input, "cancel");
      assertEquals("CANCELLED", cancelled.get("state"));
      assertEquals(cancelled, BridgePayments.admit(input, "debit"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(8, ((Map<?, ?>) cancelled.get("noDebitReceipt")).size());
      assertThrows(
          BridgeHttp.Problem.class,
          () -> BridgePayments.admit(paymentInput(1, 999, "b".repeat(64)), "debit"));
    }
  }

  @Test
  void legacyPaymentAdoptionNeverDebitsAgainAndKeepsOverflowOwed() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeWallet.debit("c".repeat(64), 1, 1000, 1);
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET nxCredit=2147483647");
      }
      var input = paymentInput(1, 1000, "c".repeat(64));
      var paid = BridgePayments.admit(input, "adopt-legacy");
      assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, 1));
      assertEquals(false, paid.get("headroomReserved"));
      var request = new com.google.gson.JsonObject();
      request.addProperty("transactionId", "c".repeat(64));
      request.addProperty("fingerprint", input.get("fingerprint").getAsString());
      request.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      request.addProperty("debitReference", "debit:" + "c".repeat(64));
      var owed = BridgePayments.complete(request, true);
      assertEquals("DEBITED", owed.get("state"));
      assertEquals("1000", owed.get("owedAmount"));
      assertFalse(owed.containsKey("refundReceipt"));
      BridgeWallet.change(1, 1, -1000);
      assertEquals("REFUNDED", BridgePayments.complete(request, true).get("state"));
      assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void paymentFinalizationReleasesCapacityAndPreventsRefund() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "d".repeat(64));
      BridgePayments.admit(input, "debit");
      var request = new com.google.gson.JsonObject();
      request.addProperty("transactionId", "d".repeat(64));
      request.addProperty("fingerprint", input.get("fingerprint").getAsString());
      request.addProperty("purchaseId", "purchase-123");
      var done = BridgePayments.complete(request, false);
      assertEquals("FULFILLED", done.get("state"));
      assertEquals(false, done.get("headroomReserved"));
      assertEquals(done, BridgePayments.complete(request, false));
      request.remove("purchaseId");
      request.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      request.addProperty("debitReference", "debit:" + "d".repeat(64));
      assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.complete(request, true));
    }
  }

  @Test
  void canonicalPaymentIntentMatchesSortedJsonAndStrictTypes() throws Exception {
    var value = com.google.gson.JsonParser.parseString("{\"z\":1,\"a\":{\"z\":\"<&=\",\"a\":2}}");
    assertEquals("{\"a\":{\"a\":2,\"z\":\"<&=\"},\"z\":1}", BridgePayments.canonical(value));
    var input = paymentInput(1, 1000, "e".repeat(64));
    input.addProperty("accountId", "1");
    assertThrows(IllegalArgumentException.class, () -> BridgePayments.binding(input));
  }

  @Test
  void missingAccountCancellationFencesRecreatedNumericIdentity() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "1".repeat(64));
      input.addProperty("accountId", 999);
      var cancelled = BridgePayments.admit(input, "cancel");
      assertEquals("CANCELLED", cancelled.get("state"));
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute(
            "INSERT INTO accounts VALUES(999,'Replacement','password',20000,0,0,0,TIMESTAMP"
                + " '1970-01-01 00:00:00')");
      }
      assertEquals("CANCELLED", BridgePayments.admit(input, "debit").get("state"));
      var other = paymentInput(1, 1000, "2".repeat(64));
      other.addProperty("accountId", 999);
      assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.admit(other, "debit"));
      assertEquals(20000, BridgeWallet.read(999, 1));
    }
  }

  @Test
  void accountRenamePreservesRefundIncarnation() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "3".repeat(64));
      var paid = BridgePayments.admit(input, "debit");
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET name='Renamed' WHERE id=1");
      }
      var request = new com.google.gson.JsonObject();
      request.addProperty("transactionId", "3".repeat(64));
      request.addProperty("fingerprint", input.get("fingerprint").getAsString());
      request.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      request.addProperty("debitReference", "debit:" + "3".repeat(64));
      var refunded = BridgePayments.complete(request, true);
      assertEquals(paid.get("accountIncarnation"), refunded.get("accountIncarnation"));
      assertEquals("REFUNDED", refunded.get("state"));
      assertEquals(10000, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void hundredMillionUnitsAndRefundCapacityWorkForEveryCashSource() throws Exception {
    try (Fixture f = new Fixture()) {
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute(
            "UPDATE accounts SET nxCredit=2147483647,maplePoint=2147483647,nxPrepaid=2147483647");
      }
      for (int type : List.of(1, 2, 4)) {
        String id = Integer.toString(type).repeat(64);
        var input = paymentInput(type, 100000000, id);
        var paid = BridgePayments.admit(input, "debit");
        assertEquals(100000000, paid.get("amount"));
        assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.change(1, type, 1));
        var request = new com.google.gson.JsonObject();
        request.addProperty("transactionId", id);
        request.addProperty("fingerprint", input.get("fingerprint").getAsString());
        request.addProperty(
            "compensationId",
            BridgePayments.compensation(
                input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
        request.addProperty("debitReference", "debit:" + id);
        assertEquals("REFUNDED", BridgePayments.complete(request, true).get("state"));
        assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, type));
      }
    }
  }

  @Test
  void sourceBoundAwardReplaysAfterLostCommitAcknowledgement() throws Exception {
    try (Fixture f = new Fixture()) {
      f.commitAcknowledgementsToLose = 1;
      assertThrows(
          SQLException.class, () -> BridgeAwards.credit(1, 1, 100, "GROUND_NX", "stable-drop"));
      assertTrue(BridgeAwards.credit(1, 1, 100, "GROUND_NX", "stable-drop"));
      assertEquals(10100, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_awards"));
      assertThrows(
          IllegalArgumentException.class,
          () -> BridgeAwards.credit(1, 1, 250, "GROUND_NX", "stable-drop"));
    }
  }

  static void couponFixture(Fixture f, int kind, int quantity) throws Exception {
    try (Connection c = f.connection();
        Statement s = c.createStatement()) {
      s.execute(
          "CREATE TABLE nxcode(id INT PRIMARY KEY,code VARCHAR(32),expiration BIGINT,retriever"
              + " VARCHAR(13))");
      s.execute("CREATE TABLE nxcode_items(codeid INT,type INT,item INT,quantity INT)");
      s.execute("INSERT INTO nxcode VALUES(10,'LEGACYCASH',9223372036854775807,NULL)");
      s.execute("INSERT INTO nxcode_items VALUES(10," + kind + ",0," + quantity + ")");
    }
  }

  @Test
  void couponDualSourceRefusalRollsBackEveryCreditAndConsumption() throws Exception {
    try (Fixture f = new Fixture()) {
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET nxPrepaid=2147483647");
      }
      BridgePayments.admit(paymentInput(4, 1000, "5".repeat(64)), "debit");
      couponFixture(f, 3, 5000);
      assertThrows(BridgeHttp.Problem.class, () -> BridgeCoupons.claim(f.chr, "LEGACYCASH"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(Integer.MAX_VALUE - 1000, BridgeWallet.read(1, 4));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM nxcode WHERE retriever IS NOT NULL"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims"));
    }
  }

  @Test
  void couponLostClaimAckRetainsOneCashAwardAndDeliveryIdentity() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 3, 5000);
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgeCoupons.claim(f.chr, "LEGACYCASH"));
      var claim = BridgeCoupons.claim(f.chr, "LEGACYCASH");
      assertNotNull(claim.receipt());
      assertEquals(15000, BridgeWallet.read(1, 1));
      assertEquals(1, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims"));
    }
  }

  static void mtsFixture(Fixture f) throws Exception {
    try (Connection c = f.connection();
        Statement s = c.createStatement()) {
      s.execute(
          "INSERT INTO accounts VALUES(2,'Seller','password',0,0,0,0,TIMESTAMP '1970-01-01"
              + " 00:00:00')");
      s.execute("UPDATE accounts SET nxPrepaid=5000 WHERE id=1");
      s.execute("CREATE TABLE characters(id INT PRIMARY KEY,accountid INT)");
      s.execute("INSERT INTO characters VALUES(7,1),(22,2)");
      s.execute("CREATE TABLE mts_items(id INT PRIMARY KEY,seller INT,price INT,transfer INT)");
      s.execute("INSERT INTO mts_items VALUES(77,22,1000,0)");
      s.execute("CREATE TABLE mts_cart(itemid INT,cid INT)");
      s.execute("INSERT INTO mts_cart VALUES(77,7)");
    }
  }

  @Test
  void mtsSaleAckLossReplaysOneBuyerChargeAndOneSellerCredit() throws Exception {
    try (Fixture f = new Fixture()) {
      mtsFixture(f);
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgeAwards.sale(1, 7, 77));
      BridgeAwards.sale(1, 7, 77);
      assertEquals(3800, BridgeWallet.read(1, 4));
      assertEquals(1000, BridgeWallet.read(2, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_awards"));
      assertEquals(7, f.scalar("SELECT seller FROM mts_items WHERE id=77"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM mts_cart"));
    }
  }

  @Test
  void fundingSharesRefundCapacityAndReplaysItsOriginalReceipt() throws Exception {
    try (Fixture f = new Fixture()) {
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("UPDATE accounts SET nxCredit=2147483647");
      }
      var input = paymentInput(1, 1000, "6".repeat(64));
      BridgePayments.admit(input, "debit");
      try (Connection c = f.connection()) {
        assertThrows(
            BridgeHttp.Problem.class,
            () -> BridgeAwards.operatorFund(c, "Collector", 1, 1, "operator-1"));
      }
      var done = new com.google.gson.JsonObject();
      done.addProperty("transactionId", "6".repeat(64));
      done.addProperty("fingerprint", input.get("fingerprint").getAsString());
      done.addProperty("purchaseId", "delivered-original");
      BridgePayments.complete(done, false);
      f.commitAcknowledgementsToLose = 1;
      try (Connection c = f.connection()) {
        assertThrows(
            SQLException.class,
            () -> BridgeAwards.operatorFund(c, "Collector", 1, 1000, "operator-1"));
      }
      try (Connection c = f.connection()) {
        BridgeAwards.operatorFund(c, "Collector", 1, 1000, "operator-1");
      }
      assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_operator_funding"));
    }
  }

  @Test
  void voteExchangeRollsBackConsiderationOnRefusalAndReplaysLostAck() throws Exception {
    try (Fixture f = new Fixture()) {
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("ALTER TABLE accounts ADD votepoints INT DEFAULT 0");
        s.execute("UPDATE accounts SET votepoints=2,nxCredit=2147483647");
      }
      String key = "11111111-1111-4111-8111-111111111111";
      assertThrows(BridgeHttp.Problem.class, () -> BridgeExchanges.vote(1, 3000, key));
      assertEquals(2, f.scalar("SELECT votepoints FROM accounts WHERE id=1"));
      BridgeWallet.change(1, 1, -3000);
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgeExchanges.vote(1, 3000, key));
      BridgeExchanges.vote(1, 3000, key);
      assertEquals(1, f.scalar("SELECT votepoints FROM accounts WHERE id=1"));
      assertEquals(Integer.MAX_VALUE, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_awards"));
    }
  }

  @Test
  void leafExchangeSavesConsumptionAndCreditTogetherAndNeverConsumesReplay() throws Exception {
    try (Fixture f = new Fixture()) {
      Item leaf = new Item(4001126, (short) 1, (short) 2);
      f.inventory.addItemFromDB(leaf);
      String key = "22222222-2222-4222-8222-222222222222";
      assertTrue(BridgeExchanges.exchange(f.client, false, 4001126, 3000, key));
      assertEquals(13000, BridgeWallet.read(1, 1));
      assertEquals(1, f.inventory.getItem((short) 1).getQuantity());
      assertEquals(1, f.scalar("SELECT quantity FROM saved_items WHERE id=1"));
      assertTrue(BridgeExchanges.exchange(f.client, false, 4001126, 3000, key));
      assertEquals(1, f.inventory.getItem((short) 1).getQuantity());
      assertEquals(13000, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void mixedCouponSaveAckLossNeverRemintsItemsOrCreditsAgain() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 4, 100);
      try (Connection c = f.connection();
          Statement s = c.createStatement()) {
        s.execute("INSERT INTO nxcode_items VALUES(10,5,4031757,1)");
      }
      when(f.chr.canHold(anyInt(), anyInt())).thenReturn(true);
      when(f.items.getSlotMax(any(), anyInt())).thenReturn((short) 200);
      f.loseCommitAcknowledgement = true;
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(10100, BridgeWallet.read(1, 1));
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='DELIVERED'"));
      assertEquals(1, f.inventory.countById(4031757));
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(1, f.inventory.countById(4031757));
      f.loseCommitAcknowledgement = false;
      f.chr.saveCharToDB(true);
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(1, f.inventory.countById(4031757));
      assertEquals(10100, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void legacySchemaMigrationPreservesRowsAndCreatesIndexesIdempotently() throws Exception {
    try (Fixture f = new Fixture();
        Connection c = f.connection();
        Statement s = c.createStatement()) {
      s.execute("DROP TABLE card_bridge_payment_attribution");
      s.execute("DROP TABLE card_bridge_payments");
      s.execute(
          "CREATE TABLE card_bridge_payments(order_id CHAR(64) PRIMARY KEY,account_id INT,cash_type"
              + " INT,amount INT,balance_after INT,created_at TIMESTAMP DEFAULT"
              + " CURRENT_TIMESTAMP)");
      s.execute(
          "INSERT INTO card_bridge_payments(order_id,account_id,cash_type,amount,balance_after)"
              + " VALUES('"
              + "7".repeat(64)
              + "',1,1,1000,9000)");
      BridgePayments.initialize(c);
      BridgePayments.initialize(c);
      assertEquals(
          1,
          f.scalar("SELECT COUNT(*) FROM card_bridge_payments WHERE payment_state='LEGACY_PAID'"));
      assertEquals(1000, BridgePayments.reserved(c, 1, 1));
    }
  }

  static List<Object> paymentRace(
      Fixture f,
      java.util.concurrent.Callable<Map<String, Object>> first,
      java.util.concurrent.Callable<Map<String, Object>> second)
      throws Exception {
    var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
    var ready = new java.util.concurrent.CountDownLatch(2);
    var start = new java.util.concurrent.CountDownLatch(1);
    try {
      List<java.util.concurrent.Future<Object>> results = new ArrayList<>();
      for (var task : List.of(first, second))
        results.add(
            pool.submit(
                () -> {
                  try (MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class)) {
                    db.when(DatabaseConnection::getConnection).thenAnswer(i -> f.connection());
                    ready.countDown();
                    start.await();
                    try {
                      return task.call();
                    } catch (BridgeHttp.Problem rejected) {
                      return rejected.code;
                    }
                  }
                }));
      assertTrue(ready.await(5, java.util.concurrent.TimeUnit.SECONDS));
      start.countDown();
      List<Object> output = new ArrayList<>();
      for (var result : results) output.add(result.get(10, java.util.concurrent.TimeUnit.SECONDS));
      return output;
    } finally {
      start.countDown();
      pool.shutdownNow();
    }
  }

  @Test
  void concurrentDebitAndCancellationProduceOneImmutableOutcome() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "8".repeat(64));
      paymentRace(
          f,
          () -> BridgePayments.admit(input, "debit"),
          () -> BridgePayments.admit(input, "cancel"));
      var request = new com.google.gson.JsonObject();
      request.addProperty("transactionId", "8".repeat(64));
      var state = BridgePayments.status(request);
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payments"));
      assertEquals(state.get("state").equals("DEBITED") ? 9000 : 10000, BridgeWallet.read(1, 1));
      assertTrue(Set.of("DEBITED", "CANCELLED").contains(state.get("state")));
    }
  }

  @Test
  void concurrentFinalizationAndRefundCannotBothCommit() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "9".repeat(64));
      BridgePayments.admit(input, "debit");
      var finalize = new com.google.gson.JsonObject();
      finalize.addProperty("transactionId", "9".repeat(64));
      finalize.addProperty("fingerprint", input.get("fingerprint").getAsString());
      finalize.addProperty("purchaseId", "original-purchase");
      var refund = new com.google.gson.JsonObject();
      refund.addProperty("transactionId", "9".repeat(64));
      refund.addProperty("fingerprint", input.get("fingerprint").getAsString());
      refund.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      refund.addProperty("debitReference", "debit:" + "9".repeat(64));
      var results =
          paymentRace(
              f,
              () -> BridgePayments.complete(finalize, false),
              () -> BridgePayments.complete(refund, true));
      assertEquals(1, results.stream().filter(x -> x instanceof Map).count());
      assertEquals(1, results.stream().filter(x -> x.equals("PAYMENT_TERMINAL_CONFLICT")).count());
      assertTrue(BridgeWallet.read(1, 1) == 9000 || BridgeWallet.read(1, 1) == 10000);
      try (Connection c = f.connection()) {
        assertEquals(0, BridgePayments.reserved(c, 1, 1));
      }
    }
  }

  @Test
  void historicalUnanchoredDebitCannotRefundAReusedAccount() throws Exception {
    try (Fixture f = new Fixture();
        Connection c = f.connection();
        Statement st = c.createStatement()) {
      String id = "0".repeat(64);
      st.execute(
          "INSERT INTO card_bridge_payments(order_id,account_id,cash_type,amount,balance_after)"
              + " VALUES('"
              + id
              + "',1,1,1000,9000)");
      st.execute("DELETE FROM accounts WHERE id=1");
      st.execute(
          "INSERT INTO accounts VALUES(1,'Replacement','password',20000,0,0,0,TIMESTAMP '1970-01-01"
              + " 00:00:00')");
      var rejected =
          assertThrows(
              BridgeHttp.Problem.class,
              () -> BridgePayments.admit(paymentInput(1, 1000, id), "adopt-legacy"));
      assertEquals("LEGACY_IDENTITY_UNPROVEN", rejected.code);
      assertEquals(20000, BridgeWallet.read(1, 1));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.debit(id, 1, 1000));
    }
  }

  @Test
  void identityTombstoneFencesEveryWebsiteAccountAuthority() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = paymentInput(1, 1000, "0".repeat(64));
      input.addProperty("accountId", 999);
      BridgePayments.admit(input, "cancel");
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute(
            "INSERT INTO accounts VALUES(999,'Reused','password',20000,0,0,0,TIMESTAMP '1970-01-01"
                + " 00:00:00')");
        st.execute(
            "INSERT INTO card_bridge_sessions VALUES('"
                + "a".repeat(64)
                + "',999,9223372036854775807)");
      }
      assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.resolve("Reused", null));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.resolve(null, 999));
      assertThrows(
          BridgeHttp.Problem.class,
          () -> BridgeSessions.login("Reused", "password", "b".repeat(64)));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.session("a".repeat(64)));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.wallet(999));
    }
  }

  @Test
  void everyBoundTransitionReplaysAfterLostCommitAcknowledgement() throws Exception {
    try (Fixture f = new Fixture()) {
      for (String action : List.of("debit", "cancel")) {
        String id = action.equals("debit") ? "ab".repeat(32) : "cd".repeat(32);
        var input = paymentInput(1, 1000, id);
        f.commitAcknowledgementsToLose = 1;
        assertThrows(SQLException.class, () -> BridgePayments.admit(input, action));
        var saved = BridgePayments.admit(input, action);
        assertEquals(action.equals("debit") ? "DEBITED" : "CANCELLED", saved.get("state"));
      }
      var input = paymentInput(1, 1000, "ef".repeat(32));
      BridgePayments.admit(input, "debit");
      var done = new com.google.gson.JsonObject();
      done.addProperty("transactionId", "ef".repeat(32));
      done.addProperty("fingerprint", input.get("fingerprint").getAsString());
      done.addProperty("purchaseId", "committed-original");
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgePayments.complete(done, false));
      assertEquals("FULFILLED", BridgePayments.complete(done, false).get("state"));
      input = paymentInput(1, 1000, "ab".repeat(32));
      var refund = new com.google.gson.JsonObject();
      refund.addProperty("transactionId", "ab".repeat(32));
      refund.addProperty("fingerprint", input.get("fingerprint").getAsString());
      refund.addProperty(
          "compensationId",
          BridgePayments.compensation(
              input.get("preparationId").getAsString(), input.get("fingerprint").getAsString()));
      refund.addProperty("debitReference", "debit:" + "ab".repeat(32));
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgePayments.complete(refund, true));
      assertEquals("REFUNDED", BridgePayments.complete(refund, true).get("state"));
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(3, f.scalar("SELECT COUNT(*) FROM card_bridge_payments"));
    }
  }

  @Test
  void couponCombinedSlotsRejectBeforeCashOrCodeConsumption() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 4, 100);
      when(f.items.getSlotMax(any(), anyInt())).thenReturn((short) 200);
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO nxcode_items VALUES(10,5,4031757,1),(10,5,4031758,1)");
      }
      for (short slot = 1; slot < 16; slot++)
        f.inventory.addItemFromDB(new Item(4000000 + slot, slot, (short) 1));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeCoupons.claim(f.chr, "LEGACYCASH"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM nxcode WHERE retriever IS NOT NULL"));
      verify(f.client, never()).enableCSActions();
    }
  }

  @Test
  void couponEquipmentQuantityIsMaterializedIndividually() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 5, 2);
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("UPDATE nxcode_items SET item=1003050");
      }
      Inventory equips = new Inventory(f.chr, InventoryType.EQUIP, (byte) 16);
      when(f.chr.getInventory(InventoryType.EQUIP)).thenReturn(equips);
      when(f.items.getEquipById(1003050)).thenAnswer(i -> new Equip(1003050, (short) 0));
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(2, equips.list().size());
      for (Item item : equips.list()) assertEquals(1, item.getQuantity());
      f.transport.verify(BridgeHttp::uncertainCommit, never());
    }
  }

  static Character replacement(Fixture f, Inventory inventory) throws Exception {
    return replacement(f, inventory, 7);
  }

  static Character replacement(Fixture f, Inventory inventory, int characterId) throws Exception {
    Character fresh = mock(Character.class);
    when(fresh.getId()).thenReturn(characterId);
    when(fresh.getAccountID()).thenReturn(1);
    when(fresh.getName()).thenReturn("Collector");
    when(fresh.getInventory(any())).thenReturn(inventory);
    when(fresh.getCashShop()).thenReturn(f.cashShop);
    when(fresh.getClient()).thenReturn(f.client);
    when(f.client.getPlayer()).thenReturn(fresh);
    doAnswer(
            i -> {
              f.saveInventory(fresh, inventory);
              return null;
            })
        .when(fresh)
        .saveCharToDB(true);
    BridgeCharacters.activate(fresh);
    return fresh;
  }

  @Test
  void failedLeafSaveAndReconnectCannotCreditUnconsumedPersistedLeaf() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeCharacters.activate(f.chr);
      f.inventory.addItemFromDB(new Item(4001126, (short) 1, (short) 1));
      f.chr.saveCharToDB(true);
      f.failSave = true;
      assertFalse(
          BridgeExchanges.exchange(
              f.client, false, 4001126, 3000, "33333333-3333-4333-8333-333333333333"));
      f.chr.saveCharToDB(true);
      assertEquals(10000, BridgeWallet.read(1, 1));
      Inventory reloaded = new Inventory(f.chr, InventoryType.ETC, (byte) 16);
      reloaded.addItemFromDB(
          new Item(
              4001126, (short) 1, (short) f.scalar("SELECT quantity FROM saved_items WHERE id=1")));
      Character fresh = replacement(f, reloaded);
      f.failSave = false;
      fresh.saveCharToDB(true);
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(1, reloaded.countById(4001126));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_awards"));
      try (Connection c = f.connection()) {
        assertThrows(SQLException.class, () -> BridgeRewards.beforeSave(c, f.chr));
      }
    }
  }

  @Test
  void failedMixedCouponSaveReconnectAndExpiryRecoverDurablePayload() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeCharacters.activate(f.chr);
      couponFixture(f, 4, 100);
      when(f.items.getSlotMax(any(), anyInt())).thenReturn((short) 200);
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO nxcode_items VALUES(10,5,4031757,1)");
      }
      f.failSave = true;
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      f.chr.saveCharToDB(true);
      assertEquals(10100, BridgeWallet.read(1, 1));
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='PENDING'"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM saved_items"));
      Inventory reloaded = new Inventory(f.chr, InventoryType.ETC, (byte) 16);
      Character fresh = replacement(f, reloaded);
      f.failSave = false;
      fresh.saveCharToDB(true);
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='PENDING'"));
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("UPDATE nxcode SET expiration=0");
        BridgeCoupons.clean(c, 1);
        assertEquals(1, f.scalar("SELECT COUNT(*) FROM nxcode"));
      }
      BridgeCoupons.recover(f.client);
      assertEquals(1, reloaded.countById(4031757));
      assertEquals(10100, BridgeWallet.read(1, 1));
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='DELIVERED'"));
      try (Connection c = f.connection()) {
        BridgeCoupons.clean(c, 1);
      }
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM nxcode"));
    }
  }

  @Test
  void ordinaryNativeBuyCommitsInventoryWithDebitForEverySource() throws Exception {
    for (int type : List.of(1, 2, 4))
      try (Fixture f = new Fixture()) {
        try (Connection c = f.connection();
            Statement st = c.createStatement()) {
          st.execute("UPDATE accounts SET maplePoint=10000,nxPrepaid=10000");
        }
        assertTrue(BridgeNativePurchases.buy(f.client, type, nativeOffer(1003050), false));
        assertEquals(9000, BridgeWallet.read(1, type));
        assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
        assertEquals(
            1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases WHERE kind='item'"));
        BridgeWallet.debit("ac".repeat(32), 1, 500, 1);
        f.chr.saveCharToDB(true);
        assertEquals(type == 1 ? 8500 : 9500, BridgeWallet.read(1, 1));
      }
  }

  @Test
  void ordinaryNativeSaveFailureRollsBackInventoryAndEveryCashSource() throws Exception {
    for (int type : List.of(1, 2, 4))
      try (Fixture f = new Fixture()) {
        try (Connection c = f.connection();
            Statement st = c.createStatement()) {
          st.execute("UPDATE accounts SET maplePoint=10000,nxPrepaid=10000");
        }
        doThrow(new SQLException("Injected cash inventory failure")).when(f.cashShop).save(any());
        assertFalse(BridgeNativePurchases.buy(f.client, type, nativeOffer(1003050), false));
        assertEquals(10000, BridgeWallet.read(1, type));
        assertTrue(f.cashInventory.isEmpty());
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
      }
  }

  @Test
  void packageNativeSaveAndLostAckPreserveAllContentsAndOneCharge() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashShop.CashItemFactory> factory =
            mockStatic(CashShop.CashItemFactory.class)) {
      var pack = nativeOffer(9100000);
      var first = nativeOffer(1003050);
      var second = nativeOffer(1003051);
      factory
          .when(() -> CashShop.CashItemFactory.getPackageSerials(9100000))
          .thenReturn(List.of(100, 101));
      factory.when(() -> CashShop.CashItemFactory.getItem(100)).thenReturn(first);
      factory.when(() -> CashShop.CashItemFactory.getItem(101)).thenReturn(second);
      f.loseNativeCommitAcknowledgement = true;
      assertTrue(BridgeNativePurchases.buy(f.client, 1, pack, true));
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases WHERE kind='package'"));
      f.loseNativeCommitAcknowledgement = false;
      f.chr.saveCharToDB(true);
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
    }
  }

  @Test
  void packageSaveFailureRollsBackEveryCashSourceAndEveryItem() throws Exception {
    for (int type : List.of(1, 2, 4))
      try (Fixture f = new Fixture();
          MockedStatic<CashShop.CashItemFactory> factory =
              mockStatic(CashShop.CashItemFactory.class)) {
        try (Connection c = f.connection();
            Statement st = c.createStatement()) {
          st.execute("UPDATE accounts SET maplePoint=10000,nxPrepaid=10000");
        }
        var pack = nativeOffer(9100000);
        var first = nativeOffer(1003050);
        var second = nativeOffer(1003051);
        factory
            .when(() -> CashShop.CashItemFactory.getPackageSerials(9100000))
            .thenReturn(List.of(100, 101));
        factory.when(() -> CashShop.CashItemFactory.getItem(100)).thenReturn(first);
        factory.when(() -> CashShop.CashItemFactory.getItem(101)).thenReturn(second);
        doThrow(new SQLException("Injected package inventory failure"))
            .when(f.cashShop)
            .save(any());
        assertFalse(BridgeNativePurchases.buy(f.client, type, pack, true));
        assertEquals(10000, BridgeWallet.read(1, type));
        assertTrue(f.cashInventory.isEmpty());
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      }
  }

  @Test
  void ordinaryNotificationFailureCannotUndoPaidPersistedInventory() throws Exception {
    try (Fixture f = new Fixture()) {
      doThrow(new IllegalStateException("Injected packet loss")).when(f.client).sendPacket(any());
      assertThrows(
          IllegalStateException.class,
          () -> BridgeNativePurchases.buy(f.client, 1, nativeOffer(1003050), false));
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  static void slotFixture(Fixture f) throws Exception {
    try (Connection c = f.connection();
        Statement st = c.createStatement()) {
      st.execute("ALTER TABLE accounts ADD characterslots INT DEFAULT 3");
      st.execute("UPDATE accounts SET nxCredit=10000,maplePoint=10000,nxPrepaid=10000");
      st.execute(
          "CREATE TABLE characters(id INT PRIMARY KEY,accountid INT,equipslots INT,useslots"
              + " INT,setupslots INT,etcslots INT)");
      st.execute("INSERT INTO characters VALUES(7,1,16,16,16,16),(8,2,16,16,16,16)");
      st.execute("CREATE TABLE storages(accountid INT,world INT,slots INT)");
      st.execute("INSERT INTO storages VALUES(1,0,16)");
    }
    server.Storage storage = mock(server.Storage.class);
    when(storage.getSlots()).thenReturn((byte) 16);
    when(f.chr.getStorage()).thenReturn(storage);
    when(f.client.getCharacterSlots()).thenReturn((short) 3);
  }

  @Test
  void inventoryStorageAndCharacterSlotsPersistAtomicallyForEverySource() throws Exception {
    for (int type : List.of(1, 2, 4))
      for (int category : List.of(1, 2, 3))
        try (Fixture f = new Fixture()) {
          slotFixture(f);
          var offer = category == 3 ? nativeOffer(5430000) : null;
          assertTrue(
              BridgeNativePurchases.slots(
                  f.client, type, offer, category, category == 1 ? 4 : 0, category == 3 ? 1 : 4));
          assertEquals(category == 3 ? 9000 : 6000, BridgeWallet.read(1, type));
          assertEquals(
              category == 3 ? 4 : 20,
              f.scalar(
                  category == 1
                      ? "SELECT etcslots FROM characters WHERE id=7"
                      : category == 2
                          ? "SELECT slots FROM storages WHERE accountid=1"
                          : "SELECT characterslots FROM accounts WHERE id=1"));
          if (category == 1) assertEquals(20, f.inventory.getSlotLimit());
          if (category == 2) verify(f.chr.getStorage()).bridgeSetSlots(20);
          if (category == 3) verify(f.client).setCharacterSlots((byte) 4);
          assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
        }
  }

  @Test
  void slotRefusalOrFailedCommitPreservesBalanceAndOriginalMemoryLimits() throws Exception {
    for (int category : List.of(1, 2, 3))
      try (Fixture f = new Fixture()) {
        slotFixture(f);
        f.failNativeCommitBefore = true;
        assertFalse(
            BridgeNativePurchases.slots(
                f.client,
                1,
                category == 3 ? nativeOffer(5430000) : null,
                category,
                category == 1 ? 4 : 0,
                category == 3 ? 1 : 4));
        assertEquals(10000, BridgeWallet.read(1, 1));
        assertEquals(
            category == 3 ? 3 : 16,
            f.scalar(
                category == 1
                    ? "SELECT etcslots FROM characters WHERE id=7"
                    : category == 2
                        ? "SELECT slots FROM storages WHERE accountid=1"
                        : "SELECT characterslots FROM accounts WHERE id=1"));
        assertEquals(16, f.inventory.getSlotLimit());
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
        f.failNativeCommitBefore = false;
        assertFalse(
            BridgeNativePurchases.slots(
                f.client,
                1,
                nativeOffer(1003050),
                category,
                category == 1 ? 4 : 0,
                category == 3 ? 1 : 8));
        assertEquals(10000, BridgeWallet.read(1, 1));
      }
  }

  @Test
  void slotLostAcknowledgementRefreshesMemoryOnlyAfterDurableReceipt() throws Exception {
    try (Fixture f = new Fixture()) {
      slotFixture(f);
      f.loseNativeCommitAcknowledgement = true;
      assertTrue(BridgeNativePurchases.slots(f.client, 2, null, 1, 4, 4));
      assertEquals(6000, BridgeWallet.read(1, 2));
      assertEquals(20, f.inventory.getSlotLimit());
      assertEquals(20, f.scalar("SELECT etcslots FROM characters WHERE id=7"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void giftingPersistsEntitlementWithChargeAndRejectsInvalidRecipientWithoutDebit()
      throws Exception {
    try (Fixture f = new Fixture()) {
      slotFixture(f);
      var offer = nativeOffer(1003050);
      assertFalse(BridgeNativePurchases.gift(f.client, offer, 999, "A gift"));
      assertEquals(10000, BridgeWallet.read(1, 4));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM gifts"));
      f.loseNativeCommitAcknowledgement = true;
      assertTrue(BridgeNativePurchases.gift(f.client, offer, 8, "A gift"));
      assertEquals(9000, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM gifts WHERE `to`=8"));
      assertEquals(
          1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases WHERE kind='gift'"));
      assertTrue(f.cashInventory.isEmpty());
    }
  }

  @Test
  void receivingGiftRollbackPreservesEntitlementAndLostAckDeliversExactlyOnce() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashShop.CashItemFactory> factory =
            mockStatic(CashShop.CashItemFactory.class)) {
      var offer = nativeOffer(1003050);
      factory.when(() -> CashShop.CashItemFactory.getItem(50100000)).thenReturn(offer);
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO gifts VALUES(DEFAULT,7,'Sender','A gift',50100000,-1)");
      }
      f.failNativeCommitBefore = true;
      assertTrue(BridgeNativePurchases.receiveGifts(f.client).isEmpty());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertTrue(f.cashInventory.isEmpty());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      f.failNativeCommitBefore = false;
      f.loseNativeCommitAcknowledgement = true;
      assertEquals(1, BridgeNativePurchases.receiveGifts(f.client).size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      assertEquals(
          1,
          f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases WHERE kind='gift_delivery'"));
      assertTrue(BridgeNativePurchases.receiveGifts(f.client).isEmpty());
      assertEquals(1, f.cashInventory.size());
    }
  }

  @Test
  void disabledMtsPacketCannotReachAnyStockMutationFromAnOpenedCashShop() throws Exception {
    try (Fixture f = new Fixture()) {
      when(f.cashShop.isOpened()).thenReturn(true);
      InPacket request = mock(InPacket.class);
      when(request.available()).thenReturn(10);
      when(request.readByte()).thenReturn((byte) 2);
      boolean original = config.YamlConfig.config.server.USE_MTS;
      config.YamlConfig.config.server.USE_MTS = false;
      try {
        new net.server.channel.handlers.MTSHandler().handlePacket(request, f.client);
        verify(request, never()).readByte();
        verify(f.client).enableCSActions();
      } finally {
        config.YamlConfig.config.server.USE_MTS = original;
      }
    }
  }

  @Test
  void lostWriterLeaseRejectsOrdinarySaveAndRawCashChangesWithoutPendingAwards() throws Exception {
    try (Fixture f = new Fixture()) {
      f.transport
          .when(BridgeHttp::requireLease)
          .thenThrow(new SQLException("Injected lost writer lease"));
      try (Connection con = f.connection()) {
        assertThrows(SQLException.class, () -> BridgeRewards.beforeSave(con, f.chr));
      }
      assertThrows(IllegalStateException.class, () -> BridgeWallet.change(1, 1, -1));
      assertThrows(IllegalStateException.class, () -> BridgeWallet.change(1, 1, 1));
      assertThrows(SQLException.class, () -> BridgeCharacters.activate(f.chr));
      assertEquals(10000, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void duplicateRechargeableRowsRejectWithoutClaimOrGameRecovery() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 4, 100);
      when(f.items.getSlotMax(any(), anyInt())).thenReturn((short) 1000);
      Inventory use = new Inventory(f.chr, InventoryType.USE, (byte) 16);
      when(f.chr.getInventory(InventoryType.USE)).thenReturn(use);
      for (short slot = 1; slot < 16; slot++)
        use.addItemFromDB(new Item(2000000 + slot, slot, (short) 1));
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO nxcode_items VALUES(10,5,2070000,1),(10,5,2070000,1)");
      }
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(0, use.countById(2070000));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM nxcode WHERE retriever IS NOT NULL"));
      f.transport.verify(BridgeHttp::uncertainCommit, never());
    }
  }

  @Test
  void duplicateCashRowsCountEveryGeneratedObjectBeforeClaim() throws Exception {
    try (Fixture f = new Fixture()) {
      couponFixture(f, 4, 100);
      when(f.items.isCash(5220000)).thenReturn(true);
      for (int i = 0; i < 99; i++) f.cashInventory.add(new Item(5000000 + i, (short) 0, (short) 1));
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO nxcode_items VALUES(10,5,5220000,1),(10,5,5220000,1)");
      }
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      assertEquals(99, f.cashInventory.size());
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_coupon_claims"));
      f.transport.verify(BridgeHttp::uncertainCommit, never());
    }
  }

  @Test
  void switchingCharactersRecoversMixedCouponAndFencesOldAccountInventorySaves() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeCharacters.activate(f.chr);
      couponFixture(f, 4, 100);
      when(f.items.getSlotMax(any(), anyInt())).thenReturn((short) 200);
      try (Connection c = f.connection();
          Statement st = c.createStatement()) {
        st.execute("INSERT INTO nxcode_items VALUES(10,5,4031757,1)");
      }
      f.failSave = true;
      BridgeCoupons.handle(f.client, "LEGACYCASH");
      f.chr.saveCharToDB(true);
      Inventory otherInventory = new Inventory(f.chr, InventoryType.ETC, (byte) 16);
      Character other = replacement(f, otherInventory, 8);
      f.failSave = false;
      other.saveCharToDB(true);
      BridgeCoupons.recover(f.client);
      assertEquals(1, otherInventory.countById(4031757));
      assertEquals(10100, BridgeWallet.read(1, 1));
      assertEquals(
          1,
          f.scalar(
              "SELECT COUNT(*) FROM card_bridge_coupon_claims WHERE state='DELIVERED' AND"
                  + " claim_character=8"));
      assertFalse(BridgeCharacters.current(f.chr));
      try (Connection c = f.connection()) {
        assertThrows(SQLException.class, () -> BridgeRewards.beforeSave(c, f.chr));
      }
    }
  }

  @Test
  void switchingCharactersReconcilesUncommittedCodeBeforeFreshDelivery() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeCharacters.activate(f.chr);
      f.register();
      f.failSave = true;
      BridgeRewards.handle(f.client, CODE);
      f.chr.saveCharToDB(true);
      assertEquals("PENDING", BridgeRewards.status(ID).get("status"));
      Inventory otherInventory = new Inventory(f.chr, InventoryType.ETC, (byte) 16);
      Character other = replacement(f, otherInventory, 8);
      assertEquals("READY", BridgeRewards.status(ID).get("status"));
      f.failSave = false;
      BridgeRewards.handle(f.client, CODE);
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertEquals(1, otherInventory.countById(4031757));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
      assertFalse(BridgeCharacters.current(f.chr));
    }
  }

  static com.google.gson.JsonObject legacyAttribution(Fixture f, String id) throws Exception {
    try (Connection con = f.connection();
        Statement st = con.createStatement()) {
      st.execute(
          "ALTER TABLE accounts ADD createdat TIMESTAMP(0) NOT NULL DEFAULT CURRENT_TIMESTAMP");
      st.execute("UPDATE accounts SET createdat=TIMESTAMP '2005-01-01 00:00:00'");
      st.execute(
          "ALTER TABLE card_bridge_payments ALTER COLUMN created_at TIMESTAMP(0) NOT NULL DEFAULT"
              + " CURRENT_TIMESTAMP");
      st.execute(
          "INSERT INTO"
              + " card_bridge_payments(order_id,account_id,cash_type,amount,balance_after,created_at)"
              + " VALUES('"
              + id
              + "',1,1,1000,9000,TIMESTAMP '2026-01-01 00:00:00')");
    }
    var account = new com.google.gson.JsonObject();
    account.addProperty("accountId", 1);
    account.addProperty("createdAt", "2005-01-01T00:00:00.000Z");
    var payment = new com.google.gson.JsonObject();
    payment.addProperty("transactionId", id);
    payment.addProperty("accountId", 1);
    payment.addProperty("cashType", 1);
    payment.addProperty("amount", 1000);
    payment.addProperty("balanceAfter", 9000);
    payment.addProperty("createdAt", "2026-01-01T00:00:00.000Z");
    var evidence = new com.google.gson.JsonObject();
    evidence.addProperty("version", 1);
    evidence.addProperty("kind", "managed-cosmic-backup/1");
    evidence.addProperty("schema", "cosmic-createdat-timestamp/1");
    evidence.addProperty("backupDigest", "a".repeat(64));
    evidence.addProperty("databaseDigest", "b".repeat(64));
    evidence.add("account", account);
    evidence.add("payment", payment);
    var input = new com.google.gson.JsonObject();
    input.addProperty("transactionId", id);
    input.addProperty("accountId", 1);
    input.addProperty("cashType", 1);
    input.addProperty("amount", 1000);
    input.addProperty("decisionKey", "legacy-decision-1");
    input.addProperty("operatorId", "operator-1");
    input.addProperty("reason", "Verified original consistent managed snapshot");
    input.add("evidence", evidence);
    return input;
  }

  @Test
  void attributedLegacySnapshotAnchorsIdentityAcrossRenameWithoutFinancialMutation()
      throws Exception {
    try (Fixture f = new Fixture()) {
      String id = "de".repeat(32);
      var input = legacyAttribution(f, id);
      try (Connection con = f.connection();
          Statement st = con.createStatement()) {
        st.execute("UPDATE accounts SET name='Renamed'");
      }
      BridgeSessions.resolve("Renamed", null);
      String retainedIncarnation;
      try (Connection con = f.connection();
          Statement st = con.createStatement();
          ResultSet identity =
              st.executeQuery(
                  "SELECT incarnation FROM card_bridge_account_identity WHERE account_id=1")) {
        identity.next();
        retainedIncarnation = identity.getString(1);
      }
      var result = BridgePayments.attributeLegacy(input);
      assertEquals(retainedIncarnation, result.get("accountIncarnation"));
      assertEquals("ATTRIBUTED", result.get("state"));
      assertNotNull(result.get("accountIncarnation"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
      assertEquals(
          1,
          f.scalar(
              "SELECT COUNT(*) FROM card_bridge_payments WHERE payment_state='LEGACY_PAID' AND"
                  + " intent IS NULL"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_codes"));
      assertEquals(result, BridgePayments.attributeLegacy(input));
      var bound = BridgePayments.admit(paymentInput(1, 1000, id), "adopt-legacy");
      assertEquals(result.get("accountIncarnation"), bound.get("accountIncarnation"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(result, BridgePayments.attributeLegacy(input));
    }
  }

  @Test
  void attributedLegacyDecisionReplaysLostCommitAckAndRejectsAnyEvidenceChange() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = legacyAttribution(f, "ca".repeat(32));
      f.commitAcknowledgementsToLose = 1;
      assertThrows(SQLException.class, () -> BridgePayments.attributeLegacy(input));
      var result = BridgePayments.attributeLegacy(input);
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      for (String field : List.of("reason", "operatorId", "decisionKey")) {
        var changed = input.deepCopy();
        changed.addProperty(field, "changed-value");
        assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.attributeLegacy(changed));
      }
      var changed = input.deepCopy();
      changed.getAsJsonObject("evidence").addProperty("databaseDigest", "c".repeat(64));
      assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.attributeLegacy(changed));
      assertEquals(result, BridgePayments.attributeLegacy(input));
    }
  }

  @Test
  void legacyAttributionRefusesReplacementEqualMissingAndUnsupportedEvidence() throws Exception {
    for (String scenario :
        List.of("replacement", "equal", "missing", "precision", "terms", "snapshot", "evidence"))
      try (Fixture f = new Fixture()) {
        var input = legacyAttribution(f, "bc".repeat(32));
        try (Connection con = f.connection();
            Statement st = con.createStatement()) {
          if (scenario.equals("replacement"))
            st.execute("UPDATE accounts SET createdat=TIMESTAMP '2026-02-01 00:00:00'");
          if (scenario.equals("equal")) {
            st.execute("UPDATE accounts SET createdat=TIMESTAMP '2026-01-01 00:00:00'");
            input
                .getAsJsonObject("evidence")
                .getAsJsonObject("account")
                .addProperty("createdAt", "2026-01-01T00:00:00.000Z");
          }
          if (scenario.equals("missing")) st.execute("ALTER TABLE accounts DROP COLUMN createdat");
          if (scenario.equals("precision"))
            st.execute(
                "ALTER TABLE accounts ALTER COLUMN createdat TIMESTAMP(6) NOT NULL DEFAULT"
                    + " CURRENT_TIMESTAMP");
        }
        if (scenario.equals("terms"))
          input
              .getAsJsonObject("evidence")
              .getAsJsonObject("payment")
              .addProperty("balanceAfter", 8000);
        if (scenario.equals("snapshot"))
          input
              .getAsJsonObject("evidence")
              .getAsJsonObject("payment")
              .addProperty("createdAt", "2026-01-02T00:00:00.000Z");
        if (scenario.equals("evidence"))
          input.getAsJsonObject("evidence").addProperty("kind", "unverified-json/1");
        assertThrows(RuntimeException.class, () -> BridgePayments.attributeLegacy(input));
        assertEquals(10000, BridgeWallet.read(1, 1));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_account_identity"));
        assertEquals(
            1,
            f.scalar(
                "SELECT COUNT(*) FROM card_bridge_payments WHERE account_incarnation IS NULL AND"
                    + " payment_state='LEGACY_PAID'"));
      }
  }

  @Test
  void legacyAttributionRejectsDatetimeEvenWhenJdbcClassifiesItAsTimestamp() throws Exception {
    rejectedTimestampMetadata("datetime");
  }

  @Test
  void legacyAttributionRejectsUnknownTimestampPrecision() throws Exception {
    rejectedTimestampMetadata("unknownPrecision");
  }

  @Test
  void mysqlUnknownJdbcPrecisionRequiresAuthoritativeZeroAndReplaysWithoutFinancialMutation()
      throws Exception {
    for (String table : List.of("accounts", "card_bridge_payments"))
      try (Fixture f = new Fixture()) {
        var input = legacyAttribution(f, "fa".repeat(32));
        String column = table.equals("accounts") ? "createdat" : "created_at";
        f.db
            .when(DatabaseConnection::getConnection)
            .thenAnswer(i -> mysqlTimestampMetadata(f.connection(), table, column, "zero"));
        var result = BridgePayments.attributeLegacy(input);
        assertEquals("ATTRIBUTED", result.get("state"));
        assertEquals(result, BridgePayments.attributeLegacy(input));
        assertEquals(10000, BridgeWallet.read(1, 1));
        assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_codes"));
        assertEquals(
            1,
            f.scalar(
                "SELECT COUNT(*) FROM card_bridge_payments WHERE payment_state='LEGACY_PAID' AND"
                    + " intent IS NULL"));
      }
  }

  @Test
  void mysqlPrecisionFallbackRejectsUnknownMissingDuplicateOrIncompatibleSchema() throws Exception {
    for (String table : List.of("accounts", "card_bridge_payments"))
      for (String scenario :
          List.of(
              "unknown",
              "missing",
              "duplicate",
              "type",
              "physicalType",
              "nullable",
              "unknownNullable",
              "missingDefault",
              "default",
              "fractional",
              "unknownType"))
        try (Fixture f = new Fixture()) {
          var input = legacyAttribution(f, "fb".repeat(32));
          String column = table.equals("accounts") ? "createdat" : "created_at";
          f.db
              .when(DatabaseConnection::getConnection)
              .thenAnswer(i -> mysqlTimestampMetadata(f.connection(), table, column, scenario));
          var refused =
              assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.attributeLegacy(input));
          assertEquals("LEGACY_IDENTITY_UNPROVEN", refused.code);
          assertEquals(10000, BridgeWallet.read(1, 1));
          assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
          assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_account_identity"));
          assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_codes"));
          assertEquals(
              1,
              f.scalar(
                  "SELECT COUNT(*) FROM card_bridge_payments WHERE account_incarnation IS NULL AND"
                      + " payment_state='LEGACY_PAID' AND intent IS NULL"));
        }
  }

  private static Connection mysqlTimestampMetadata(
      Connection con, String table, String column, String scenario) throws SQLException {
    Connection jdbc = timestampMetadata(con, table, column, "unknownPrecision");
    DatabaseMetaData metadata = jdbc.getMetaData();
    DatabaseMetaData mysql =
        (DatabaseMetaData)
            jdbcMetadataProxy(
                DatabaseMetaData.class,
                (proxy, method, args) ->
                    method.getName().equals("getDatabaseProductName")
                        ? "MySQL"
                        : invokeJdbc(metadata, method, args));
    String type = scenario.equals("type") ? "'datetime'" : "'timestamp'";
    if (scenario.equals("unknownType")) type = "CAST(NULL AS VARCHAR)";
    String physical = scenario.equals("physicalType") ? "'datetime'" : "'timestamp'";
    String precision =
        scenario.equals("unknown")
            ? "CAST(NULL AS INTEGER)"
            : scenario.equals("fractional") ? "6" : "0";
    String nullable = scenario.equals("nullable") ? "'YES'" : "'NO'";
    if (scenario.equals("unknownNullable")) nullable = "CAST(NULL AS VARCHAR)";
    String definition =
        scenario.equals("missingDefault")
            ? "CAST(NULL AS VARCHAR)"
            : scenario.equals("default") ? "'CURRENT_TIMESTAMP(6)'" : "'CURRENT_TIMESTAMP'";
    String projection =
        "SELECT " + String.join(",", type, physical, precision, nullable, definition);
    if (scenario.equals("missing")) projection += " WHERE FALSE";
    if (scenario.equals("duplicate")) projection += " UNION ALL " + projection;
    String schemaProjection = projection;
    return (Connection)
        jdbcMetadataProxy(
            Connection.class,
            (proxy, method, args) -> {
              if (method.getName().equals("getMetaData")) return mysql;
              if (method.getName().equals("createStatement")) {
                Statement statement = (Statement) invokeJdbc(jdbc, method, args);
                return jdbcMetadataProxy(
                    Statement.class,
                    (ignored, operation, parameters) ->
                        operation.getName().equals("executeQuery")
                                && parameters[0].equals(
                                    "SELECT @@session.time_zone,@@system_time_zone")
                            ? statement.executeQuery("SELECT '+00:00','UTC'")
                            : invokeJdbc(statement, operation, parameters));
              }
              if (method.getName().equals("prepareStatement")
                  && args[0] instanceof String sql
                  && sql.startsWith("SELECT DATA_TYPE,COLUMN_TYPE,DATETIME_PRECISION")) {
                assertTrue(sql.endsWith("WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND COLUMN_NAME=?"));
                PreparedStatement statement = con.prepareStatement(schemaProjection);
                Map<Integer, String> parameters = new HashMap<>();
                return jdbcMetadataProxy(
                    PreparedStatement.class,
                    (ignored, operation, values) -> {
                      if (operation.getName().equals("setString")) {
                        parameters.put((Integer) values[0], (String) values[1]);
                        return null;
                      }
                      if (operation.getName().equals("executeQuery")) {
                        assertEquals(Map.of(1, con.getCatalog(), 2, table, 3, column), parameters);
                      }
                      return invokeJdbc(statement, operation, values);
                    });
              }
              return invokeJdbc(jdbc, method, args);
            });
  }

  private static void rejectedTimestampMetadata(String scenario) throws Exception {
    for (String table : List.of("accounts", "card_bridge_payments"))
      try (Fixture f = new Fixture()) {
        var input = legacyAttribution(f, "da".repeat(32));
        String column = table.equals("accounts") ? "createdat" : "created_at";
        f.db
            .when(DatabaseConnection::getConnection)
            .thenAnswer(i -> timestampMetadata(f.connection(), table, column, scenario));
        var refused =
            assertThrows(BridgeHttp.Problem.class, () -> BridgePayments.attributeLegacy(input));
        assertEquals("LEGACY_IDENTITY_UNPROVEN", refused.code);
        assertEquals(10000, BridgeWallet.read(1, 1));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_account_identity"));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_codes"));
        assertEquals(
            1,
            f.scalar(
                "SELECT COUNT(*) FROM card_bridge_payments WHERE account_incarnation IS NULL AND"
                    + " payment_state='LEGACY_PAID' AND intent IS NULL"));
      }
  }

  private static Object jdbcMetadataProxy(
      Class<?> type, java.lang.reflect.InvocationHandler handler) {
    return java.lang.reflect.Proxy.newProxyInstance(
        type.getClassLoader(), new Class<?>[] {type}, handler);
  }

  private static Object invokeJdbc(Object target, java.lang.reflect.Method method, Object[] args)
      throws Throwable {
    try {
      return method.invoke(target, args);
    } catch (java.lang.reflect.InvocationTargetException failure) {
      throw failure.getCause();
    }
  }

  private static Connection timestampMetadata(
      Connection con, String table, String column, String scenario) throws SQLException {
    DatabaseMetaData metadata = con.getMetaData();
    DatabaseMetaData altered =
        (DatabaseMetaData)
            jdbcMetadataProxy(
                DatabaseMetaData.class,
                (proxy, method, args) -> {
                  Object result = invokeJdbc(metadata, method, args);
                  if (!method.getName().equals("getColumns")
                      || !table.equalsIgnoreCase((String) args[2])) return result;
                  ResultSet fields = (ResultSet) result;
                  boolean[] unknown = {false};
                  return jdbcMetadataProxy(
                      ResultSet.class,
                      (ignored, getter, parameters) -> {
                        if (getter.getName().equals("wasNull") && unknown[0]) return true;
                        unknown[0] = false;
                        if (parameters != null
                            && parameters.length == 1
                            && parameters[0] instanceof String label
                            && column.equalsIgnoreCase(fields.getString("COLUMN_NAME"))) {
                          if (getter.getName().equals("getString")
                              && label.equals("TYPE_NAME")
                              && scenario.equals("datetime")) return "DATETIME";
                          if (getter.getName().equals("getInt") && label.equals("DATA_TYPE"))
                            return Types.TIMESTAMP;
                          if (getter.getName().equals("getInt")
                              && label.equals("DECIMAL_DIGITS")
                              && scenario.equals("unknownPrecision")) {
                            unknown[0] = true;
                            return 0;
                          }
                        }
                        return invokeJdbc(fields, getter, parameters);
                      });
                });
    return (Connection)
        jdbcMetadataProxy(
            Connection.class,
            (proxy, method, args) ->
                method.getName().equals("getMetaData") ? altered : invokeJdbc(con, method, args));
  }

  @Test
  void concurrentExactLegacyAttributionCommitsOneAuditedDecision() throws Exception {
    try (Fixture f = new Fixture()) {
      var input = legacyAttribution(f, "ed".repeat(32));
      var results =
          paymentRace(
              f,
              () -> BridgePayments.attributeLegacy(input),
              () -> BridgePayments.attributeLegacy(input));
      assertEquals(results.get(0), results.get(1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payment_attribution"));
      assertEquals(10000, BridgeWallet.read(1, 1));
    }
  }

  public static Timestamp utcNow() {
    return Timestamp.from(java.time.Instant.now());
  }

  static final String CODE = "BBBBBBBBBBBBBBB", ID = "11111111-2222-4333-8444-555555555555";

  static class Fixture implements AutoCloseable {
    final String url = "jdbc:h2:mem:" + UUID.randomUUID() + ";MODE=MySQL;DB_CLOSE_DELAY=-1";
    final MockedStatic<DatabaseConnection> db = mockStatic(DatabaseConnection.class);
    final MockedStatic<BridgeHttp> transport = mockStatic(BridgeHttp.class);
    MockedStatic<ItemInformationProvider> information;
    final MockedStatic<PacketCreator> packets = mockStatic(PacketCreator.class);
    ItemInformationProvider items;
    final Character chr = mock(Character.class);
    final Client client = mock(Client.class);
    final Inventory inventory = new Inventory(chr, InventoryType.ETC, (byte) 16);
    final CashShop cashShop = mock(CashShop.class);
    final List<Item> cashInventory = new ArrayList<>();
    boolean failSave;
    boolean loseCommitAcknowledgement;
    boolean loseNativeCommitAcknowledgement;
    boolean failNativeCommitBefore;
    int commitAcknowledgementsToLose;

    Fixture() throws Exception {
      db.when(DatabaseConnection::getConnection).thenAnswer(i -> connection());
      transport.when(BridgeHttp::enabled).thenReturn(true);
      transport.when(BridgeHttp::codeKey).thenReturn("test-code-index-key-".repeat(3));

      when(client.getPlayer()).thenReturn(chr);
      when(client.getAccID()).thenReturn(1);
      when(chr.getCashShop()).thenReturn(cashShop);
      when(cashShop.getItemsSize()).thenAnswer(i -> cashInventory.size());
      doAnswer(
              i -> {
                cashInventory.add(i.getArgument(0));
                return null;
              })
          .when(cashShop)
          .addToInventory(any());
      when(chr.getId()).thenReturn(7);
      when(chr.getAccountID()).thenReturn(1);
      when(chr.getName()).thenReturn("Collector");
      when(cashShop.getCash(anyInt())).thenAnswer(i -> BridgeWallet.read(1, i.getArgument(0)));
      doAnswer(
              i -> {
                cashInventory.remove(i.getArgument(0));
                return null;
              })
          .when(cashShop)
          .removeFromInventory(any());
      doAnswer(
              i -> {
                Connection con = i.getArgument(0);
                try (Statement st = con.createStatement()) {
                  st.execute("DELETE FROM saved_cash_items");
                }
                for (Item item : cashInventory)
                  try (PreparedStatement ps =
                      con.prepareStatement("INSERT INTO saved_cash_items VALUES(?,?)")) {
                    ps.setInt(1, item.getItemId());
                    ps.setLong(2, item.getExpiration());
                    ps.executeUpdate();
                  }
                return null;
              })
          .when(cashShop)
          .save(any());
      when(chr.getClient()).thenReturn(client);
      when(chr.getInventory(any())).thenReturn(inventory);
      try (Connection con = connection();
          Statement st = con.createStatement()) {
        st.execute("CREATE ALIAS UTC_TIMESTAMP FOR 'server.cardbridge.BridgeContractTest.utcNow'");
        st.execute(
            "CREATE TABLE accounts(id INT PRIMARY KEY,name VARCHAR(13),password"
                + " VARCHAR(128),nxCredit INT,maplePoint INT,nxPrepaid INT,banned INT,tempban"
                + " TIMESTAMP)");
        st.execute(
            "INSERT INTO accounts VALUES(1,'Collector','password',10000,0,0,0,TIMESTAMP '1970-01-01"
                + " 00:00:00')");
        st.execute("CREATE TABLE saved_items(id INT PRIMARY KEY,quantity INT)");
        st.execute("CREATE TABLE saved_cash_items(item_id INT PRIMARY KEY,expiration BIGINT)");
        st.execute("CREATE TABLE monstercarddata(id INT,cardid INT,mobid INT)");
        st.execute(
            "CREATE TABLE rings(id INT PRIMARY KEY,itemid INT,partnerRingId INT,partnerChrId"
                + " INT,partnername VARCHAR(13))");
        st.execute(
            "CREATE TABLE gifts(id INT AUTO_INCREMENT PRIMARY KEY,`to` INT,`from`"
                + " VARCHAR(13),message VARCHAR(255),sn INT,ringid INT)");
        st.execute(
            "CREATE TABLE namechanges(characterid INT,old VARCHAR(13),new"
                + " VARCHAR(13),completionTime TIMESTAMP)");
        st.execute(
            "CREATE TABLE worldtransfers(characterid INT,`from` INT,`to` INT,completionTime"
                + " TIMESTAMP)");
        String schema =
            new String(
                BridgeContractTest.class
                    .getResourceAsStream("/card-bridge/schema.sql")
                    .readAllBytes(),
                StandardCharsets.UTF_8);
        for (String sql : schema.split(";")) if (!sql.isBlank()) st.execute(sql);
      }
      information = mockStatic(ItemInformationProvider.class);
      items = mock(ItemInformationProvider.class);
      information.when(ItemInformationProvider::getInstance).thenReturn(items);
      when(items.getName(anyInt())).thenReturn("Series One reward");
      doAnswer(
              i -> {
                try (Connection con = connection()) {
                  con.setAutoCommit(false);
                  try {
                    try (Statement st = con.createStatement()) {
                      st.execute("DELETE FROM saved_items");
                    }
                    for (Item item : inventory.list())
                      try (PreparedStatement ps =
                          con.prepareStatement("INSERT INTO saved_items VALUES(?,?)")) {
                        ps.setInt(1, item.getPosition());
                        ps.setInt(2, item.getQuantity());
                        ps.executeUpdate();
                      }
                    try (Statement st = con.createStatement()) {
                      st.execute("DELETE FROM saved_cash_items");
                    }
                    for (Item item : cashInventory)
                      try (PreparedStatement ps =
                          con.prepareStatement("INSERT INTO saved_cash_items VALUES(?,?)")) {
                        ps.setInt(1, item.getItemId());
                        ps.setLong(2, item.getExpiration());
                        ps.executeUpdate();
                      }
                    BridgeRewards.beforeSave(con, chr);
                    if (failSave) throw new SQLException("Injected inventory commit failure");
                    con.commit();
                    if (loseCommitAcknowledgement)
                      throw new SQLException("Injected lost commit acknowledgement");
                    BridgeRewards.afterSave(chr);
                  } catch (Exception e) {
                    con.rollback();
                  }
                }
                return null;
              })
          .when(chr)
          .saveCharToDB(true);
    }

    void saveInventory(Character target, Inventory actualInventory) throws Exception {
      try (BridgeCharacters.Guard guard = BridgeCharacters.save(target)) {
        if (!BridgeCharacters.current(target)) return;
        try (Connection con = connection()) {
          con.setAutoCommit(false);
          try {
            try (Statement st = con.createStatement()) {
              st.execute("DELETE FROM saved_items");
            }
            for (Item item : actualInventory.list())
              try (PreparedStatement ps =
                  con.prepareStatement("INSERT INTO saved_items VALUES(?,?)")) {
                ps.setInt(1, item.getPosition());
                ps.setInt(2, item.getQuantity());
                ps.executeUpdate();
              }
            cashShop.save(con);
            BridgeRewards.beforeSave(con, target);
            if (failSave) throw new SQLException("Injected inventory commit failure");
            con.commit();
            if (loseCommitAcknowledgement)
              throw new SQLException("Injected lost save acknowledgement");
            BridgeRewards.afterSave(target);
          } catch (Exception denied) {
            con.rollback();
          }
        }
      }
    }

    Connection connection() throws SQLException {
      Connection actual = DriverManager.getConnection(url);
      if (!loseNativeCommitAcknowledgement
          && !failNativeCommitBefore
          && commitAcknowledgementsToLose == 0) return actual;
      return (Connection)
          java.lang.reflect.Proxy.newProxyInstance(
              Connection.class.getClassLoader(),
              new Class<?>[] {Connection.class},
              (proxy, method, args) -> {
                try {
                  if (failNativeCommitBefore && method.getName().equals("commit"))
                    throw new SQLException("Injected rejected native commit");
                  Object value = method.invoke(actual, args);
                  if (method.getName().equals("commit")
                      && (loseNativeCommitAcknowledgement || commitAcknowledgementsToLose > 0)) {
                    if (commitAcknowledgementsToLose > 0) commitAcknowledgementsToLose--;
                    throw new SQLException("Injected lost database commit acknowledgement");
                  }
                  return value;
                } catch (java.lang.reflect.InvocationTargetException error) {
                  throw error.getCause();
                }
              });
    }

    int scalar(String sql) throws SQLException {
      try (Connection c = connection();
          Statement st = c.createStatement();
          ResultSet rs = st.executeQuery(sql)) {
        rs.next();
        return rs.getInt(1);
      }
    }

    void register() throws Exception {
      BridgeRewards.register(ID, 1, CODE, 4031757, 1, 0, 1);
    }

    public void close() throws Exception {
      var field = BridgeRewards.class.getDeclaredField("pending");
      field.setAccessible(true);
      ((Map<?, ?>) field.get(null)).clear();
      for (Class<?> owner :
          List.of(BridgeCoupons.class, BridgeExchanges.class, BridgeCharacters.class)) {
        var state = owner.getDeclaredField(owner == BridgeCharacters.class ? "owners" : "pending");
        state.setAccessible(true);
        ((Map<?, ?>) state.get(null)).clear();
        if (owner == BridgeCharacters.class) {
          var accountState = owner.getDeclaredField("accountOwners");
          accountState.setAccessible(true);
          ((Map<?, ?>) accountState.get(null)).clear();
        }
      }
      packets.close();
      information.close();
      transport.close();
      db.close();
    }
  }

  @Test
  void seriesOneRejectsOtherSeriesAndWrongPetPrefixes() {
    assertEquals(17, SeriesOne.REWARDS.size());
    SeriesOne.validate("C01" + CODE, 5000034, 1, 30, 1);
    assertThrows(
        IllegalArgumentException.class, () -> SeriesOne.validate("C02" + CODE, 5000034, 1, 30, 1));
    assertThrows(IllegalArgumentException.class, () -> SeriesOne.validate(CODE, 4031757, 1, 0, 2));
    assertThrows(IllegalArgumentException.class, () -> SeriesOne.validate(CODE, 2000005, 1, 0, 1));
    assertEquals(CODE, SeriesOne.normalize("bbbbb bbbbb-bbbbb"));
  }

  @Test
  void exactBodyAndPathAreAuthenticatedAndNoncesCannotReplay() {
    String secret = "test-shared-key-".repeat(4),
        time = "1790942400000",
        nonce = "a".repeat(32),
        body = "{\"example\":1}",
        path = "/codes/register";
    String signature = BridgeCrypto.signature(secret, "POST", path, time, nonce, body);
    var used = new ConcurrentHashMap<String, Long>();
    assertTrue(
        BridgeCrypto.verify(
            secret, "POST", path, time, nonce, signature, body, used, Long.parseLong(time)));
    assertFalse(
        BridgeCrypto.verify(
            secret, "POST", path, time, nonce, signature, body, used, Long.parseLong(time)));
    assertFalse(
        BridgeCrypto.verify(
            secret,
            "POST",
            "/debit",
            time,
            nonce,
            signature,
            body,
            new ConcurrentHashMap<>(),
            Long.parseLong(time)));
    assertFalse(
        BridgeCrypto.verify(
            secret,
            "POST",
            path,
            time,
            nonce,
            signature,
            body + " ",
            new ConcurrentHashMap<>(),
            Long.parseLong(time)));
  }

  static boolean verifyNonceAt(
      ConcurrentHashMap<String, Long> used, long signedAt, long now, String nonce) {
    String secret = "signature-test-key-".repeat(3), time = Long.toString(signedAt);
    String body = "{}", path = "/wallet";
    return BridgeCrypto.verify(
        secret,
        "POST",
        path,
        time,
        nonce,
        BridgeCrypto.signature(secret, "POST", path, time, nonce, body),
        body,
        used,
        now);
  }

  @Test
  void futureDatedSignatureNonceSurvivesUntilItsInclusiveFinalValidityBoundary() {
    long now = 1790942400000L, signedAt = now + 59000;
    String nonce = "b".repeat(32);
    var used = new ConcurrentHashMap<String, Long>();
    assertTrue(verifyNonceAt(used, signedAt, now, nonce));
    assertFalse(verifyNonceAt(used, signedAt, now, nonce));
    assertFalse(verifyNonceAt(used, signedAt, now + 61000, nonce));
    assertFalse(verifyNonceAt(used, signedAt, signedAt + 60000, nonce));
    assertEquals(signedAt + 60000, used.get(nonce));
    assertFalse(verifyNonceAt(used, signedAt, signedAt + 60001, nonce));
    assertTrue(verifyNonceAt(used, signedAt + 60001, signedAt + 60001, "c".repeat(32)));
    assertFalse(used.containsKey(nonce));
  }

  @Test
  void signatureSkewBoundariesAndOldNonceExpiryRemainExact() {
    long now = 1790942400000L;
    assertTrue(verifyNonceAt(new ConcurrentHashMap<>(), now + 60000, now, "a".repeat(32)));
    assertFalse(verifyNonceAt(new ConcurrentHashMap<>(), now + 60001, now, "a".repeat(32)));
    assertTrue(verifyNonceAt(new ConcurrentHashMap<>(), now - 60000, now, "a".repeat(32)));
    assertFalse(verifyNonceAt(new ConcurrentHashMap<>(), now - 60001, now, "a".repeat(32)));
    String nonce = "d".repeat(32);
    var used = new ConcurrentHashMap<String, Long>();
    assertTrue(verifyNonceAt(used, now - 59000, now, nonce));
    assertFalse(verifyNonceAt(used, now - 59000, now + 1000, nonce));
    assertTrue(verifyNonceAt(used, now + 1001, now + 1001, "e".repeat(32)));
    assertFalse(used.containsKey(nonce));
  }

  @Test
  void nonceCapacityNeverEvictsAStillValidSignatureAndExpiredEntriesReleaseCapacity() {
    long now = 1790942400000L;
    var used = new ConcurrentHashMap<String, Long>();
    for (int i = 0; i < BridgeCrypto.MAX_NONCES; i++) used.put(String.format("%032x", i), now);
    String incoming = "f".repeat(32);
    assertFalse(verifyNonceAt(used, now, now, incoming));
    assertEquals(BridgeCrypto.MAX_NONCES, used.size());
    assertTrue(verifyNonceAt(used, now + 1, now + 1, incoming));
    assertEquals(1, used.size());
  }

  @Test
  void simultaneousValidRequestsCannotGrowTheNonceCacheBeyondItsCap() throws Exception {
    long now = 1790942400000L;
    var used = new ConcurrentHashMap<String, Long>();
    for (int i = 0; i < BridgeCrypto.MAX_NONCES - 1; i++)
      used.put(String.format("%032x", i), now + 60000);
    var executor = java.util.concurrent.Executors.newFixedThreadPool(8);
    var start = new java.util.concurrent.CountDownLatch(1);
    var ready = new java.util.concurrent.CountDownLatch(8);
    List<java.util.concurrent.Future<Boolean>> responses = new ArrayList<>();
    try {
      for (int i = 0; i < 8; i++) {
        String nonce = "f".repeat(31) + Integer.toHexString(i);
        responses.add(
            executor.submit(
                () -> {
                  ready.countDown();
                  start.await();
                  return verifyNonceAt(used, now, now, nonce);
                }));
      }
      assertTrue(ready.await(3, java.util.concurrent.TimeUnit.SECONDS));
      start.countDown();
      int accepted = 0;
      for (var response : responses)
        if (response.get(5, java.util.concurrent.TimeUnit.SECONDS)) accepted++;
      assertEquals(1, accepted);
      assertEquals(BridgeCrypto.MAX_NONCES, used.size());
    } finally {
      start.countDown();
      executor.shutdownNow();
    }
  }

  @Test
  void rewardItemAndUsedOutboxCommitTogetherAndCannotBeGrantedTwice() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.inventory.list().size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals(1, f.inventory.list().size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
    }
  }

  @Test
  void failedSaveDoesNotMarkUsedAndRetryDoesNotAddAnotherItem() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.failSave = true;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("PENDING", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.inventory.list().size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM saved_items"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
      f.failSave = false;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.inventory.list().size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_items"));
    }
  }

  @Test
  void acknowledgedLostRewardCommitReconcilesIdenticalReceiptWithoutAnotherItem() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.loseCommitAcknowledgement = true;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertTrue(BridgeRewards.hasPending(f.chr));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_items"));
      f.loseCommitAcknowledgement = false;
      f.chr.saveCharToDB(true);
      assertFalse(BridgeRewards.hasPending(f.chr));
      assertEquals(1, f.inventory.list().size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
    }
  }

  @Test
  void acknowledgedLostClaimCommitStillDeliversOnceWithinSameProcessEpoch() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.commitAcknowledgementsToLose = 1;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.inventory.list().size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
      assertFalse(BridgeRewards.hasPending(f.chr));
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals(1, f.inventory.list().size());
    }
  }

  @Test
  void mismatchedUsedReceiptCannotClearClaimOrPersistLaterInventory() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.loseCommitAcknowledgement = true;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      try (Connection con = f.connection();
          Statement st = con.createStatement()) {
        st.execute(
            "UPDATE card_bridge_codes SET receipt_id='99999999-9999-4999-8999-999999999999'");
      }
      f.inventory.list().iterator().next().setQuantity((short) 2);
      f.loseCommitAcknowledgement = false;
      f.chr.saveCharToDB(true);
      assertTrue(BridgeRewards.hasPending(f.chr));
      assertEquals(1, f.scalar("SELECT quantity FROM saved_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
    }
  }

  @Test
  void accountResolverRejectsUnknownBannedAndTemporaryBannedAccounts() throws Exception {
    try (Fixture f = new Fixture()) {
      assertEquals(1, BridgeSessions.resolve("Collector", null).get("accountId"));
      assertEquals("Collector", BridgeSessions.resolve(null, 1).get("name"));
      assertThrows(IllegalArgumentException.class, () -> BridgeSessions.resolve("Collector", 1));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.resolve("Unknown", null));
      try (Connection con = f.connection();
          Statement st = con.createStatement()) {
        st.execute("UPDATE accounts SET banned=1");
        assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.resolve(null, 1));
        st.execute("UPDATE accounts SET banned=0,tempban=TIMESTAMP '2099-01-01 00:00:00'");
        assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.resolve(null, 1));
      }
    }
  }

  @Test
  void operatorDiagnosticsAreBoundedAggregatesWithoutCodePayloads() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.failSave = true;
      BridgeRewards.handle(f.client, CODE);
      try (Connection con = f.connection();
          PreparedStatement ps =
              con.prepareStatement(
                  "INSERT INTO"
                      + " card_bridge_outbox(receipt_id,payload,attempts,last_attempt,last_error)"
                      + " VALUES(?,?,?,?,?)")) {
        ps.setString(1, "99999999-9999-4999-8999-999999999999");
        ps.setString(2, CODE);
        ps.setInt(3, 4);
        ps.setLong(4, System.currentTimeMillis());
        ps.setString(5, "CALLBACK_UNAVAILABLE");
        ps.executeUpdate();
      }
      Map<String, Object> diagnostic = BridgeRewards.diagnostics();
      assertEquals(1L, diagnostic.get("pendingRewards"));
      assertEquals(1L, diagnostic.get("pendingCallbacks"));
      assertEquals(4L, diagnostic.get("attempts"));
      assertEquals("CALLBACK_UNAVAILABLE", diagnostic.get("lastError"));
      assertFalse(BridgeHttp.JSON.toJson(diagnostic).contains(CODE));
      assertFalse(diagnostic.containsKey("payload"));
    }
  }

  static CashShop.CashItem nativeOffer(int itemId) {
    CashShop.CashItem offer = mock(CashShop.CashItem.class);
    when(offer.isOnSale()).thenReturn(true);
    when(offer.getPrice()).thenReturn(1000);
    when(offer.getItemId()).thenReturn(itemId);
    when(offer.getSN()).thenReturn(50100000);
    Equip item = new Equip(itemId, (short) 0);
    when(offer.toItem()).thenReturn(item);
    return offer;
  }

  static Character partner() {
    Character partner = mock(Character.class);
    when(partner.getId()).thenReturn(8);
    when(partner.getName()).thenReturn("Partner");
    return partner;
  }

  static void handleCrush(Fixture f, CashShop.CashItem offer, int cashType) throws Exception {
    try (MockedStatic<CashShop.CashItemFactory> factory =
        mockStatic(CashShop.CashItemFactory.class)) {
      factory.when(() -> CashShop.CashItemFactory.getItem(50100000)).thenReturn(offer);
      when(f.cashShop.isOpened()).thenReturn(true);
      when(f.client.tryacquireClient()).thenReturn(true);
      when(f.client.checkBirthDate(any())).thenReturn(true);
      Channel channel = mock(Channel.class);
      PlayerStorage storage = mock(PlayerStorage.class);
      when(f.client.getChannelServer()).thenReturn(channel);
      when(channel.getPlayerStorage()).thenReturn(storage);
      Character recipient = partner();
      when(storage.getCharacterByName("Partner")).thenReturn(recipient);
      InPacket packet = mock(InPacket.class);
      when(packet.readByte()).thenReturn((byte) 0x1D);
      when(packet.readInt()).thenReturn(19900101, cashType, 50100000);
      when(packet.readString()).thenReturn("Partner", "A gift");
      new CashOperationHandler(mock(NoteService.class)).handlePacket(packet, f.client);
    }
  }

  @Test
  void patchedNativeHandlerRejectsUnsupportedTypeAndConcurrentWebsiteDebitBeforeDelivery()
      throws Exception {
    try (Fixture f = new Fixture()) {
      CashShop.CashItem offer = nativeOffer(1112000);
      handleCrush(f, offer, 3);
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM rings"));
      when(offer.toItem())
          .thenAnswer(
              i -> {
                BridgeWallet.debit("f".repeat(64), 1, 10000, 1);
                return new Equip(1112000, (short) 0);
              });
      handleCrush(f, offer, 1);
      assertEquals(0, BridgeWallet.read(1, 1));
      assertEquals(0, f.cashInventory.size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM rings"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void forgedRingActionRejectsOrdinaryCashEquipAndWrongRingFamilyBeforeItemFactory()
      throws Exception {
    try (Fixture f = new Fixture()) {
      CashShop.CashItem hat = nativeOffer(1003050);
      handleCrush(f, hat, 1);
      verify(hat, never()).toItem();
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM rings"));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertTrue(
          BridgeNativePurchases.ring(
              f.client, 1, hat, partner(), "Gift", true, mock(NoteService.class)));
      verify(hat, never()).toItem();
      CashShop.CashItem friend = nativeOffer(1112800);
      assertTrue(
          BridgeNativePurchases.ring(
              f.client, 1, friend, partner(), "Gift", false, mock(NoteService.class)));
      verify(friend, never()).toItem();
      for (int item : new int[] {1112803, 1112806, 1112807, 1112809, 1112808, 1112100, 5000000}) {
        CashShop.CashItem unrelated = nativeOffer(item);
        assertFalse(BridgeNativePurchases.validRingOffer(unrelated, true));
        assertFalse(BridgeNativePurchases.validRingOffer(unrelated, false));
      }
      f.transport.when(BridgeHttp::enabled).thenReturn(false);
      handleCrush(f, hat, 1);
      verify(hat, never()).toItem();
      verify(f.cashShop, never()).gainCash(anyInt(), anyInt());
      assertEquals(0, f.cashInventory.size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void nativeRingDebitBenefitAndInventoryCommitTogetherAndFailureRollsBackAll() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashIdGenerator> ids = mockStatic(CashIdGenerator.class);
        MockedStatic<Ring> rings = mockStatic(Ring.class)) {
      ids.when(CashIdGenerator::generateCashId).thenReturn(1001, 1002, 1003, 1004);
      CashShop.CashItem offer = nativeOffer(1112000);
      handleCrush(f, offer, 1);
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM rings"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
      doThrow(new SQLException("Injected inventory persistence failure"))
          .when(f.cashShop)
          .save(any());
      handleCrush(f, offer, 1);
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(1, f.cashInventory.size());
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM rings"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM gifts"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void nativeRequestsCommitWithPaymentAndRejectedRequestKeepsSelectedWallet() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeWallet.change(1, 4, 5000);
      CashShop.CashItem offer = nativeOffer(5060000);
      assertTrue(BridgeNativePurchases.request(f.client, offer, "NewName", 0, false));
      assertEquals(4000, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM namechanges"));
      assertTrue(BridgeNativePurchases.request(f.client, offer, "OtherName", 0, false));
      assertEquals(4000, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM namechanges"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
      assertEquals(10000, BridgeWallet.read(1, 1));
    }
  }

  @Test
  void friendshipUsesMaplePointsAndWorldRequestUsesPrepaidWithAtomicRollback() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashIdGenerator> ids = mockStatic(CashIdGenerator.class);
        MockedStatic<Ring> rings = mockStatic(Ring.class)) {
      ids.when(CashIdGenerator::generateCashId).thenReturn(3001, 3002);
      BridgeWallet.change(1, 2, 2000);
      BridgeWallet.change(1, 4, 2000);
      assertTrue(
          BridgeNativePurchases.ring(
              f.client,
              2,
              nativeOffer(1112800),
              partner(),
              "A gift",
              true,
              mock(NoteService.class)));
      assertEquals(1000, BridgeWallet.read(1, 2));
      assertEquals(10000, BridgeWallet.read(1, 1));
      CashShop.CashItem transfer = nativeOffer(5060001);
      assertTrue(BridgeNativePurchases.request(f.client, transfer, null, 1, true));
      assertEquals(1000, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM worldtransfers"));
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
      assertTrue(BridgeNativePurchases.request(f.client, transfer, null, 2, true));
      assertEquals(1000, BridgeWallet.read(1, 4));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM worldtransfers"));
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void nativeAndWebsiteSpendingShareTheSameAccountRowAndCannotOverdraw() throws Exception {
    try (Fixture f = new Fixture();
        Connection nativePurchase = f.connection()) {
      nativePurchase.setAutoCommit(false);
      BridgeNativePurchases.charge(nativePurchase, 1, 1, 8500);
      java.util.concurrent.CountDownLatch started = new java.util.concurrent.CountDownLatch(1);
      java.util.concurrent.ExecutorService executor =
          java.util.concurrent.Executors.newSingleThreadExecutor();
      try {
        var result =
            executor.submit(
                () -> {
                  try (MockedStatic<DatabaseConnection> threadDb =
                      mockStatic(DatabaseConnection.class)) {
                    threadDb
                        .when(DatabaseConnection::getConnection)
                        .thenAnswer(i -> f.connection());
                    started.countDown();
                    return assertThrows(
                            BridgeHttp.Problem.class,
                            () -> BridgeWallet.debit("e".repeat(64), 1, 2000, 1))
                        .code;
                  }
                });
        assertTrue(started.await(3, java.util.concurrent.TimeUnit.SECONDS));
        nativePurchase.commit();
        assertEquals("INSUFFICIENT_FUNDS", result.get(5, java.util.concurrent.TimeUnit.SECONDS));
        assertEquals(1500, BridgeWallet.read(1, 1));
        assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_payments"));
      } finally {
        executor.shutdownNow();
      }
    }
  }

  @Test
  void nativeAmbiguousCommitUsesDurableReceiptInsteadOfRefundingDeliveredRing() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashIdGenerator> ids = mockStatic(CashIdGenerator.class);
        MockedStatic<Ring> rings = mockStatic(Ring.class)) {
      ids.when(CashIdGenerator::generateCashId).thenReturn(2001, 2002);
      f.loseNativeCommitAcknowledgement = true;
      handleCrush(f, nativeOffer(1112000), 1);
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(2, f.scalar("SELECT COUNT(*) FROM rings"));
      assertEquals(1, f.cashInventory.size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
      ids.verify(() -> CashIdGenerator.freeCashId(anyInt()), never());
    }
  }

  @Test
  void nativeReceiptLookupWaitsForTheExistingAccountTransactionBeforeDeclaringAbsence()
      throws Exception {
    try (Fixture f = new Fixture();
        Connection original = f.connection()) {
      String receipt = "77777777-7777-4777-8777-777777777777";
      original.setAutoCommit(false);
      BridgeNativePurchases.charge(original, 1, 1, 1000);
      try (PreparedStatement ps =
          original.prepareStatement(
              "INSERT INTO card_bridge_native_purchases VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)")) {
        ps.setString(1, receipt);
        ps.setInt(2, 1);
        ps.setInt(3, 7);
        ps.setInt(4, 1);
        ps.setInt(5, 1000);
        ps.setString(6, "crush");
        ps.executeUpdate();
      }
      java.util.concurrent.CountDownLatch started = new java.util.concurrent.CountDownLatch(1);
      java.util.concurrent.ExecutorService executor =
          java.util.concurrent.Executors.newSingleThreadExecutor();
      try {
        var result =
            executor.submit(
                () -> {
                  try (MockedStatic<DatabaseConnection> threadDb =
                      mockStatic(DatabaseConnection.class)) {
                    threadDb
                        .when(DatabaseConnection::getConnection)
                        .thenAnswer(i -> f.connection());
                    started.countDown();
                    return BridgeNativePurchases.receiptExists(receipt, 1, 7, 1, 1000, "crush");
                  }
                });
        assertTrue(started.await(3, java.util.concurrent.TimeUnit.SECONDS));
        assertThrows(
            java.util.concurrent.TimeoutException.class,
            () -> result.get(200, java.util.concurrent.TimeUnit.MILLISECONDS));
        original.commit();
        assertTrue(result.get(5, java.util.concurrent.TimeUnit.SECONDS));
        assertEquals(9000, BridgeWallet.read(1, 1));
      } finally {
        executor.shutdownNow();
      }
    }
  }

  @Test
  void nativeReceiptReconciliationRejectsChangedTermsAndMissingAccount() throws Exception {
    try (Fixture f = new Fixture();
        Connection con = f.connection();
        Statement st = con.createStatement()) {
      String receipt = "77777777-7777-4777-8777-777777777777";
      st.execute(
          "INSERT INTO card_bridge_native_purchases VALUES('"
              + receipt
              + "',1,7,1,1000,'crush',CURRENT_TIMESTAMP)");
      assertTrue(BridgeNativePurchases.receiptExists(receipt, 1, 7, 1, 1000, "crush"));
      assertThrows(
          SQLException.class,
          () -> BridgeNativePurchases.receiptExists(receipt, 1, 7, 4, 1000, "crush"));
      assertThrows(
          SQLException.class,
          () -> BridgeNativePurchases.receiptExists(receipt, 1, 8, 1, 1000, "crush"));
      assertThrows(
          SQLException.class,
          () -> BridgeNativePurchases.receiptExists(receipt, 1, 7, 1, 1000, "friendship"));
      assertThrows(
          SQLException.class,
          () -> BridgeNativePurchases.receiptExists(receipt, 2, 7, 1, 1000, "crush"));
      assertFalse(
          BridgeNativePurchases.receiptExists(
              "88888888-8888-4888-8888-888888888888", 1, 7, 1, 1000, "crush"));
    }
  }

  @Test
  void bridgeDisabledRingChargesBeforeCreatingBenefitAndRefundsRejectedRing() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<Ring> rings = mockStatic(Ring.class)) {
      f.transport.when(BridgeHttp::enabled).thenReturn(false);
      java.util.concurrent.atomic.AtomicInteger delta =
          new java.util.concurrent.atomic.AtomicInteger();
      doAnswer(
              i -> {
                delta.addAndGet(i.getArgument(1));
                return null;
              })
          .when(f.cashShop)
          .gainCash(anyInt(), anyInt());
      rings
          .when(() -> Ring.createRing(anyInt(), any(), any()))
          .thenAnswer(
              i -> {
                assertEquals(-1000, delta.get());
                return new tools.Pair<>(-1, -1);
              });
      handleCrush(f, nativeOffer(1112000), 1);
      assertEquals(0, delta.get());
      assertEquals(0, f.cashInventory.size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM rings"));
    }
  }

  @Test
  void nativeTransactionCannotPersistAnUnfinishedCodeRewardOutsideItsUsedTransaction()
      throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.failSave = true;
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("PENDING", BridgeRewards.status(ID).get("status"));
      handleCrush(f, nativeOffer(1112000), 1);
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_native_purchases"));
    }
  }

  @Test
  void lostLeaseFailsClosedAndRequestsOnlyOneProcessRestart() throws Exception {
    var leaseField = BridgeHttp.class.getDeclaredField("lease");
    leaseField.setAccessible(true);
    var lostField = BridgeHttp.class.getDeclaredField("leaseLost");
    lostField.setAccessible(true);
    var oldTerminate = BridgeHttp.terminate;
    Connection lease = mock(Connection.class);
    Statement statement = mock(Statement.class);
    ResultSet result = mock(ResultSet.class);
    when(lease.createStatement()).thenReturn(statement);
    when(statement.executeQuery(anyString())).thenReturn(result);
    when(result.next()).thenReturn(true);
    when(result.getInt(1)).thenReturn(1, 0);
    java.util.concurrent.atomic.AtomicInteger exits =
        new java.util.concurrent.atomic.AtomicInteger();
    BridgeHttp.terminate =
        code -> {
          assertEquals(75, code);
          exits.incrementAndGet();
        };
    leaseField.set(null, lease);
    lostField.setBoolean(null, false);
    try {
      BridgeHttp.requireLease();
      assertThrows(SQLException.class, BridgeHttp::requireLease);
      assertThrows(SQLException.class, BridgeHttp::requireLease);
      assertEquals(1, exits.get());
    } finally {
      BridgeHttp.terminate = oldTerminate;
      leaseField.set(null, null);
      lostField.setBoolean(null, false);
    }
  }

  @Test
  void petRetryKeepsOneCashItemAndItsThirtyDayExpiration() throws Exception {
    try (Fixture f = new Fixture();
        MockedStatic<CashShop> factory = mockStatic(CashShop.class)) {
      int item = 5000034;
      Item reward = new Item(item, (short) 0, (short) 1, -1);
      String code = "C01" + CODE;
      when(f.items.isCash(item)).thenReturn(true);
      factory.when(() -> CashShop.generateCouponItem(item, (short) 1)).thenReturn(reward);
      BridgeRewards.register(ID, 1, code, item, 1, 30, 1);
      long before = System.currentTimeMillis();
      f.failSave = true;
      assertTrue(BridgeRewards.handle(f.client, code));
      assertEquals("PENDING", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.cashInventory.size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      long lifetime = java.util.concurrent.TimeUnit.DAYS.toMillis(30);
      assertTrue(reward.getExpiration() >= before + lifetime);
      assertTrue(reward.getExpiration() <= System.currentTimeMillis() + lifetime);
      f.failSave = false;
      assertTrue(BridgeRewards.handle(f.client, code));
      assertEquals("USED", BridgeRewards.status(ID).get("status"));
      assertEquals(1, f.cashInventory.size());
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM saved_cash_items"));
      factory.verify(() -> CashShop.generateCouponItem(item, (short) 1), times(1));
      f.packets.verify(
          () -> PacketCreator.showCouponRedeemedItems(1, 0, 0, List.of(reward), List.of()),
          times(1));
    }
  }

  @Test
  void fullOrdinaryAndCashInventoriesLeaveTheCodeReadyWithoutPartialGrant() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      for (int i = 0; i < 16; i++)
        f.inventory.addItem(new Item(4030000 + i, (short) 0, (short) 1, -1));
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("READY", BridgeRewards.status(ID).get("status"));
      assertEquals(16, f.inventory.list().size());
      assertEquals(0, f.scalar("SELECT COUNT(*) FROM card_bridge_outbox"));
    }
    try (Fixture f = new Fixture()) {
      int item = 5000034;
      when(f.items.isCash(item)).thenReturn(true);
      BridgeRewards.register(ID, 1, "C01" + CODE, item, 1, 30, 1);
      when(f.cashShop.getItemsSize()).thenReturn(100);
      assertTrue(BridgeRewards.handle(f.client, "C01" + CODE));
      assertEquals("READY", BridgeRewards.status(ID).get("status"));
      assertEquals(0, f.cashInventory.size());
    }
  }

  @Test
  void registrationIsImmutableAndAnotherAccountCannotClaimTheReward() throws Exception {
    try (Fixture f = new Fixture()) {
      f.register();
      f.register();
      assertThrows(
          IllegalArgumentException.class,
          () -> BridgeRewards.register(ID, 2, CODE, 4031757, 1, 0, 1));
      when(f.client.getAccID()).thenReturn(2);
      assertTrue(BridgeRewards.handle(f.client, CODE));
      assertEquals("READY", BridgeRewards.status(ID).get("status"));
      assertEquals(0, f.inventory.list().size());
    }
  }

  @Test
  void eachCashTypePaysIndependentlyAndReceiptKeepsTheSelection() throws Exception {
    try (Fixture f = new Fixture()) {
      BridgeWallet.change(1, 2, 5000);
      BridgeWallet.change(1, 4, 7000);
      assertEquals(4000, BridgeWallet.debit("c".repeat(64), 1, 1000, 2).get("balance"));
      assertEquals(6000, BridgeWallet.debit("d".repeat(64), 1, 1000, 4).get("balance"));
      assertEquals(10000, BridgeWallet.read(1, 1));
      assertThrows(
          IllegalArgumentException.class, () -> BridgeWallet.debit("c".repeat(64), 1, 1000, 4));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.debit("e".repeat(64), 1, 5000, 2));
      assertEquals(4000, BridgeWallet.read(1, 2));
      assertEquals(6000, BridgeWallet.read(1, 4));
      assertEquals(
          2,
          f.scalar(
              "SELECT cash_type FROM card_bridge_payments WHERE order_id='"
                  + "c".repeat(64)
                  + "'"));
    }
  }

  @Test
  void nativeSessionsResolveOnlyTheSavedAccountAndExpireOrRevoke() throws Exception {
    try (Fixture f = new Fixture()) {
      String token = "a".repeat(64);
      var person = BridgeSessions.login("Collector", "password", token);
      assertEquals(1, person.get("accountId"));
      assertEquals(person, BridgeSessions.session(token));
      assertThrows(
          BridgeHttp.Problem.class,
          () -> BridgeSessions.login("Collector", "wrong", "b".repeat(64)));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_sessions"));
      BridgeSessions.logout(token);
      assertThrows(BridgeHttp.Problem.class, () -> BridgeSessions.session(token));
      assertTrue(
          BridgeSessions.passwordMatches(
              "password", tools.BCrypt.hashpw("password", tools.BCrypt.gensalt(4))));
      assertFalse(
          BridgeSessions.passwordMatches(
              "wrong", tools.BCrypt.hashpw("password", tools.BCrypt.gensalt(4))));
    }
  }

  @Test
  void authoritativeWalletDebitsOnceAndRejectsChangedTermsOrOverdraft() throws Exception {
    try (Fixture f = new Fixture()) {
      String order = "a".repeat(64);
      assertEquals(9000, BridgeWallet.debit(order, 1, 1000).get("balance"));
      assertEquals(9000, BridgeWallet.debit(order, 1, 1000).get("balance"));
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertThrows(IllegalArgumentException.class, () -> BridgeWallet.debit(order, 1, 2000));
      assertThrows(BridgeHttp.Problem.class, () -> BridgeWallet.debit("b".repeat(64), 1, 10000));
      assertEquals(9000, BridgeWallet.read(1, 1));
      assertEquals(1, f.scalar("SELECT COUNT(*) FROM card_bridge_payments"));
      BridgeWallet.change(1, 1, 500);
      assertEquals(9500, BridgeWallet.read(1, 1));
    }
  }
}
