#!/usr/bin/env python3
"""Prepare, build, start and operate a complete Cosmic card installation."""
from pathlib import Path
import argparse
from contextlib import closing
import base64
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "deployment/cosmic"
RUNTIME = json.loads((TEMPLATES / "runtime.json").read_text(encoding="utf-8"))
PRIVATE_BACKUP_FILES = (
    "config.yaml",
    "database.env",
    "game.env",
    "cards.env",
    "catalog.json",
    "test-account.json",
)


class ActivationError(RuntimeError):
    """Ingress reopened; retain current data rather than restoring over new transactions."""


class CommandError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def run(command, *, cwd=None, data=None, capture=False, structured_error=False):
    result = subprocess.run(
        command,
        cwd=cwd,
        input=data,
        capture_output=True,
        text=isinstance(data, str) or data is None,
    )
    if result.returncode:
        # Account input, SQL output and generated configuration never enter diagnostics.
        if command[:2] == ["docker", "build"] or "build" in command:
            sys.stderr.write(
                result.stderr.decode()
                if isinstance(result.stderr, bytes)
                else result.stderr
            )
        code = None
        if structured_error:
            stderr = result.stderr[-4096:]
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            lines = stderr.rstrip().splitlines()
            if lines and len(lines[-1]) <= 2048:
                try:
                    value = json.loads(lines[-1])
                    candidate = value.get("code") if isinstance(value, dict) else None
                    if isinstance(candidate, str) and re.fullmatch(
                        r"[A-Z][A-Z0-9_]{0,80}", candidate
                    ):
                        code = candidate
                except ValueError:
                    pass
        raise CommandError(
            "Command failed: " + command[0] + ". Inspect the service or build logs.",
            code,
        )
    if capture:
        return result.stdout
    return result


def private_file(path, data):
    atomic_migration_file(path, data)


def load(directory):
    directory = Path(directory).resolve()
    data = json.loads((directory / "installation.json").read_text(encoding="utf-8"))
    if data.get("format") != 1 or data.get("directory") != str(directory):
        raise RuntimeError("This is not the original managed installation directory.")
    return directory, data


def compose(directory, *args, data=None, capture=False):
    return run(
        ["docker", "compose", "-f", str(directory / "compose.json"), *args],
        cwd=directory,
        data=data,
        capture=capture,
    )


def prerequisites():
    if os.name != "posix":
        raise RuntimeError(
            "Run the managed installer on Linux, or inside WSL2 with Docker integration enabled."
        )
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    for tool in ["git", "docker"]:
        if not shutil.which(tool):
            raise RuntimeError(
                tool
                + " is required. Install Git and Docker with Compose before running setup."
            )
    run(["docker", "compose", "version"])
    run(["docker", "info", "--format", "{{.ServerVersion}}"])


def origin(value):
    url = urllib.parse.urlsplit(value)
    if (
        url.scheme not in ["http", "https"]
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("Use a complete HTTP(S) origin without a path or credentials.")
    if url.scheme == "http" and url.hostname not in ["127.0.0.1", "localhost", "::1"]:
        raise ValueError("Remote players require an HTTPS origin and TLS termination.")
    return value


def configure_yaml(source, replacements):
    text = source
    for key, value in replacements.items():
        pattern = r"(?m)^([ \t]*)" + re.escape(key) + r":[^\r\n]*(\r?\n|$)"
        text, count = re.subn(
            pattern, lambda m: m[1] + key + ": " + json.dumps(value) + m[2], text
        )
        if count != 1:
            raise RuntimeError("Expected one Cosmic configuration field: " + key)
    return text


def secure_seed_account(game):
    path = game / "src/main/resources/db/data/161-admin-data.sql"
    if not path.is_file():
        raise RuntimeError("The supported Cosmic seed migration is missing.")
    statement = "UPDATE accounts SET banned=1,password='',nxCredit=0,maplePoint=0,nxPrepaid=0 WHERE name='admin';"
    text = path.read_text(encoding="utf-8")
    if statement not in text:
        path.write_text(text.rstrip() + "\n\n" + statement + "\n", encoding="utf-8")


def managed_catalog(series_one=False, source=None):
    catalog = json.loads(
        (Path(source) if source else ROOT / "data/catalog.example.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(catalog, dict) or not all(
        isinstance(catalog.get(name), list) and catalog[name]
        for name in ["lines", "cards", "variants", "rarities", "products"]
    ):
        raise RuntimeError("Choose a complete framework catalog with cards and packs.")
    if any(v.get("codes") for v in catalog["variants"]) or any(
        c.get("type") == "code" for c in catalog["cards"]
    ):
        raise RuntimeError(
            "The managed catalog input must contain collectibles only; use --series-one to add the supplied code insert."
        )
    if series_one:
        primary = catalog["lines"][0]["id"]
        inserts = {}
        for product in catalog["products"]:
            line = product["lineId"]
            if line in inserts:
                continue
            code_id = "series-one-code" + (
                ""
                if line == primary
                else "-" + hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
            )
            inserts[line] = code_id + ".standard"
            catalog["cards"].append(
                {
                    "id": code_id,
                    "lineId": line,
                    "name": "Series One code card",
                    "type": "code",
                    "behavior": {"tradable": False, "albumEligible": True},
                }
            )
            catalog["variants"].append(
                {
                    "id": inserts[line],
                    "cardId": code_id,
                    "rarityId": catalog["rarities"][0]["id"],
                    "codes": [
                        {
                            "id": "game",
                            "poolId": "v83.series-one",
                            "reveal": "peel",
                            "transfer": "block",
                            "title": "Series One reward",
                        }
                    ],
                }
            )
        for product in catalog["products"]:
            product["slots"].append(
                {
                    "id": "series-one-insert",
                    "role": "insert",
                    "count": 1,
                    "pool": [{"variantId": inserts[product["lineId"]], "weight": 1}],
                }
            )
    return catalog


def bridge_snapshot(directory):
    destination = directory / "bridge"
    if destination.is_symlink():
        raise RuntimeError("The managed build directory must not be a symbolic link.")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(exist_ok=True)
    for folder in ["src", "starter", "data"]:
        shutil.copytree(ROOT / folder, destination / folder, dirs_exist_ok=True)
    for name in [
        "package.json",
        "package-lock.json",
        "LICENSE",
        "NOTICE",
        "NOTICE.md",
        "THIRD_PARTY_NOTICES.md",
        "THIRD-PARTY-NOTICES.md",
    ]:
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, destination / name)
    (destination / "tools").mkdir(exist_ok=True)
    for name in ["doctor.mjs", "admin.mjs", "rewards.mjs", "payments.mjs"]:
        shutil.copy2(ROOT / "tools" / name, destination / "tools" / name)
    # Only distributable source enters this directory; container users must read it
    # independently of the operator's private installation umask.
    destination.chmod(0o755)
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    digest = hashlib.sha256()
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(destination).as_posix().encode())
            digest.update(path.read_bytes())
    for folder in [ROOT / "java", TEMPLATES]:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(path.read_bytes())
    digest.update((ROOT / "tools/install-cosmic.py").read_bytes())
    digest.update((ROOT / "tools/cosmic.py").read_bytes())
    return digest.hexdigest()


def create_compose(meta, images):
    project = meta["project"]
    private = "./private/"
    shared = {
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        },
    }
    return {
        "name": project,
        "services": {
            "network": {
                **shared,
                "image": images["node"],
                "user": "1000:1000",
                "read_only": True,
                "cap_drop": ["ALL"],
                "entrypoint": ["/bin/sleep", "infinity"],
                "ports": [
                    "127.0.0.1:" + str(meta["webPort"]) + ":8080",
                    meta["bind"] + ":" + str(meta["loginPort"]) + ":8484",
                    meta["bind"] + ":7575-7577:7575-7577",
                ],
            },
            "db": {
                **shared,
                "image": images["database"],
                "env_file": [private + "database.env"],
                "volumes": ["database:/var/lib/mysql"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        'MYSQL_PWD="$$MYSQL_ROOT_PASSWORD" mysql -h 127.0.0.1 -u root -N -e "SELECT 1"',
                    ],
                    "interval": "5s",
                    "timeout": "5s",
                    "retries": 60,
                },
            },
            "cosmic": {
                **shared,
                "image": project + ":game-" + meta["bridgeHash"][:12],
                "build": {
                    "context": ".",
                    "dockerfile": "support/Game.Dockerfile",
                    "args": {
                        "MAVEN_IMAGE": images["maven"],
                        "JAVA_IMAGE": images["java"],
                    },
                },
                "network_mode": "service:network",
                "depends_on": {
                    "db": {"condition": "service_healthy"},
                    "network": {"condition": "service_started"},
                },
                "env_file": [private + "game.env"],
                "environment": {"DB_HOST": "db"},
                "volumes": [private + "config.yaml:/opt/server/config.yaml:ro"],
                "stop_grace_period": "120s",
                "healthcheck": {
                    "test": ["CMD-SHELL", 'bash -c "echo >/dev/tcp/127.0.0.1/8486"'],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 120,
                },
            },
            "cards": {
                **shared,
                "image": project + ":cards-" + meta["bridgeHash"][:12],
                "build": {
                    "context": ".",
                    "dockerfile": "support/Card.Dockerfile",
                    "args": {"NODE_IMAGE": images["node"]},
                },
                "network_mode": "service:network",
                "depends_on": {"cosmic": {"condition": "service_started"}},
                "env_file": [private + "cards.env"],
                "read_only": True,
                "volumes": [
                    "card_state:/opt/card/state",
                    private + "catalog.json:/opt/card/catalog.json:ro",
                    "./media:/assets:ro",
                ],
                "tmpfs": ["/tmp:size=16m"],
                "healthcheck": {
                    "test": ["CMD", "node", "tools/doctor.mjs", "--quiet"],
                    "interval": "10s",
                    "timeout": "15s",
                    "retries": 90,
                },
            },
            "web": {
                **shared,
                "image": images["web"],
                "user": "101:101",
                "network_mode": "service:network",
                "depends_on": {"cards": {"condition": "service_healthy"}},
                "read_only": True,
                "cap_drop": ["ALL"],
                "tmpfs": ["/tmp:size=16m"],
                "volumes": ["./support/nginx.conf:/etc/nginx/nginx.conf:ro"],
                "entrypoint": ["nginx", "-g", "daemon off;"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "wget -q -O /dev/null http://127.0.0.1:8080/api/library/health",
                    ],
                    "interval": "10s",
                    "timeout": "15s",
                    "retries": 20,
                },
            },
        },
        "volumes": {"database": {}, "card_state": {}},
    }


def lock_images(directory):
    lock = directory / "images.lock.json"
    if lock.exists():
        return json.loads(lock.read_text(encoding="utf-8"))
    result = {}
    for name, image in RUNTIME["images"].items():
        print("Preparing container runtime: " + name, flush=True)
        run(["docker", "pull", image])
        digest = json.loads(
            run(
                [
                    "docker",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    "{{json .RepoDigests}}",
                ],
                capture=True,
            )
        )[0]
        if "@sha256:" not in digest:
            raise RuntimeError("Runtime image has no content digest.")
        result[name] = digest
    private_file(lock, json.dumps(result, indent=2) + "\n")
    return result


