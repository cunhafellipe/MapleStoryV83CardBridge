#!/usr/bin/env python3
"""Install the v83 adapter into a supported Cosmic/HeavenMS-derived checkout."""
from pathlib import Path
import argparse, shutil, re


def masked_java(text):
    """Preserve offsets while excluding comments and literals from hook validation."""
    return re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/',
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group()),
        text,
        flags=re.S,
    )


def patch_character_save(text):
    text = text.replace(
        "server.cardbridge.BridgeRewards.beforeSave", "BridgeRewards.beforeSave"
    ).replace("server.cardbridge.BridgeRewards.afterSave", "BridgeRewards.afterSave")
    code = masked_java(text)
    signatures = list(
        re.finditer(
            r"\bpublic\s+synchronized\s+void\s+saveCharToDB\s*\(\s*boolean\s+notAutosave\s*\)",
            code,
        )
    )
    if len(signatures) != 1:
        raise SystemExit("Unsupported character save method")
    start = code.find("{", signatures[0].end())
    if start < 0:
        raise SystemExit("Unsupported character save method")
    depth, end = 1, start + 1
    while depth and end < len(code):
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    if depth:
        raise SystemExit("Unsupported character save method")
    commits = list(re.finditer(r"\bcon\s*\.\s*commit\s*\(\s*\)\s*;", code[start:end]))
    if len(commits) != 1:
        raise SystemExit("Character save requires exactly one transaction commit")
    commit_start = start + commits[0].start()
    commit_end = start + commits[0].end()
    hooks = list(
        re.finditer(r"\bBridgeRewards\s*\.\s*(beforeSave|afterSave)\s*\(", code)
    )
    if not hooks:
        line_start = text.rfind("\n", 0, commit_start) + 1
        indent = text[line_start:commit_start]
        if indent.strip():
            raise SystemExit("Unsupported character transaction commit layout")
        patched = (
            text[:commit_start]
            + "BridgeRewards.beforeSave(con, this);\n"
            + indent
            + text[commit_start:commit_end]
            + "\n"
            + indent
            + "BridgeRewards.afterSave(this);"
            + text[commit_end:]
        )
        return patch_character_save(patched)
    valid = (
        len(hooks) == 2
        and hooks[0].group(1) == "beforeSave"
        and hooks[1].group(1) == "afterSave"
        and start
        < hooks[0].start()
        < commit_start
        < commit_end
        < hooks[1].start()
        < end
    )
    if valid:
        pair = code[hooks[0].start() : end]
        valid = bool(
            re.match(
                r"BridgeRewards\s*\.\s*beforeSave\s*\(\s*con\s*,\s*this\s*\)\s*;\s*"
                r"con\s*\.\s*commit\s*\(\s*\)\s*;\s*"
                r"BridgeRewards\s*\.\s*afterSave\s*\(\s*this\s*\)\s*;",
                pair,
            )
        )
    if not valid:
        raise SystemExit(
            "Invalid character save hooks: require one beforeSave, commit, afterSave pair in the synchronized save method"
        )
    return text


