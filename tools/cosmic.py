#!/usr/bin/env python3
"""Prepare, build, start and operate a complete Cosmic card installation."""
from contextlib import closing
from pathlib import Path
import argparse
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
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Configuration paths must not be symbolic links.")
    if path.exists():
        path.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data.encode() if isinstance(data, str) else data)
        stream.flush()
        os.fsync(stream.fileno())


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
    for name in ["doctor.mjs", "admin.mjs", "rewards.mjs"]:
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


def start(directory, meta, *, allow_recovery=False):
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
 console.log(database.prepare("SELECT COUNT(*) AS n FROM orders WHERE state NOT IN ('complete','rejected')").get().n);
}catch{
 console.error(JSON.stringify({code:'PENDING_STATE_UNAVAILABLE'}));process.exitCode=1;
}finally{
 database?.close();if(temporary)rmSync(temporary,{recursive:true,force:true});
}
"""


def backup(directory, meta, restart=True, require_resolved=False):
    no_players(directory)
    destination = (
        directory
        / "backups"
        / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3))
    )
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive_media(directory, destination / "media.tar.gz")
    image = compose(directory, "images", "-q", "cards", capture=True).strip()
    success = False
    stage = "writer shutdown"
    failure = None
    try:
        compose(directory, "stop", "web", "cards", "cosmic")
        stage = "stopped game-account check"
        no_players(directory)
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
        if restart or not success:
            try:
                start(directory, meta)
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
            + "; previous services restarted."
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
    return path


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
        if version not in [0, 1] or not {"orders", "registrations"} <= tables:
            raise RuntimeError("Backup bridge schema is unsupported or incomplete.")


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
        for name, required in [
            ("framework.sqlite", set()),
            ("bridge.sqlite", {"orders", "registrations"}),
        ]:
            try:
                con = sqlite3.connect((root / name).as_uri() + "?mode=ro", uri=True)
                try:
                    if con.execute("PRAGMA quick_check").fetchone()[
                        0
                    ] != "ok" or not required <= {
                        row[0]
                        for row in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }:
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


def recovery_state_probe_script():
    # Validate opaque headers on private copies; runtime startup authenticates the encrypted state.
    schemas = json.dumps(
        {version: normalized_schema_sql(version) for version in range(2, 9)},
        separators=(",", ":"),
    )
    return (
        r"""