def install(args):
    prerequisites()
    directory = Path(args.directory).resolve()
    if (
        directory == ROOT
        or ROOT in directory.parents
        or directory == directory.parent
        or directory == Path.home()
    ):
        raise RuntimeError(
            "Choose a separate installation directory outside this source repository."
        )
    ipaddress.IPv4Address(args.game_host)
    ipaddress.IPv4Address(args.bind)
    public_origin = origin(args.origin or "http://127.0.0.1:" + str(args.web_port))
    if (
        not all(1 <= p <= 65535 for p in [args.web_port, args.login_port])
        or args.web_port == args.login_port
        or any(p in [7575, 7576, 7577] for p in [args.web_port, args.login_port])
    ):
        raise ValueError(
            "Choose separate valid web and login ports outside the channel range."
        )
    options = {k: v for k, v in vars(args).items() if k != "directory"}
    custom_catalog = getattr(args, "catalog", None)
    if custom_catalog and (
        Path(custom_catalog).resolve() == directory
        or directory in Path(custom_catalog).resolve().parents
    ):
        raise RuntimeError(
            "Keep the source catalog outside the managed installation directory."
        )
    prepared_catalog = managed_catalog(args.series_one, custom_catalog)
    if custom_catalog:
        options["catalogHash"] = file_hash(Path(custom_catalog))
    marker = directory / "setup.pending.json"
    if directory.exists() and any(directory.iterdir()):
        if (
            not marker.is_file()
            or json.loads(marker.read_text(encoding="utf-8")) != options
        ):
            raise RuntimeError(
                "Installation directory is not empty. Use the same install options to resume, or start/upgrade for a completed installation."
            )
        if (directory / "installation.json").is_file():
            _, meta = load(directory)
            finish_install(directory, meta, args)
            return
    directory.mkdir(parents=True, exist_ok=True)
    private_file(marker, json.dumps(options, indent=2) + "\n")
    private_file(directory / ".gitignore", "*\n")
    game = directory / "cosmic"
    if args.cosmic_directory:
        source = Path(args.cosmic_directory).resolve()
        if (
            source == directory
            or directory in source.parents
            or source in directory.parents
        ):
            raise RuntimeError("Source and installation directories must be separate.")
        revision = run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture=True
        ).strip()
        if run(
            ["git", "-C", str(source), "status", "--porcelain"], capture=True
        ).strip():
            raise RuntimeError(
                "Commit source changes before creating a deployment copy."
            )
        shutil.copytree(
            source,
            game,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", "target", "*.log", ".env", "*.env", "state", "node_modules"
            ),
        )
    else:
        print("Preparing supported Cosmic source", flush=True)
        if not game.exists():
            run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    RUNTIME["cosmicRepository"],
                    str(game),
                ]
            )
        run(["git", "-C", str(game), "checkout", "--quiet", RUNTIME["cosmicRevision"]])
        revision = run(
            ["git", "-C", str(game), "rev-parse", "HEAD"], capture=True
        ).strip()
        if revision != RUNTIME["cosmicRevision"]:
            raise RuntimeError(
                "Cosmic source revision does not match the supported release."
            )
    run([sys.executable, str(ROOT / "tools/install-cosmic.py"), str(game), "--check"])
    run([sys.executable, str(ROOT / "tools/install-cosmic.py"), str(game)])
    secure_seed_account(game)
    support = directory / "support"
    shutil.copytree(TEMPLATES, support, dirs_exist_ok=True)
    config = (game / "config.yaml").read_text(encoding="utf-8")
    # Build fixtures have no deployment credentials; the real configuration is mounted only at runtime.
    test_config = configure_yaml(
        config,
        {
            "DB_USER": "cosmic_app",
            "DB_PASS": "",
            "HOST": "127.0.0.1",
            "LANHOST": "127.0.0.1",
            "LOCALHOST": "127.0.0.1",
        },
    )
    (support / "test-config.yaml").write_text(test_config, encoding="utf-8")
    private = directory / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    key_path = private / "setup-keys.json"
    if key_path.exists():
        keys = json.loads(key_path.read_text(encoding="utf-8"))
    else:
        keys = {
            name: base64.b64encode(secrets.token_bytes(32)).decode()
            for name in ["shared", "state", "encryption", "index", "csrf", "gameIndex"]
        }
        keys.update(database=secrets.token_hex(32), databaseRoot=secrets.token_hex(32))
        private_file(key_path, json.dumps(keys) + "\n")
    game_password, root_password = keys["database"], keys["databaseRoot"]
    private_file(
        private / "config.yaml",
        configure_yaml(
            config,
            {
                "DB_USER": "cosmic_app",
                "DB_PASS": game_password,
                "DB_URL_FORMAT": "jdbc:mysql://%s:3306/cosmic?allowPublicKeyRetrieval=true&useSSL=false",
                "HOST": args.game_host,
                "LANHOST": args.game_host,
                "LOCALHOST": args.game_host,
            },
        ),
    )
    private_file(
        private / "database.env",
        "MYSQL_DATABASE=cosmic\nMYSQL_USER=cosmic_app\nMYSQL_PASSWORD="
        + game_password
        + "\nMYSQL_ROOT_PASSWORD="
        + root_password
        + "\n",
    )
    shared = keys["shared"]
    private_file(
        private / "cards.env",
        "\n".join(
            [
                "PUBLIC_ORIGIN=" + public_origin,
                "GAME_URL=http://127.0.0.1:8486",
                "GAME_SHARED_KEY=" + shared,
                "STATE_KEY=" + keys["state"],
                "CODE_ENCRYPTION_KEY=" + keys["encryption"],
                "CODE_INDEX_KEY=" + keys["index"],
                "CSRF_KEY=" + keys["csrf"],
                "STATE_DIRECTORY=/opt/card/state",
                "CATALOG_PATH=/opt/card/catalog.json",
                "ASSET_ROOT=/assets",
                "AUTH_MODE=bridge",
                "PORT=8487",
                "ACCEPTED_CASH_TYPES=1,2,4",
                "TRUST_PROXY=1",
                "ENABLE_SERIES_ONE_REWARDS=" + ("1" if args.series_one else "0"),
            ]
        )
        + "\n",
    )
    private_file(
        private / "game.env",
        "\n".join(
            [
                "CARD_BRIDGE_ENABLED=1",
                "CARD_BRIDGE_PORT=8486",
                "CARD_BRIDGE_SHARED_KEY=" + shared,
                "CARD_BRIDGE_CODE_KEY=" + keys["gameIndex"],
                "CARD_BRIDGE_CALLBACK_URL=http://127.0.0.1:8487/api/library/provider/used",
                "CARD_BRIDGE_SESSION_SOURCE=bridge",
                "CARD_BRIDGE_ACCEPTED_CASH_TYPES=1,2,4",
            ]
        )
        + "\n",
    )
    private_file(
        private / "catalog.json",
        json.dumps(prepared_catalog, indent=2) + "\n",
    )
    # Only these non-key runtime files are mounted. The private parent stays owner-only.
    (private / "config.yaml").chmod(0o644)
    (private / "catalog.json").chmod(0o644)
    meta = {
        "format": 1,
        "directory": str(directory),
        "project": "cosmic-cards-"
        + hashlib.sha256(str(directory).encode()).hexdigest()[:10],
        "cosmicRevision": revision,
        "origin": public_origin,
        "bind": args.bind,
        "gameHost": args.game_host,
        "webPort": args.web_port,
        "loginPort": args.login_port,
        "seriesOneEnabled": args.series_one,
        "smokeCollectibles": sum(
            slot["count"] for slot in prepared_catalog["products"][0]["slots"]
        )
        - (1 if args.series_one else 0),
        "bridgeHash": bridge_snapshot(directory),
    }
    private_file(
        directory / ".dockerignore",
        "private/\nbackups/\ncosmic/.git\ncosmic/target/\ninstallation.json\n*.log\n",
    )
    images = lock_images(directory)
    private_file(
        directory / "compose.json",
        json.dumps(create_compose(meta, images), indent=2) + "\n",
    )
    private_file(directory / "installation.json", json.dumps(meta, indent=2) + "\n")
    finish_install(directory, meta, args)


def finish_install(directory, meta, args):
    print(
        "Building Cosmic, running its tests, and building the card service", flush=True
    )
    compose(directory, "build")
    start(directory, meta)
    if args.test_account:
        private = directory / "private"
        credentials = private / "test-account.json"
        if not credentials.exists():
            private_file(
                credentials,
                json.dumps(
                    {"username": "CardTest", "password": secrets.token_urlsafe(24)}
                )
                + "\n",
            )
        account = json.loads(credentials.read_text(encoding="utf-8"))
        account_command(directory, {"action": "create", **account})
        account_command(
            directory,
            {
                "action": "fund",
                "username": "CardTest",
                "cashType": 4,
                "amount": 5000,
                "key": "setup-funding-v1",
            },
        )
        smoke(directory, meta)
        print(
            "Disposable CardTest account details saved in private/test-account.json; one pack was purchased for verification."
        )
    print("Website: " + meta["origin"] + "/library/", flush=True)
    grants = admin_command(directory, "list", capture=True)
    if not grants.get("items"):
        command = "python3 tools/cosmic.py --directory " + shlex.quote(str(directory))
        print("Owner setup incomplete: no website administrator has been granted.")
        print("Create an ordinary account: " + command + " account create Owner")
        print("Grant website administration: " + command + " admin grant Owner")
    print("Private configuration: " + str(directory / "private"))
    print(
        "Check services: python3 tools/cosmic.py --directory "
        + shlex.quote(str(directory))
        + " doctor"
    )
    (directory / "setup.pending.json").unlink(missing_ok=True)
    (directory / "private/setup-keys.json").unlink(missing_ok=True)


def doctor(directory, meta):
    compose(directory, "exec", "-T", "cards", "node", "tools/doctor.mjs")
    # Test the public-facing reverse proxy from inside the shared namespace as well.
    compose(
        directory,
        "exec",
        "-T",
        "cards",
        "node",
        "--input-type=module",
        "-e",
        "const r=await fetch('http://127.0.0.1:8080/api/library/health',{signal:AbortSignal.timeout(12000)});if(!r.ok||(await r.json()).gameReady!==true)process.exit(1)",
    )
    print("Ready: " + meta["origin"] + "/library/", flush=True)


def start(
    directory, meta, *, allow_recovery=False, allow_migration=False, allow_upgrade=False
):
    if not allow_upgrade and (directory / "upgrade.pending.json").exists():
        raise RuntimeError(
            "Upgrade is incomplete. Use upgrade --finish-upgrade; ordinary start cannot bypass its recorded phase."
        )
    if not allow_migration and (directory / "migration.pending.json").exists():
        raise RuntimeError(
            "Legacy migration remains closed. Use upgrade --finish-legacy-migration; ordinary start cannot expose unresolved payments."
        )
    if not allow_recovery and (directory / "recovery.pending.json").exists():
        raise RuntimeError(
            "This disaster recovery is incomplete. Inspect the preserved recovery directory and backup before exposing services."
        )
    ensure_media(directory)
    compose(directory, "up", "-d", "--wait", "db", "network")
    proxy = directory / "support/nginx.conf"
    text = proxy.read_text(encoding="utf-8")
    if "__DOCKER_GATEWAY__" in text:
        gateway = run(
            [
                "docker",
                "network",
                "inspect",
                meta["project"] + "_default",
                "--format",
                "{{(index .IPAM.Config 0).Gateway}}",
            ],
            capture=True,
        ).strip()
        ipaddress.IPv4Address(gateway)
        proxy.write_text(text.replace("__DOCKER_GATEWAY__", gateway), encoding="utf-8")
    compose(directory, "up", "-d", "--no-build", "--wait", "--wait-timeout", "900")
    doctor(directory, meta)


def account_command(directory, value):
    result = compose(
        directory,
        "exec",
        "-T",
        "cosmic",
        "java",
        "-cp",
        "Server.jar:/opt/card-tools",
        "AccountCommand",
        data=json.dumps(value),
        capture=True,
    )
    if "Operator account action completed." not in result:
        raise RuntimeError("The operator account action did not confirm completion.")


def admin_command(directory, action, username=None, capture=False, account_id=None):
    args = ["exec", "-T", "cards", "node", "tools/admin.mjs", action]
    if username:
        args.append(username)
    if account_id is not None:
        args.extend(["--account-id", str(account_id)])
    result = compose(directory, *args, capture=True)
    value = json.loads(result)
    if capture:
        return value
    print(json.dumps(value, indent=2))


def smoke(directory, meta):
    if not (directory / "private/test-account.json").is_file():
        raise RuntimeError(
            "Smoke requires the installation's disposable account; install with --test-account."
        )
    account = json.loads(
        (directory / "private/test-account.json").read_text(encoding="utf-8")
    )
    # Run where the game and bridge run; public DNS/TLS can be checked separately with doctor.
    script = """
import{existsSync,readFileSync,openSync,writeFileSync,fsyncSync,closeSync,renameSync}from'node:fs';
import{DatabaseSync}from'node:sqlite';import{createGameClient}from'./src/protocol.mjs';
const origin=process.env.PUBLIC_ORIGIN,base='http://127.0.0.1:8487',path=process.env.STATE_DIRECTORY+'/smoke-receipt.json';
function save(receipt){const temporary=path+'.pending';const fd=openSync(temporary,'w',0o600);try{writeFileSync(fd,JSON.stringify(receipt));fsyncSync(fd)}finally{closeSync(fd)}renameSync(temporary,path);const dir=openSync(process.env.STATE_DIRECTORY,'r');try{fsyncSync(dir)}finally{closeSync(dir)}}
let cookie='',csrf='';
const input=JSON.parse(await new Promise(resolve=>{let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>resolve(s))}));
async function call(route,body){const r=await fetch(base+'/api/library/'+route,{method:body?'POST':'GET',headers:{Cookie:cookie,Origin:origin,'Content-Type':'application/json','X-CSRF-Token':csrf},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(15000)});const v=await r.json();if(!r.ok)throw Error(v.code??'SMOKE_FAILED');if(r.headers.get('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];if(v.csrf)csrf=v.csrf;return v;}
await call('session');await call('login',{username:input.username,password:input.password});
let receipt=existsSync(path)?JSON.parse(readFileSync(path,'utf8')):null;
if(receipt&&receipt.username!==input.username)throw Error('SMOKE_ACCOUNT_MISMATCH');
if(!receipt){const game=createGameClient({url:process.env.GAME_URL,secret:process.env.GAME_SHARED_KEY}),person=await game('/resolve-account',{name:input.username}),db=new DatabaseSync(process.env.STATE_DIRECTORY+'/bridge.sqlite',{readOnly:true});let legacy;try{const row=db.prepare('SELECT body FROM orders WHERE account_id=? AND request_key=?').get(person.accountId,'setup-smoke-v1');legacy=row?JSON.parse(row.body):null}finally{db.close()}if(legacy){receipt={format:1,username:input.username,quote:legacy.quote,key:'setup-smoke-v1',expected:input.collectibles+(input.seriesOne?1:0)};save(receipt)}}
if(!receipt){const catalog=await call('catalog'),product=catalog.products.find(p=>p.enabled!==false);if(!product)throw Error('NO_SMOKE_OFFER');const quote=await call('quote',{productId:product.id,quantity:1,cashType:4});receipt={format:1,username:input.username,quote,key:'setup-smoke-v2',expected:product.slots.reduce((n,s)=>n+s.count,0)};save(receipt)}
const bought=await call('buy',{...receipt.quote,key:receipt.key}),replay=await call('buy',{...receipt.quote,key:receipt.key});
if(JSON.stringify(bought)!==JSON.stringify(replay))throw Error('RETRY_MISMATCH');
const pack=bought.packs[0];if(receipt.packId&&receipt.packId!==pack.id)throw Error('SMOKE_PACK_MISMATCH');receipt.packId=pack.id;save(receipt);
const opened=await call('open',{packId:pack.id,key:'setup-open-'+pack.id});if(opened.cards.length!==receipt.expected)throw Error('PACK_CONTENTS');
const ids=opened.cards.map(c=>c.id).sort();if(receipt.cardIds&&JSON.stringify(receipt.cardIds)!==JSON.stringify(ids))throw Error('SMOKE_CARD_MISMATCH');receipt.cardIds=ids;save(receipt);
const ownCodes=[];let after='';do{const page=await call('codes',{after,limit:50});ownCodes.push(...page.items.filter(c=>ids.includes(c.copyId)));after=page.next??''}while(after&&ownCodes.length<opened.cards.filter(c=>c.definition.type==='code').length);
if(ownCodes.length!==opened.cards.filter(c=>c.definition.type==='code').length)throw Error('CODE_OWNERSHIP');
for(const code of ownCodes){if(code.registration!=='ready')throw Error('REGISTRATION');const revealed=await call('reveal',{codeId:code.id,key:'setup-reveal-'+code.id});if(!/^(C0[123])?[A-Z2-9]{15}$/.test(revealed.code))throw Error('CODE_PATTERN')}
await call('logout',{});if((await call('session')).signedIn)throw Error('LOGOUT');console.log('Setup smoke passed: only its durable purchase, pack and card identities were verified.');
"""
    compose(
        directory,
        "exec",
        "-T",
        "cards",
        "node",
        "--input-type=module",
        "-e",
        script,
        data=json.dumps(
            {
                **account,
                "seriesOne": meta.get("seriesOneEnabled", False),
                "collectibles": meta.get("smokeCollectibles", 8),
            }
        ),
    )
    print(
        "Setup smoke passed. Native-client Cash Shop redemption is a separate gameplay check."
    )