p = argparse.ArgumentParser()
p.add_argument("server", type=Path)
p.add_argument("--check", action="store_true")
args = p.parse_args()
base = args.server.resolve()
source = Path(__file__).resolve().parents[1] / "java"
patches = {
    "src/main/java/net/server/channel/handlers/CouponCodeHandler.java": [
        (
            "                Pair<Integer, List<Pair<Integer, Pair<Integer, Integer>>>> codeRes =",
            "                if (server.cardbridge.BridgeRewards.handle(c, code)) return;\n                Pair<Integer, List<Pair<Integer, Pair<Integer, Integer>>>> codeRes =",
        )
    ],
    "src/main/java/client/Character.java": [
        ("package client;", "package client;\nimport server.cardbridge.BridgeRewards;")
    ],
    "src/main/java/server/CashShop.java": [
        (
            "    public int getCash(int type) {\n",
            "    public int getCash(int type) {\n        if (server.cardbridge.BridgeHttp.enabled() && (type == 1 || type == 2 || type == 4)) return server.cardbridge.BridgeWallet.read(accountId, type);\n",
        ),
        (
            "    public void gainCash(int type, int cash) {\n",
            "    public void gainCash(int type, int cash) {\n        if (server.cardbridge.BridgeHttp.enabled() && (type == 1 || type == 2 || type == 4)) { if (cash != 0) server.cardbridge.BridgeWallet.change(accountId, type, cash); return; }\n",
        ),
        (
            "    public void save(Connection con) throws SQLException {\n        try (PreparedStatement",
            "    public void save(Connection con) throws SQLException {\n        if (!server.cardbridge.BridgeHttp.enabled()) {\n        try (PreparedStatement",
        ),
        (
            "            ps.setInt(4, accountId);\n            ps.executeUpdate();\n        }\n\n        List<Pair<Item, InventoryType>> itemsWithType",
            "            ps.setInt(4, accountId);\n            ps.executeUpdate();\n        }\n        }\n\n        List<Pair<Item, InventoryType>> itemsWithType",
        ),
    ],
    "src/main/java/net/server/Server.java": [
        (
            "package net.server;",
            "package net.server;\nimport server.cardbridge.BridgeHttp;",
        ),
        (
            "        loginServer.start();",
            "        BridgeHttp.start();\n        loginServer.start();",
        ),
    ],
    "pom.xml": [],
}
proposed = {}
for rel, replacements in patches.items():
    file = base / rel
    if not file.is_file():
        raise SystemExit("Unsupported checkout: missing " + rel)
    text = file.read_text(encoding="utf-8")
    for old, new in replacements:
        if (
            rel.endswith("CouponCodeHandler.java")
            and "if (server.cardbridge.BridgeRewards.handle(c, code)) return;" in text
        ):
            if (
                text.count(
                    "if (server.cardbridge.BridgeRewards.handle(c, code)) return;"
                )
                != 1
            ):
                raise SystemExit("Duplicate card code handler hook")
            continue
        if new in text:
            continue
        if text.count(old) != 1:
            raise SystemExit(
                "Unsupported source revision: "
                + rel
                + ". Apply the documented hook manually."
            )
        text = text.replace(old, new, 1)
    if rel == "pom.xml":
        for group, artifact, version, scope in [
            ("com.google.code.gson", "gson", "2.13.2", None),
            ("com.h2database", "h2", "2.3.232", "test"),
        ]:
            blocks = re.findall(r"<dependency>.*?</dependency>", text, re.S)
            found = [
                b
                for b in blocks
                if re.search(r"<artifactId>\s*" + artifact + r"\s*</artifactId>", b)
            ]
            if any(
                not re.search(
                    r"<version>\s*" + re.escape(version) + r"\s*</version>", b
                )
                for b in found
            ):
                raise SystemExit(
                    "Conflicting "
                    + artifact
                    + " version; resolve the dependency before installing."
                )
            if found:
                for duplicate in found[1:]:
                    first = text.find(duplicate)
                    second = text.find(duplicate, first + len(duplicate))
                    if second >= 0:
                        text = text[:second] + text[second + len(duplicate) :]
                continue
            if text.count("    <dependencies>") != 1:
                raise SystemExit("Unsupported Maven dependencies layout")
            block = (
                "\n        <dependency>\n            <groupId>"
                + group
                + "</groupId>\n            <artifactId>"
                + artifact
                + "</artifactId>\n            <version>"
                + version
                + "</version>"
            )
            if scope:
                block += "\n            <scope>" + scope + "</scope>"
            text = text.replace(
                "    <dependencies>",
                "    <dependencies>" + block + "\n        </dependency>",
                1,
            )
        if "<id>card-bridge-contract</id>" not in text:
            # The game initializes WZ paths once per JVM; keep adapter SQL fixtures isolated.
            plugin = re.search(
                r"<plugin>\s*<groupId>org.apache.maven.plugins</groupId>\s*<artifactId>maven-surefire-plugin</artifactId>.*?</plugin>",
                text,
                re.S,
            )
            if plugin is None or "<executions>" in plugin.group():
                raise SystemExit(
                    "Resolve the Surefire execution layout before installing adapter tests."
                )
            replacement = plugin.group().replace(
                "</plugin>",
                "<executions>\n                    <execution><id>default-test</id><configuration><excludes><exclude>**/BridgeContractTest.java</exclude></excludes></configuration></execution>\n                    <execution><id>card-bridge-contract</id><goals><goal>test</goal></goals><configuration><includes><include>**/BridgeContractTest.java</include></includes><forkCount>1</forkCount><reuseForks>false</reuseForks></configuration></execution>\n                </executions>"
                + "\n            </plugin>",
            )
            text = text[: plugin.start()] + replacement + text[plugin.end() :]
    if rel.endswith("client/Character.java"):
        text = patch_character_save(text)
    if rel.endswith("net/server/Server.java"):
        text = text.replace(
            "server.cardbridge.BridgeHttp.start()", "BridgeHttp.start()"
        )
        if "BridgeHttp.acquireLease();" not in text:
            anchor = "        DatabaseMigrations.runDatabaseMigrations();"
            if text.count(anchor) != 1:
                raise SystemExit("Unsupported game startup layout")
            text = text.replace(
                anchor, "        BridgeHttp.acquireLease();\n" + anchor, 1
            )
    proposed[file] = text

