package server.cardbridge;

import client.Character;
import java.lang.ref.WeakReference;
import java.sql.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;
import tools.DatabaseConnection;

/** Serializes account inventory loads and saves, fencing replaced character instances. */
public final class BridgeCharacters {
  private BridgeCharacters() {}

  private static final ReentrantLock[] accounts = new ReentrantLock[256],
      characters = new ReentrantLock[256];
  private static final ConcurrentHashMap<Integer, WeakReference<Character>>
      owners = new ConcurrentHashMap<>(),
      accountOwners = new ConcurrentHashMap<>();

  static {
    for (int i = 0; i < accounts.length; i++) {
      accounts[i] = new ReentrantLock();
      characters[i] = new ReentrantLock();
    }
  }

  public static Guard load(int id) throws SQLException {
    int account;
    try (Connection con = DatabaseConnection.getConnection();
        PreparedStatement ps =
            con.prepareStatement("SELECT accountid FROM characters WHERE id=?")) {
      ps.setQueryTimeout(5);
      ps.setInt(1, id);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()) throw new SQLException("Character owner unavailable");
        account = rs.getInt(1);
      }
    }
    return new Guard(id, account);
  }

  public static Guard save(Character chr) {
    return new Guard(chr.getId(), chr.getAccountID());
  }

  public static void activate(Character chr) throws SQLException {
    try (Guard guard = save(chr)) {
      BridgeHttp.requireLease();
      try (Connection con = DatabaseConnection.getConnection()) {
        con.setAutoCommit(false);
        try {
          BridgePayments.lockAccount(con, chr.getAccountID(), 1);
          BridgePayments.assertLiveIdentity(con, chr.getAccountID());
          con.commit();
        } catch (Exception denied) {
          con.rollback();
          throw denied;
        }
      }
      WeakReference<Character> previousAccount = accountOwners.get(chr.getAccountID());
      Character old = previousAccount == null ? null : previousAccount.get();
      if (old != null && old != chr) {
        BridgeRewards.replace(old);
        BridgeCoupons.retire(old);
        BridgeExchanges.retire(old);
      }
      WeakReference<Character> previous = owners.get(chr.getId());
      Character replaced = previous == null ? null : previous.get();
      if (replaced != null && replaced != old && replaced != chr) {
        BridgeRewards.replace(replaced);
        BridgeCoupons.retire(replaced);
        BridgeExchanges.retire(replaced);
      }
      owners.put(chr.getId(), new WeakReference<>(chr));
      accountOwners.put(chr.getAccountID(), new WeakReference<>(chr));
    }
  }

  public static boolean current(Character chr) {
    WeakReference<Character> owner = owners.get(chr.getId()),
        account = accountOwners.get(chr.getAccountID());
    return (owner == null || owner.get() == chr) && (account == null || account.get() == chr);
  }

  public static final class Guard implements AutoCloseable {
    private final ReentrantLock account, character;
    private final int accountId;

    private Guard(int id, int accountId) {
      this.accountId = accountId;
      account = accounts[Math.floorMod(accountId, accounts.length)];
      character = characters[Math.floorMod(id, characters.length)];
      account.lock();
      character.lock();
    }

    public void validate(Character loaded) throws SQLException {
      if (loaded.getAccountID() != accountId)
        throw new SQLException("Character ownership changed during load");
    }

    public void close() {
      character.unlock();
      account.unlock();
    }
  }
}