def no_players(directory):
    script = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root -N cosmic -e "SELECT COUNT(*) FROM accounts WHERE loggedin<>0"'
    count = compose(
        directory, "exec", "-T", "db", "sh", "-c", script, capture=True
    ).strip()
    if count != "0":
        raise RuntimeError("Game accounts are connected. Maintenance was not started.")


def ensure_media(directory):
    path = directory / "media"
    if path.is_symlink() or path.resolve() != directory.resolve() / "media":
        raise RuntimeError("Managed media directory must not be a symbolic link.")
    path.mkdir(mode=0o755, exist_ok=True)
    path.chmod(0o755)
    return path


def media_name(name, directory=False):
    pattern = r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+" + (
        r"" if directory else r"\.(?:png|jpg|jpeg|webp)"
    )
    return bool(re.fullmatch(pattern, name))


def archive_media(directory, destination):
    root = ensure_media(directory)
    paths = sorted(root.rglob("*"))
    total = 0
    for path in paths:
        name = path.relative_to(root).as_posix()
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or not media_name(name, path.is_dir())
            or (not path.is_file() and not path.is_dir())
        ):
            raise RuntimeError(
                "Media must contain only safe raster files and ordinary directories."
            )
        if path.is_file():
            size = path.stat().st_size
            total += size
            if size > 32 * 1024 * 1024 or total > 512 * 1024 * 1024:
                raise RuntimeError(
                    "Managed media exceeds its 32 MiB per-file or 512 MiB total backup limit."
                )
    with tarfile.open(destination, "w:gz") as archive:
        for path in paths:
            archive.add(
                path, arcname=path.relative_to(root).as_posix(), recursive=False
            )
    destination.chmod(0o600)


def validate_media_archive(path):
    seen, total = set(), 0
    with tarfile.open(path) as archive:
        for member in archive:
            total += member.size
            if (
                not media_name(member.name, member.isdir())
                or member.name in seen
                or not (member.isfile() or member.isdir())
                or member.size > 32 * 1024 * 1024
                or total > 512 * 1024 * 1024
            ):
                raise RuntimeError("Unexpected or oversized media archive member.")
            seen.add(member.name)


def restore_media(directory, source):
    root = ensure_media(directory)
    archive = source / "media.tar.gz"
    if archive.exists():
        validate_media_archive(archive)
    if root.resolve() != directory.resolve() / "media":
        raise RuntimeError("Unsafe media restore directory.")
    shutil.rmtree(root)
    root.mkdir(mode=0o755)
    if archive.exists():
        with tarfile.open(archive) as files:
            for member in files:
                destination = root / member.name
                if member.isdir():
                    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    with files.extractfile(member) as incoming, destination.open(
                        "wb"
                    ) as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
        for path in root.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)


def pending_guard_script():
    return r"""
import{copyFileSync,lstatSync,mkdtempSync,rmSync}from'node:fs';
import{join}from'node:path';import{tmpdir}from'node:os';import{DatabaseSync}from'node:sqlite';
let temporary,database;
try{
 const source=process.argv[1]??'/opt/card/state';
 temporary=mkdtempSync(join(process.argv[2]??tmpdir(),'card-pending-'));
 let bytes=0;
 for(const name of ['bridge.sqlite','bridge.sqlite-wal']){
  const path=join(source,name);let info;
  try{info=lstatSync(path)}catch(error){if(name.endsWith('-wal')&&error.code==='ENOENT')continue;throw error}
  if(!info.isFile()||(bytes+=info.size)>256*1024*1024)throw Error('INVALID_PENDING_STATE');
  copyFileSync(path,join(temporary,name));
 }
 // The source stays read-only. SQLite rebuilds its WAL index only in this private copy.
 database=new DatabaseSync(join(temporary,'bridge.sqlite'),{readOnly:true});
 const version=database.prepare('PRAGMA user_version').get().user_version;
 const discovery=database.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_discovery'").get();
 if(![0,1,2].includes(version)||(version===2&&!discovery))throw Error('INVALID_PENDING_STATE');
 const orders=database.prepare("SELECT COUNT(*) AS n FROM orders WHERE state NOT IN ('complete','rejected','refunded')").get().n;
 const orphans=discovery?database.prepare('SELECT COUNT(*) AS n FROM payment_discovery').get().n:0;
 console.log(orders+orphans);
}catch{
 console.error(JSON.stringify({code:'PENDING_STATE_UNAVAILABLE'}));process.exitCode=1;
}finally{
 database?.close();if(temporary)rmSync(temporary,{recursive:true,force:true});
}
"""


def live_pending_guard_script():
    return r"""
import{DatabaseSync}from'node:sqlite';const db=new DatabaseSync('/opt/card/state/bridge.sqlite',{readOnly:true});
try{
 const version=db.prepare('PRAGMA user_version').get().user_version;
 const discovery=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_discovery'").get();
 if(![0,1,2].includes(version)||(version===2&&!discovery))throw Error('UNSUPPORTED_STATE');
 const orders=db.prepare("SELECT COUNT(*) AS n FROM orders WHERE state NOT IN ('complete','rejected','refunded')").get().n;
 console.log(orders+(discovery?db.prepare('SELECT COUNT(*) AS n FROM payment_discovery').get().n:0));
}finally{db.close()}
"""


def state_schema_spec(version=2):
    with closing(sqlite3.connect(":memory:")) as model:
        model.executescript(normalized_schema_sql(version))
        tables = [
            row[0]
            for row in model.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        result = {
            name: [row[1] for row in model.execute("PRAGMA table_info(" + name + ")")]
            for name in tables
        }
        result["indexes"] = [
            row[0]
            for row in model.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'framework_%' ORDER BY name"
            )
        ]
        return result


def normalized_schema_sql(version):
    if version not in range(2, 9):
        raise RuntimeError("Unsupported normalized framework schema.")
    base = r"""
CREATE TABLE framework_meta(id INTEGER PRIMARY KEY CHECK(id=1),schema_version INTEGER NOT NULL,codec_check TEXT NOT NULL) STRICT;
CREATE TABLE framework_fields(name TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('object','array','scalar')),payload TEXT) STRICT;
CREATE TABLE framework_entities(collection TEXT NOT NULL,entity_key TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,
 identity_hash TEXT,owner_id TEXT,holder_id TEXT,state TEXT,status TEXT,card_id TEXT,variant_id TEXT,line_id TEXT,rarity_id TEXT,pool_id TEXT,provider_id TEXT,
 created_at TEXT NOT NULL,name_sort TEXT NOT NULL,search_text TEXT NOT NULL,card_type TEXT NOT NULL,PRIMARY KEY(collection,entity_key),
 FOREIGN KEY(collection) REFERENCES framework_fields(name) ON DELETE CASCADE) STRICT;
CREATE UNIQUE INDEX framework_identity ON framework_entities(collection,identity_hash) WHERE identity_hash IS NOT NULL;
CREATE INDEX framework_owner ON framework_entities(collection,owner_id,created_at DESC,entity_key);
CREATE INDEX framework_owner_state ON framework_entities(collection,owner_id,state,created_at DESC,entity_key);
CREATE INDEX framework_owner_status ON framework_entities(collection,owner_id,status,created_at DESC,entity_key);
CREATE INDEX framework_owner_name ON framework_entities(collection,owner_id,state,name_sort,entity_key);
CREATE INDEX framework_owner_variant ON framework_entities(collection,owner_id,state,variant_id,created_at DESC,entity_key);
CREATE INDEX framework_holder ON framework_entities(collection,holder_id,created_at DESC,entity_key);
CREATE INDEX framework_holder_status ON framework_entities(collection,holder_id,status,created_at DESC,entity_key);
CREATE INDEX framework_pool_status ON framework_entities(collection,pool_id,status,created_at DESC,entity_key);
CREATE INDEX framework_provider_state ON framework_entities(collection,provider_id,state,entity_key);
CREATE INDEX framework_provider_cursor ON framework_entities(collection,provider_id,entity_key,state);
CREATE INDEX framework_status ON framework_entities(collection,status,created_at DESC,entity_key);
CREATE INDEX framework_created ON framework_entities(collection,created_at DESC,entity_key);
CREATE INDEX framework_name ON framework_entities(collection,name_sort,entity_key);
CREATE INDEX framework_order ON framework_entities(collection,ordinal);
"""
    sql = base + (
        r"""
CREATE TABLE framework_code_holders(collection TEXT NOT NULL CHECK(collection='codes'),code_id TEXT NOT NULL,holder_hash TEXT NOT NULL,copy_id TEXT,
 PRIMARY KEY(holder_hash,code_id),FOREIGN KEY(collection,code_id) REFERENCES framework_entities(collection,entity_key) ON DELETE CASCADE) STRICT;
CREATE INDEX framework_code_membership ON framework_code_holders(collection,code_id);
"""
        if version >= 3
        else ""
    )
    if version >= 4:
        sql += "CREATE INDEX framework_notification_owner_order ON framework_entities(collection,owner_id,ordinal,entity_key);"
    if version >= 5:
        sql += r"""
CREATE TABLE framework_action_work(collection TEXT NOT NULL CHECK(collection='actionJobs'),job_key TEXT NOT NULL,ordinal INTEGER NOT NULL,due_at INTEGER,reservation_key TEXT,active INTEGER NOT NULL CHECK(active IN (0,1)),
 PRIMARY KEY(job_key),FOREIGN KEY(collection,job_key) REFERENCES framework_entities(collection,entity_key) ON DELETE CASCADE) STRICT;
CREATE INDEX framework_action_due ON framework_action_work(due_at,ordinal,job_key) WHERE due_at IS NOT NULL;
CREATE INDEX framework_action_reservation ON framework_action_work(reservation_key,active);
"""
    if version >= 6:
        sql += r"""
CREATE TABLE framework_expiration_work(collection TEXT NOT NULL CHECK(collection IN ('trades','listings')),entity_key TEXT NOT NULL,ordinal INTEGER NOT NULL,due_at INTEGER,
 PRIMARY KEY(collection,entity_key),FOREIGN KEY(collection,entity_key) REFERENCES framework_entities(collection,entity_key) ON DELETE CASCADE) STRICT;
CREATE INDEX framework_expiration_due ON framework_expiration_work(collection,due_at,ordinal,entity_key) WHERE due_at IS NOT NULL;
"""
    if version >= 7:
        sql += "CREATE INDEX framework_owner_card_state ON framework_entities(collection,owner_id,card_id,state);"
    if version >= 8:
        sql += "CREATE INDEX framework_code_prefix_expiry ON framework_entities(collection,pool_id,status,created_at,name_sort,entity_key,variant_id); CREATE UNIQUE INDEX framework_code_order_unique ON framework_entities(name_sort) WHERE collection='_codeStock' AND name_sort!='';"
    return sql


def framework_schema_entries(con):
    return [
        (kind, name, table, re.sub(r"\s+", " ", sql).strip() if sql else None)
        for kind, name, table, sql in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        if name.startswith("framework_") or table.startswith("framework_")
    ]


def validate_codec_header(body, version):
    try:
        value = json.loads(body)
        if type(value.get("encrypted")) is int and value["encrypted"] == 1:
            if set(value) != {"encrypted", "iv", "tag", "data"}:
                raise ValueError()
            if (
                len(base64.b64decode(value["iv"], validate=True)) != 12
                or len(base64.b64decode(value["tag"], validate=True)) != 16
                or not base64.b64decode(value["data"], validate=True)
            ):
                raise ValueError()
        elif value != {
            "format": "digital-card.indexed-state",
            "version": version,
            "recordKeys": "blind-v1",
        }:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, KeyError):
        raise RuntimeError(
            "Backup framework authentication marker is invalid."
        ) from None


def validate_database_schema(con, framework):
    version = con.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if framework:
        if "framework_state" in tables:
            columns = [
                row[1] for row in con.execute("PRAGMA table_info(framework_state)")
            ]
            row = con.execute(
                "SELECT schema_version,length(body) FROM framework_state WHERE id=1"
            ).fetchone()
            if (
                version not in [0, 1]
                or columns != ["id", "schema_version", "body"]
                or [
                    (row[1], row[2], row[5])
                    for row in con.execute("PRAGMA table_info(framework_state)")
                ]
                != [
                    ("id", "INTEGER", 1),
                    ("schema_version", "INTEGER", 0),
                    ("body", "TEXT", 0),
                ]
                or len(framework_schema_entries(con)) != 1
                or row is None
                or row[0] != 1
                or not row[1]
                or any(
                    name.startswith("framework_") and name != "framework_state"
                    for name in tables
                )
            ):
                raise RuntimeError(
                    "Backup framework schema is unsupported or incomplete."
                )
        else:
            if version not in range(2, 9):
                raise RuntimeError(
                    "Backup framework schema is unsupported or incomplete."
                )
            with closing(sqlite3.connect(":memory:")) as model:
                model.executescript(normalized_schema_sql(version))
                if framework_schema_entries(con) != framework_schema_entries(model):
                    raise RuntimeError(
                        "Backup framework schema is unsupported or incomplete."
                    )
            if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError(
                    "Backup framework foreign-key relationships are invalid."
                )
            row = con.execute(
                "SELECT schema_version,codec_check FROM framework_meta WHERE id=1"
            ).fetchone()
            if row is None or row[0] != version or not row[1]:
                raise RuntimeError(
                    "Backup framework schema is unsupported or incomplete."
                )
            if version >= 3:
                validate_codec_header(row[1], version)

    else:
        if (
            version not in [0, 1, 2]
            or not {"orders", "registrations"} <= tables
            or (version == 2 and "payment_discovery" not in tables)
        ):
            raise RuntimeError("Backup bridge schema is unsupported or incomplete.")
        if version == 2 and [
            row[1] for row in con.execute("PRAGMA table_info(payment_discovery)")
        ] != ["id", "source", "first_seen", "checked_at"]:
            raise RuntimeError("Backup bridge schema is unsupported or incomplete.")