# Keep changes to the upstream handler bounded to validated transaction boundaries.
handler = base / "src/main/java/net/server/channel/handlers/CashOperationHandler.java"
text = handler.read_text(encoding="utf-8")
if "BridgeNativePurchases.ring" not in text:

    def replace_once(old, new):
        global text
        if text.count(old) != 1:
            raise SystemExit(
                "Unsupported Cash Shop purchase layout; apply compatible hooks manually."
            )
        text = text.replace(old, new, 1)

    for cash, friendship, notification in [
        ("toCharge", "false", "showBoughtCashItem(eqp, c.getAccID())"),
        ("payment", "true", "showBoughtCashRing(eqp, partner.getName(), c.getAccID())"),
    ]:
        needle = (
            "                            if (itemRing.toItem() instanceof Equip eqp) {"
        )
        # The two identical source anchors are replaced in their respective action regions.
        start = text.index("action == " + ("0x1D" if friendship == "false" else "0x23"))
        end = text.index("} else if (action ==", start + 20)
        region = text[start:end]
        if region.count(needle) != 1:
            raise SystemExit("Unsupported native ring layout")
        region = region.replace(
            needle,
            "                            if (!server.cardbridge.BridgeNativePurchases.supportedCash("
            + cash
            + ")"
            " || !canBuy(chr, itemRing, cs.getCash("
            + cash
            + "))) { c.enableCSActions(); return; }\n"
            "                            if (server.cardbridge.BridgeNativePurchases.ring(c, "
            + cash
            + ", itemRing, partner, text, "
            + friendship
            + ", noteService)) return;\n"
            + needle
            + "\n                                cs.gainCash("
            + cash
            + ", -itemRing.getPrice());",
            1,
        )
        region = region.replace(
            "                                Pair<Integer, Integer> rings = Ring.createRing(itemRing.getItemId(), chr, partner);",
            "                                Pair<Integer, Integer> rings = Ring.createRing(itemRing.getItemId(), chr, partner);\n"
            "                                if (rings.getLeft() < 0 || rings.getRight() < 0) { cs.gainCash("
            + cash
            + ", itemRing.getPrice()); c.enableCSActions(); return; }",
            1,
        )
        old = (
            "                                cs.gainCash(toCharge, itemRing, chr.getWorld());"
            if cash == "toCharge"
            else "                                cs.gainCash(payment, -itemRing.getPrice());"
        )
        # Remove only the original post-delivery debit, retaining the new pre-delivery debit.
        position = region.index("c.sendPacket(PacketCreator." + notification + ");")
        region = region[:position] + region[position:].replace(old + "\n", "", 1)
        text = text[:start] + region + text[end:]

    replace_once(
        "                        if (chr.registerNameChange(newName)) { //success",
        "                        if (server.cardbridge.BridgeNativePurchases.request(c, cItem, newName, 0, false)) return;\n"
        "                        Item nameItem = cItem.toItem();\n"
        "                        cs.gainCash(4, cItem, chr.getWorld());\n"
        "                        if (chr.registerNameChange(newName)) { //success",
    )
    replace_once(
        "                            Item item = cItem.toItem();\n                            c.sendPacket(PacketCreator.showNameChangeSuccess(item, c.getAccID()));\n                            cs.gainCash(4, cItem, chr.getWorld());",
        "                            Item item = nameItem;\n                            c.sendPacket(PacketCreator.showNameChangeSuccess(item, c.getAccID()));",
    )
    replace_once(
        "                            cs.addToInventory(item);\n                        } else {\n                            c.sendPacket(PacketCreator.showCashShopMessage((byte) 0));\n                        }\n                    }\n                    c.enableCSActions();\n                } else if (action == 0x31)",
        "                            cs.addToInventory(item);\n                        } else {\n                            cs.gainCash(4, cItem.getPrice());\n                            c.sendPacket(PacketCreator.showCashShopMessage((byte) 0));\n                        }\n                    }\n                    c.enableCSActions();\n                } else if (action == 0x31)",
    )
    replace_once(
        "                        } else if (chr.registerWorldTransfer(newWorldSelection)) {",
        "                        } else {\n"
        "                            if (server.cardbridge.BridgeNativePurchases.request(c, cItem, null, newWorldSelection, true)) return;\n"
        "                            Item transferItem = cItem.toItem();\n"
        "                            cs.gainCash(4, cItem, chr.getWorld());\n"
        "                            if (chr.registerWorldTransfer(newWorldSelection)) {",
    )
    replace_once(
        "                            Item item = cItem.toItem();\n                            c.sendPacket(PacketCreator.showWorldTransferSuccess(item, c.getAccID()));\n                            cs.gainCash(4, cItem, chr.getWorld());",
        "                            Item item = transferItem;\n                            c.sendPacket(PacketCreator.showWorldTransferSuccess(item, c.getAccID()));",
    )
    replace_once(
        "                            c.sendPacket(PacketCreator.showWorldTransferSuccess(item, c.getAccID()));\n                            cs.addToInventory(item);\n                        } else {\n                            c.sendPacket(PacketCreator.showCashShopMessage((byte) 0));\n                        }",
        "                            c.sendPacket(PacketCreator.showWorldTransferSuccess(item, c.getAccID()));\n                            cs.addToInventory(item);\n                        } else {\n                            cs.gainCash(4, cItem.getPrice());\n                            c.sendPacket(PacketCreator.showCashShopMessage((byte) 0));\n                        }\n                        }",
    )
    replace_once(
        "if (item != null && item.isOnSale() && item.getPrice() <= cash)",
        "if (item != null && item.isOnSale() && item.getPrice() > 0 && item.getPrice() <= 100000000 && item.getPrice() <= cash)",
    )
    replace_once(
        "worldTransferError != 0 || newWorldSelection >=",
        "worldTransferError != 0 || newWorldSelection < 0 || newWorldSelection >=",
    )