import{copyFileSync,lstatSync,mkdtempSync,rmSync}from'node:fs';
import{join}from'node:path';import{tmpdir}from'node:os';import{DatabaseSync}from'node:sqlite';
const schemas=SCHEMA_MODELS;let temporary,database,code='RECOVERY_COPY_UNAVAILABLE';
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
 }else if(![0,1].includes(version)||!tables.has('orders')||!tables.has('registrations'))throw Error('INVALID_SCHEMA');
}
try{
 const source=process.argv[1]??'/opt/card/state';temporary=mkdtempSync(join(process.argv[2]??tmpdir(),'card-recovery-'));let bytes=0;
 for(const file of ['framework.sqlite','bridge.sqlite'])for(const suffix of ['','-wal']){
  const name=file+suffix;let info;try{info=lstatSync(join(source,name))}catch(error){if(suffix&&error.code==='ENOENT')continue;throw error}
  if(!info.isFile()||(bytes+=info.size)>256*1024*1024)throw Error('INVALID_STATE');copyFileSync(join(source,name),join(temporary,name));
 }
 for(const file of ['framework.sqlite','bridge.sqlite']){
  code=file==='framework.sqlite'?'RECOVERY_FRAMEWORK_UNAVAILABLE':'RECOVERY_BRIDGE_UNAVAILABLE';
  database=new DatabaseSync(join(temporary,file),{readOnly:true});
  if(database.prepare('PRAGMA quick_check').get().quick_check!=='ok')throw Error('INVALID_STATE');schema(database,file==='framework.sqlite');
  database.close();database=null;
 }
 console.log(JSON.stringify({ok:true}));
}catch{console.error(JSON.stringify({code}));process.exitCode=1}
finally{database?.close();if(temporary)rmSync(temporary,{recursive:true,force:true})}
""".replace(
            "SCHEMA_MODELS", schemas
        )
        .replace("REQUIRE_LEGACY", "false")
        .replace("REQUIRE_NORMALIZED", "false")
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


def restore_data(directory, meta, source, restore_configuration=True):
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
    activate_configuration(directory, meta, profile)


def recreate_namespace(directory):
    # Database data and its service stay intact; consumers must leave the old namespace first.
    compose(directory, "stop", "web", "cards", "cosmic", "network")
    compose(directory, "rm", "-f", "web", "cards", "cosmic", "network")


def activate_configuration(directory, meta, profile):
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
    start(directory, meta, allow_recovery=True)
    if recovery_marker.exists():
        private_file(
            recovery_marker, json.dumps({**recovery, "phase": "activating"}) + "\n"
        )
    private_file(directory / "compose.json", json.dumps(profile, indent=2) + "\n")
    try:
        recreate_namespace(directory)
        start(directory, meta, allow_recovery=True)
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
    source = validate_backup(source)
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


def upgrade(directory, meta):
    no_players(directory)
    pending = compose(
        directory,
        "exec",
        "-T",
        "cards",
        "node",
        "--input-type=module",
        "-e",
        "import{DatabaseSync}from'node:sqlite';let d=new DatabaseSync('/opt/card/state/bridge.sqlite',{readOnly:true});console.log(d.prepare(\"SELECT COUNT(*) AS n FROM orders WHERE state NOT IN ('complete','rejected')\").get().n);d.close()",
        capture=True,
    ).strip()
    if pending != "0":
        raise RuntimeError("Finish pending purchases before upgrading.")
    previous = json.loads((directory / "compose.json").read_text(encoding="utf-8"))
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
    candidate = create_compose(new_meta, lock_images(directory))
    private_file(directory / "compose.json", json.dumps(candidate, indent=2) + "\n")
    try:
        compose(directory, "build")
    except Exception:
        private_file(directory / "compose.json", json.dumps(previous, indent=2) + "\n")
        raise
    private_file(directory / "compose.json", json.dumps(previous, indent=2) + "\n")
    safety = backup(directory, meta, restart=False, require_resolved=True)
    if "ENABLE_SERIES_ONE_REWARDS" not in settings:
        path = directory / "private/cards.env"
        private_file(
            path,
            path.read_text(encoding="utf-8").rstrip()
            + "\nENABLE_SERIES_ONE_REWARDS="
            + rewards
            + "\n",
        )
    if "ASSET_ROOT" not in settings:
        path = directory / "private/cards.env"
        private_file(
            path, path.read_text(encoding="utf-8").rstrip() + "\nASSET_ROOT=/assets\n"
        )
    staged = json.loads(json.dumps(candidate))
    staged["services"]["network"]["ports"] = []
    private_file(directory / "compose.json", json.dumps(staged, indent=2) + "\n")
    private_file(directory / "installation.json", json.dumps(new_meta, indent=2) + "\n")
    try:
        recreate_namespace(directory)
        start(directory, new_meta)
    except Exception as error:
        try:
            restore_data(directory, meta, safety)
        except Exception:
            raise RuntimeError(
                "Upgrade and rollback both failed. Stop services and restore the safety backup: "
                + str(safety)
            ) from error
        raise RuntimeError(
            "Upgrade failed; previous images, keys and data were restored."
        ) from None
    # No public game or website ingress was available during validation. From this point
    # onward preserve new transactions even if the final ingress activation has a fault.
    private_file(directory / "compose.json", json.dumps(candidate, indent=2) + "\n")
    try:
        recreate_namespace(directory)
        start(directory, new_meta)
    except Exception as error:
        try:
            compose(directory, "stop", "web", "cards", "cosmic")
        except Exception:
            raise RuntimeError(
                "Upgrade ingress activation and service shutdown failed. Preserve candidate data and inspect services; no backup was restored. Safety backup: "
                + str(safety)
            ) from error
        raise RuntimeError(
            "Upgrade ingress activation failed; services stopped and candidate data preserved. Inspect configuration and run start; do not restore over new transactions. Safety backup: "
            + str(safety)
        ) from error
    print("Upgrade completed; catalog, issued codes and encryption keys retained.")


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
        for name in ["setup.pending.json", "recovery.pending.json"]
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
    for name in ["start", "doctor", "stop", "backup", "upgrade", "smoke"]:
        commands.add_parser(name)
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
        backup(directory, meta)
    elif args.command == "restore":
        restore(directory, meta, args.backup)
    elif args.command == "upgrade":
        upgrade(directory, meta)
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