def recovery_state_probe_script(require_legacy=False, require_normalized=False):
    # Validate opaque headers on private copies; runtime startup authenticates the encrypted state.
    schemas = json.dumps(
        {version: normalized_schema_sql(version) for version in range(2, 9)},
        separators=(",", ":"),
    )
    return (
        r"""
import{copyFileSync,lstatSync,mkdtempSync,rmSync}from'node:fs';
import{join}from'node:path';import{tmpdir}from'node:os';import{DatabaseSync}from'node:sqlite';
const schemas=SCHEMA_MODELS,legacy=REQUIRE_LEGACY,normalized=REQUIRE_NORMALIZED;let temporary,database;
const entries=db=>db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all().filter(x=>x.name.startsWith('framework_')||x.tbl_name.startsWith('framework_')).map(x=>({...x,sql:x.sql?.replace(/\s+/g,' ').trim()}));
function codecHeader(body,version){
 const value=JSON.parse(body);
 if(value?.encrypted===1){
  const binary=(text,size)=>typeof text==='string'&&/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text)&&Buffer.from(text,'base64').length===(size??Buffer.from(text,'base64').length)&&Buffer.from(text,'base64').length>0;
  if(Object.keys(value).sort().join(',')!=='data,encrypted,iv,tag'||!binary(value.iv,12)||!binary(value.tag,16)||!binary(value.data))throw Error('INVALID_MARKER');
 }else if(value?.format!=='digital-card.indexed-state'||value?.version!==version||value?.recordKeys!=='blind-v1'||Object.keys(value).sort().join(',')!=='format,recordKeys,version')throw Error('INVALID_MARKER');
}
function schema(db,framework){
 const version=db.prepare('PRAGMA user_version').get().user_version;
 const tables=new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(x=>x.name));
 const columns=name=>db.prepare('PRAGMA table_info('+name+')').all().map(x=>x.name);
 const same=(a,b)=>JSON.stringify([...a].sort())===JSON.stringify([...b].sort());
 if(framework){
  if(tables.has('framework_state')){
   const row=db.prepare('SELECT schema_version,length(body) AS bytes FROM framework_state WHERE id=1').get();
   if(![0,1].includes(version)||JSON.stringify(db.prepare('PRAGMA table_info(framework_state)').all().map(({name,type,pk})=>({name,type,pk})))!==JSON.stringify([{name:'id',type:'INTEGER',pk:1},{name:'schema_version',type:'INTEGER',pk:0},{name:'body',type:'TEXT',pk:0}])||entries(db).length!==1||!same(columns('framework_state'),['id','schema_version','body'])||!row||row.schema_version!==1||!row.bytes||[...tables].some(x=>x.startsWith('framework_')&&x!=='framework_state'))throw Error('INVALID_SCHEMA');
  }else{
   if(!Object.hasOwn(schemas,version)||db.prepare('PRAGMA foreign_key_check').get())throw Error('INVALID_SCHEMA');
   const model=new DatabaseSync(':memory:');try{model.exec(schemas[version]);if(JSON.stringify(entries(db))!==JSON.stringify(entries(model)))throw Error('INVALID_SCHEMA')}finally{model.close()}
   const row=db.prepare('SELECT schema_version,codec_check FROM framework_meta WHERE id=1').get();
   if(!row||row.schema_version!==version||!row.codec_check)throw Error('INVALID_SCHEMA');
   if(version>=3)codecHeader(row.codec_check,version);
  }
 }else if(![0,1,2].includes(version)||!tables.has('orders')||!tables.has('registrations')||(version===2&&(!tables.has('payment_discovery')||!same(columns('payment_discovery'),['id','source','first_seen','checked_at']))))throw Error('INVALID_SCHEMA');
}
try{
 const source=process.argv[1]??'/opt/card/state';temporary=mkdtempSync(join(process.argv[2]??tmpdir(),'card-recovery-'));let bytes=0;
 for(const file of ['framework.sqlite','bridge.sqlite'])for(const suffix of ['','-wal']){
  const name=file+suffix;let info;try{info=lstatSync(join(source,name))}catch(error){if(suffix&&error.code==='ENOENT')continue;throw error}
  if(!info.isFile()||(bytes+=info.size)>256*1024*1024)throw Error('INVALID_STATE');copyFileSync(join(source,name),join(temporary,name));
 }
 for(const file of ['framework.sqlite','bridge.sqlite']){
  database=new DatabaseSync(join(temporary,file),{readOnly:true});
  if(database.prepare('PRAGMA quick_check').get().quick_check!=='ok')throw Error('INVALID_STATE');schema(database,file==='framework.sqlite');
  if(normalized&&database.prepare('PRAGMA user_version').get().user_version!==(file==='framework.sqlite'?8:2))throw Error('NORMALIZED_STATE_REQUIRED');
  if(legacy&&file==='framework.sqlite'&&!database.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='framework_state'").get())throw Error('LEGACY_SOURCE_REQUIRED');
  if(legacy&&file==='bridge.sqlite')for(const row of database.prepare("SELECT body FROM orders WHERE state NOT IN ('complete','rejected','refunded')").iterate())if(Object.hasOwn(JSON.parse(row.body),'financialVersion'))throw Error('LEGACY_SOURCE_REQUIRED');
  database.close();database=null;
 }
 console.log(JSON.stringify({ok:true}));
}catch{console.error(JSON.stringify({code:'RECOVERY_STATE_UNAVAILABLE'}));process.exitCode=1}
finally{database?.close();if(temporary)rmSync(temporary,{recursive:true,force:true})}
""".replace(
            "SCHEMA_MODELS", schemas
        )
        .replace("REQUIRE_LEGACY", "true" if require_legacy else "false")
        .replace("REQUIRE_NORMALIZED", "true" if require_normalized else "false")
    )


def service_image_id(directory, service):
    profile = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    # Compose can report a platform config digest that is not a runnable image ID.
    return run(
        ["docker", "image", "inspect", profile["services"][service]["image"],
         "--format", "{{.Id}}"], capture=True
    ).strip()


def stopped_state_probe(directory, meta, script):
    image = service_image_id(directory, "cards")
    return run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=272m",
            "--user",
            "10002:10002",
            "--mount",
            "type=volume,source="
            + meta["project"]
            + "_card_state,target=/opt/card/state,readonly",
            "--entrypoint",
            "node",
            image,
            "--input-type=module",
            "-e",
            script,
        ],
        capture=True,
        structured_error=True,
    ).strip()


def backup(directory, meta, restart=True, require_resolved=False, require_legacy=False):
    if (directory / "migration.pending.json").exists() and restart:
        raise RuntimeError(
            "Migration ingress is fenced. Use backup --keep-stopped to preserve current data without reopening services."
        )
    if (directory / "migration.pending.json").exists() and json.loads(
        (directory / "migration.pending.json").read_text(encoding="utf-8")
    ).get("phase") == "staging":
        raise RuntimeError(
            "Complete the recorded migration staging before taking another backup. The original consistent migration backup is retained."
        )
    upgrade_marker = directory / "upgrade.pending.json"
    if upgrade_marker.exists():
        phase = json.loads(upgrade_marker.read_text(encoding="utf-8")).get("phase")
        if restart or phase in ["staging", "rolling_back", "rollback_activating"]:
            raise RuntimeError(
                "Upgrade is fenced. Finish recorded staging/rollback first; only a staged candidate may use backup --keep-stopped."
            )
    if require_resolved and require_legacy:
        raise ValueError(
            "Legacy migration and resolved-only backup guards are mutually exclusive."
        )
    no_players(directory)
    destination = (
        directory
        / "backups"
        / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
    )
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive_media(directory, destination / "media.tar.gz")
    image = service_image_id(directory, "cards")
    success = False
    restarted = False
    stage = "writer shutdown"
    failure = None
    try:
        compose(directory, "stop", "web", "cards", "cosmic")
        stage = "stopped game-account check"
        no_players(directory)
        if require_legacy:
            stage = "stopped legacy-migration check"
            stopped_state_probe(
                directory, meta, recovery_state_probe_script(require_legacy=True)
            )
        if require_resolved:
            stage = "stopped pending-purchase check"
            pending = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=272m",
                    "--user",
                    "10002:10002",
                    "--mount",
                    "type=volume,source="
                    + meta["project"]
                    + "_card_state,target=/opt/card/state,readonly",
                    "--entrypoint",
                    "node",
                    image,
                    "--input-type=module",
                    "-e",
                    pending_guard_script(),
                ],
                capture=True,
                structured_error=True,
            ).strip()
            if pending != "0":
                raise RuntimeError(
                    "Unresolved purchases remain at the stopped-writer boundary. Complete outstanding purchases before changing the installation."
                )
        stage = "game database dump"
        sql = compose(
            directory,
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -u root --single-transaction --routines --events --triggers --hex-blob --databases cosmic --add-drop-database',
            capture=True,
        )
        private_file(destination / "database.sql", sql)
        stage = "card state archive"
        state = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "10002:10002",
                "--mount",
                "type=volume,source="
                + meta["project"]
                + "_card_state,target=/state,readonly",
                "--entrypoint",
                "tar",
                image,
                "-C",
                "/state",
                "-czf",
                "-",
                ".",
            ],
            data=b"",
            capture=True,
        )
        private_file(destination / "state.tar.gz", state)
        stage = "private configuration copy"
        for name in PRIVATE_BACKUP_FILES:
            path = directory / "private" / name
            if path.is_file():
                private_file(destination / "private" / name, path.read_bytes())
        for name in ["compose.json", "installation.json", "images.lock.json"]:
            shutil.copy2(directory / name, destination / name)
        for marker_name in ["migration.pending.json", "upgrade.pending.json"]:
            marker_path = directory / marker_name
            if marker_path.exists():
                if marker_path.is_symlink():
                    raise RuntimeError("Operation marker must not be a symbolic link.")
                private_file(destination / marker_name, marker_path.read_bytes())
        (destination / "support").mkdir()
        shutil.copy2(
            directory / "support/nginx.conf", destination / "support/nginx.conf"
        )
        profile = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
        stage = "runtime image inspection"
        images = {
            profile["services"][name]["image"]: run(
                [
                    "docker",
                    "image",
                    "inspect",
                    profile["services"][name]["image"],
                    "--format",
                    "{{.Id}}",
                ],
                capture=True,
            ).strip()
            for name in ["cosmic", "cards"]
        }
        stage = "runtime image archive"
        run(
            [
                "docker",
                "image",
                "save",
                "--output",
                str(destination / "runtime-images.tar"),
                *images,
            ]
        )
        private_file(
            destination / "runtime-images.json", json.dumps(images, indent=2) + "\n"
        )
        sums = {
            p.relative_to(destination).as_posix(): file_hash(p)
            for p in destination.rglob("*")
            if p.is_file()
        }
        private_file(destination / "checksums.json", json.dumps(sums, indent=2) + "\n")
        stage = "backup integrity validation"
        validate_backup(destination)
        success = True
    except Exception as error:
        failure = error
    finally:
        if (restart or not success) and not any(
            (directory / name).exists()
            for name in ["migration.pending.json", "upgrade.pending.json"]
        ):
            try:
                start(directory, meta)
                restarted = True
            except Exception:
                if success:
                    raise RuntimeError(
                        "Backup was created but service restart failed. Keep the private backup and inspect services: "
                        + str(destination)
                    ) from None
                raise RuntimeError(
                    "Backup failed during "
                    + stage
                    + " and service restart failed. Inspect services and retain the incomplete private backup: "
                    + str(destination)
                ) from None
    if failure is not None:
        code = getattr(failure, "code", None)
        reason = (
            " (" + code + ")"
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", code)
            else ""
        )
        action = (
            " Complete outstanding purchases before changing the installation."
            if stage == "stopped pending-purchase check"
            and isinstance(failure, RuntimeError)
            and str(failure).startswith("Unresolved purchases")
            else ""
        )
        raise RuntimeError(
            "Backup failed during "
            + stage
            + reason
            + (
                "; previous services restarted."
                if restarted
                else "; services remain stopped behind the migration fence."
            )
            + action
            + " Incomplete private backup: "
            + str(destination)
        ) from None
    print("Private backup: " + str(destination))
    return destination


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_backup(path):
    path = Path(path).resolve()
    if (path / "checksums.json").is_symlink() or (
        path / "checksums.json"
    ).stat().st_size > 65536:
        raise RuntimeError("Backup checksum manifest is invalid.")
    sums = json.loads((path / "checksums.json").read_text(encoding="utf-8"))
    allowed = {
        "database.sql",
        "state.tar.gz",
        "media.tar.gz",
        "compose.json",
        "installation.json",
        "images.lock.json",
        "runtime-images.tar",
        "runtime-images.json",
        "migration.pending.json",
        "upgrade.pending.json",
        "support/nginx.conf",
        *("private/" + name for name in PRIVATE_BACKUP_FILES),
    }
    if (
        not isinstance(sums, dict)
        or len(sums) > len(allowed)
        or not set(sums) <= allowed
        or not {
            "database.sql",
            "state.tar.gz",
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
        }
        <= set(sums)
    ):
        raise RuntimeError("Unexpected or incomplete backup files.")
    for name, expected in sums.items():
        p = path / name
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or p.is_symlink()
            or not p.resolve().is_relative_to(path)
            or not p.is_file()
            or file_hash(p) != expected
        ):
            raise RuntimeError("Backup integrity check failed.")
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    if actual != set(sums) | {"checksums.json"}:
        raise RuntimeError("Unexpected backup files outside the checksum manifest.")
    with tarfile.open(path / "state.tar.gz") as archive:
        allowed_state = {
            "framework.sqlite",
            "framework.sqlite-wal",
            "framework.sqlite-shm",
            "bridge.sqlite",
            "bridge.sqlite-wal",
            "bridge.sqlite-shm",
            "smoke-receipt.json",
            "smoke-receipt.json.pending",
        }
        seen = set()
        total = 0
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if member.isdir() and member.name in [".", "./"]:
                continue
            total += member.size
            if (
                not member.isfile()
                or name not in allowed_state
                or name in seen
                or total > 256 * 1024 * 1024
            ):
                raise RuntimeError("Unexpected state archive member.")
            seen.add(name)
        if not {"framework.sqlite", "bridge.sqlite"} <= seen:
            raise RuntimeError(
                "State archive must contain framework.sqlite and bridge.sqlite."
            )
    if (path / "media.tar.gz").exists():
        validate_media_archive(path / "media.tar.gz")
    validate_state_databases(path / "state.tar.gz")
    for marker_name in ["migration.pending.json", "upgrade.pending.json"]:
        if not (path / marker_name).exists():
            continue
        marker_path = path / marker_name
        if marker_path.stat().st_size > 65536:
            raise RuntimeError("Backup migration marker is invalid.")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        profile = json.loads((path / "compose.json").read_text(encoding="utf-8"))
        staged = json.loads(json.dumps(marker.get("candidate", {})))
        if (
            not isinstance(staged.get("services"), dict)
            or "network" not in staged["services"]
        ):
            raise RuntimeError("Backup migration marker is invalid.")
        staged["services"]["network"]["ports"] = []
        if (
            marker.get("format") != 1
            or marker.get("phase") not in ["migrating", "validating", "activating"]
            or marker.get("meta")
            != json.loads((path / "installation.json").read_text(encoding="utf-8"))
            or marker.get("binding") != migration_binding(path)
            or marker.get("runtimeImages")
            != json.loads((path / "runtime-images.json").read_text(encoding="utf-8"))
            or not (
                profile == staged
                or marker.get("phase") == "activating"
                and profile == marker.get("candidate")
            )
        ):
            raise RuntimeError("Backup migration marker is invalid.")
    return path