for cash, friendship in [("toCharge", "false"), ("payment", "true")]:
    family = (
        "server.cardbridge.BridgeNativePurchases.validRingOffer(itemRing, "
        + friendship
        + ")"
    )
    if family not in text:
        anchor = (
            "!server.cardbridge.BridgeNativePurchases.supportedCash("
            + cash
            + ") || !canBuy"
        )
        if text.count(anchor) != 1:
            raise SystemExit("Unsupported ring-family validation layout")
        text = text.replace(
            anchor,
            "!server.cardbridge.BridgeNativePurchases.supportedCash("
            + cash
            + ") || !"
            + family
            + " || !canBuy",
            1,
        )
for marker in [
    "BridgeNativePurchases.ring(c, toCharge, itemRing, partner, text, false, noteService)",
    "BridgeNativePurchases.ring(c, payment, itemRing, partner, text, true, noteService)",
    "BridgeNativePurchases.request(c, cItem, newName, 0, false)",
    "BridgeNativePurchases.request(c, cItem, null, newWorldSelection, true)",
    "item.getPrice() > 0 && item.getPrice() <= 100000000",
    "newWorldSelection < 0",
    "BridgeNativePurchases.validRingOffer(itemRing, false)",
    "BridgeNativePurchases.validRingOffer(itemRing, true)",
]:
    if text.count(marker) != 1:
        raise SystemExit(
            "Incomplete or conflicting native purchase hooks; repair the compatible source before installing."
        )
proposed[handler] = text
character = base / "src/main/java/client/Character.java"
if "public void bridgeNameChangePending()" not in proposed[character]:
    if (
        proposed[character].count(
            "    public boolean registerNameChange(String newName) {"
        )
        != 1
    ):
        raise SystemExit("Unsupported name-change registration layout")
    proposed[character] = proposed[character].replace(
        "    public boolean registerNameChange(String newName) {",
        "    public void bridgeNameChangePending() { pendingNameChange = true; }\n\n"
        "    public boolean registerNameChange(String newName) {",
        1,
    )


