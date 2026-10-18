import com.google.gson.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.util.UUID;
import tools.BCrypt;
import tools.DatabaseConnection;

/** Operator-only account provisioning; credentials arrive on stdin. */
public final class AccountCommand {
  public static void main(String[] args) throws Exception {
    byte[] raw = System.in.readNBytes(4097);
    if (raw.length > 4096) throw new IllegalArgumentException("Input too large");
    JsonObject input =
        JsonParser.parseString(new String(raw, StandardCharsets.UTF_8)).getAsJsonObject();
    String name = input.get("username").getAsString();
    if (!name.matches("[A-Za-z0-9]{3,13}"))
      throw new IllegalArgumentException("Invalid account name");
    if (!DatabaseConnection.initializeConnectionPool())
      throw new IllegalStateException("Database unavailable");
    try (Connection con = DatabaseConnection.getConnection()) {
      if ("create".equals(input.get("action").getAsString()))
        create(con, name, input.get("password").getAsString());
      else if ("fund".equals(input.get("action").getAsString())) fund(con, name, input);
      else throw new IllegalArgumentException("Unsupported operator action");
    }
    System.out.println("Operator account action completed.");
    System.exit(0);
  }

  private static void create(Connection con, String name, String password) throws SQLException {
    if (password.getBytes(StandardCharsets.UTF_8).length < 8
        || password.getBytes(StandardCharsets.UTF_8).length > 72)
      throw new IllegalArgumentException("Use 8 to 72 UTF-8 password bytes");
    try (PreparedStatement ps =
        con.prepareStatement(
            "INSERT INTO"
                + " accounts(name,password,tos,nxCredit,nxPrepaid,maplePoint,gender,characterslots,tempban)"
                + " VALUES(?,?,1,0,0,0,10,3,'2000-01-01 00:00:00') ON DUPLICATE KEY UPDATE"
                + " id=id")) {
      ps.setString(1, name);
      ps.setString(2, BCrypt.hashpw(password, BCrypt.gensalt(12)));
      ps.executeUpdate();
    }
    try (PreparedStatement ps =
        con.prepareStatement("SELECT password FROM accounts WHERE name=?")) {
      ps.setString(1, name);
      try (ResultSet rs = ps.executeQuery()) {
        if (!rs.next()
            || !rs.getString(1).startsWith("$2")
            || !BCrypt.checkpw(password, rs.getString(1)))
          throw new IllegalStateException(
              "The account exists with different credentials; it was not changed");
      }
    }
  }

  private static void fund(Connection con, String name, JsonObject input) throws Exception {
    for (String field : new String[] {"cashType", "amount"}) {
      if (!input.has(field)
          || !input.get(field).isJsonPrimitive()
          || !input.get(field).getAsJsonPrimitive().isNumber()
          || !input.get(field).getAsString().matches("[1-9][0-9]{0,8}"))
        throw new IllegalArgumentException("Invalid funding amount or cash source");
    }
    int type = input.get("cashType").getAsInt(), amount = input.get("amount").getAsInt();
    String key = input.has("key") ? input.get("key").getAsString() : UUID.randomUUID().toString();
    server.cardbridge.BridgeAwards.operatorFund(con, name, type, amount, key);
  }
}