def validate_state_databases(path):
    with tempfile.TemporaryDirectory(prefix="cosmic-backup-check-") as temporary:
        root = Path(temporary)
        with tarfile.open(path) as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if member.isfile() and (
                    name.endswith(".sqlite")
                    or name.endswith(".sqlite-wal")
                    or name.endswith(".sqlite-shm")
                ):
                    with archive.extractfile(member) as incoming, (root / name).open(
                        "wb"
                    ) as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    (root / name).chmod(0o600)
        for name in ["framework.sqlite", "bridge.sqlite"]:
            try:
                con = sqlite3.connect((root / name).as_uri() + "?mode=ro", uri=True)
                try:
                    if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError(
                            "Backup SQLite integrity or schema check failed."
                        )
                    validate_database_schema(con, name == "framework.sqlite")
                finally:
                    con.close()
            except sqlite3.Error:
                raise RuntimeError(
                    "Backup SQLite integrity or schema check failed."
                ) from None


def backup_identity(source):
    saved = json.loads((source / "installation.json").read_text(encoding="utf-8"))
    if (
        saved.get("format") != 1
        or not isinstance(saved.get("directory"), str)
        or not Path(saved["directory"]).is_absolute()
        or not re.fullmatch(r"cosmic-cards-[a-f0-9]{10}", saved.get("project", ""))
        or not re.fullmatch(r"[a-f0-9]{64}", saved.get("bridgeHash", ""))
        or not re.fullmatch(r"[a-f0-9]{40}", saved.get("cosmicRevision", ""))
    ):
        raise RuntimeError("Backup installation identity is invalid.")
    profile = json.loads((source / "compose.json").read_text(encoding="utf-8"))
    images = json.loads((source / "runtime-images.json").read_text(encoding="utf-8"))
    expected = {profile["services"][name]["image"] for name in ["cosmic", "cards"]}
    if (
        profile.get("name") != saved["project"]
        or set(images) != expected
        or any(
            not name.startswith(saved["project"] + ":")
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            for name, digest in images.items()
        )
    ):
        raise RuntimeError("Backup runtime identity differs from its installation.")
    return saved


def load_backup_images(source):
    run(["docker", "image", "load", "--input", str(source / "runtime-images.tar")])
    verify_backup_runtime_images(source)


def verify_backup_runtime_images(source):
    images = json.loads((source / "runtime-images.json").read_text(encoding="utf-8"))
    for name, digest in images.items():
        if (
            run(
                ["docker", "image", "inspect", name, "--format", "{{.Id}}"],
                capture=True,
            ).strip()
            != digest
        ):
            raise RuntimeError("Backup runtime image identity differs.")


def recover_empty(directory, source, expected_project, relocate=False, retry=False):
    source = validate_backup(source)
    if any(
        (source / name).exists()
        for name in ["migration.pending.json", "upgrade.pending.json"]
    ):
        raise RuntimeError(
            "This backup contains unfinished migration state. Automatic recovery cannot discard its ingress fence. Preserve the marker, current data and original migration backup for qualified recovery."
        )
    saved = backup_identity(source)
    if saved["project"] != expected_project:
        raise RuntimeError("Backup belongs to a different installation project.")
    target = Path(directory).absolute()
    if (
        target.is_symlink()
        or target.resolve() != target
        or target == target.parent
        or target == Path.home()
        or target == ROOT
        or ROOT in target.parents
    ):
        raise RuntimeError(
            "Choose a separate absolute recovery directory without symbolic links."
        )
    backup_digest = file_hash(source / "checksums.json")
    resuming, preserve_current = False, False
    if target.exists() and any(target.iterdir()):
        if not retry:
            raise RuntimeError(
                "Disaster recovery requires an empty destination; no files were changed."
            )
        marker = target / "recovery.pending.json"
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 4096:
            raise RuntimeError("Retry requires an incomplete recovery marker.")
        recovery = json.loads(marker.read_text(encoding="utf-8"))
        current_directory, current = load(target)
        profile = json.loads((target / "compose.json").read_text(encoding="utf-8"))
        if (
            current_directory != target
            or current["project"] != saved["project"]
            or recovery.get("project") != saved["project"]
            or recovery.get("backupDigest") != backup_digest
        ):
            raise RuntimeError(
                "Retry must use the exact recovery directory, project and verified backup."
            )
        if recovery.get("phase") == "activating":
            preserve_current = True
        elif (
            recovery.get("phase") not in ["prepared", "validating"]
            or profile["services"]["network"]["ports"]
        ):
            raise RuntimeError(
                "Recovery phase is invalid or ingress may have opened before its recorded phase. Preserve current data."
            )
        resuming = True
    elif retry:
        raise RuntimeError("Retry requires the existing incomplete recovery directory.")
    if not relocate and str(target) != saved["directory"]:
        raise RuntimeError(
            "Use the original directory or explicitly select --relocate."
        )
    if target == source or target in source.parents:
        raise RuntimeError("Keep the backup outside the recovery destination.")
    prerequisites()
    containers = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=" + saved["project"],
        ],
        capture=True,
    ).strip()
    volumes = run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + saved["project"],
        ],
        capture=True,
    ).strip()
    for suffix in ["_database", "_card_state"]:
        volumes += run(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                "name=^" + saved["project"] + suffix + "$",
            ],
            capture=True,
        ).strip()
    if (containers or volumes) and not resuming:
        raise RuntimeError(
            "This installation still has Docker resources. Use in-place restore or remove only its disposable resources first."
        )
    locks = json.loads((source / "images.lock.json").read_text(encoding="utf-8"))
    if set(locks) != set(RUNTIME["images"]) or any(
        not isinstance(v, str)
        or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", v)
        for v in locks.values()
    ):
        raise RuntimeError("Backup public runtime image locks are invalid.")
    if preserve_current:
        if (
            current["bridgeHash"] != saved["bridgeHash"]
            or current["cosmicRevision"] != saved["cosmicRevision"]
            or json.loads((target / "images.lock.json").read_text(encoding="utf-8"))
            != locks
        ):
            raise RuntimeError(
                "Activation retry must use the recorded runtime identity and image locks."
            )
        resume_recovery_activation(target, current, source, locks)
        return
    load_backup_images(source)
    for name in ["database", "node", "web"]:
        run(["docker", "pull", locks[name]])
    recovered = {**saved, "directory": str(target)}
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.chmod(0o700)
    (target / "private").mkdir(mode=0o700, exist_ok=True)
    ensure_media(target)
    shutil.copytree(TEMPLATES, target / "support", dirs_exist_ok=True)
    for file in (source / "private").iterdir():
        if file.is_file():
            private_file(target / "private" / file.name, file.read_bytes())
    for name in ["config.yaml", "catalog.json"]:
        (target / "private" / name).chmod(0o644)
    private_file(target / "installation.json", json.dumps(recovered, indent=2) + "\n")
    private_file(target / "images.lock.json", json.dumps(locks, indent=2) + "\n")
    cold_profile = create_compose(recovered, locks)
    cold_profile["services"]["network"]["ports"] = []
    private_file(target / "compose.json", json.dumps(cold_profile, indent=2) + "\n")
    private_file(target / ".gitignore", "*\n")
    private_file(
        target / "recovery.pending.json",
        json.dumps(
            {
                "project": saved["project"],
                "backupDigest": backup_digest,
                "phase": "prepared",
            }
        )
        + "\n",
    )
    try:
        compose(target, "up", "-d", "--no-build", "--wait", "db", "network")
        restore_data(target, recovered, source, restore_configuration=False)
        (target / "recovery.pending.json").unlink()
    except Exception as error:
        try:
            compose(target, "stop", "web", "cards", "cosmic", "network", "db")
        except Exception:
            raise RuntimeError(
                "Disaster recovery failed and service shutdown also failed. Inspect Docker services; keep the backup and recovery directory for diagnosis."
            ) from error
        raise RuntimeError(
            "Disaster recovery failed; services were stopped. Preserve the backup and inspect the recovery directory before retrying."
        ) from error
    print(
        "Disaster recovery completed with original project, accounts, keys and balances: "
        + str(target)
    )


def resume_recovery_activation(directory, meta, source, locks):
    # This phase can include post-backup transactions. Do not import or copy any data.
    verify_backup_runtime_images(source)
    for suffix in ["_database", "_card_state"]:
        run(
            [
                "docker",
                "volume",
                "inspect",
                meta["project"] + suffix,
                "--format",
                "{{.Name}}",
            ],
            capture=True,
        )
    compose(directory, "up", "-d", "--no-build", "--wait", "db")
    no_players(directory)
    image = create_compose(meta, locks)["services"]["cards"]["image"]
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=272m",
                "--user",
                "10002:10002",
                "--mount",
                "type=volume,source="
                + meta["project"]
                + "_card_state,target=/opt/card/state,readonly",
                "--entrypoint",
                "node",
                image,
                "--input-type=module",
                "-e",
                recovery_state_probe_script(),
            ],
            capture=True,
            structured_error=True,
        )
    except CommandError as error:
        code = (
            error.code
            if isinstance(error.code, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", error.code)
            else "RECOVERY_STATE_UNAVAILABLE"
        )
        raise RuntimeError(
            "Activation retry validation failed ("
            + code
            + "); current databases and recovery marker preserved. Inspect the recorded runtime/state and repeat the same recover --retry command."
        ) from None
    try:
        activate_configuration(directory, meta, create_compose(meta, locks))
        (directory / "recovery.pending.json").unlink()
    except Exception as error:
        try:
            compose(directory, "stop", "web", "cards", "cosmic", "network", "db")
        except Exception:
            raise RuntimeError(
                "Activation retry and service shutdown failed. Current data and the recovery marker were preserved; inspect services before retrying."
            ) from error
        raise RuntimeError(
            "Activation retry failed; services stopped, current data and the recovery marker preserved. Correct the fault and repeat the same recover --retry command."
        ) from error
    print(
        "Recovery activation completed; current databases and transactions retained: "
        + str(directory)
    )


def restore_data(
    directory, meta, source, restore_configuration=True, *, allow_upgrade=False
):
    compose(directory, "stop", "web", "cards", "cosmic")
    # Database credentials do not change during restore; backups belong to this installation.
    if (directory / "private/database.env").read_bytes() != (
        source / "private/database.env"
    ).read_bytes():
        raise RuntimeError(
            "Restore database credentials differ from this installation."
        )
    images = json.loads((source / "runtime-images.json").read_text(encoding="utf-8"))
    if len(images) != 2 or any(
        not name.startswith(meta["project"] + ":")
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        for name, digest in images.items()
    ):
        raise RuntimeError("Unexpected backup runtime images.")
    load_backup_images(source)
    # Reset only the managed application's fixed database, including tables created
    # after the backup. The restricted application user cannot modify other databases.
    sql = (
        b"DROP DATABASE IF EXISTS cosmic;\nCREATE DATABASE cosmic;\nUSE cosmic;\n"
        + (source / "database.sql").read_bytes()
    )
    compose(
        directory,
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u cosmic_app cosmic',
        data=sql,
    )
    source_profile = json.loads((source / "compose.json").read_text(encoding="utf-8"))
    image = source_profile["services"]["cards"]["image"]
    run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--user",
            "10002:10002",
            "--mount",
            "type=volume,source="
            + meta["project"]
            + "_card_state,target=/opt/card/state",
            "--entrypoint",
            "sh",
            image,
            "-c",
            'find /opt/card/state -maxdepth 1 -type f \\( -name "*.sqlite*" -o -name "smoke-receipt.json*" \\) -delete; tar -C /opt/card/state -xzf -',
        ],
        data=(source / "state.tar.gz").read_bytes(),
    )
    for name in ["game.env", "cards.env", "catalog.json", "config.yaml"]:
        private_file(
            directory / "private" / name, (source / "private" / name).read_bytes()
        )
    (directory / "private/config.yaml").chmod(0o644)
    (directory / "private/catalog.json").chmod(0o644)
    if restore_configuration:
        private_file(directory / "compose.json", (source / "compose.json").read_bytes())
        private_file(
            directory / "installation.json", (source / "installation.json").read_bytes()
        )
    private_file(
        directory / "images.lock.json", (source / "images.lock.json").read_bytes()
    )
    if restore_configuration:
        (directory / "support/nginx.conf").write_bytes(
            (source / "support/nginx.conf").read_bytes()
        )
    restore_media(directory, source)
    profile = (
        json.loads((directory / "compose.json").read_text(encoding="utf-8"))
        if restore_configuration
        else create_compose(
            meta, json.loads((source / "images.lock.json").read_text(encoding="utf-8"))
        )
    )
    activate_configuration(directory, meta, profile, allow_upgrade=allow_upgrade)


def recreate_namespace(directory):
    # Database data and its service stay intact; consumers must leave the old namespace first.
    compose(directory, "stop", "web", "cards", "cosmic", "network")
    compose(directory, "rm", "-f", "web", "cards", "cosmic", "network")