def financial_hook(rel, old, new):
    file = base / rel
    text = proposed.get(file, file.read_text(encoding="utf-8"))
    if text.count(new) == 1:
        return
    if new in text or text.count(old) != 1:
        raise SystemExit("Unsupported or conflicting financial hook: " + rel)
    proposed[file] = text.replace(old, new, 1)


financial_hook(
    "src/main/java/net/server/channel/handlers/CouponCodeHandler.java",
    "                if (server.cardbridge.BridgeRewards.handle(c, code)) return;",
    "                if (server.cardbridge.BridgeRewards.handle(c, code)) return;\n                if (server.cardbridge.BridgeHttp.enabled()) { server.cardbridge.BridgeCoupons.handle(c, code); return; }",
)
financial_hook(
    "src/main/java/net/server/channel/handlers/EnterCashShopHandler.java",
    "            mc.getCashShop().open(true);\n            mc.saveCharToDB();",
    "            mc.getCashShop().open(true);\n            if (server.cardbridge.BridgeHttp.enabled()) server.cardbridge.BridgeCoupons.recover(c);\n            mc.saveCharToDB();",
)
financial_hook(
    "src/main/java/server/maps/MapItem.java",
    "public class MapItem extends AbstractMapObject {",
    "public class MapItem extends AbstractMapObject {\n    private final String cardAwardId = java.util.UUID.randomUUID().toString();\n    public String getCardAwardId() { return cardAwardId; }",
)
for indent in ["                                ", "                        "]:
    old = indent + "this.getCashShop().gainCash(1, nxGain);"
    financial_hook(
        "src/main/java/client/Character.java",
        old,
        indent
        + "if (server.cardbridge.BridgeHttp.enabled()) {\n"
        + indent
        + "    if (!server.cardbridge.BridgeAwards.loot(this, mapitem)) { sendPacket(PacketCreator.enableActions()); return; }\n"
        + indent
        + "} else this.getCashShop().gainCash(1, nxGain);",
    )
financial_hook(
    "src/main/java/client/command/commands/gm4/ForceVacCommand.java",
    "                    player.getCashShop().gainCash(1, mapItem.getItemId() == ItemId.NX_CARD_100 ? 100 : 250);",
    "                    if (server.cardbridge.BridgeHttp.enabled()) {\n                        if (!server.cardbridge.BridgeAwards.loot(player, mapItem)) continue;\n                    } else player.getCashShop().gainCash(1, mapItem.getItemId() == ItemId.NX_CARD_100 ? 100 : 250);",
)
financial_hook(
    "src/main/java/client/command/commands/gm3/GiveNxCommand.java",
    "            victim.getCashShop().gainCash(type, value);",
    "            if (server.cardbridge.BridgeHttp.enabled()) {\n                if (!server.cardbridge.BridgeAwards.grant(victim, type, value)) return;\n            } else victim.getCashShop().gainCash(type, value);",
)
for case in ["case 16: { //buy", "case 17: { //buy from cart"]:
    old = (
        "            "
        + case
        + "\n                int id = p.readInt(); // id of the item"
    )
    financial_hook(
        "src/main/java/net/server/channel/handlers/MTSHandler.java",
        old,
        old
        + "\n                if (server.cardbridge.BridgeHttp.enabled()) {\n                    server.cardbridge.BridgeAwards.mts(c, id);\n                    c.sendPacket(PacketCreator.showMTSCash(c.getPlayer()));\n                    c.sendPacket(PacketCreator.transferInventory(getTransfer(c.getPlayer().getId())));\n                    c.sendPacket(PacketCreator.notYetSoldInv(getNotYetSold(c.getPlayer().getId())));\n                    break;\n                }",
    )
