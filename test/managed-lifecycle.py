#!/usr/bin/env python3
"""Qualify a disposable managed deployment using native identity and real Docker data."""
import argparse
import base64
import copy
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location(
    "managed", Path(__file__).resolve().parents[1] / "tools/cosmic.py"
)
managed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(managed)


class Api:
    def __init__(self, origin):
        self.origin = origin
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, route, body=None, expected=200):
        headers = {
            "Origin": self.origin,
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrf,
        }
        request = urllib.request.Request(
            self.origin + "/api/library/" + route,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        assert response.status == expected, (
            "Unexpected HTTP status for " + route + ": " + str(response.status)
        )
        value = json.loads(response.read())
        self.csrf = value.get("csrf", self.csrf)
        return value

    def login(self, username, password):
        session = self.call("session")
        if session.get("signedIn"):
            self.call("logout", {})
            self.call("session")
        self.call("login", {"username": username, "password": password})


def publish(api, change):
    catalog = api.call("admin/catalog")["catalog"]
    change(catalog)
    preview = api.call("admin/preview", {"catalog": catalog})
    assert not preview.get("errors"), "Catalog preview errors"
    result = api.call(
        "admin/publish",
        {
            **{
                k: preview[k]
                for k in [
                    "previewId",
                    "digest",
                    "expectedVersion",
                    "policyRevision",
                    "adminRevision",
                ]
            },
            "idempotencyKey": "qualification-" + secrets.token_hex(12),
        },
    )
    return result


def state_summary(api):
    state = api.call("state")
    return {
        "wallet": state["wallet"]["balances"],
        "packs": sorted(p["id"] for p in state["packs"]),
    }


def purchase(api, product_id, cash_type, identity):
    quote = api.call(
        "quote", {"productId": product_id, "quantity": 1, "cashType": cash_type}
    )
    result = api.call("buy", {**quote, "key": identity})
    assert result == api.call(
        "buy", {**quote, "key": identity}
    ), "Purchase retry changed its receipt"
    return result


def docker_command(directory, args, timeout=25):
    result = subprocess.run(
        ["docker", *args],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, "Bounded Docker qualification command failed"
    return result.stdout.strip()


def native_sql(directory, query):
    return docker_command(
        directory,
        [
            "compose",
            "-f",
            str(directory / "compose.json"),
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N cosmic -e "$1"',
            "qualification",
            query,
        ],
    )


def runtime_identity(directory, service):
    identity = docker_command(
        directory,
        ["compose", "-f", str(directory / "compose.json"), "ps", "-aq", service],
    )
    assert identity and "\n" not in identity, "Expected one managed service container"
    item = json.loads(docker_command(directory, ["inspect", identity]))[0]
    return {
        "id": item["Id"],
        "image": item["Image"],
        "restartCount": item["RestartCount"],
        "startedAt": item["State"]["StartedAt"],
        "state": item["State"]["Status"],
        "restartPolicy": item["HostConfig"]["RestartPolicy"]["Name"],
    }


def durable_native_signature(directory):
    queries = [
        "SELECT id,nxCredit,maplePoint,nxPrepaid FROM accounts ORDER BY id;",
        "SELECT * FROM card_bridge_payments ORDER BY order_id;",
        "SELECT issuance_id,code_hash,account_id,item_id,quantity,pet_days,state,claim_session,claim_character,receipt_id,used_at FROM card_bridge_codes ORDER BY issuance_id;",
        "SELECT receipt_id,account_id,character_id,cash_type,amount,kind FROM card_bridge_native_purchases ORDER BY receipt_id;",
        "SELECT receipt_id,payload,delivered FROM card_bridge_outbox ORDER BY receipt_id;",
    ]
    return [
        hashlib.sha256(native_sql(directory, query).encode()).hexdigest()
        for query in queries
    ]


def require_stable_runtime(directory, meta):
    assert not (directory / "setup.pending.json").exists()
    assert not (directory / "recovery.pending.json").exists()
    managed.no_players(directory)
    managed.doctor(directory, meta)
    game = runtime_identity(directory, "cosmic")
    assert game["state"] == "running" and game["restartPolicy"] == "unless-stopped"
    return game


def require_supervised_recovery(directory, meta, before, fault_at):
    deadline = time.monotonic() + 240
    while True:
        try:
            managed.doctor(directory, meta)
            after = runtime_identity(directory, "cosmic")
            if after["restartCount"] > before["restartCount"]:
                break
        except (RuntimeError, AssertionError):
            pass
        assert (
            time.monotonic() < deadline
        ), "Lease loss did not recover under supervision"
        time.sleep(5)
    assert after["id"] == before["id"] and after["image"] == before["image"]
    assert after["startedAt"] != before["startedAt"] and after["state"] == "running"
    events = docker_command(
        directory,
        [
            "events",
            "--since",
            str(fault_at),
            "--until",
            str(int(time.time())),
            "--filter",
            "container=" + before["id"],
            "--filter",
            "event=die",
            "--format",
            "{{json .}}",
        ],
    )
    assert any(
        event.get("time", 0) >= fault_at
        and event.get("Actor", {}).get("ID") == before["id"]
        and event.get("Actor", {}).get("Attributes", {}).get("exitCode") == "75"
        for event in (json.loads(line) for line in events.splitlines())
    ), "Actual lease-loss exit 75 was not observed"


def qualify_native_runtime(directory, meta):
    original = require_stable_runtime(directory, meta)
    durable = durable_native_signature(directory)
    logins = native_sql(directory, "SELECT id,loggedin FROM accounts ORDER BY id;")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(directory / "compose.json"),
            "exec",
            "-T",
            "cosmic",
            "timeout",
            "--kill-after=5s",
            "35s",
            "java",
            "-Xms64m",
            "-Xmx256m",
            "-jar",
            "Server.jar",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode not in [0, 124], "Second writer was not rejected promptly"
    assert (
        "WRITER_LEASE_UNAVAILABLE" in result.stderr
    ), "Writer lease admission was not established"
    assert (
        native_sql(directory, "SELECT id,loggedin FROM accounts ORDER BY id;") == logins
    )
    assert (
        runtime_identity(directory, "cosmic") == original
    ), "Second writer changed the supervised game"
    managed.doctor(directory, meta)
    assert durable_native_signature(directory) == durable

    before = require_stable_runtime(directory, meta)
    pid = native_sql(
        directory,
        "SELECT ID FROM information_schema.PROCESSLIST WHERE ID=IS_USED_LOCK('card_bridge_writer') AND USER='cosmic_app' AND DB='cosmic';",
    )
    assert (
        pid.isdecimal() and int(pid) > 0
    ), "Exact managed writer connection not identified"
    fault_at = int(time.time())
    native_sql(directory, "KILL CONNECTION " + pid)
    require_supervised_recovery(directory, meta, before, fault_at)
    assert (
        durable_native_signature(directory) == durable
    ), "Writer lease recovery changed durable obligations"

    before = require_stable_runtime(directory, meta)
    db = runtime_identity(directory, "db")
    fault_at = int(time.time())
    docker_command(directory, ["restart", "--time", "10", db["id"]], timeout=30)
    require_supervised_recovery(directory, meta, before, fault_at)
    restored_db = runtime_identity(directory, "db")
    assert restored_db["id"] == db["id"] and restored_db["image"] == db["image"]
    assert (
        durable_native_signature(directory) == durable
    ), "Database recovery changed durable obligations"
    print(
        "Passed: rejected second writer and actual exit-75 supervised lease/database recovery retain native obligations"
    )


def qualify(directory, sandbox, expected_lines=None):
    directory, meta = managed.load(directory)
    sandbox = Path(sandbox).resolve()
    assert (
        directory.is_relative_to(sandbox) and directory != sandbox
    ), "Only disposable sandbox installations are accepted"
    assert (
        directory.name == "CosmicCardServer"
    ), "Unexpected qualification installation name"
    assert (
        meta["origin"] == "http://127.0.0.1:8490"
    ), "Qualification requires the local default origin"
    managed.compose(
        directory,
        "exec",
        "-T",
        "cosmic",
        "sh",
        "-c",
        'test "$(id -u)" = 10001 && test -r Server.jar && test -r LICENSE && test -r /opt/card-tools/AccountCommand.class && test -z "$(find wz scripts \\( -type f ! -readable -o -type d \\( ! -readable -o ! -executable \\) \\) -print -quit)"',
    )
    managed.compose(
        directory,
        "exec",
        "-T",
        "cards",
        "node",
        "--input-type=module",
        "-e",
        "import{accessSync,constants}from'node:fs';if(process.getuid()!==10002)throw Error('EXPECTED_UNPRIVILEGED_USER');for(const p of ['src/server.mjs','starter/index.html','data/series-one.json','tools/doctor.mjs','tools/admin.mjs','tools/rewards.mjs'])accessSync(p,constants.R_OK)",
    )
    print(
        "Passed: unprivileged game/card users can read all public runtime inputs after private-umask installation"
    )
    account = json.loads((directory / "private/test-account.json").read_text())
    password = account["password"]
    for name in ["SiteOwner", "SecondAdmin", "Collector"]:
        managed.account_command(
            directory, {"action": "create", "username": name, "password": password}
        )
    managed.admin_command(directory, "grant", "SiteOwner", capture=True)
    first = managed.admin_command(directory, "grant", "SecondAdmin", capture=True)
    assert (
        managed.admin_command(directory, "grant", "SecondAdmin", capture=True)[
            "changed"
        ]
        is False
    )
    owner, second, collector, smoke_user = [Api(meta["origin"]) for _ in range(4)]
    for api, name in [
        (owner, "SiteOwner"),
        (second, "SecondAdmin"),
        (collector, "Collector"),
        (smoke_user, account["username"]),
    ]:
        api.login(name, password)
    collector.call("admin/catalog", expected=403)
    second.call("admin/catalog")
    managed.admin_command(directory, "revoke", "SecondAdmin", capture=True)
    second.call("admin/catalog", expected=403)
    managed.admin_command(
        directory, "revoke", account_id=first["accountId"], capture=True
    )
    if expected_lines is not None:
        active = owner.call("admin/catalog")["catalog"]
        seeded = json.loads((directory / "private/catalog.json").read_text())
        assert (
            len(active["lines"]) == expected_lines
        ), "Custom catalog lines were not activated"
        assert {line["id"] for line in active["lines"]} == {
            line["id"] for line in seeded["lines"]
        }, "Active catalog replaced the supplied collection identities"
        assert {product["id"] for product in active["products"]} == {
            product["id"] for product in seeded["products"]
        }, "Active catalog replaced the supplied pack identities"
    original_image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    (managed.ensure_media(directory) / "original.png").write_bytes(original_image)

    def first_publication(catalog):
        catalog["products"][0]["price"]["amount"] = 1002
        catalog["cards"][0].setdefault("metadata", {})[
            "image"
        ] = "/assets/library/original.png"

    publish(owner, first_publication)
    baseline_catalog = owner.call("admin/catalog")["catalog"]
    product = baseline_catalog["products"][0]["id"]
    collector_receipts = []
    for cash_type in [1, 2, 4]:
        quote = collector.call(
            "quote", {"productId": product, "quantity": 1, "cashType": cash_type}
        )
        denied = collector.call(
            "buy",
            {**quote, "key": "qualification-empty-" + str(cash_type)},
            expected=409,
        )
        assert (
            denied["code"] == "INSUFFICIENT_FUNDS"
        ), "Selected empty wallet was not rejected"
        managed.account_command(
            directory,
            {
                "action": "fund",
                "username": "Collector",
                "cashType": cash_type,
                "amount": 5000,
                "key": "qualification-fund-" + str(cash_type),
            },
        )
        result = purchase(
            collector, product, cash_type, "qualification-buy-" + str(cash_type)
        )
        opening = {
            "packId": result["packs"][0]["id"],
            "key": "qualification-open-" + str(cash_type),
        }
        collector_receipts.append((opening, collector.call("open", opening)))
    extra = purchase(smoke_user, product, 4, "qualification-extra-unopened")
    prior_smoke = state_summary(smoke_user)
    managed.smoke(directory, meta)
    assert (
        state_summary(smoke_user) == prior_smoke
    ), "Smoke changed an unrelated purchase or wallet"
    assert extra["packs"][0]["id"] in prior_smoke["packs"], "Extra pack disappeared"
    print(
        "Passed: explicit owners, revocation, live publication, three selected wallets and isolated smoke"
    )

    baseline = state_summary(collector)
    backup = managed.backup(directory, meta)
    managed.compose(
        directory,
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u cosmic_app cosmic -e "CREATE TABLE card_bridge_restore_probe(id INTEGER PRIMARY KEY); INSERT INTO card_bridge_restore_probe VALUES(1)"',
    )
    managed.account_command(
        directory,
        {
            "action": "fund",
            "username": "Collector",
            "cashType": 1,
            "amount": 111,
            "key": "qualification-restore-mutation",
        },
    )
    publish(owner, lambda catalog: catalog["products"][0]["price"].update(amount=1005))
    assert state_summary(collector) != baseline, "Restore fixture did not mutate"
    managed.restore(directory, meta, backup)
    tables = managed.compose(
        directory,
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u cosmic_app cosmic -N -e "SHOW TABLES LIKE \'card_bridge_restore_probe\'"',
        capture=True,
    ).strip()
    assert not tables, "Restore retained a table created after the backup"
    owner.login("SiteOwner", password)
    collector.login("Collector", password)
    assert (
        state_summary(collector) == baseline
    ), "Restore did not reverse the actual wallet mutation"
    assert (
        owner.call("admin/catalog")["catalog"] == baseline_catalog
    ), "Restore did not reverse active catalog mutation"
    print(
        "Passed: stopped-writer backup restored an actual wallet and catalog mutation"
    )

    original_doctor, activation_checks = managed.doctor, 0

    def fail_after_ingress(path, saved):
        nonlocal activation_checks
        original_doctor(path, saved)
        activation_checks += 1
        if activation_checks == 2:
            managed.account_command(
                path,
                {
                    "action": "fund",
                    "username": "Collector",
                    "cashType": 1,
                    "amount": 222,
                    "key": "qualification-late-activation",
                },
            )
            raise RuntimeError(
                "Injected failure after ingress and a durable wallet mutation"
            )

    managed.doctor = fail_after_ingress
    try:
        try:
            managed.restore(directory, meta, backup)
            raise AssertionError("Injected late activation failure was not reported")
        except RuntimeError as error:
            assert "current data preserved" in str(
                error
            ), "Restore rewound data after ingress opened"
    finally:
        managed.doctor = original_doctor
    managed.start(directory, meta)
    collector.login("Collector", password)
    expected_balances = copy.deepcopy(baseline["wallet"])
    for balance in expected_balances:
        if balance["cashType"] == 1:
            balance["amount"] += 222
    assert (
        collector.call("state")["wallet"]["balances"] == expected_balances
    ), "Late activation lost a post-restore wallet mutation"
    print(
        "Passed: late restore activation failure preserves a real post-exposure MySQL transaction"
    )

    qualify_native_runtime(directory, meta)
    owner.login("SiteOwner", password)
    collector.login("Collector", password)
    result = purchase(collector, product, 1, "qualification-after-mysql-restart")
    collector.call(
        "open",
        {"packId": result["packs"][0]["id"], "key": "qualification-open-after-mysql"},
    )
    managed.upgrade(directory, meta)
    directory, meta = managed.load(directory)
    owner.login("SiteOwner", password)
    assert owner.call("admin/catalog")["catalog"] == baseline_catalog
    assert managed.admin_command(directory, "list", capture=True)[
        "items"
    ], "Upgrade lost administrator grants"
    managed.smoke(directory, meta)
    print(
        "Passed: real MySQL restart, native login/purchase recovery and successful upgrade"
    )

    candidate = sandbox / "FailedCandidateBridge"
    assert not candidate.exists(), "Candidate test directory must be empty"
    shutil.copytree(
        managed.ROOT,
        candidate,
        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
    )
    source = candidate / "src/server.mjs"
    source.write_text("process.exit(42);\n" + source.read_text())
    previous_root, previous_templates, previous_compose = (
        managed.ROOT,
        managed.TEMPLATES,
        managed.compose,
    )

    def short_wait(path, *args, **kwargs):
        return previous_compose(
            path, *["60" if a == "900" else a for a in args], **kwargs
        )

    managed.ROOT, managed.TEMPLATES, managed.compose = (
        candidate,
        candidate / "deployment/cosmic",
        short_wait,
    )
    try:
        try:
            managed.upgrade(directory, meta)
            raise AssertionError("Failed candidate unexpectedly became ready")
        except RuntimeError as error:
            assert "previous images, keys and data were restored" in str(
                error
            ), "Candidate failure did not confirm rollback"
    finally:
        managed.ROOT, managed.TEMPLATES, managed.compose = (
            previous_root,
            previous_templates,
            previous_compose,
        )
    managed.doctor(directory, meta)
    owner.login("SiteOwner", password)
    assert owner.call("admin/catalog")["catalog"] == baseline_catalog
    print(
        "Passed: real failing candidate startup restored previous images and active catalog"
    )

    collector.login("Collector", password)
    cold_summary = state_summary(collector)
    cold_backup = managed.backup(directory, meta)
    offhost = sandbox / "OffHostCardBackup"
    assert not offhost.exists(), "Off-host fixture directory must be empty"
    shutil.move(str(cold_backup), offhost)
    managed.no_players(directory)
    managed.compose(directory, "down", "--volumes", "--remove-orphans")
    assert (
        directory.resolve().is_relative_to(sandbox)
        and directory.name == "CosmicCardServer"
    )
    shutil.rmtree(directory)
    recovered = sandbox / "RecoveredCardServer"
    original_doctor, cold_checks = managed.doctor, 0

    def fail_cold_activation(path, saved):
        nonlocal cold_checks
        original_doctor(path, saved)
        cold_checks += 1
        if cold_checks == 2:
            managed.account_command(
                path,
                {
                    "action": "fund",
                    "username": "Collector",
                    "cashType": 4,
                    "amount": 333,
                    "key": "qualification-cold-activation",
                },
            )
            raise RuntimeError(
                "Injected cold recovery activation failure after a durable wallet mutation"
            )

    managed.doctor = fail_cold_activation
    try:
        try:
            managed.recover_empty(recovered, offhost, meta["project"], relocate=True)
            raise AssertionError("Injected cold activation failure was not reported")
        except RuntimeError as error:
            assert "services were stopped" in str(
                error
            ), "Cold recovery did not stop after activation failed"
    finally:
        managed.doctor = original_doctor
    assert (
        json.loads((recovered / "recovery.pending.json").read_text())["phase"]
        == "activating"
    )
    try:
        managed.start(recovered, managed.load(recovered)[1])
        raise AssertionError("Ordinary start bypassed the incomplete recovery marker")
    except RuntimeError as error:
        assert "incomplete" in str(error)
    managed.recover_empty(
        recovered, offhost, meta["project"], relocate=True, retry=True
    )
    for balance in cold_summary["wallet"]:
        if balance["cashType"] == 4:
            balance["amount"] += 333
    collector.login("Collector", password)
    owner.login("SiteOwner", password)
    assert (
        state_summary(collector) == cold_summary
    ), "Cold restore changed wallet/pack ownership"
    assert (
        owner.call("admin/catalog")["catalog"] == baseline_catalog
    ), "Cold restore lost active catalog/admin grant"
    for opening, expected in collector_receipts:
        assert (
            collector.call("open", opening) == expected
        ), "Cold restore changed previously opened card identities"
    with urllib.request.urlopen(
        meta["origin"] + "/assets/library/original.png", timeout=10
    ) as image:
        assert image.read() == original_image, "Cold restore lost original static media"
    managed.smoke(recovered, managed.load(recovered)[1])
    purchase(collector, product, 4, "qualification-after-cold-restore")
    print(
        "Passed: backup-only empty-target recovery preserved owners/catalog/wallets/cards and accepts a new purchase"
    )
    recovered, current_meta = managed.load(recovered)
    prior_catalog = owner.call("admin/catalog")["catalog"]
    managed.enable_series_one(recovered, current_meta, product)
    owner.login("SiteOwner", password)
    collector.login("Collector", password)
    reward_catalog = owner.call("admin/catalog")["catalog"]
    selected_pack = next(p for p in reward_catalog["products"] if p["id"] == product)
    inserts = [s for s in selected_pack["slots"] if s["id"] == "series-one-insert"]
    assert len(inserts) == 1 and inserts[0]["count"] == 1
    for prior in prior_catalog["products"]:
        if prior["id"] != product:
            assert prior == next(
                p for p in reward_catalog["products"] if p["id"] == prior["id"]
            ), "Reward setup changed an unselected pack"
    managed.enable_series_one(recovered, managed.load(recovered)[1], product)
    owner.login("SiteOwner", password)
    collector.login("Collector", password)
    assert (
        owner.call("admin/catalog")["catalog"] == reward_catalog
    ), "Reward setup retry added another insert or catalog version"
    issued = purchase(collector, product, 1, "qualification-reward-opt-in")
    opened = collector.call(
        "open",
        {"packId": issued["packs"][0]["id"], "key": "qualification-open-reward-opt-in"},
    )
    code_copies = [
        c["id"] for c in opened["cards"] if c["definition"]["type"] == "code"
    ]
    assert (
        len(code_copies) == 1
    ), "Reward opt-in did not issue exactly one dedicated code card"
    own_codes, after = [], ""
    while True:
        page = collector.call("codes", {"after": after, "limit": 50})
        own_codes.extend(c for c in page["items"] if c["copyId"] in code_copies)
        after = page.get("next")
        if not after:
            break
    assert (
        len(own_codes) == 1 and own_codes[0]["registration"] == "ready"
    ), "Reward opt-in did not register the account-bound code"
    revealed = collector.call(
        "reveal",
        {"codeId": own_codes[0]["id"], "key": "qualification-reveal-reward-opt-in"},
    )
    assert len(revealed["code"].replace("-", "").replace(" ", "")) in [15, 18]
    for opening, expected in collector_receipts:
        assert (
            collector.call("open", opening) == expected
        ), "Reward opt-in changed previously issued cards"
    print(
        "Passed: stopped-writer reward opt-in is idempotent and future selected packs issue one registered code"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--sandbox-root", required=True)
    parser.add_argument("--expected-lines", type=int, choices=[1, 2])
    arguments = parser.parse_args()
    qualify(arguments.directory, arguments.sandbox_root, arguments.expected_lines)