def activate_configuration(directory, meta, profile, *, allow_upgrade=False):
    staged = json.loads(json.dumps(profile))
    staged["services"]["network"]["ports"] = []
    private_file(directory / "compose.json", json.dumps(staged, indent=2) + "\n")
    recovery_marker = directory / "recovery.pending.json"
    if recovery_marker.exists():
        recovery = json.loads(recovery_marker.read_text(encoding="utf-8"))
        private_file(
            recovery_marker,
            json.dumps(
                {
                    **recovery,
                    "phase": (
                        "activating"
                        if recovery.get("phase") == "activating"
                        else "validating"
                    ),
                }
            )
            + "\n",
        )
    recreate_namespace(directory)
    start(directory, meta, allow_recovery=True, allow_upgrade=allow_upgrade)
    if recovery_marker.exists():
        private_file(
            recovery_marker, json.dumps({**recovery, "phase": "activating"}) + "\n"
        )
    if allow_upgrade:
        marker = json.loads(
            (directory / "upgrade.pending.json").read_text(encoding="utf-8")
        )
        write_migration(directory, marker, "rollback_activating")
    private_file(directory / "compose.json", json.dumps(profile, indent=2) + "\n")
    try:
        recreate_namespace(directory)
        start(directory, meta, allow_recovery=True, allow_upgrade=allow_upgrade)
    except Exception as error:
        try:
            compose(directory, "stop", "web", "cards", "cosmic")
        except Exception:
            raise ActivationError(
                "Ingress activation and service shutdown failed. Preserve current data and inspect services; no backup was restored."
            ) from error
        raise ActivationError(
            "Ingress activation failed; services stopped and current data preserved. Correct configuration before starting; do not restore over new transactions."
        ) from error


def restore(directory, meta, source):
    if any(
        (directory / name).exists()
        for name in ["migration.pending.json", "upgrade.pending.json"]
    ):
        raise RuntimeError(
            "Upgrade/migration data must be preserved. Finish the recorded migration; do not restore an older backup over new payment identities."
        )
    source = validate_backup(source)
    if any(
        (source / name).exists()
        for name in ["migration.pending.json", "upgrade.pending.json"]
    ):
        raise RuntimeError(
            "Automatic restore cannot discard an unfinished migration fence. Preserve the current-state archive, marker and original migration backup."
        )
    saved = backup_identity(source)
    if saved["directory"] != str(directory) or saved["project"] != meta["project"]:
        raise RuntimeError("Restore this backup to its original installation only.")
    safety = backup(directory, meta, restart=False)
    try:
        restore_data(directory, saved, source)
    except ActivationError as error:
        raise RuntimeError(str(error) + " Safety backup: " + str(safety)) from None
    except Exception as error:
        try:
            restore_data(directory, meta, safety)
        except Exception:
            raise RuntimeError(
                "Restore and rollback both failed. Keep services stopped and recover the safety backup: "
                + str(safety)
            ) from error
        raise RuntimeError(
            "Restore failed; the pre-restore backup was restored."
        ) from None
    print("Backup restored and connection verified.")


def migration_candidate_probe_script():
    return r"""
import{mkdtempSync,rmSync,accessSync}from'node:fs';import{join}from'node:path';
import{tmpdir}from'node:os';import{DatabaseSync}from'node:sqlite';import{CardFramework}from'@digital-card/framework';
import{SQLiteStore}from'@digital-card/framework/sqlite';import{Journal}from'./src/journal.mjs';
let root,store,journal,db;
try{
 for(const method of ['prepareExternalPurchase','commitExternalPurchase','cancelExternalPurchase','confirmExternalCompensation','pendingExternalPurchases','resolveLegacyExternalPurchase','backfillCompletionReservations'])if(typeof CardFramework.prototype[method]!=='function')throw Error('MISSING_RECOVERY_API');
 accessSync('./tools/payments.mjs');root=mkdtempSync(join(tmpdir(),'card-candidate-'));
 store=new SQLiteStore(join(root,'framework.sqlite'),{encryptionKey:Buffer.alloc(32,7)});
 db=new DatabaseSync(join(root,'framework.sqlite'),{readOnly:true});
 if(db.prepare('PRAGMA user_version').get().user_version!==8||!db.prepare("SELECT 1 FROM sqlite_master WHERE name='framework_meta'").get()||db.prepare("SELECT 1 FROM sqlite_master WHERE name='framework_state'").get())throw Error('UNSUPPORTED_RECOVERY_RUNTIME');db.close();db=null;
 const authenticated=new SQLiteStore(join(root,'framework.sqlite'),{encryptionKey:Buffer.alloc(32,7),readOnly:true});try{if(authenticated.diagnostics().storageSchema!==8)throw Error('UNSUPPORTED_RECOVERY_RUNTIME')}finally{authenticated.close()}
 journal=new Journal(join(root,'bridge.sqlite'));
 if(journal.db.prepare('PRAGMA user_version').get().user_version!==2||!journal.db.prepare("SELECT 1 FROM sqlite_master WHERE name='payment_discovery'").get())throw Error('UNSUPPORTED_RECOVERY_RUNTIME');
 console.log(JSON.stringify({ok:true,frameworkSchema:8,bridgeSchema:2}));
}catch{console.error(JSON.stringify({code:'MIGRATION_CANDIDATE_UNSUPPORTED'}));process.exitCode=1}
finally{db?.close();journal?.close();store?.close();if(root)rmSync(root,{recursive:true,force:true})}
"""


def migration_closure_probe_script():
    return r"""
import{copyFileSync,lstatSync,mkdtempSync,rmSync}from'node:fs';import{join}from'node:path';
import{DatabaseSync}from'node:sqlite';import{CardFramework}from'@digital-card/framework';
import{SQLiteStore}from'@digital-card/framework/sqlite';import{PaymentAuthority,paymentProviderId,paymentActor}from'./src/payment-authority.mjs';import{createGameClient}from'./src/protocol.mjs';
let root,store,core,db;
try{
 root=mkdtempSync('/tmp/card-closure-');let bytes=0;
 for(const file of ['framework.sqlite','bridge.sqlite'])for(const suffix of ['','-wal']){
  const name=file+suffix;let stat;try{stat=lstatSync('/opt/card/state/'+name)}catch(error){if(suffix&&error.code==='ENOENT')continue;throw error}
  if(!stat.isFile()||(bytes+=stat.size)>256*1024*1024)throw Error('INVALID_STATE');copyFileSync('/opt/card/state/'+name,join(root,name));
 }
 const key=Buffer.from(process.env.STATE_KEY??'','base64');if(key.length!==32)throw Error('INVALID_KEY');
 store=new SQLiteStore(join(root,'framework.sqlite'),{encryptionKey:key,readOnly:true});
 if(store.diagnostics().storageSchema!==8)throw Error('INVALID_SCHEMA');
 core=new CardFramework({store,externalPurchaseProviders:{[paymentProviderId]:new PaymentAuthority()}});
 const pending=core.pendingExternalPurchases(paymentActor,{providerId:paymentProviderId,after:null,limit:1});
 if(!Array.isArray(pending.items)||pending.items.length>1||pending.nextCursor!==null&&typeof pending.nextCursor!=='string')throw Error('INVALID_PAGE');
 db=new DatabaseSync(join(root,'bridge.sqlite'),{readOnly:true});
 if(db.prepare('PRAGMA user_version').get().user_version!==2||db.prepare('PRAGMA quick_check').get().quick_check!=='ok')throw Error('INVALID_SCHEMA');
 const journal=db.prepare("SELECT COUNT(*) AS n FROM orders WHERE state NOT IN ('complete','rejected','refunded')").get().n;
 const orphans=db.prepare('SELECT COUNT(*) AS n FROM payment_discovery').get().n;
 const game=createGameClient({url:process.env.GAME_URL,secret:process.env.GAME_SHARED_KEY});
 // The authenticated pending route requires the native lease without calling stopped card writers.
 const native=await game('/payment/pending',{after:null,limit:1});
 if(!Array.isArray(native.items)||native.items.length>1||native.nextCursor!==null&&typeof native.nextCursor!=='string')throw Error('INVALID_PAGE');
 const clear=journal===0&&orphans===0&&pending.items.length===0&&pending.nextCursor===null&&native.items.length===0&&native.nextCursor===null;
 console.log(JSON.stringify({ok:true,clear,journal,orphans,frameworkPending:pending.items.length>0,nativePending:native.items.length>0}));
}catch(error){const code=/^[A-Z][A-Z0-9_]{0,80}$/.test(error.code??'')?error.code:'MIGRATION_CLOSURE_UNAVAILABLE';console.error(JSON.stringify({code}));process.exitCode=1}
finally{db?.close();if(core)core.close();else store?.close();if(root)rmSync(root,{recursive:true,force:true})}
"""


def migration_binding(directory):
    names = [
        "installation.json",
        "images.lock.json",
        *[
            "private/" + name
            for name in [
                "cards.env",
                "game.env",
                "database.env",
                "config.yaml",
                "catalog.json",
            ]
        ],
    ]
    return {name: file_hash(directory / name) for name in names}


def atomic_migration_file(path, data):
    if path.is_symlink():
        raise RuntimeError("Migration paths must not be symbolic links.")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".migration-", dir=path.parent
    )
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def migration_staging_bytes(marker, safety):
    if set(marker["oldBinding"]) != set(migration_binding(safety)):
        raise RuntimeError("Recorded configuration binding paths differ.")
    values = {name: (safety / name).read_bytes() for name in marker["oldBinding"]}
    values["installation.json"] = (json.dumps(marker["meta"], indent=2) + "\n").encode()
    values["images.lock.json"] = (
        json.dumps(marker["candidateLocks"], indent=2) + "\n"
    ).encode()
    cards = values["private/cards.env"].decode("utf-8")
    settings = {
        line.split("=", 1)[0]
        for line in cards.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    if "ENABLE_SERIES_ONE_REWARDS" not in settings:
        cards = (
            cards.rstrip()
            + "\nENABLE_SERIES_ONE_REWARDS="
            + ("1" if marker["meta"]["seriesOneEnabled"] else "0")
            + "\n"
        )
    if "ASSET_ROOT" not in settings:
        cards = cards.rstrip() + "\nASSET_ROOT=/assets\n"
    values["private/cards.env"] = cards.encode()
    return values


def stage_legacy_migration(directory, marker, safety):
    expected_old = {name: file_hash(safety / name) for name in marker["oldBinding"]}
    values = migration_staging_bytes(marker, safety)
    expected_new = {
        name: hashlib.sha256(value).hexdigest() for name, value in values.items()
    }
    if marker["oldBinding"] != expected_old or marker["newBinding"] != expected_new:
        raise RuntimeError(
            "Migration staging identity differs from its verified backup."
        )
    for name in expected_old:
        path = directory / name
        if path.is_symlink() or file_hash(path) not in [
            expected_old[name],
            expected_new[name],
        ]:
            raise RuntimeError(
                "Migration staging keys or configuration differ from recorded old/new identities."
            )
    profile = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    staged = json.loads(json.dumps(marker["candidate"]))
    staged["services"]["network"]["ports"] = []
    original_profile = json.loads((safety / "compose.json").read_text(encoding="utf-8"))
    if profile not in [original_profile, staged]:
        raise RuntimeError(
            "Migration staging ingress differs from the recorded profiles."
        )
    atomic_migration_file(
        directory / "compose.json", json.dumps(staged, indent=2) + "\n"
    )
    for name, value in values.items():
        if file_hash(directory / name) != expected_new[name]:
            atomic_migration_file(directory / name, value)
    marker = {**marker, "phase": "migrating", "binding": migration_binding(directory)}
    return write_migration(directory, marker)


def migration_images(profile):
    return {
        profile["services"][service]["image"]: run(
            [
                "docker",
                "image",
                "inspect",
                profile["services"][service]["image"],
                "--format",
                "{{.Id}}",
            ],
            capture=True,
        ).strip()
        for service in ["cosmic", "cards"]
    }


def write_migration(directory, marker, phase=None):
    if phase is not None:
        marker = {
            **marker,
            "phase": (
                marker["phase"]
                if marker["phase"] in ["activating", "rollback_activating"]
                else phase
            ),
        }
    atomic_migration_file(
        directory
        / (
            "upgrade.pending.json"
            if marker.get("kind") == "upgrade"
            else "migration.pending.json"
        ),
        json.dumps(marker, indent=2) + "\n",
    )
    return marker


def payment_closure(directory, meta, profile):
    namespace = compose(directory, "ps", "-q", "network", capture=True).strip()
    return json.loads(
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "container:" + namespace,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=272m",
                "--user",
                "10002:10002",
                "--env-file",
                str(directory / "private/cards.env"),
                "--mount",
                "type=volume,source="
                + meta["project"]
                + "_card_state,target=/opt/card/state,readonly",
                "--entrypoint",
                "node",
                profile["services"]["cards"]["image"],
                "--input-type=module",
                "-e",
                migration_closure_probe_script(),
            ],
            capture=True,
            structured_error=True,
        )
    )


