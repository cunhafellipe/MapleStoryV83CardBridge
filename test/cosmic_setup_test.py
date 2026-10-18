import importlib.util
import io
import json
import os
import sqlite3
import stat
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from contextlib import closing

spec = importlib.util.spec_from_file_location(
    "cosmic_setup", Path(__file__).parents[1] / "tools/cosmic.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupSafety(unittest.TestCase):
    def state_pair(self, root, normalized):
        framework = sqlite3.connect(root / "framework.sqlite")
        framework.execute("PRAGMA journal_mode=WAL")
        if normalized:
            storage_version = normalized if type(normalized) is int else 2
            framework.executescript(setup.normalized_schema_sql(storage_version))
            marker = (
                json.dumps(
                    {
                        "format": "digital-card.indexed-state",
                        "version": storage_version,
                        "recordKeys": "blind-v1",
                    }
                )
                if storage_version >= 3
                else "opaque-fixture"
            )
            framework.execute(
                "INSERT INTO framework_meta VALUES(1,?,?)", (storage_version, marker)
            )
            framework.execute("PRAGMA user_version=" + str(storage_version))
        else:
            framework.executescript(
                "CREATE TABLE framework_state(id INTEGER PRIMARY KEY,schema_version INTEGER NOT NULL,body TEXT NOT NULL); INSERT INTO framework_state VALUES(1,1,'opaque-fixture');"
            )
        framework.commit()
        framework.close()
        bridge = sqlite3.connect(root / "bridge.sqlite")
        bridge.executescript(
            "PRAGMA journal_mode=WAL; CREATE TABLE orders(id TEXT PRIMARY KEY,state TEXT,body TEXT); CREATE TABLE registrations(code_id TEXT PRIMARY KEY);"
        )
        if normalized:
            bridge.executescript(
                "CREATE TABLE payment_discovery(id TEXT PRIMARY KEY,source TEXT,first_seen TEXT,checked_at TEXT); PRAGMA user_version=2;"
            )
        bridge.close()

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_recovery_copy_probe_accepts_known_schemas_preserves_wal_and_rejects_future(
        self,
    ):
        for normalized in [False, True, 3, 4, 5, 6, 7, 8]:
            with self.subTest(
                normalized=normalized
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state, work = root / "state", root / "work"
                state.mkdir()
                work.mkdir()
                self.state_pair(state, normalized)

                def probe(ok):
                    before = {p.name: setup.file_hash(p) for p in state.iterdir()}
                    result = setup.subprocess.run(
                        [
                            setup.shutil.which("node"),
                            "--input-type=module",
                            "-e",
                            setup.recovery_state_probe_script(),
                            str(state),
                            str(work),
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, ok, result.stderr)
                    self.assertEqual(
                        before, {p.name: setup.file_hash(p) for p in state.iterdir()}
                    )
                    self.assertEqual(list(work.iterdir()), [])

                for name in ["framework.sqlite", "bridge.sqlite"]:
                    with closing(sqlite3.connect(state / name)) as con:
                        setup.validate_database_schema(con, name == "framework.sqlite")
                probe(True)
                writer = sqlite3.connect(state / "bridge.sqlite")
                try:
                    writer.execute("PRAGMA wal_autocheckpoint=0")
                    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    writer.execute("PRAGMA user_version=3")
                    writer.commit()
                    self.assertGreater(
                        Path(str(state / "bridge.sqlite") + "-wal").stat().st_size, 0
                    )
                    with self.assertRaisesRegex(RuntimeError, "unsupported"):
                        setup.validate_database_schema(writer, False)
                    probe(False)
                finally:
                    writer.close()

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_legacy_schema_rejects_extra_indexes_and_mistyped_columns(self):
        for mutation in [
            "CREATE INDEX framework_legacy_extra ON framework_state(schema_version)",
            "DROP TABLE framework_state; CREATE TABLE framework_state(id TEXT PRIMARY KEY,schema_version INTEGER,body TEXT); INSERT INTO framework_state VALUES('1',1,'opaque')",
        ]:
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state, work = root / "state", root / "work"
                state.mkdir()
                work.mkdir()
                self.state_pair(state, False)
                with closing(sqlite3.connect(state / "framework.sqlite")) as con:
                    con.executescript(mutation)
                    with self.assertRaises(RuntimeError):
                        setup.validate_database_schema(con, True)
                result = setup.subprocess.run(
                    [
                        setup.shutil.which("node"),
                        "--input-type=module",
                        "-e",
                        setup.recovery_state_probe_script(),
                        str(state),
                        str(work),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(work.iterdir()), [])

    def test_backup_schema_rejects_future_mixed_and_incomplete_normalized_state(self):
        for mutation in [
            "PRAGMA user_version=3",
            "UPDATE framework_meta SET schema_version=3",
            "CREATE TABLE framework_state(id INTEGER,schema_version INTEGER,body TEXT)",
            "DROP INDEX framework_owner",
            "UPDATE framework_meta SET codec_check=''",
            "DROP TABLE framework_entities",
        ]:
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.state_pair(root, True)
                with closing(sqlite3.connect(root / "framework.sqlite")) as con:
                    con.executescript(mutation)
                    with self.assertRaises((RuntimeError, sqlite3.Error)):
                        setup.validate_database_schema(con, True)

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_schema_three_rejects_holder_layout_foreign_keys_and_marker_tampering(self):
        mutations = [
            "PRAGMA user_version=4",
            "UPDATE framework_meta SET schema_version=2",
            "UPDATE framework_meta SET codec_check='{}'",
            "DROP INDEX framework_code_membership",
            "DROP INDEX framework_code_membership; CREATE INDEX framework_code_membership ON framework_code_holders(copy_id)",
            "DROP TABLE framework_code_holders; CREATE TABLE framework_code_holders(collection TEXT,code_id TEXT,holder_hash TEXT,copy_id TEXT)",
            "INSERT INTO framework_code_holders VALUES('codes','missing-code','blind-holder','copy-id')",
            "CREATE TABLE framework_extra(id INTEGER)",
            "CREATE TABLE framework_state(id INTEGER,schema_version INTEGER,body TEXT)",
        ]
        for mutation in mutations:
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state, work = root / "state", root / "work"
                state.mkdir()
                work.mkdir()
                self.state_pair(state, 3)
                with closing(sqlite3.connect(state / "framework.sqlite")) as con:
                    con.executescript(mutation)
                    with self.assertRaises((RuntimeError, sqlite3.Error)):
                        setup.validate_database_schema(con, True)
                before = {p.name: setup.file_hash(p) for p in state.iterdir()}
                result = setup.subprocess.run(
                    [
                        setup.shutil.which("node"),
                        "--input-type=module",
                        "-e",
                        setup.recovery_state_probe_script(),
                        str(state),
                        str(work),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    before, {p.name: setup.file_hash(p) for p in state.iterdir()}
                )
                self.assertEqual(list(work.iterdir()), [])

    def test_schema_eight_rejects_unknown_versions_indexes_and_work_relationships(self):
        for mutation in [
            "PRAGMA user_version=99",
            "UPDATE framework_meta SET schema_version=7",
            "DROP INDEX framework_code_prefix_expiry",
            "DROP INDEX framework_code_order_unique",
            "DROP INDEX framework_owner_card_state",
            "DROP INDEX framework_action_due",
            "DROP TABLE framework_expiration_work",
            "UPDATE framework_meta SET codec_check='{}'",
            "INSERT INTO framework_action_work VALUES('actionJobs','missing-job',1,0,NULL,1)",
        ]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.state_pair(root, 8)
                with closing(sqlite3.connect(root / "framework.sqlite")) as con:
                    setup.validate_database_schema(con, True)
                    con.executescript(mutation)
                    with self.assertRaises((RuntimeError, sqlite3.Error)):
                        setup.validate_database_schema(con, True)

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_current_encrypted_schema_authenticates_and_wrong_key_preserves_source(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, work = root / "state", root / "work"
            state.mkdir()
            work.mkdir()
            create = "import{SQLiteStore}from'@digital-card/framework/sqlite';import{Journal}from'./src/journal.mjs';const root=process.argv[1];const store=new SQLiteStore(root+'/framework.sqlite',{encryptionKey:Buffer.alloc(32,7)});if(store.diagnostics().storageSchema!==8)throw Error('EXPECTED_SCHEMA_8');store.close();const journal=new Journal(root+'/bridge.sqlite');journal.close();"
            result = setup.subprocess.run(
                [
                    setup.shutil.which("node"),
                    "--input-type=module",
                    "-e",
                    create,
                    str(state),
                ],
                cwd=setup.ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            before = {p.name: setup.file_hash(p) for p in state.iterdir()}
            inspected = work / "archive-framework.sqlite"
            setup.shutil.copyfile(state / "framework.sqlite", inspected)
            with closing(sqlite3.connect(inspected)) as con:
                setup.validate_database_schema(con, True)
            for member in work.iterdir():
                member.unlink()
            probe = setup.subprocess.run(
                [
                    setup.shutil.which("node"),
                    "--input-type=module",
                    "-e",
                    setup.recovery_state_probe_script(require_normalized=True),
                    str(state),
                    str(work),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            authenticate = "import{SQLiteStore}from'@digital-card/framework/sqlite';const path=process.argv[1];let accepted=false;try{new SQLiteStore(path,{encryptionKey:Buffer.alloc(32,9),readOnly:true});accepted=true}catch{}if(accepted)throw Error('WRONG_KEY_ACCEPTED');"
            result = setup.subprocess.run(
                [
                    setup.shutil.which("node"),
                    "--input-type=module",
                    "-e",
                    authenticate,
                    str(state / "framework.sqlite"),
                ],
                cwd=setup.ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                before, {p.name: setup.file_hash(p) for p in state.iterdir()}
            )
            self.assertEqual(list(work.iterdir()), [])
            authenticate_copy = "import{SQLiteStore}from'@digital-card/framework/sqlite';import{copyFileSync,mkdtempSync,rmSync}from'node:fs';import{join}from'node:path';const root=mkdtempSync(join(process.argv[2],'authenticate-'));try{copyFileSync(process.argv[1],join(root,'framework.sqlite'));const store=new SQLiteStore(join(root,'framework.sqlite'),{encryptionKey:Buffer.alloc(32,7),readOnly:true});store.close()}finally{rmSync(root,{recursive:true,force:true})}"
            result = setup.subprocess.run(
                [
                    setup.shutil.which("node"),
                    "--input-type=module",
                    "-e",
                    authenticate_copy,
                    str(state / "framework.sqlite"),
                    str(work),
                ],
                cwd=setup.ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                before, {p.name: setup.file_hash(p) for p in state.iterdir()}
            )
            self.assertEqual(list(work.iterdir()), [])

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_legacy_source_probe_rejects_normalized_or_new_financial_orders(self):
        for normalized, modern in [(False, False), (False, True), (True, False)]:
            with self.subTest(
                normalized=normalized, modern=modern
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state, work = root / "state", root / "work"
                state.mkdir()
                work.mkdir()
                self.state_pair(state, normalized)
                with closing(sqlite3.connect(state / "bridge.sqlite")) as con:
                    con.execute(
                        "INSERT INTO orders VALUES(?,?,?)",
                        (
                            "legacy",
                            "paid",
                            json.dumps(
                                {"financialVersion": 1} if modern else {"id": "legacy"}
                            ),
                        ),
                    )
                    con.commit()
                before = {p.name: setup.file_hash(p) for p in state.iterdir()}
                result = setup.subprocess.run(
                    [
                        setup.shutil.which("node"),
                        "--input-type=module",
                        "-e",
                        setup.recovery_state_probe_script(require_legacy=True),
                        str(state),
                        str(work),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode == 0, not normalized and not modern, result.stderr
                )
                self.assertEqual(
                    before, {p.name: setup.file_hash(p) for p in state.iterdir()}
                )
                self.assertEqual(list(work.iterdir()), [])

    def migration_fixture(self, root):
        installation, safety = root / "installation", root / "backup"
        installation.mkdir()
        original = self.backup_fixture(safety, installation)
        for name in [
            "compose.json",
            "installation.json",
            "images.lock.json",
            "private/cards.env",
            "private/game.env",
            "private/database.env",
            "private/config.yaml",
            "private/catalog.json",
        ]:
            setup.private_file(installation / name, (safety / name).read_bytes())
        for name, data in [
            ("private/cards.env", "ENABLE_SERIES_ONE_REWARDS=0\nASSET_ROOT=/assets\n"),
            ("private/catalog.json", json.dumps(setup.managed_catalog())),
        ]:
            setup.private_file(installation / name, data)
            setup.private_file(safety / name, data)
        self.checksum_fixture(safety)
        meta = {**original, "bridgeHash": "e" * 64, "seriesOneEnabled": False}
        locks = json.loads((safety / "images.lock.json").read_text())
        locks["node"] = "public.example/node@sha256:" + "f" * 64
        candidate = setup.create_compose(meta, locks)
        marker = {
            "format": 1,
            "phase": "staging",
            "oldMeta": original,
            "meta": meta,
            "candidateLocks": locks,
            "candidate": candidate,
            "backup": str(safety),
            "backupDigest": setup.file_hash(safety / "checksums.json"),
            "oldBinding": setup.migration_binding(installation),
            "runtimeImages": {
                candidate["services"][name]["image"]: "sha256:" + "d" * 64
                for name in ["cards", "cosmic"]
            },
        }
        values = setup.migration_staging_bytes(marker, safety)
        marker["newBinding"] = {
            name: setup.hashlib.sha256(value).hexdigest()
            for name, value in values.items()
        }
        setup.write_migration(installation, marker)
        return installation, original, meta, marker, safety

    def attribution_fixture(self, root):
        installation, original, meta, marker, safety = self.migration_fixture(root)
        retained = installation / "backups" / "original"
        retained.parent.mkdir()
        setup.shutil.move(str(safety), str(retained))
        marker.update(kind="legacy", backup=str(retained))
        setup.write_migration(installation, marker)
        marker = setup.stage_legacy_migration(installation, marker, retained)
        return installation, meta, marker, retained

    def test_managed_attribution_keeps_writers_stopped_and_uses_bound_readonly_inputs(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            installation, meta, marker, safety = self.attribution_fixture(
                Path(directory).resolve()
            )
            before = setup.migration_binding(installation)
            marker_hash = setup.file_hash(installation / "migration.pending.json")
            calls = []

            def compose(path, *args, **kwargs):
                calls.append(args)
                return "c" * 64 if args == ("ps", "-q", "network") else ""

            expected = {
                "orderId": "a" * 64,
                "decisionKey": "attribution-001",
                "state": "attributed",
                "evidenceFingerprint": "b" * 64,
            }
            with patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "no_players") as players, patch.object(
                setup, "compose", side_effect=compose
            ), patch.object(
                setup, "run", return_value=json.dumps(expected)
            ) as run, patch.object(
                setup, "start"
            ) as start, patch.object(
                setup, "finish_legacy_migration"
            ) as finish, patch.object(
                setup, "restore_data"
            ) as restore, patch(
                "sys.stdout", io.StringIO()
            ):
                result = setup.attribute_legacy_payment(
                    installation,
                    meta,
                    "a" * 64,
                    "attribution-001",
                    "maintenance-operator",
                    "Verify original retained account",
                )
                self.assertEqual(result, expected)
                self.assertEqual(players.call_count, 2)
                start.assert_not_called()
                finish.assert_not_called()
                restore.assert_not_called()
            command = run.call_args.args[0]
            for option, value in [
                ("--network", "container:" + "c" * 64),
                ("--user", "0:0"),
                ("--cap-drop", "ALL"),
                ("--cap-add", "DAC_READ_SEARCH"),
                ("--security-opt", "no-new-privileges"),
                ("--tmpfs", "/tmp:rw,noexec,nosuid,size=272m"),
                ("--env-file", str(installation / "private/cards.env")),
            ]:
                self.assertEqual(command[command.index(option) + 1], value)
            self.assertIn("--read-only", command)
            mounts = [
                command[i + 1] for i, value in enumerate(command) if value == "--mount"
            ]
            self.assertEqual(
                mounts,
                [
                    "type=bind,source="
                    + str(installation)
                    + ",target="
                    + str(installation)
                    + ",readonly",
                    "type=bind,source="
                    + str(safety)
                    + ",target="
                    + str(safety)
                    + ",readonly",
                    "type=volume,source="
                    + meta["project"]
                    + "_card_state,target=/opt/card/state,readonly",
                ],
            )
            tail = command[command.index("tools/payments.mjs") :]
            self.assertEqual(
                tail,
                [
                    "tools/payments.mjs",
                    "attribute-legacy",
                    "--deployment-directory",
                    str(installation),
                    "--order-id",
                    "a" * 64,
                    "--decision-key",
                    "attribution-001",
                    "--operator-id",
                    "maintenance-operator",
                    "--reason",
                    "Verify original retained account",
                    "--confirm-bridge-stopped",
                ],
            )
            self.assertTrue(run.call_args.kwargs["structured_error"])
            self.assertFalse(any(args[0] in ["up", "start"] for args in calls))
            self.assertEqual(before, setup.migration_binding(installation))
            self.assertEqual(
                marker_hash, setup.file_hash(installation / "migration.pending.json")
            )

    def test_managed_attribution_rejects_fences_and_unbound_ingress_before_stopping(
        self,
    ):
        cases = [
            "ordinary",
            "setup",
            "recovery",
            "staging",
            "activating",
            "open",
            "binding",
            "backup",
        ]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                installation, meta, marker, safety = self.attribution_fixture(
                    Path(directory).resolve()
                )
                if case in ["ordinary", "setup", "recovery"]:
                    setup.private_file(
                        installation
                        / {
                            "ordinary": "upgrade.pending.json",
                            "setup": "setup.pending.json",
                            "recovery": "recovery.pending.json",
                        }[case],
                        "{}",
                    )
                elif case in ["staging", "activating"]:
                    setup.write_migration(installation, marker, case)
                elif case == "open":
                    setup.private_file(
                        installation / "compose.json", json.dumps(marker["candidate"])
                    )
                elif case == "binding":
                    setup.private_file(installation / "private/cards.env", "changed")
                elif case == "backup":
                    setup.private_file(
                        safety / "database.sql", "corrupted retained snapshot"
                    )
                with patch.object(
                    setup, "migration_images", return_value=marker["runtimeImages"]
                ), patch.object(setup, "compose") as compose, patch.object(
                    setup, "run"
                ) as run:
                    with self.assertRaises(RuntimeError):
                        setup.attribute_legacy_payment(
                            installation,
                            meta,
                            "a" * 64,
                            "attribution-001",
                            "operator",
                            "Verify account",
                        )
                    compose.assert_not_called()
                    run.assert_not_called()

    def test_managed_attribution_rejects_invalid_terms_before_maintenance_and_live_writers(
        self,
    ):
        invalid = [
            ("A" * 64, "attribution-001", "operator", "Reason"),
            ("a" * 64, "short", "operator", "Reason"),
            ("a" * 64, "attribution-001", " ", "Reason"),
            ("a" * 64, "attribution-001", "operator", "bad\nreason"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            installation, meta, marker, safety = self.attribution_fixture(
                Path(directory).resolve()
            )
            with patch.object(setup, "compose") as compose, patch.object(
                setup, "run"
            ) as run:
                for terms in invalid:
                    with self.assertRaises(ValueError):
                        setup.attribute_legacy_payment(installation, meta, *terms)
                compose.assert_not_called()
                run.assert_not_called()

            def compose(path, *args, **kwargs):
                return (
                    "still-running"
                    if args == ("ps", "--status", "running", "-q", "cards")
                    else ""
                )

            with patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "no_players"), patch.object(
                setup, "compose", side_effect=compose
            ), patch.object(
                setup, "run"
            ) as run:
                with self.assertRaisesRegex(
                    RuntimeError, "writers must remain stopped"
                ):
                    setup.attribute_legacy_payment(
                        installation,
                        meta,
                        "a" * 64,
                        "attribution-001",
                        "operator",
                        "Reason",
                    )
                run.assert_not_called()

    def test_recovery_validation_failure_reports_safe_category_and_preserves_current_data(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup.private_file(root / "recovery.pending.json", '{"phase":"activating"}')
            setup.private_file(root / "current-receipt", "retained")
            meta = {"project": "cosmic-cards-fixture"}

            def run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    self.assertIn("--read-only", command)
                    self.assertIn("/tmp:rw,noexec,nosuid,size=272m", command)
                    self.assertTrue(
                        any(arg.endswith("/opt/card/state,readonly") for arg in command)
                    )
                    self.assertTrue(kwargs["structured_error"])
                    raise setup.CommandError(
                        "Suppressed internal diagnostic", "RECOVERY_BRIDGE_UNAVAILABLE"
                    )
                return "fixture"

            with patch.object(setup, "verify_backup_runtime_images"), patch.object(
                setup, "run", side_effect=run
            ), patch.object(setup, "compose"), patch.object(
                setup, "no_players"
            ), patch.object(
                setup,
                "create_compose",
                return_value={"services": {"cards": {"image": "fixture"}}},
            ), patch.object(
                setup, "activate_configuration"
            ) as activate:
                with self.assertRaisesRegex(
                    RuntimeError, "RECOVERY_BRIDGE_UNAVAILABLE"
                ) as error:
                    setup.resume_recovery_activation(root, meta, root, {})
                self.assertNotIn("Suppressed", str(error.exception))
                activate.assert_not_called()
            self.assertEqual((root / "current-receipt").read_text(), "retained")
            self.assertEqual(
                json.loads((root / "recovery.pending.json").read_text())["phase"],
                "activating",
            )

    def test_managed_attribution_failure_preserves_fence_and_exact_retry_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            installation, meta, marker, safety = self.attribution_fixture(
                Path(directory).resolve()
            )
            attempts = []

            def compose(path, *args, **kwargs):
                return "c" * 64 if args == ("ps", "-q", "network") else ""

            def run(command, **kwargs):
                attempts.append(command)
                if len(attempts) == 1:
                    raise setup.CommandError(
                        "Suppressed private diagnostic", "LEGACY_EVIDENCE_REJECTED"
                    )
                return json.dumps(
                    {
                        "orderId": "a" * 64,
                        "decisionKey": "attribution-001",
                        "state": "attributed",
                        "evidenceFingerprint": "b" * 64,
                    }
                )

            with patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "no_players"), patch.object(
                setup, "compose", side_effect=compose
            ), patch.object(
                setup, "run", side_effect=run
            ), patch.object(
                setup, "start"
            ) as start, patch(
                "sys.stdout", io.StringIO()
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "LEGACY_EVIDENCE_REJECTED"
                ) as error:
                    setup.attribute_legacy_payment(
                        installation,
                        meta,
                        "a" * 64,
                        "attribution-001",
                        "operator",
                        "Verify account",
                    )
                self.assertNotIn("Suppressed private", str(error.exception))
                self.assertTrue((installation / "migration.pending.json").exists())
                setup.attribute_legacy_payment(
                    installation,
                    meta,
                    "a" * 64,
                    "attribution-001",
                    "operator",
                    "Verify account",
                )
                start.assert_not_called()
            self.assertEqual(attempts[0], attempts[1])
            self.assertFalse(
                json.loads((installation / "compose.json").read_text())["services"][
                    "network"
                ]["ports"]
            )

    def test_migration_staging_retries_each_old_new_identity_without_losing_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            installation, original, meta, marker, safety = self.migration_fixture(root)
            atomic = setup.atomic_migration_file

            def fail_lock(path, data):
                if path.name == "images.lock.json":
                    raise RuntimeError("staging write unavailable")
                return atomic(path, data)

            with patch.object(setup, "atomic_migration_file", side_effect=fail_lock):
                with self.assertRaisesRegex(RuntimeError, "staging write"):
                    setup.stage_legacy_migration(installation, marker, safety)
            self.assertEqual(
                json.loads((installation / "installation.json").read_text()), meta
            )
            self.assertEqual(
                json.loads((installation / "migration.pending.json").read_text())[
                    "phase"
                ],
                "staging",
            )
            with self.assertRaisesRegex(RuntimeError, "remains closed"):
                setup.start(installation, meta)
            resumed = setup.stage_legacy_migration(installation, marker, safety)
            self.assertEqual(resumed["phase"], "migrating")
            self.assertEqual(resumed["binding"], marker["newBinding"])
            self.assertEqual(
                setup.migration_binding(installation), marker["newBinding"]
            )
            self.assertEqual(
                json.loads((installation / "compose.json").read_text())["services"][
                    "network"
                ]["ports"],
                [],
            )
            self.assertEqual(
                setup.file_hash(safety / "checksums.json"), marker["backupDigest"]
            )
            self.assertEqual(list(installation.rglob("*.migration-*")), [])

    def test_only_explicit_legacy_upgrade_admits_unresolved_backup_and_preserves_old_locks(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            installation, original, meta, marker, safety = self.migration_fixture(root)
            (installation / "migration.pending.json").unlink()
            setup.private_file(installation / "cosmic/pom.xml", "fixture")
            setup.private_file(
                installation / "cosmic/config.yaml",
                "DB_USER: app\nDB_PASS: fixture\nHOST: 127.0.0.1\nLANHOST: 127.0.0.1\nLOCALHOST: 127.0.0.1\n",
            )
            old_locks = (installation / "images.lock.json").read_bytes()
            with patch.object(setup, "no_players"), patch.object(
                setup, "compose", return_value="1"
            ), patch.object(setup, "backup") as backup, patch.object(
                setup, "bridge_snapshot"
            ) as snapshot:
                with self.assertRaisesRegex(RuntimeError, "Finish pending purchases"):
                    setup.upgrade(installation, original)
                backup.assert_not_called()
                snapshot.assert_not_called()

            def locks(path):
                setup.private_file(
                    path / "images.lock.json", json.dumps(marker["candidateLocks"])
                )
                return marker["candidateLocks"]

            def backup(path, saved, **kwargs):
                self.assertFalse(kwargs["require_resolved"])
                self.assertTrue(kwargs["require_legacy"])
                self.assertEqual((path / "images.lock.json").read_bytes(), old_locks)
                self.assertEqual(
                    json.loads((path / "compose.json").read_text()),
                    json.loads((safety / "compose.json").read_text()),
                )
                return safety

            with patch.object(setup, "no_players"), patch.object(
                setup, "compose", return_value="1"
            ), patch.object(setup, "run"), patch.object(
                setup, "bridge_snapshot", return_value="e" * 64
            ), patch.object(
                setup, "lock_images", side_effect=locks
            ), patch.object(
                setup, "backup", side_effect=backup
            ), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(
                setup,
                "finish_legacy_migration",
                side_effect=RuntimeError("candidate paused"),
            ):
                with self.assertRaisesRegex(RuntimeError, "candidate paused"):
                    setup.upgrade(installation, original, migrate_legacy=True)
            pending = json.loads((installation / "migration.pending.json").read_text())
            self.assertEqual(pending["phase"], "staging")
            self.assertEqual(pending["oldMeta"], original)
            self.assertEqual(
                json.loads((installation / "installation.json").read_text()), original
            )
            self.assertEqual(
                (installation / "images.lock.json").read_bytes(), old_locks
            )
            with self.assertRaisesRegex(RuntimeError, "remains closed"):
                setup.start(installation, original)

    def test_migration_compose_publication_is_atomic_after_phase_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            installation, original, meta, marker, safety = self.migration_fixture(root)
            marker = setup.stage_legacy_migration(installation, marker, safety)
            before = (installation / "compose.json").read_bytes()
            replace = setup.os.replace

            def fail_compose(source, destination):
                if Path(destination).name == "compose.json":
                    Path(source).write_text("{")
                    raise OSError("atomic publication unavailable")
                return replace(source, destination)

            with patch.object(setup, "no_players"), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup.os, "replace", side_effect=fail_compose):
                with self.assertRaisesRegex(OSError, "atomic publication"):
                    setup.finish_legacy_migration(installation, meta)
            self.assertEqual((installation / "compose.json").read_bytes(), before)
            self.assertEqual(
                json.loads((installation / "migration.pending.json").read_text())[
                    "phase"
                ],
                "validating",
            )
            self.assertEqual(list(installation.rglob("*.migration-*")), [])
            with self.assertRaisesRegex(RuntimeError, "remains closed"):
                setup.start(installation, meta)

    def test_migration_pending_and_late_activation_never_restore_current_data(self):
        for pending, late in [(True, False), (False, True)]:
            with self.subTest(
                pending=pending, late=late
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                installation, original, meta, marker, safety = self.migration_fixture(
                    root
                )
                observed = []

                def start(path, saved, **kwargs):
                    ports = json.loads((path / "compose.json").read_text())["services"][
                        "network"
                    ]["ports"]
                    observed.append(ports)
                    if ports and late:
                        (root / "post-exposure-receipt").write_text("retained")
                        raise RuntimeError("activation unavailable")

                with patch.object(setup, "no_players"), patch.object(
                    setup, "migration_images", return_value=marker["runtimeImages"]
                ), patch.object(
                    setup, "compose", return_value="namespace"
                ), patch.object(
                    setup, "recreate_namespace"
                ), patch.object(
                    setup, "stopped_state_probe", return_value='{"ok":true}'
                ), patch.object(
                    setup, "stopped_state_probe_after_stop", return_value="0"
                ), patch.object(
                    setup,
                    "payment_closure",
                    return_value={"ok": True, "clear": not pending},
                ), patch.object(
                    setup, "start", side_effect=start
                ), patch.object(
                    setup, "restore_data"
                ) as restore:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "current databases, identities and marker were preserved",
                    ):
                        setup.finish_legacy_migration(installation, original)
                    restore.assert_not_called()
                saved = json.loads(
                    (installation / "migration.pending.json").read_text()
                )
                self.assertEqual(saved["phase"], "activating" if late else "validating")
                with self.assertRaisesRegex(RuntimeError, "remains closed"):
                    setup.start(installation, meta)
                if late:
                    self.assertEqual(
                        (root / "post-exposure-receipt").read_text(), "retained"
                    )
                    self.assertEqual(observed[0], [])
                    self.assertTrue(observed[-1])
                    with patch.object(setup, "no_players"), patch.object(
                        setup, "migration_images", return_value=marker["runtimeImages"]
                    ), patch.object(
                        setup, "compose", return_value="namespace"
                    ), patch.object(
                        setup, "recreate_namespace"
                    ), patch.object(
                        setup, "stopped_state_probe", return_value='{"ok":true}'
                    ), patch.object(
                        setup,
                        "payment_closure",
                        return_value={"ok": True, "clear": True},
                    ), patch.object(
                        setup,
                        "start",
                        side_effect=RuntimeError("private validation unavailable"),
                    ), patch.object(
                        setup, "restore_data"
                    ) as restore:
                        with self.assertRaisesRegex(
                            RuntimeError, "marker were preserved"
                        ):
                            setup.finish_legacy_migration(installation, meta)
                        restore.assert_not_called()
                    self.assertEqual(
                        json.loads(
                            (installation / "migration.pending.json").read_text()
                        )["phase"],
                        "activating",
                    )
                    self.assertEqual(
                        (root / "post-exposure-receipt").read_text(), "retained"
                    )

    def test_unfinished_migration_backups_cannot_bypass_fence_through_restore_or_recover(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            installation, original, meta, marker, safety = self.migration_fixture(root)
            marker = setup.stage_legacy_migration(installation, marker, safety)
            current = root / "current-backup"
            self.backup_fixture(current, installation)
            for name in marker["binding"]:
                setup.private_file(current / name, (installation / name).read_bytes())
            setup.private_file(
                current / "compose.json", (installation / "compose.json").read_bytes()
            )
            setup.private_file(
                current / "runtime-images.json", json.dumps(marker["runtimeImages"])
            )
            setup.write_migration(current, marker)
            self.checksum_fixture(current)
            setup.validate_backup(current)
            target = root / "lost-installation"
            with self.assertRaisesRegex(
                RuntimeError, "Automatic recovery cannot discard"
            ):
                setup.recover_empty(target, current, meta["project"])
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(
                RuntimeError, "Automatic restore cannot discard"
            ):
                setup.restore(root / "other-installation", meta, current)
            self.assertFalse((root / "other-installation").exists())

    @unittest.skipUnless(
        setup.shutil.which("node"), "Requires Node.js with node:sqlite"
    )
    def test_pending_guard_preserves_original_checkpointed_and_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, work = root / "state", root / "work"
            state.mkdir()
            work.mkdir()
            path = state / "bridge.sqlite"
            database = sqlite3.connect(path)
            database.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE orders(state TEXT); INSERT INTO orders VALUES('complete'),('rejected'),('refunded');"
            )
            database.commit()
            database.close()

            def probe(expected):
                before = {p.name: setup.file_hash(p) for p in state.iterdir()}
                result = setup.subprocess.run(
                    [
                        setup.shutil.which("node"),
                        "--input-type=module",
                        "-e",
                        setup.pending_guard_script(),
                        str(state),
                        str(work),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))
                self.assertEqual(
                    before, {p.name: setup.file_hash(p) for p in state.iterdir()}
                )
                self.assertEqual(list(work.iterdir()), [])

            self.assertFalse(Path(str(path) + "-wal").exists())
            probe(0)
            database = sqlite3.connect(path)
            try:
                database.execute("PRAGMA wal_autocheckpoint=0")
                database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                database.executescript(
                    "CREATE TABLE payment_discovery(id TEXT PRIMARY KEY); PRAGMA user_version=2;"
                )
                database.execute("INSERT INTO payment_discovery VALUES('orphan')")
                database.commit()
                probe(1)
                database.execute("DELETE FROM payment_discovery")
                database.commit()
                probe(0)
                database.execute("INSERT INTO orders VALUES('pending')")
                database.commit()
                self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
                primary = root / "primary-only.sqlite"
                setup.shutil.copyfile(path, primary)
                baseline = sqlite3.connect(primary)
                try:
                    self.assertEqual(
                        baseline.execute(
                            "SELECT COUNT(*) FROM orders WHERE state='pending'"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    baseline.close()
                probe(1)
                for state_name in [
                    "prepared",
                    "fulfilled",
                    "refund_required",
                    "quarantined",
                    "legacy_reconciliation",
                ]:
                    database.execute("INSERT INTO orders VALUES(?)", (state_name,))
                database.commit()
                probe(6)
            finally:
                database.close()

    def test_service_image_resolves_runnable_id_from_configured_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.json").write_text(json.dumps({
                "services": {"cards": {"image": "fixture:cards"}}
            }))
            with patch.object(setup, "run", return_value="sha256:runnable\n") as run:
                self.assertEqual(setup.service_image_id(root, "cards"), "sha256:runnable")
                run.assert_called_once_with(
                    ["docker", "image", "inspect", "fixture:cards", "--format", "{{.Id}}"],
                    capture=True,
                )

    def test_backup_error_reports_checkpoint_and_restart_outcome_without_raw_error(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(setup, "no_players"), patch.object(
                setup, "archive_media"
            ), patch.object(
                setup, "service_image_id", return_value="fixture:image"
            ), patch.object(
                setup, "compose", return_value="fixture:image"
            ), patch.object(
                setup,
                "run",
                side_effect=setup.CommandError(
                    "private-value", "PENDING_STATE_UNAVAILABLE"
                ),
            ), patch.object(
                setup, "start"
            ) as start:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"stopped pending-purchase check \(PENDING_STATE_UNAVAILABLE\); previous services restarted",
                ) as error:
                    setup.backup(
                        root,
                        {"project": "fixture"},
                        restart=False,
                        require_resolved=True,
                    )
                start.assert_called_once()
                self.assertNotIn("private-value", str(error.exception))
            with patch.object(setup, "no_players"), patch.object(
                setup, "archive_media"
            ), patch.object(
                setup, "service_image_id", return_value="fixture:image"
            ), patch.object(
                setup, "compose", return_value="fixture:image"
            ), patch.object(
                setup, "run", side_effect=RuntimeError("private-value")
            ), patch.object(
                setup, "start", side_effect=RuntimeError("private-restart-value")
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "stopped pending-purchase check and service restart failed",
                ) as error:
                    setup.backup(root, {"project": "fixture"}, require_resolved=True)
                self.assertNotIn("private-value", str(error.exception))
                self.assertNotIn("private-restart-value", str(error.exception))

    @unittest.skipUnless(os.name == "posix", "Requires POSIX permission modes")
    def test_public_snapshot_remains_readable_under_private_operator_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = os.umask(0o077)
            try:
                setup.bridge_snapshot(root)
            finally:
                os.umask(previous)
            for path in [root / "bridge", *(root / "bridge").rglob("*")]:
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode), 0o755 if path.is_dir() else 0o644
                )
            for name in ["doctor.mjs", "admin.mjs", "rewards.mjs", "payments.mjs"]:
                self.assertTrue((root / "bridge/tools" / name).is_file())

    def test_structured_command_error_retains_only_bounded_semantic_code(self):
        for stderr, expected in [
            (
                'warning\n{"code":"INVALID_PRODUCTS","message":"private-value"}\n',
                "INVALID_PRODUCTS",
            ),
            ('{"code":"private-value"}', None),
            ('{"code":"REWARD_CONFLICT","message":"' + "x" * 2048 + '"}', None),
            ('{"code":"PENDING_PURCHASES"}\nnot JSON', None),
            ('["INVALID_PRODUCTS"]', None),
        ]:
            with patch.object(
                setup.subprocess,
                "run",
                return_value=Namespace(returncode=1, stderr=stderr, stdout=""),
            ):
                with self.assertRaises(setup.CommandError) as error:
                    setup.run(["docker", "run"], structured_error=True)
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn("private-value", str(error.exception))
        with patch.object(
            setup.subprocess,
            "run",
            return_value=Namespace(
                returncode=1, stderr='{"code":"REWARD_CONFLICT"}', stdout=""
            ),
        ):
            with self.assertRaises(setup.CommandError) as error:
                setup.run(["docker", "run"])
            self.assertIsNone(error.exception.code)

    def reward_fixture(self, root):
        meta = {"project": "cosmic-cards-0123456789", "seriesOneEnabled": False}
        profile = {
            "services": {
                "cards": {"image": "fixture:cards"},
                "network": {"ports": ["127.0.0.1:8490:8080"]},
            }
        }
        setup.private_file(root / "compose.json", json.dumps(profile))
        setup.private_file(
            root / "private/cards.env",
            "STATE_KEY=fixture-preserved\nENABLE_SERIES_ONE_REWARDS=0\n",
        )
        return meta, profile

    def test_reward_enable_requires_unique_ids_and_completed_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            meta, _ = self.reward_fixture(root)
            with patch.object(setup, "run") as run, patch.object(
                setup, "backup"
            ) as backup:
                for products in ["", "pack,pack", "pack, other", "../pack", "pack;"]:
                    with self.assertRaises(ValueError):
                        setup.enable_series_one(root, meta, products)
                for marker in ["setup.pending.json", "recovery.pending.json"]:
                    (root / marker).write_text("{}")
                    with self.assertRaisesRegex(
                        RuntimeError, "Complete installation or recovery"
                    ):
                        setup.enable_series_one(root, meta, "pack")
                    (root / marker).unlink()
                run.assert_not_called()
                backup.assert_not_called()

    def test_reward_enable_uses_stopped_backup_and_offline_unprivileged_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            meta, profile = self.reward_fixture(root)
            result = {"changed": True, "version": 2, "products": ["pack"]}
            with patch.object(
                setup, "run", side_effect=[None, json.dumps(result)]
            ) as run, patch.object(
                setup, "backup", return_value=root / "safety"
            ) as backup, patch.object(
                setup, "activate_configuration"
            ) as activate, patch.object(
                setup, "restore_data"
            ) as restore:
                setup.enable_series_one(root, meta, "pack")
                backup.assert_called_once_with(
                    root, meta, restart=False, require_resolved=True
                )
                self.assertIn(
                    "STATE_KEY=fixture-preserved\nENABLE_SERIES_ONE_REWARDS=1",
                    (root / "private/cards.env").read_text(),
                )
                command = run.call_args.args[0]
                self.assertEqual(
                    command[command.index("--tmpfs") + 1],
                    "/tmp:rw,noexec,nosuid,size=272m,mode=1777",
                )
                self.assertEqual(
                    command[:7],
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                    ],
                )
                self.assertEqual(
                    command[-5:],
                    [
                        "tools/rewards.mjs",
                        "enable-series-one",
                        "--products",
                        "pack",
                        "--confirm-stopped",
                    ],
                )
                self.assertIn("10002:10002", command)
                self.assertIn(
                    "type=volume,src=cosmic-cards-0123456789_card_state,dst=/opt/card/state",
                    command,
                )
                activate.assert_called_once_with(
                    root, {**meta, "seriesOneEnabled": True}, profile
                )
                restore.assert_not_called()

    def test_reward_enable_failure_restores_configuration_before_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            meta, _ = self.reward_fixture(root)
            before = (root / "private/cards.env").read_bytes()

            def rollback(path, saved, source):
                self.assertEqual(saved, meta)
                setup.private_file(path / "private/cards.env", before)

            with patch.object(
                setup,
                "run",
                side_effect=[
                    None,
                    setup.CommandError("tool failure", "REWARD_CONFLICT"),
                ],
            ), patch.object(
                setup, "backup", return_value=root / "safety"
            ), patch.object(
                setup, "restore_data", side_effect=rollback
            ) as restore, patch.object(
                setup, "activate_configuration"
            ) as activate:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"REWARD_CONFLICT\); previous provider configuration, catalog and data were restored",
                ):
                    setup.enable_series_one(root, meta, "pack")
                restore.assert_called_once_with(root, meta, root / "safety")
                activate.assert_not_called()
                self.assertEqual((root / "private/cards.env").read_bytes(), before)

    def test_reward_enable_activation_failure_preserves_current_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            meta, _ = self.reward_fixture(root)
            with patch.object(
                setup,
                "run",
                side_effect=[
                    None,
                    json.dumps({"changed": True, "version": 2, "products": ["pack"]}),
                ],
            ), patch.object(
                setup, "backup", return_value=root / "safety"
            ), patch.object(
                setup,
                "activate_configuration",
                side_effect=setup.ActivationError("current data preserved"),
            ), patch.object(
                setup, "restore_data"
            ) as restore:
                with self.assertRaisesRegex(
                    RuntimeError, "current data preserved.*Safety backup"
                ):
                    setup.enable_series_one(root, meta, "pack")
                restore.assert_not_called()
                self.assertIn(
                    "ENABLE_SERIES_ONE_REWARDS=1",
                    (root / "private/cards.env").read_text(),
                )
                self.assertTrue(
                    json.loads((root / "installation.json").read_text())[
                        "seriesOneEnabled"
                    ]
                )

    def test_failed_cold_recovery_retry_is_bound_to_backup_and_closed_ingress_phase(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, target = root / "backup", root / "recovered"
            meta = self.backup_fixture(source, root / "original")
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value=""
            ), patch.object(setup, "load_backup_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "restore_data", side_effect=RuntimeError("interrupted")
            ):
                with self.assertRaisesRegex(RuntimeError, "services were stopped"):
                    setup.recover_empty(target, source, meta["project"], True)
            marker = target / "recovery.pending.json"
            recovery = json.loads(marker.read_text())
            self.assertEqual(recovery["phase"], "prepared")
            marker.write_text(json.dumps({**recovery, "backupDigest": "0" * 64}))
            with self.assertRaisesRegex(RuntimeError, "exact recovery"):
                setup.recover_empty(target, source, meta["project"], True, True)
            marker.write_text(json.dumps({**recovery, "phase": "unknown"}))
            with self.assertRaisesRegex(RuntimeError, "phase is invalid"):
                setup.recover_empty(target, source, meta["project"], True, True)
            marker.write_text(json.dumps({**recovery, "phase": "validating"}))
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value="owned-resources"
            ), patch.object(setup, "load_backup_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "restore_data"
            ):
                setup.recover_empty(target, source, meta["project"], True, True)
            self.assertFalse(marker.exists())

    def test_activation_retry_preserves_current_data_and_never_downgrades_to_backup_import(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, target = root / "backup", root / "recovered"
            meta = self.backup_fixture(source, root / "original")
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value=""
            ), patch.object(setup, "load_backup_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "restore_data", side_effect=RuntimeError("interrupted")
            ):
                with self.assertRaises(RuntimeError):
                    setup.recover_empty(target, source, meta["project"], True)
            marker = target / "recovery.pending.json"
            recovery = json.loads(marker.read_text())
            marker.write_text(json.dumps({**recovery, "phase": "activating"}))
            preserved = target / "private/cards.env"
            preserved.write_text("current configuration after exposure")

            def fail_private_check(path, _, **kwargs):
                self.assertEqual(json.loads(marker.read_text())["phase"], "activating")
                raise RuntimeError("private check failed")

            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value="owned-resources"
            ), patch.object(setup, "verify_backup_runtime_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "no_players"
            ), patch.object(
                setup, "restore_data"
            ) as restore, patch.object(
                setup, "load_backup_images"
            ) as load_images, patch.object(
                setup, "start", side_effect=fail_private_check
            ):
                with self.assertRaisesRegex(RuntimeError, "current data.*preserved"):
                    setup.recover_empty(target, source, meta["project"], True, True)
                self.assertEqual(json.loads(marker.read_text())["phase"], "activating")
                self.assertEqual(
                    preserved.read_text(), "current configuration after exposure"
                )
                restore.assert_not_called()
                load_images.assert_not_called()
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value="owned-resources"
            ), patch.object(setup, "verify_backup_runtime_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "no_players"
            ), patch.object(
                setup, "restore_data"
            ) as restore, patch.object(
                setup, "load_backup_images"
            ) as load_images, patch.object(
                setup, "start"
            ):
                setup.recover_empty(target, source, meta["project"], True, True)
                restore.assert_not_called()
                load_images.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertEqual(
                preserved.read_text(), "current configuration after exposure"
            )

    def test_activation_failure_is_distinct_from_private_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = {"services": {"network": {"ports": ["127.0.0.1:8490:8080"]}}}
            seen = []

            def start(path, _, **kwargs):
                ports = json.loads((path / "compose.json").read_text())["services"][
                    "network"
                ]["ports"]
                seen.append(ports)
                if ports:
                    raise RuntimeError("late failure")

            with patch.object(setup, "recreate_namespace"), patch.object(
                setup, "compose"
            ), patch.object(setup, "start", side_effect=start):
                with self.assertRaises(setup.ActivationError):
                    setup.activate_configuration(root, {}, profile)
            self.assertEqual(seen, [[], profile["services"]["network"]["ports"]])

    def test_incomplete_recovery_cannot_be_started_and_empty_state_cannot_be_restored(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            setup.private_file(source / "recovery.pending.json", "{}")
            with patch.object(setup, "compose") as compose:
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    setup.start(source, {})
                compose.assert_not_called()
            (source / "recovery.pending.json").unlink()
            self.backup_fixture(source, source.parent / "original")
            with tarfile.open(source / "state.tar.gz", "w:gz"):
                pass
            self.checksum_fixture(source)
            with self.assertRaisesRegex(RuntimeError, "must contain"):
                setup.validate_backup(source)

    def test_restore_does_not_rollback_after_ingress_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, target = root / "backup", root / "original"
            meta = self.backup_fixture(source, target)
            with patch.object(
                setup, "backup", return_value=root / "safety"
            ), patch.object(
                setup,
                "restore_data",
                side_effect=setup.ActivationError("current data preserved"),
            ) as restore:
                with self.assertRaisesRegex(RuntimeError, "current data preserved"):
                    setup.restore(target, meta, source)
                self.assertEqual(restore.call_count, 1)

    def test_upgrade_validates_without_ingress_and_preserves_data_after_activation_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            installation, meta, candidate_meta, marker, source = self.migration_fixture(
                root
            )
            (installation / "migration.pending.json").unlink()
            setup.private_file(installation / "cosmic/pom.xml", "fixture")
            setup.private_file(
                installation / "cosmic/config.yaml",
                "DB_USER: app\nDB_PASS: fixture\nHOST: 127.0.0.1\nLANHOST: 127.0.0.1\nLOCALHOST: 127.0.0.1\n",
            )
            observed = []

            def start(path, _, **kwargs):
                ports = json.loads((path / "compose.json").read_text())["services"][
                    "network"
                ]["ports"]
                observed.append(ports)
                if ports:
                    raise RuntimeError("activation unavailable")

            locks = json.loads((source / "images.lock.json").read_text())
            with patch.object(setup, "no_players"), patch.object(
                setup, "compose", return_value="0"
            ), patch.object(setup, "run"), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(
                setup, "bridge_snapshot", return_value="e" * 64
            ), patch.object(
                setup, "lock_images", return_value=locks
            ), patch.object(
                setup, "backup", return_value=source
            ), patch.object(
                setup, "recreate_namespace"
            ), patch.object(
                setup, "start", side_effect=start
            ), patch.object(
                setup, "stopped_state_probe_after_stop", return_value="0"
            ), patch.object(
                setup, "payment_closure", return_value={"ok": True, "clear": True}
            ), patch.object(
                setup, "restore_data"
            ) as restore:
                with self.assertRaisesRegex(RuntimeError, "candidate data preserved"):
                    setup.upgrade(installation, meta)
                restore.assert_not_called()
            self.assertEqual(observed[0], [])
            self.assertTrue(observed[1])

    def test_ordinary_upgrade_atomic_interruptions_resume_without_discarding_data(self):
        for checkpoint in [
            "installation",
            "locks",
            "environment",
            "closed",
            "activation",
        ]:
            with self.subTest(
                checkpoint=checkpoint
            ), tempfile.TemporaryDirectory() as directory:
                installation, original, meta, marker, safety = self.migration_fixture(
                    Path(directory).resolve()
                )
                (installation / "migration.pending.json").unlink()
                setup.private_file(
                    installation / "private/cards.env", "STATE_KEY=fixture\n"
                )
                setup.private_file(safety / "private/cards.env", "STATE_KEY=fixture\n")
                self.checksum_fixture(safety)
                marker.update(
                    kind="upgrade",
                    oldBinding=setup.migration_binding(installation),
                    backupDigest=setup.file_hash(safety / "checksums.json"),
                )
                values = setup.migration_staging_bytes(marker, safety)
                marker["newBinding"] = {
                    name: setup.hashlib.sha256(value).hexdigest()
                    for name, value in values.items()
                }
                setup.write_migration(installation, marker)
                replace = setup.os.replace
                interrupted = []

                def fault(source, destination):
                    name = Path(destination).name
                    data = Path(source).read_bytes()
                    ports = (
                        json.loads(data)["services"]["network"]["ports"]
                        if name == "compose.json"
                        else None
                    )
                    selected = (
                        (checkpoint == "installation" and name == "installation.json")
                        or (checkpoint == "locks" and name == "images.lock.json")
                        or (checkpoint == "environment" and name == "cards.env")
                        or (checkpoint == "closed" and ports == [])
                        or (checkpoint == "activation" and bool(ports))
                    )
                    if selected and not interrupted:
                        interrupted.append(True)
                        Path(source).write_bytes(b"{")
                        raise KeyboardInterrupt("interrupted atomic replacement")
                    replace(source, destination)

                with patch.object(setup, "no_players"), patch.object(
                    setup, "migration_images", return_value=marker["runtimeImages"]
                ), patch.object(setup, "compose"), patch.object(
                    setup, "recreate_namespace"
                ), patch.object(
                    setup, "start"
                ), patch.object(
                    setup, "stopped_state_probe_after_stop", return_value="0"
                ), patch.object(
                    setup, "payment_closure", return_value={"ok": True, "clear": True}
                ), patch.object(
                    setup, "restore_data"
                ) as restore:
                    with patch.object(
                        setup.os, "replace", side_effect=fault
                    ), self.assertRaises(KeyboardInterrupt):
                        setup.finish_ordinary_upgrade(installation, original)
                    self.assertTrue(interrupted)
                    pending = json.loads(
                        (installation / "upgrade.pending.json").read_text()
                    )
                    self.assertIn(pending["phase"], ["staging", "activating"])
                    json.loads((installation / "installation.json").read_text())
                    json.loads((installation / "compose.json").read_text())
                    (installation / "durable-new-receipt").write_text("retain")
                    setup.finish_ordinary_upgrade(
                        installation,
                        json.loads((installation / "installation.json").read_text()),
                    )
                    restore.assert_not_called()
                self.assertFalse((installation / "upgrade.pending.json").exists())
                self.assertEqual(
                    (installation / "durable-new-receipt").read_text(), "retain"
                )
                self.assertEqual(list(installation.rglob("*.migration-*")), [])

    def test_ordinary_failed_rollback_keeps_durable_phase_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            installation, original, meta, marker, safety = self.migration_fixture(
                Path(directory).resolve()
            )
            (installation / "migration.pending.json").unlink()
            marker["kind"] = "upgrade"
            setup.write_migration(installation, marker)
            with patch.object(setup, "no_players"), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "compose"), patch.object(
                setup, "recreate_namespace"
            ), patch.object(
                setup,
                "start",
                side_effect=RuntimeError("private validation unavailable"),
            ), patch.object(
                setup, "restore_data", side_effect=RuntimeError("rollback unavailable")
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "rollback remains incomplete"
                ):
                    setup.finish_ordinary_upgrade(installation, original)
            self.assertEqual(
                json.loads((installation / "upgrade.pending.json").read_text())[
                    "phase"
                ],
                "rolling_back",
            )
            with self.assertRaisesRegex(RuntimeError, "finish-upgrade"):
                setup.start(installation, meta)
            with patch.object(setup, "no_players"), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "restore_data") as restore:
                setup.finish_ordinary_upgrade(installation, meta)
                restore.assert_called_once_with(
                    installation, original, safety, allow_upgrade=True
                )
            self.assertFalse((installation / "upgrade.pending.json").exists())

    def test_ordinary_rollback_activation_retry_never_reimports_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            installation, original, meta, marker, safety = self.migration_fixture(
                Path(directory).resolve()
            )
            (installation / "migration.pending.json").unlink()
            marker.update(kind="upgrade", phase="rollback_activating")
            setup.write_migration(installation, marker)
            (installation / "post-rollback-receipt").write_text("retain")
            with patch.object(setup, "no_players"), patch.object(
                setup, "migration_images", return_value=marker["runtimeImages"]
            ), patch.object(setup, "activate_configuration") as activate, patch.object(
                setup, "restore_data"
            ) as restore:
                setup.finish_ordinary_upgrade(installation, original)
                restore.assert_not_called()
                activate.assert_called_once()
            self.assertEqual(
                (installation / "post-rollback-receipt").read_text(), "retain"
            )
            self.assertFalse((installation / "upgrade.pending.json").exists())

    def test_original_static_media_survives_backup_restore_and_rejects_unsafe_members(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            installation, backup = base / "installation", base / "backup"
            installation.mkdir()
            backup.mkdir()
            media = setup.ensure_media(installation)
            image = setup.base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            (media / "original.png").write_bytes(image)
            setup.archive_media(installation, backup / "media.tar.gz")
            (media / "original.png").write_bytes(b"changed")
            (media / "later.png").write_bytes(image)
            setup.restore_media(installation, backup)
            self.assertEqual((media / "original.png").read_bytes(), image)
            self.assertFalse((media / "later.png").exists())
            with tarfile.open(backup / "media.tar.gz", "w:gz") as archive:
                info = tarfile.TarInfo("../outside.png")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(RuntimeError, "media archive"):
                setup.restore_media(installation, backup)
            self.assertEqual((media / "original.png").read_bytes(), image)

    def backup_fixture(self, source, original):
        meta = {
            "format": 1,
            "directory": str(original),
            "project": "cosmic-cards-0123456789",
            "bridgeHash": "a" * 64,
            "cosmicRevision": "b" * 40,
            "bind": "127.0.0.1",
            "webPort": 8490,
            "loginPort": 8484,
        }
        locks = {
            k: "public.example/" + k + "@sha256:" + "c" * 64
            for k in setup.RUNTIME["images"]
        }
        profile = setup.create_compose(meta, locks)
        for name, data in {
            "installation.json": meta,
            "images.lock.json": locks,
            "compose.json": profile,
            "runtime-images.json": {
                profile["services"][k]["image"]: "sha256:" + "d" * 64
                for k in ["cosmic", "cards"]
            },
        }.items():
            setup.private_file(source / name, json.dumps(data))
        for name in [
            "database.sql",
            "runtime-images.tar",
            "support/nginx.conf",
            "private/config.yaml",
            "private/database.env",
            "private/game.env",
            "private/cards.env",
            "private/catalog.json",
        ]:
            setup.private_file(source / name, "fixture")
        with tarfile.open(source / "state.tar.gz", "w:gz") as archive:
            for name in ["framework.sqlite", "bridge.sqlite", "smoke-receipt.json"]:
                if name.endswith(".sqlite"):
                    database = sqlite3.connect(":memory:")
                    if name == "framework.sqlite":
                        database.executescript(
                            "CREATE TABLE framework_state(id INTEGER PRIMARY KEY,schema_version INTEGER NOT NULL,body TEXT); INSERT INTO framework_state VALUES(1,1,'fixture');"
                        )
                    else:
                        database.executescript(
                            "CREATE TABLE orders(id INTEGER PRIMARY KEY); CREATE TABLE registrations(code_id TEXT PRIMARY KEY);"
                        )
                    data = database.serialize()
                    database.close()
                else:
                    data = b"{}"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.checksum_fixture(source)
        return meta

    def checksum_fixture(self, source):
        setup.private_file(
            source / "checksums.json",
            json.dumps(
                {
                    p.relative_to(source).as_posix(): setup.file_hash(p)
                    for p in source.rglob("*")
                    if p.is_file() and p.name != "checksums.json"
                }
            ),
        )

    def test_empty_recovery_rejects_identity_nonempty_and_corruption_before_writes(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base / "backup", base / "recovered"
            meta = self.backup_fixture(source, base / "original")
            with patch.object(setup, "run") as run, patch.object(
                setup, "prerequisites"
            ) as prerequisites:
                with self.assertRaisesRegex(RuntimeError, "different installation"):
                    setup.recover_empty(target, source, "cosmic-cards-0000000000", True)
                with self.assertRaisesRegex(RuntimeError, "relocate"):
                    setup.recover_empty(target, source, meta["project"])
                target.mkdir()
                (target / "existing").write_text("keep")
                with self.assertRaisesRegex(RuntimeError, "empty destination"):
                    setup.recover_empty(target, source, meta["project"], True)
                self.assertEqual((target / "existing").read_text(), "keep")
                (source / "database.sql").write_text("corrupt")
                with self.assertRaisesRegex(RuntimeError, "integrity"):
                    setup.recover_empty(base / "another", source, meta["project"], True)
                run.assert_not_called()
                prerequisites.assert_not_called()

    def test_empty_recovery_preserves_keys_identity_and_relocates_only_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base / "backup", base / "recovered"
            meta = self.backup_fixture(source, base / "original")
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value=""
            ), patch.object(setup, "load_backup_images"), patch.object(
                setup, "compose"
            ), patch.object(
                setup, "restore_data"
            ) as restore:
                setup.recover_empty(target, source, meta["project"], True)
            recovered = json.loads((target / "installation.json").read_text())
            self.assertEqual(recovered, {**meta, "directory": str(target)})
            self.assertEqual(
                (target / "private/cards.env").read_bytes(),
                (source / "private/cards.env").read_bytes(),
            )
            self.assertFalse((target / "recovery.pending.json").exists())
            restore.assert_called_once_with(
                target, recovered, source, restore_configuration=False
            )

    def test_empty_recovery_refuses_existing_project_resources_before_directory_creation(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base / "backup", base / "recovered"
            meta = self.backup_fixture(source, base / "original")
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "run", return_value="existing-volume"
            ):
                with self.assertRaisesRegex(RuntimeError, "Docker resources"):
                    setup.recover_empty(target, source, meta["project"], True)
            self.assertFalse(target.exists())

    def test_duplicate_state_members_and_unmanifested_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            self.backup_fixture(source, source.parent / "original")
            (source / "unlisted").write_text("unexpected")
            with self.assertRaisesRegex(RuntimeError, "outside"):
                setup.validate_backup(source)
            (source / "unlisted").unlink()
            with tarfile.open(source / "state.tar.gz", "w:gz") as archive:
                for _ in range(2):
                    info = tarfile.TarInfo("bridge.sqlite")
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
            self.checksum_fixture(source)
            with self.assertRaisesRegex(RuntimeError, "state archive"):
                setup.validate_backup(source)

    def test_restore_reports_failed_rollback_and_retains_safety_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base / "backup", base / "original"
            meta = self.backup_fixture(source, target)
            safety = base / "safety"
            with patch.object(setup, "backup", return_value=safety), patch.object(
                setup, "restore_data", side_effect=RuntimeError("unavailable")
            ) as restore:
                with self.assertRaisesRegex(RuntimeError, "both failed.*safety"):
                    setup.restore(target, meta, source)
                self.assertEqual(restore.call_count, 2)

    def test_admin_commands_use_local_cli_and_no_shell_interpolation(self):
        with patch.object(
            setup,
            "compose",
            return_value='{"accountId":12,"role":"admin","changed":true}',
        ) as compose:
            setup.admin_command(Path("fixture"), "grant", "Owner", capture=True)
            compose.assert_called_once_with(
                Path("fixture"),
                "exec",
                "-T",
                "cards",
                "node",
                "tools/admin.mjs",
                "grant",
                "Owner",
                capture=True,
            )
        with patch.object(
            setup, "compose", return_value='{"accountId":12,"changed":true}'
        ) as compose:
            setup.admin_command(Path("fixture"), "revoke", account_id=12, capture=True)
            compose.assert_called_once_with(
                Path("fixture"),
                "exec",
                "-T",
                "cards",
                "node",
                "tools/admin.mjs",
                "revoke",
                "--account-id",
                "12",
                capture=True,
            )

    def test_custom_collectible_catalog_installs_without_rewards(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            catalog = setup.managed_catalog()
            catalog["name"] = "External card collection"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            self.assertEqual(setup.managed_catalog(source=path), catalog)
            self.assertFalse(
                any(
                    v.get("codes")
                    for v in setup.managed_catalog(source=path)["variants"]
                )
            )
            path.write_text(json.dumps(setup.managed_catalog(True)), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                setup.managed_catalog(source=path)

    def test_reward_profile_is_explicit_and_inserts_one_code_per_pack(self):
        plain = setup.managed_catalog()
        self.assertFalse(any(v.get("codes") for v in plain["variants"]))
        reward = setup.managed_catalog(True)
        for product in reward["products"]:
            self.assertEqual(sum(s["count"] for s in product["slots"]), 9)
            inserts = [s for s in product["slots"] if s.get("role") == "insert"]
            self.assertEqual(len(inserts), 1)
            self.assertEqual(inserts[0]["count"], 1)
        self.assertFalse(
            any(v.get("codes") for v in setup.managed_catalog()["variants"])
        )

    def test_reward_profile_uses_matching_line_for_every_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            catalog = setup.managed_catalog()
            catalog["lines"].append({**catalog["lines"][0], "id": "second-line"})
            catalog["products"].append(
                {
                    **catalog["products"][0],
                    "id": "second-pack",
                    "lineId": "second-line",
                    "slots": [],
                }
            )
            path.write_text(json.dumps(catalog))
            result = setup.managed_catalog(True, path)
            ids = []
            for product in result["products"]:
                insert = next(
                    s for s in product["slots"] if s["id"] == "series-one-insert"
                )
                variant = next(
                    v
                    for v in result["variants"]
                    if v["id"] == insert["pool"][0]["variantId"]
                )
                card = next(c for c in result["cards"] if c["id"] == variant["cardId"])
                self.assertEqual(card["lineId"], product["lineId"])
                ids.append(card["id"])
            self.assertEqual(ids[0], "series-one-code")
            self.assertEqual(
                ids[1],
                "series-one-code-"
                + setup.hashlib.sha256(b"second-line").hexdigest()[:16],
            )

    def test_seed_account_is_disabled_before_login_and_patch_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory)
            p = game / "src/main/resources/db/data/161-admin-data.sql"
            p.parent.mkdir(parents=True)
            p.write_text("SELECT 1;\n")
            setup.secure_seed_account(game)
            first = p.read_bytes()
            setup.secure_seed_account(game)
            self.assertEqual(first, p.read_bytes())
            self.assertIn("banned=1,password=''", p.read_text())

    def test_interrupted_install_resumes_without_replacing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory).resolve()
            args = Namespace(
                directory=str(p),
                command="install",
                cosmic_directory=None,
                game_host="127.0.0.1",
                bind="127.0.0.1",
                origin=None,
                web_port=8490,
                login_port=8484,
                test_account=True,
                series_one=False,
                catalog=None,
            )
            options = {k: v for k, v in vars(args).items() if k != "directory"}
            setup.private_file(p / "setup.pending.json", json.dumps(options))
            setup.private_file(
                p / "installation.json", json.dumps({"format": 1, "directory": str(p)})
            )
            setup.private_file(p / "private/cards.env", "fixture persistent keys")
            with patch.object(setup, "prerequisites"), patch.object(
                setup, "finish_install"
            ) as finish:
                setup.install(args)
                finish.assert_called_once()
                self.assertEqual(
                    (p / "private/cards.env").read_text(), "fixture persistent keys"
                )
                args.web_port = 8499
                with self.assertRaises(RuntimeError):
                    setup.install(args)

    def test_external_plaintext_origins_are_rejected(self):
        for origin in [
            "http://cards.example.test",
            "https://fixture@cards.example.test",
            "https://:fixture@cards.example.test",
            "https://cards.example.test/path",
        ]:
            with self.assertRaises(ValueError):
                setup.origin(origin)
        self.assertEqual(
            setup.origin("https://cards.example.test"), "https://cards.example.test"
        )

    def test_config_requires_exact_fields_and_preserves_unrelated_settings(self):
        source = (
            'server:\n    DB_PASS: "old"\n    HOST: 127.0.0.1 # address\n    WORLD: 3\n'
        )
        updated = setup.configure_yaml(
            source, {"DB_PASS": "fixture-value", "HOST": "192.0.2.1"}
        )
        self.assertIn("WORLD: 3", updated)
        self.assertNotIn('"old"', updated)
        with self.assertRaises(RuntimeError):
            setup.configure_yaml(source, {"UNKNOWN": "value"})
        with self.assertRaises(RuntimeError):
            setup.configure_yaml(
                source + "    HOST: 127.0.0.1\n", {"HOST": "192.0.2.1"}
            )

    def test_database_and_adapter_ports_stay_private(self):
        meta = {
            "project": "fixture",
            "bridgeHash": "a" * 64,
            "bind": "127.0.0.1",
            "webPort": 8490,
            "loginPort": 8484,
        }
        profile = setup.create_compose(
            meta, {k: "image@sha256:" + ("a" * 64) for k in setup.RUNTIME["images"]}
        )
        self.assertNotIn("ports", profile["services"]["db"])
        self.assertEqual(
            profile["services"]["cards"]["network_mode"], "service:network"
        )
        self.assertEqual(profile["services"]["web"]["network_mode"], "service:network")
        self.assertEqual(
            profile["services"]["cosmic"]["network_mode"], "service:network"
        )
        self.assertFalse(
            any(
                ":8486" in port or ":8487" in port
                for port in profile["services"]["network"]["ports"]
            )
        )
        self.assertTrue(profile["services"]["cards"]["read_only"])
        self.assertIn("./media:/assets:ro", profile["services"]["cards"]["volumes"])
        for service in profile["services"].values():
            self.assertEqual(
                service["logging"]["options"], {"max-size": "10m", "max-file": "3"}
            )

    def test_restore_rejects_foreign_files_and_state_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / "checksums.json").write_text(json.dumps({"../outside": "a" * 64}))
            with self.assertRaises(RuntimeError):
                setup.validate_backup(p)
            with tarfile.open(p / "state.tar.gz", "w:gz") as archive:
                data = b"fixture"
                info = tarfile.TarInfo("../outside")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            names = [
                "database.sql",
                "compose.json",
                "installation.json",
                "images.lock.json",
                "runtime-images.tar",
                "runtime-images.json",
                "support/nginx.conf",
                "private/config.yaml",
                "private/database.env",
                "private/game.env",
                "private/cards.env",
                "private/catalog.json",
            ]
            for name in names:
                setup.private_file(p / name, b"fixture")
            sums = {
                x.relative_to(p)
                .as_posix(): setup.hashlib.sha256(x.read_bytes())
                .hexdigest()
                for x in p.rglob("*")
                if x.is_file() and x.name != "checksums.json"
            }
            (p / "checksums.json").write_text(json.dumps(sums))
            with self.assertRaises(RuntimeError):
                setup.validate_backup(p)


if __name__ == "__main__":
    unittest.main()