financial_hook(
    "src/main/java/net/server/channel/handlers/CashOperationHandler.java",
    "                        Item item = cItem.toItem();\n                        cs.gainCash(useNX, cItem, chr.getWorld());",
    "                        if (server.cardbridge.BridgeHttp.enabled()) {\n                            if (server.cardbridge.BridgeNativePurchases.buy(c, useNX, cItem, false) && !YamlConfig.config.server.USE_ENFORCE_ITEM_SUGGESTION) Server.getInstance().getWorld(chr.getWorld()).addCashItemBought(cItem.getSN());\n                            return;\n                        }\n                        Item item = cItem.toItem();\n                        cs.gainCash(useNX, cItem, chr.getWorld());",
)
financial_hook(
    "src/main/java/net/server/channel/handlers/CashOperationHandler.java",
    "                    } else { // Package\n                        cs.gainCash(useNX, cItem, chr.getWorld());",
    "                    } else { // Package\n                        if (server.cardbridge.BridgeHttp.enabled()) {\n                            if (server.cardbridge.BridgeNativePurchases.buy(c, useNX, cItem, true) && !YamlConfig.config.server.USE_ENFORCE_ITEM_SUGGESTION) Server.getInstance().getWorld(chr.getWorld()).addCashItemBought(cItem.getSN());\n                            return;\n                        }\n                        cs.gainCash(useNX, cItem, chr.getWorld());",
)
financial_hook(
    "src/main/java/net/server/Server.java",
    "        long timeClear = System.currentTimeMillis() - DAYS.toMillis(14);",
    "        long timeClear = System.currentTimeMillis() - DAYS.toMillis(14);\n        if (BridgeHttp.enabled()) { server.cardbridge.BridgeCoupons.clean(con, timeClear); return; }",
)
financial_hook(
    "src/main/java/client/Character.java",
    "    public static Character loadCharFromDB(final int charid, Client client, boolean channelserver) throws SQLException {",
    "    public static Character loadCharFromDB(final int charid, Client client, boolean channelserver) throws SQLException {\n        if (!server.cardbridge.BridgeHttp.enabled()) return bridgeLoadCharFromDB(charid, client, channelserver);\n        try (server.cardbridge.BridgeCharacters.Guard guard = server.cardbridge.BridgeCharacters.load(charid)) {\n            Character loaded = bridgeLoadCharFromDB(charid, client, channelserver);\n            guard.validate(loaded);\n            if (channelserver) server.cardbridge.BridgeCharacters.activate(loaded);\n            return loaded;\n        }\n    }\n\n    private static Character bridgeLoadCharFromDB(final int charid, Client client, boolean channelserver) throws SQLException {",
)
# Keep the transaction hooks inside the supported synchronized save method.
text = proposed[character]
if "BridgeCharacters.save(this)" not in text:
    code = masked_java(text)
    match = re.search(
        r"public\s+synchronized\s+void\s+saveCharToDB\s*\(\s*boolean\s+notAutosave\s*\)",
        code,
    )
    start = code.find("{", match.end())
    depth, end = 1, start + 1
    while depth:
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    text = (
        text[: start + 1]
        + "\n        try (server.cardbridge.BridgeCharacters.Guard guard = server.cardbridge.BridgeCharacters.save(this)) {\n            if (server.cardbridge.BridgeHttp.enabled() && !server.cardbridge.BridgeCharacters.current(this)) return;"
        + text[start + 1 : end - 1]
        + "        }\n    "
        + text[end - 1 :]
    )
    proposed[character] = patch_character_save(text)
elif text.count("BridgeCharacters.save(this)") != 1:
    raise SystemExit("Conflicting character ownership guard")
financial_hook(
    "src/main/java/server/CashShop.java",
    "        public static List<Item> getPackage(int itemId) {",
    "        public static List<Integer> getPackageSerials(int itemId) {\n            List<Integer> serials = packages.get(itemId);\n            return serials == null ? List.of() : List.copyOf(serials);\n        }\n\n        public static List<Item> getPackage(int itemId) {",
)