def finish_legacy_migration(directory, meta):
    path = directory / "migration.pending.json"
    if not path.is_file():
        raise RuntimeError("No recorded legacy migration exists for this installation.")
    marker = json.loads(path.read_text(encoding="utf-8"))
    if (
        marker.get("format") != 1
        or marker.get("phase")
        not in ["staging", "migrating", "validating", "activating"]
        or meta not in [marker.get("meta"), marker.get("oldMeta")]
    ):
        raise RuntimeError(
            "The recorded migration identity, keys or configuration differ. Preserve current data and the original marker."
        )
    safety = validate_backup(marker["backup"])
    original = backup_identity(safety)
    if (
        file_hash(safety / "checksums.json") != marker["backupDigest"]
        or original["project"] != meta["project"]
        or original["directory"] != str(directory)
    ):
        raise RuntimeError("The recorded migration backup identity differs.")
    if marker["phase"] == "staging":
        if (
            marker.get("oldMeta") != original
            or create_compose(marker["meta"], marker["candidateLocks"])
            != marker["candidate"]
            or migration_images(marker["candidate"]) != marker["runtimeImages"]
        ):
            raise RuntimeError("The recorded staging source or runtime differs.")
        no_players(directory)
        marker = stage_legacy_migration(directory, marker, safety)
        meta = marker["meta"]
    if marker.get("meta") != meta or marker.get("binding") != migration_binding(
        directory
    ):
        raise RuntimeError(
            "The recorded migration keys or configuration differ. Preserve current data and the marker."
        )
    profile = create_compose(
        meta, json.loads((directory / "images.lock.json").read_text(encoding="utf-8"))
    )
    if (
        profile != marker["candidate"]
        or migration_images(profile) != marker["runtimeImages"]
    ):
        raise RuntimeError("The recorded migration runtime identity differs.")
    previous = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    staged = json.loads(json.dumps(profile))
    staged["services"]["network"]["ports"] = []
    if previous != staged and not (
        marker["phase"] == "activating" and previous == profile
    ):
        raise RuntimeError(
            "The migration ingress configuration differs from its recorded phase."
        )
    no_players(directory)
    marker = write_migration(directory, marker, "validating")
    atomic_migration_file(
        directory / "compose.json", json.dumps(staged, indent=2) + "\n"
    )
    try:
        recreate_namespace(directory)
        compose(
            directory, "up", "-d", "--no-build", "--wait", "db", "network", "cosmic"
        )
        compose(directory, "up", "-d", "--no-build", "cards")
        # Allow the existing state migration to finish; never reopen ingress for diagnostics.
        ready = False
        for attempt in range(30):
            try:
                stopped_state_probe(
                    directory,
                    meta,
                    recovery_state_probe_script(require_normalized=True),
                )
                ready = True
                break
            except Exception:
                if attempt < 29:
                    time.sleep(2)
        if not ready:
            raise RuntimeError("Migration state did not become available")
        compose(directory, "stop", "web", "cards")
        report = payment_closure(directory, meta, profile)
        if report.get("ok") is not True or report.get("clear") is not True:
            raise RuntimeError(
                "Original payment proof or reconciliation remains pending"
            )
        start(directory, meta, allow_migration=True)
        if stopped_state_probe_after_stop(directory, meta) != "0":
            raise RuntimeError(
                "New unresolved payment work appeared during private validation"
            )
        report = payment_closure(directory, meta, profile)
        if report.get("ok") is not True or report.get("clear") is not True:
            raise RuntimeError(
                "Native or framework payment closure changed during private validation"
            )
        marker = write_migration(directory, marker, "activating")
        atomic_migration_file(
            directory / "compose.json", json.dumps(profile, indent=2) + "\n"
        )
        recreate_namespace(directory)
        start(directory, meta, allow_migration=True)
        path.unlink()
    except Exception as error:
        try:
            compose(
                directory,
                "stop",
                "web",
                "cards",
                *(["cosmic"] if marker["phase"] == "activating" else [])
            )
        except Exception:
            raise RuntimeError(
                "Migration qualification and bridge shutdown failed. Preserve current data and marker; inspect services before retrying."
            ) from None
        if marker["phase"] != "activating":
            atomic_migration_file(
                directory / "compose.json", json.dumps(staged, indent=2) + "\n"
            )
        code = getattr(error, "code", None)
        reason = (
            " (" + code + ")"
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", code)
            else ""
        )
        raise RuntimeError(
            "Legacy migration remains closed or stopped; current databases, identities and marker were preserved. Resolve only proven obligations, then repeat upgrade --finish-legacy-migration. No backup was restored."
            + reason
        ) from None
    print(
        "Legacy migration completed; payment closure verified and original keys/catalog retained."
    )


def finish_ordinary_upgrade(directory, meta):
    path = directory / "upgrade.pending.json"
    if not path.is_file():
        raise RuntimeError("No recorded ordinary upgrade exists for this installation.")
    marker = json.loads(path.read_text(encoding="utf-8"))
    if (
        marker.get("format") != 1
        or marker.get("kind") != "upgrade"
        or marker.get("phase")
        not in [
            "staging",
            "migrating",
            "validating",
            "activating",
            "rolling_back",
            "rollback_activating",
        ]
        or meta not in [marker.get("meta"), marker.get("oldMeta")]
    ):
        raise RuntimeError(
            "Recorded upgrade identity differs; preserve current data and marker."
        )
    safety = validate_backup(marker["backup"])
    original = backup_identity(safety)
    if (
        original != marker.get("oldMeta")
        or original["project"] != meta["project"]
        or original["directory"] != str(directory)
        or file_hash(safety / "checksums.json") != marker["backupDigest"]
    ):
        raise RuntimeError("Recorded upgrade safety backup identity differs.")
    values = migration_staging_bytes(marker, safety)
    expected_old = {name: file_hash(safety / name) for name in marker["oldBinding"]}
    expected_new = {
        name: hashlib.sha256(value).hexdigest() for name, value in values.items()
    }
    if marker["oldBinding"] != expected_old or marker["newBinding"] != expected_new:
        raise RuntimeError("Recorded upgrade configuration plan differs.")
    if (
        create_compose(marker["meta"], marker["candidateLocks"]) != marker["candidate"]
        or migration_images(marker["candidate"]) != marker["runtimeImages"]
    ):
        raise RuntimeError("Recorded upgrade runtime identity differs.")
    no_players(directory)
    if marker["phase"] in ["rolling_back", "rollback_activating"]:
        for name in expected_old:
            current = directory / name
            if current.is_symlink() or file_hash(current) not in [
                expected_old[name],
                expected_new[name],
            ]:
                raise RuntimeError(
                    "Recorded rollback configuration differs; preserve the marker."
                )
        original_profile = json.loads(
            (safety / "compose.json").read_text(encoding="utf-8")
        )
        original_closed = json.loads(json.dumps(original_profile))
        original_closed["services"]["network"]["ports"] = []
        candidate_closed = json.loads(json.dumps(marker["candidate"]))
        candidate_closed["services"]["network"]["ports"] = []
        current_profile = json.loads(
            (directory / "compose.json").read_text(encoding="utf-8")
        )
        allowed_profiles = (
            [original_profile, original_closed]
            if marker["phase"] == "rollback_activating"
            else [original_profile, original_closed, candidate_closed]
        )
        if current_profile not in allowed_profiles:
            raise RuntimeError(
                "Recorded rollback ingress differs; no backup was imported."
            )
        if marker["phase"] == "rollback_activating":
            if migration_binding(directory) != expected_old:
                raise RuntimeError(
                    "Rollback activation identity differs; no backup was imported."
                )
            activate_configuration(
                directory, original, original_profile, allow_upgrade=True
            )
        else:
            restore_data(directory, original, safety, allow_upgrade=True)
        path.unlink()
        print(
            "Upgrade rollback completed; previous runtime and consistent data restored."
        )
        return
    if marker["phase"] == "staging":
        marker = stage_legacy_migration(directory, marker, safety)
    meta = marker["meta"]
    if migration_binding(directory) != marker.get("binding"):
        raise RuntimeError(
            "Recorded upgrade keys/configuration differ; preserve current data."
        )
    profile = marker["candidate"]
    staged = json.loads(json.dumps(profile))
    staged["services"]["network"]["ports"] = []
    current = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    if current != staged and not (
        marker["phase"] == "activating" and current == profile
    ):
        raise RuntimeError("Upgrade ingress differs from its recorded phase.")
    try:
        marker = write_migration(directory, marker, "validating")
        private_file(directory / "compose.json", json.dumps(staged, indent=2) + "\n")
        recreate_namespace(directory)
        start(directory, meta, allow_upgrade=True)
        if stopped_state_probe_after_stop(directory, meta) != "0":
            raise RuntimeError(
                "Unresolved payment work appeared during private validation"
            )
        closure = payment_closure(directory, meta, profile)
        if closure.get("ok") is not True or closure.get("clear") is not True:
            raise RuntimeError("Native or framework payment obligations remain")
        marker = write_migration(directory, marker, "activating")
        private_file(directory / "compose.json", json.dumps(profile, indent=2) + "\n")
        recreate_namespace(directory)
        start(directory, meta, allow_upgrade=True)
        path.unlink()
    except Exception as error:
        # The durable phase, not an in-memory assumption, decides whether imports remain safe.
        marker = json.loads(path.read_text(encoding="utf-8"))
        try:
            compose(directory, "stop", "web", "cards", "cosmic")
        except Exception:
            raise RuntimeError(
                "Upgrade shutdown failed; preserve current data and use upgrade --finish-upgrade after inspecting services."
            ) from None
        if marker["phase"] != "activating":
            marker = write_migration(directory, marker, "rolling_back")
            try:
                restore_data(directory, original, safety, allow_upgrade=True)
                path.unlink()
            except Exception:
                raise RuntimeError(
                    "Upgrade rollback remains incomplete. Keep the marker and repeat upgrade --finish-upgrade; ordinary start is blocked."
                ) from None
            raise RuntimeError(
                "Upgrade failed; previous images, keys and data were restored."
            ) from None
        raise RuntimeError(
            "Upgrade ingress activation failed; services stopped and candidate data preserved. Repeat upgrade --finish-upgrade; no backup will be imported."
        ) from None
    print("Upgrade completed; catalog, issued codes and encryption keys retained.")


def stopped_state_probe_after_stop(directory, meta):
    compose(directory, "stop", "web", "cards")
    return stopped_state_probe(directory, meta, pending_guard_script())


def upgrade(directory, meta, migrate_legacy=False):
    if any(
        (directory / name).exists()
        for name in [
            "setup.pending.json",
            "recovery.pending.json",
            "migration.pending.json",
            "upgrade.pending.json",
        ]
    ):
        raise RuntimeError(
            "Complete the recorded installation, recovery or legacy migration before upgrading."
        )
    no_players(directory)
    pending = (
        "0"
        if migrate_legacy
        else compose(
            directory,
            "exec",
            "-T",
            "cards",
            "node",
            "--input-type=module",
            "-e",
            live_pending_guard_script(),
            capture=True,
        ).strip()
    )
    if pending != "0":
        raise RuntimeError("Finish pending purchases before upgrading.")
    previous = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    previous_locks = (directory / "images.lock.json").read_bytes()
    settings = dict(
        line.split("=", 1)
        for line in (directory / "private/cards.env")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    )
    catalog = json.loads(
        (directory / "private/catalog.json").read_text(encoding="utf-8")
    )
    rewards = settings.get(
        "ENABLE_SERIES_ONE_REWARDS",
        "1" if any(v.get("codes") for v in catalog["variants"]) else "0",
    )
    if rewards not in ["0", "1"]:
        raise RuntimeError("Invalid Series One provider configuration.")
    new_meta = {
        **meta,
        "bridgeHash": bridge_snapshot(directory),
        "seriesOneEnabled": rewards == "1",
    }
    if not (directory / "cosmic/pom.xml").is_file():
        game = directory / "cosmic"
        if game.exists() and any(game.iterdir()):
            raise RuntimeError(
                "Recovered Cosmic source directory is incomplete; preserve it and choose a clean source checkout."
            )
        run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                RUNTIME["cosmicRepository"],
                str(game),
            ]
        )
        run(["git", "-C", str(game), "checkout", "--quiet", meta["cosmicRevision"]])
        secure_seed_account(game)
    run(
        [
            sys.executable,
            str(ROOT / "tools/install-cosmic.py"),
            str(directory / "cosmic"),
            "--check",
        ]
    )
    run(
        [
            sys.executable,
            str(ROOT / "tools/install-cosmic.py"),
            str(directory / "cosmic"),
        ]
    )
    shutil.copytree(TEMPLATES, directory / "support", dirs_exist_ok=True)
    config = (directory / "cosmic/config.yaml").read_text(encoding="utf-8")
    (directory / "support/test-config.yaml").write_text(
        configure_yaml(
            config,
            {
                "DB_USER": "cosmic_app",
                "DB_PASS": "",
                "HOST": "127.0.0.1",
                "LANHOST": "127.0.0.1",
                "LOCALHOST": "127.0.0.1",
            },
        ),
        encoding="utf-8",
    )
    candidate_locks = lock_images(directory)
    candidate = create_compose(new_meta, candidate_locks)
    private_file(directory / "compose.json", json.dumps(candidate, indent=2) + "\n")
    try:
        compose(directory, "build")
    except Exception:
        private_file(directory / "compose.json", json.dumps(previous, indent=2) + "\n")
        private_file(directory / "images.lock.json", previous_locks)
        raise
    private_file(directory / "compose.json", json.dumps(previous, indent=2) + "\n")
    private_file(directory / "images.lock.json", previous_locks)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--user",
            "10002:10002",
            "--entrypoint",
            "node",
            candidate["services"]["cards"]["image"],
            "--input-type=module",
            "-e",
            migration_candidate_probe_script(),
        ],
        capture=True,
        structured_error=True,
    )
    candidate_runtime_images = migration_images(candidate)
    safety = backup(
        directory,
        meta,
        restart=False,
        require_resolved=not migrate_legacy,
        require_legacy=migrate_legacy,
    )
    marker = {
        "format": 1,
        "kind": "legacy" if migrate_legacy else "upgrade",
        "phase": "staging",
        "meta": new_meta,
        "oldMeta": meta,
        "candidate": candidate,
        "backup": str(safety),
        "backupDigest": file_hash(safety / "checksums.json"),
        "oldBinding": migration_binding(directory),
        "candidateLocks": candidate_locks,
        "runtimeImages": candidate_runtime_images,
    }
    values = migration_staging_bytes(marker, safety)
    marker["newBinding"] = {
        name: hashlib.sha256(value).hexdigest() for name, value in values.items()
    }
    write_migration(directory, marker)
    if migrate_legacy:
        finish_legacy_migration(directory, meta)
    else:
        finish_ordinary_upgrade(directory, meta)


def enable_series_one(directory, meta, products):
    selected = products.split(",")
    if (
        not 1 <= len(selected) <= 1000
        or len(set(selected)) != len(selected)
        or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", p) for p in selected
        )
    ):
        raise ValueError("Provide unique published pack IDs separated by commas.")
    if any(
        (directory / name).exists()
        for name in [
            "setup.pending.json",
            "recovery.pending.json",
            "migration.pending.json",
            "upgrade.pending.json",
        ]
    ):
        raise RuntimeError("Complete installation or recovery before enabling rewards.")
    profile = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
    image = profile["services"]["cards"]["image"]
    path = directory / "private/cards.env"
    settings = path.read_text(encoding="utf-8")
    flags = re.findall(r"(?m)^ENABLE_SERIES_ONE_REWARDS=([^\r\n]*)$", settings)
    if len(flags) > 1 or (flags and flags[0] not in ["0", "1"]):
        raise RuntimeError("Invalid Series One provider configuration.")
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10002:10002",
                "--entrypoint",
                "node",
                image,
                "-e",
                "require('node:fs').accessSync('tools/rewards.mjs')",
            ]
        )
    except RuntimeError:
        raise RuntimeError(
            "Upgrade this managed installation to a release containing the reward setup tool first."
        ) from None
    safety = backup(directory, meta, restart=False, require_resolved=True)
    updated = {**meta, "seriesOneEnabled": True}
    try:
        if flags:
            configured = re.sub(
                r"(?m)^ENABLE_SERIES_ONE_REWARDS=[^\r\n]*$",
                "ENABLE_SERIES_ONE_REWARDS=1",
                settings,
            )
        else:
            configured = settings.rstrip() + "\nENABLE_SERIES_ONE_REWARDS=1\n"
        private_file(path, configured)
        result = json.loads(
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--user",
                    "10002:10002",
                    "--mount",
                    "type=volume,src="
                    + meta["project"]
                    + "_card_state,dst=/opt/card/state",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=272m,mode=1777",
                    "--env-file",
                    str(path),
                    "--entrypoint",
                    "node",
                    image,
                    "tools/rewards.mjs",
                    "enable-series-one",
                    "--products",
                    products,
                    "--confirm-stopped",
                ],
                capture=True,
                structured_error=True,
            )
        )
        if (
            not isinstance(result, dict)
            or result.get("products") != selected
            or not isinstance(result.get("changed"), bool)
        ):
            raise RuntimeError("Reward setup returned an invalid result.")
        private_file(
            directory / "installation.json", json.dumps(updated, indent=2) + "\n"
        )
        activate_configuration(directory, updated, profile)
    except ActivationError as error:
        raise RuntimeError(str(error) + " Safety backup: " + str(safety)) from None
    except Exception as error:
        code = getattr(error, "code", None)
        reason = (
            " (" + code + ")"
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", code)
            else ""
        )
        try:
            restore_data(directory, meta, safety)
        except Exception:
            raise RuntimeError(
                "Reward setup"
                + reason
                + " and rollback both failed. Keep services stopped and recover the safety backup: "
                + str(safety)
            ) from error
        raise RuntimeError(
            "Reward setup failed"
            + reason
            + "; previous provider configuration, catalog and data were restored. Safety backup: "
            + str(safety)
        ) from None
    print(json.dumps(result))


def attribute_legacy_payment(
    directory, meta, order_id, decision_key, operator_id, reason
):
    if (
        not isinstance(order_id, str)
        or not re.fullmatch(r"[a-f0-9]{64}", order_id)
        or not isinstance(decision_key, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", decision_key)
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum
            or re.search(r"[\x00-\x1f\x7f]", value)
            for value, maximum in [(operator_id, 128), (reason, 500)]
        )
    ):
        raise ValueError(
            "Provide the original order ID, stable decision key, operator identity and concise reason."
        )
    if any(
        (directory / name).exists()
        for name in [
            "upgrade.pending.json",
            "setup.pending.json",
            "recovery.pending.json",
        ]
    ):
        raise RuntimeError(
            "Complete recorded ordinary upgrade, setup or recovery before legacy attribution."
        )
    path = directory / "migration.pending.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise RuntimeError("Legacy attribution requires a recorded closed migration.")
    marker_digest = file_hash(path)
    marker = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(marker, dict)
        or marker.get("format") != 1
        or marker.get("kind") != "legacy"
        or marker.get("phase") not in ["migrating", "validating"]
        or marker.get("meta") != meta
        or meta.get("directory") != str(directory)
        or marker.get("binding") != migration_binding(directory)
    ):
        raise RuntimeError(
            "Legacy attribution requires completed staging with unchanged migration identities."
        )
    if (
        not isinstance(marker.get("backup"), str)
        or not 1 <= len(marker["backup"]) <= 4096
    ):
        raise RuntimeError("The recorded original backup path is invalid.")
    raw_backup = Path(marker["backup"])
    if (
        not raw_backup.is_absolute()
        or raw_backup.resolve() != raw_backup
        or not raw_backup.is_relative_to(directory / "backups")
        or raw_backup == directory / "backups"
        or any("," in str(p) for p in [directory, raw_backup])
    ):
        raise RuntimeError(
            "Attribution must use the original retained managed backup path."
        )
    safety = validate_backup(raw_backup)
    original = backup_identity(safety)
    if (
        file_hash(safety / "checksums.json") != marker.get("backupDigest")
        or original != marker.get("oldMeta")
        or original.get("project") != meta.get("project")
        or original.get("directory") != str(directory)
        or marker.get("oldBinding") != migration_binding(safety)
    ):
        raise RuntimeError("The original migration backup identity differs.")
    profile = create_compose(
        meta, json.loads((directory / "images.lock.json").read_text(encoding="utf-8"))
    )
    closed = json.loads(json.dumps(profile))
    closed["services"]["network"]["ports"] = []
    if (
        marker.get("candidate") != profile
        or migration_images(profile) != marker.get("runtimeImages")
        or json.loads((directory / "compose.json").read_text(encoding="utf-8"))
        != closed
    ):
        raise RuntimeError(
            "Attribution requires the recorded candidate with all public ingress closed."
        )
    no_players(directory)
    compose(directory, "stop", "web", "cards")
    no_players(directory)
    if any(
        compose(
            directory, "ps", "--status", "running", "-q", service, capture=True
        ).strip()
        for service in ["web", "cards"]
    ):
        raise RuntimeError("Bridge writers must remain stopped for attribution.")
    if (
        file_hash(path) != marker_digest
        or migration_binding(directory) != marker["binding"]
        or json.loads((directory / "compose.json").read_text(encoding="utf-8"))
        != closed
    ):
        raise RuntimeError(
            "Migration configuration changed; bridge writers remain stopped."
        )
    namespace = compose(directory, "ps", "-q", "network", capture=True).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", namespace):
        raise RuntimeError("The recorded private game network must remain active.")
    try:
        result = json.loads(
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "container:" + namespace,
                    "--user",
                    "0:0",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "DAC_READ_SEARCH",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=272m",
                    "--env-file",
                    str(directory / "private/cards.env"),
                    "--mount",
                    "type=bind,source="
                    + str(directory)
                    + ",target="
                    + str(directory)
                    + ",readonly",
                    "--mount",
                    "type=bind,source="
                    + str(safety)
                    + ",target="
                    + str(safety)
                    + ",readonly",
                    "--mount",
                    "type=volume,source="
                    + meta["project"]
                    + "_card_state,target=/opt/card/state,readonly",
                    "--entrypoint",
                    "node",
                    profile["services"]["cards"]["image"],
                    "tools/payments.mjs",
                    "attribute-legacy",
                    "--deployment-directory",
                    str(directory),
                    "--order-id",
                    order_id,
                    "--decision-key",
                    decision_key,
                    "--operator-id",
                    operator_id,
                    "--reason",
                    reason,
                    "--confirm-bridge-stopped",
                ],
                capture=True,
                structured_error=True,
            )
        )
        if (
            not isinstance(result, dict)
            or result.get("orderId") != order_id
            or result.get("decisionKey") != decision_key
            or result.get("state") != "attributed"
            or not isinstance(result.get("evidenceFingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["evidenceFingerprint"])
        ):
            raise RuntimeError("Invalid attribution response")
    except Exception as error:
        code = getattr(error, "code", None)
        reason_code = (
            " (" + code + ")"
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", code)
            else ""
        )
        raise RuntimeError(
            "Legacy attribution did not confirm completion"
            + reason_code
            + "; bridge writers remain stopped and migration ingress closed. Retry the exact decision and terms; no services were reopened."
        ) from None
    print(json.dumps(result))
    print(
        "Attribution verified; bridge writers remain stopped and migration ingress closed. No wallet, cards, adoption or refund were changed."
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default="../CosmicCardServer",
        help="Managed installation directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "install", help="Create and start a complete new deployment"
    )
    setup.add_argument(
        "--cosmic-directory",
        help="Copy an existing clean Cosmic checkout instead of cloning stock Cosmic",
    )
    setup.add_argument(
        "--game-host",
        default="127.0.0.1",
        help="IPv4 address advertised to the v83 client",
    )
    setup.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Host interface for game ports; web remains loopback",
    )
    setup.add_argument(
        "--origin", help="Public HTTP(S) origin; HTTPS is required for remote players"
    )
    setup.add_argument("--web-port", type=int, default=8490)
    setup.add_argument("--login-port", type=int, default=8484)
    setup.add_argument(
        "--test-account",
        action="store_true",
        help="Create and fund a disposable CardTest account and exercise a real pack",
    )
    setup.add_argument(
        "--series-one",
        action="store_true",
        help="Enable the Series One provider and add one code insert per pack",
    )
    setup.add_argument(
        "--catalog",
        help="Install a collectible catalog, including imported packs with direct-source images",
    )
    for name in ["start", "doctor", "stop", "smoke"]:
        commands.add_parser(name)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument(
        "--keep-stopped",
        action="store_true",
        help="Take a consistent backup without restarting writers or reopening a migration fence",
    )
    upgrade_parser = commands.add_parser("upgrade")
    migration = upgrade_parser.add_mutually_exclusive_group()
    migration.add_argument(
        "--migrate-legacy-payments",
        action="store_true",
        help="Migrate legacy framework state into a verified recovery runtime with ingress closed until payment proof is complete",
    )
    migration.add_argument(
        "--finish-legacy-migration",
        action="store_true",
        help="Privately verify the recorded migration and payment closure before activation; preserve current data",
    )
    migration.add_argument(
        "--finish-upgrade",
        action="store_true",
        help="Resume the recorded ordinary upgrade or its rollback; preserve data after activation",
    )
    recover = commands.add_parser("restore")
    recover.add_argument("--backup", required=True)
    disaster = commands.add_parser(
        "recover", help="Restore a lost installation into an empty destination"
    )
    disaster.add_argument("--backup", required=True)
    disaster.add_argument(
        "--expect-project",
        required=True,
        help="Original project from the trusted backup installation.json",
    )
    disaster.add_argument(
        "--relocate",
        action="store_true",
        help="Explicitly permit a different installation directory; retain project identity",
    )
    disaster.add_argument(
        "--retry",
        action="store_true",
        help="Retry the recorded recovery; after activation preserve current data without reimporting the backup",
    )
    rewards = commands.add_parser(
        "rewards", help="Configure optional game rewards for existing published packs"
    )
    rewards.add_argument("action", choices=["enable-series-one"])
    rewards.add_argument(
        "--products", required=True, help="Comma-separated published pack IDs"
    )
    attribution = commands.add_parser(
        "attribute-legacy-payment",
        help="Attribute one retained legacy payment from its original verified migration backup; keep ingress closed",
    )
    for name in ["order-id", "decision-key", "operator-id", "reason"]:
        attribution.add_argument("--" + name, required=True)
    admin = commands.add_parser(
        "admin", help="Manage website administrators independently of game GM roles"
    )
    admin.add_argument("action", choices=["grant", "revoke", "list"])
    admin.add_argument("username", nargs="?")
    admin.add_argument(
        "--account-id",
        type=int,
        help="Revoke a stale grant by numeric identity, without requiring an eligible native account",
    )
    account = commands.add_parser("account")
    account.add_argument("action", choices=["create", "fund"])
    account.add_argument("username")
    account.add_argument("--cash-type", type=int, choices=[1, 2, 4], default=4)
    account.add_argument("--amount", type=int)
    account.add_argument("--password-file")
    account.add_argument(
        "--request-id",
        help="Stable funding identity; reuse after an uncertain response",
    )
    args = parser.parse_args()
    if args.command == "install":
        install(args)
        return
    if args.command == "recover":
        recover_empty(
            args.directory, args.backup, args.expect_project, args.relocate, args.retry
        )
        return
    directory, meta = load(args.directory)
    if args.command == "start":
        start(directory, meta)
    elif args.command == "doctor":
        doctor(directory, meta)
    elif args.command == "stop":
        no_players(directory)
        compose(directory, "stop", "web", "cards", "cosmic", "network", "db")
    elif args.command == "backup":
        backup(directory, meta, restart=not args.keep_stopped)
    elif args.command == "restore":
        restore(directory, meta, args.backup)
    elif args.command == "upgrade":
        if args.finish_upgrade:
            finish_ordinary_upgrade(directory, meta)
        elif args.finish_legacy_migration:
            finish_legacy_migration(directory, meta)
        else:
            upgrade(directory, meta, migrate_legacy=args.migrate_legacy_payments)
    elif args.command == "attribute-legacy-payment":
        attribute_legacy_payment(
            directory,
            meta,
            args.order_id,
            args.decision_key,
            args.operator_id,
            args.reason,
        )
    elif args.command == "smoke":
        smoke(directory, meta)
    elif args.command == "rewards":
        enable_series_one(directory, meta, args.products)
    elif args.command == "account":
        value = {"action": args.action, "username": args.username}
        if args.action == "create":
            value["password"] = (
                Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
                if args.password_file
                else getpass.getpass("New account password: ")
            )
        else:
            if not args.amount:
                raise ValueError("Provide --amount for funding.")
            value.update(cashType=args.cash_type, amount=args.amount)
            if not args.request_id:
                raise ValueError(
                    "Provide a unique --request-id for funding; reuse it when retrying the same credit."
                )
            value["key"] = args.request_id
        account_command(directory, value)
        print("Operator account action completed.")
    elif args.command == "admin":
        if args.account_id is not None:
            if (
                args.action != "revoke"
                or args.username is not None
                or args.account_id < 1
            ):
                raise ValueError(
                    "Use admin revoke --account-id POSITIVE_ID without an account name."
                )
        elif (args.action == "list") != (args.username is None):
            raise ValueError("Use admin list, or admin grant/revoke ACCOUNT_NAME.")
        admin_command(directory, args.action, args.username, account_id=args.account_id)


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        ValueError,
        OSError,
        urllib.error.URLError,
        tarfile.TarError,
    ) as error:
        print("Setup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