financial_hook(
    "src/main/java/server/CashShop.java",
    "    public int getAvailableNotes() {",
    "    public void bridgeAddGiftNotes(int count) { notes += count; }\n\n    public int getAvailableNotes() {",
)
financial_hook(
    "src/main/java/server/Storage.java",
    "    public byte getSlots() {",
    '    public void bridgeSetSlots(int value) {\n        if (value < 0 || value > 48) throw new IllegalArgumentException("Invalid storage slots");\n        lock.lock();\n        try { slots = (byte) value; } finally { lock.unlock(); }\n    }\n\n    public byte getSlots() {',
)
financial_hook(
    "src/main/java/net/server/channel/handlers/EnterCashShopHandler.java",
    "            c.sendPacket(PacketCreator.showGifts(mc.getCashShop().loadGifts()));",
    "            c.sendPacket(PacketCreator.showGifts(server.cardbridge.BridgeHttp.enabled() ? server.cardbridge.BridgeNativePurchases.receiveGifts(c) : mc.getCashShop().loadGifts()));",
)
financial_hook(
    "src/main/java/net/server/channel/handlers/CashOperationHandler.java",
    '                    cs.gainCash(4, cItem, chr.getWorld());\n                    cs.gift(Integer.parseInt(recipient.get("id")), chr.getName(), message, cItem.getSN());',
    '                    if (server.cardbridge.BridgeHttp.enabled()) {\n                        if (!server.cardbridge.BridgeNativePurchases.gift(c, cItem, Integer.parseInt(recipient.get("id")), message)) return;\n                    } else {\n                        cs.gainCash(4, cItem, chr.getWorld());\n                        cs.gift(Integer.parseInt(recipient.get("id")), chr.getName(), message, cItem.getSN());\n                    }',
)
for old, route in [
    (
        "                        cs.gainCash(cash, -4000);\n                        if (chr.gainSlots(type, qty, false)) {",
        "1, type, qty",
    ),
    (
        "                        cs.gainCash(cash, cItem, chr.getWorld());\n                        if (chr.gainSlots(type, qty, false)) {",
        "1, type, qty",
    ),
    (
        "                        cs.gainCash(cash, -4000);\n                        if (chr.getStorage().gainSlots(qty)) {",
        "2, 0, qty",
    ),
    (
        "                        cs.gainCash(cash, cItem, chr.getWorld());\n                        if (chr.getStorage().gainSlots(qty)) {",
        "2, 0, qty",
    ),
    (
        "                    cs.gainCash(cash, cItem, chr.getWorld());\n                    if (c.gainCharacterSlot()) {",
        "3, 0, 1",
    ),
]:
    indent = old[: len(old) - len(old.lstrip())]
    offer = "null" if "-4000" in old else "cItem"
    financial_hook(
        "src/main/java/net/server/channel/handlers/CashOperationHandler.java",
        old,
        indent
        + "if (server.cardbridge.BridgeHttp.enabled()) { server.cardbridge.BridgeNativePurchases.slots(c, cash, "
        + offer
        + ", "
        + route
        + "); return; }\n"
        + old,
    )
financial_hook(
    "src/main/java/net/server/channel/handlers/MTSHandler.java",
    "        // TODO add karma-to-untradeable flag on sold items here",
    "        // TODO add karma-to-untradeable flag on sold items here\n        if (server.cardbridge.BridgeHttp.enabled() && !config.YamlConfig.config.server.USE_MTS) { c.enableCSActions(); return; }",
)

financial_hook(
    "src/main/java/server/CashShop.java",
    "        public static List<Integer> getPackageSerials(int itemId) {",
    "        public static CashItem bridgeFixedSlotOffer() { return new CashItem(0, 0, 4000, 0, (short) 1, true); }\n\n        public static List<Integer> getPackageSerials(int itemId) {",
)

# Validate every hook before writing any part of the installation.
financial_hook(
    "scripts/npc/1022101_old.js",
    "var choice;",
    "var choice;\nvar cardExchangeKey = Java.type('java.util.UUID').randomUUID().toString();",
)
financial_hook(
    "scripts/npc/1022101_old.js",
    "                // Leaf for Cash\n                if (useVP) {",
    "                // Leaf for Cash\n                const CardExchangeBridge = Java.type('server.cardbridge.BridgeExchanges');\n                if (Java.type('server.cardbridge.BridgeHttp').enabled()) {\n                    if (CardExchangeBridge.exchange(cm.getClient(), useVP, itemToUse, nxAmount, cardExchangeKey)) {\n                        cm.sendOk('Your exchange has been completed.');\n                    } else {\n                        cm.sendOk('Your exchange is unavailable or awaiting save confirmation. Please check your balance and inventory.');\n                    }\n                    cm.dispose();\n                    return;\n                }\n                if (useVP) {",
)
if not args.check:
    for file, text in proposed.items():
        file.write_text(text, encoding="utf-8", newline="\n")
    for file in source.rglob("*"):
        if file.is_file():
            dst = base / file.relative_to(source)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dst)
print(
    "Adapter hooks validated."
    if args.check
    else "Adapter source, resources and hooks installed."
)
