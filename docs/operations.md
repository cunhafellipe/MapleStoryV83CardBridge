# Deployment, recovery and backups

Run the bridge as an unprivileged service with Node.js 24.14+, private state files, TLS at the public proxy, and an enabled Cosmic adapter on a private loopback connection. Example nginx and systemd files are in `deploy/`; adapt host names and paths before use.

The [managed Cosmic setup](cosmic-setup.md) automates these services, readiness checks, consistent backups, restoration and upgrades. The guidance below also applies to manually operated installations.

- [State and secrets](#state-and-secrets)
- [Purchase recovery](#purchase-recovery)
- [Reward recovery](#reward-recovery)
- [Availability and limits](#availability-and-limits)
- [Retained legacy account attribution](#retained-legacy-account-attribution)
- [Proven legacy purchase repair](#proven-legacy-purchase-repair)
- [Consistent backups and restore](#consistent-backups-and-in-place-restore)
- [Lost-installation recovery](#lost-installation-recovery)
- [Internet access](#internet-access)
- [Upgrade](#upgrade)
- [Legacy payment migration](#legacy-payment-migration)
- [Rollout checklist](#rollout-checklist)

## State and secrets

`STATE_DIRECTORY` is an absolute writable path containing encrypted framework SQLite state and a separate purchase journal. The journal contains durable account/order/receipt metadata, website admin grants, preview records and attributed activity, not raw codes. The active catalog is persisted in framework state; the mounted catalog file is only an initial seed. The managed smoke receipt contains its frozen quote and pack/card IDs. Protect all of these as account data. The game database contains independent debit receipts, HMAC code indices, inventory, sessions and notification outbox.

Keep these keys outside public assets and repositories:

- `STATE_KEY`: framework state encryption.
- `CODE_ENCRYPTION_KEY`: code vault encryption.
- `CODE_INDEX_KEY`: stable framework code fingerprint and retry identity.
- `GAME_SHARED_KEY` / `CARD_BRIDGE_SHARED_KEY`: private request signatures.
- `CARD_BRIDGE_CODE_KEY`: stable game-side code fingerprint.
- `CSRF_KEY`: browser-session request binding.

Back up both service databases, the game database, configuration and keys as one recoverable installation. Stop writers or use database-supported consistent backups. Copying a live SQLite main file without its WAL is not a valid backup procedure. Test restores privately. Losing an encryption key or changing either index key can make issued codes unusable.

## Purchase recovery

The bridge prepares a durable external purchase before requesting a native debit. Its identity binds the account, original cash type, amount and reviewed catalog. A lost response is reconciled using that same identity; it does not authorize a second payment or a refund.

After a confirmed debit, the framework atomically delivers the purchase or records a refund obligation. The native adapter refunds only the original cash balance, and the bridge marks the order refunded only after authoritative confirmation. Delivered cards remain recoverable while native finalization is pending. See [payment recovery](payments.md) for each status and operator action.

A ten-second recovery pass resumes unresolved orders and registration. The opening animation does not allocate anything. Closing the browser does not undo or repeat the purchase.

Administration's Activity/orders view shows sanitized pending work and supports retrying the original order. A retry uses the same durable identity and concurrency guard as background recovery. Do not delete journal rows, issue a second payment, manually grant another pack, or add cash while the original order can still complete. Keep the original catalog and keys available for recovery.

Catalog publication is refused while purchases are unresolved. Same-version content changes are also refused. Keep original versions available until all old orders complete.

## Reward recovery

READY means registered, PENDING means delivery/save is unresolved, and USED means inventory and the notification outbox committed. A failed save preserves the current-process in-memory delivery record; retrying saves that same reward without adding another item.

An enabled game process holds a MySQL advisory writer lease. It prevents another enabled process from simultaneously becoming the reward writer. After process death, an old PENDING claim can be recovered by the next exclusive writer because an uncommitted item was not saved. The character save's conditional claim update prevents a replaced claim from committing.

Keep the native character/inventory save transaction intact. Fork code that catches or commits inventory work independently must be inspected before using this adapter.

USED notification delivery retries until acknowledged. The library also polls registered, revealed codes, and the manual Refresh control forces an account-scoped reconciliation. Neither path marks a code used based on a browser claim.

## Availability and limits

`/api/library/health` is a readiness check. It requires signed game/callback responses, matching authentication modes and accepted cash types, and usable local state. An unavailable game, callback or writer lease, mismatched configuration, or unusable storage returns failure. During an outage the public catalog remains browsable and economic actions are unavailable. Request errors omit internal exception details and plaintext code material. Run `node --env-file=/etc/card-library/.env tools/doctor.mjs` as the manual service user, or use the managed `doctor` command.

The framework defaults to 50,000 code records and 200,000 code retry/confirmation records, with bounded normalized SQLite storage. Economic commands still materialize state synchronously; card copies and history also consume capacity. Those counts are limits, not a promise that every catalog can fill them. Monitor state size and pending orders before growing the installation. One Node writer and one enabled game process are the supported model.

Behind nginx, enable `TRUST_PROXY=1` only when the proxy replaces `X-Real-IP`. The service accepts that header only from loopback. Keep direct service ports private. Configure public TLS, connection/rate limits and request timeouts at the proxy; do not expose the game adapter to the Internet.

For managed service logs, use `docker compose -f /absolute/install/compose.json logs --tail 100 cosmic cards web`. Each service retains at most three 10 MiB Docker log files. For manual services, use `journalctl -u card-library.service -n 100 --no-pager`; configure host journald retention. Inspect pending-work age and capacity in Administration before increasing account or collection volume. Leave disk space for a full runtime-image backup plus a separate safety backup during restore or upgrade. Build-time and steady-state resource needs differ; qualify your host with the intended catalog and load before public rollout.

## Retained legacy account attribution

Use this managed command for one historical payment that needs account-incarnation proof during a [closed legacy migration](#legacy-payment-migration). The original paired backup must remain under the installation's `backups` directory at its recorded absolute path. The marker must be `migrating` or `validating`, with original keys, configuration and candidate images intact; complete staging first. Ordinary upgrade, setup or recovery fences are refused. The native adapter must remain active on the private network with its writer lease and supported attribution protocol.

```sh
python3 tools/cosmic.py --directory /absolute/install attribute-legacy-payment --order-id ORIGINAL_64_CHARACTER_ORDER_ID --decision-key attribution-001 --operator-id maintenance-operator --reason "Verify original retained account"
```

Replace the order placeholder with the original 64-character lowercase hexadecimal journal order ID. Choose one stable decision key and retain the exact operator identity and reason. The command verifies the complete original backup and migration binding, checks that no players are connected, stops website/card writers, and verifies closed ingress again. Its one-off reader uses the current private environment and read-only mounts for the original installation, backup and state. A restricted root reader has only the file-read capability needed for private host files; temporary SQLite recovery copies are disposable. No persistent mount is writable.

The verifier parses the retained MySQL dump as data. It accepts only the qualified native timestamp/schema layout and requires the original account to predate the payment strictly. Missing, equal, changed or ambiguous timestamps and mismatched original terms are rejected. A manually assembled evidence document or a matching numeric account ID cannot substitute for the retained snapshot.

An `attributed` result records account continuity and an immutable operator decision. Attribution changes no wallet balance, card delivery, purchase adoption or refund. It does not start the bridge, reopen ingress or finish the migration. After a lost response or failure, keep the marker and original backup, and retry the same order, decision key, operator identity and reason. Then use [proven legacy purchase repair](#proven-legacy-purchase-repair) only where original delivery can be proven, and finish the recorded migration separately when all authoritative obligations close. Unsupported cases remain fenced.

## Proven legacy purchase repair

Use this local operator tool only when an earlier purchase is quarantined for reconciliation and you have the original journal order ID and original framework purchase key. It adopts an existing delivery only when the framework proves that delivery spent the original legacy credit. It creates no replacement cards, new credit or refund. The native payment is finalized only after that proof succeeds; an unverified obligation remains unresolved. This tool is separate from an ordinary purchase retry.

If `upgrade.pending.json` exists, complete the recorded ordinary upgrade or rollback with `upgrade --finish-upgrade` before this recipe. This repair cannot run inside an ordinary upgrade fence; do not start its services directly or delete the marker.

Back up MySQL and the paired framework/journal state together first. Keep the game adapter running and stop every bridge writer and website ingress for the entire repair. On a managed installation, use the installed card image containing `tools/payments.mjs`:

```sh
set -eu
test ! -f /absolute/install/upgrade.pending.json
if [ -f /absolute/install/migration.pending.json ]; then
  python3 tools/cosmic.py --directory /absolute/install backup --keep-stopped
  python3 -c 'import json,sys; assert not json.load(open(sys.argv[1]))["services"]["network"].get("ports"), "Migration ingress must be closed before repair"' /absolute/install/compose.json
  docker compose -f /absolute/install/compose.json up -d --no-build --wait db network cosmic
else
  python3 tools/cosmic.py --directory /absolute/install backup
fi
docker compose -f /absolute/install/compose.json stop web cards
if docker compose -f /absolute/install/compose.json run --rm --no-deps --entrypoint node cards tools/payments.mjs resolve-legacy --order-id ORIGINAL_64_CHARACTER_ORDER_ID --purchase-key ORIGINAL_PURCHASE_KEY --decision-key legacy-repair-001 --reason "Adopt verified original delivery" --operator-id maintenance-admin --confirm-bridge-stopped; then
  if [ -f /absolute/install/migration.pending.json ]; then
    python3 tools/cosmic.py --directory /absolute/install upgrade --finish-legacy-migration
  else
    python3 tools/cosmic.py --directory /absolute/install start
  fi
fi
```

Replace the order and purchase placeholders with the exact original identifiers. Choose a unique decision key and an attributable operator ID; retain the reason and result privately. If the command fails, keep bridge writers stopped, inspect the authoritative delivery evidence, and retry the same decision key and terms. Resume services and run `doctor` only after the repair succeeds or the unresolved case has been assessed. Do not restore an older backup over a possible completed repair.

For an active [legacy migration](#legacy-payment-migration), complete recorded staging first. The repair recipe refuses to start a native service while migration ingress is open. After a failed activation, run the finish command again to re-establish private validation before repair. A migration fence remains until the finish command proves payment closure; an operator assessment alone cannot override it.

For a manual installation, make the equivalent consistent backup, stop `card-library.service`, and run the same tool as the service user with its existing private environment and absolute state directory:

```sh
set -eu
sudo systemctl stop card-library.service
if sudo -u card-library /usr/bin/node --env-file=/etc/card-library/.env /opt/card-library/bridge/tools/payments.mjs resolve-legacy --order-id ORIGINAL_64_CHARACTER_ORDER_ID --purchase-key ORIGINAL_PURCHASE_KEY --decision-key legacy-repair-001 --reason "Adopt verified original delivery" --operator-id maintenance-admin --confirm-bridge-stopped; then
  sudo systemctl start card-library.service
fi
```

The confirmation flag asserts that the bridge writer is stopped; it does not stop services or bypass proof checks. Run this against the installation's existing keys and paired state, never a new state directory. Managed upgrade and reward changes treat verified `refunded` orders as terminal alongside `complete` and `rejected`; prepared, delivered-but-not-finalized, refund-required, quarantined and legacy-reconciliation work remains pending.

## Consistent backups and in-place restore

With no native accounts connected, run:

```sh
python3 tools/cosmic.py --directory /absolute/install backup
```

The command stops website, card and game writers; dumps MySQL; archives stopped framework/journal state and smoke identity; saves keys, bootstrap configuration, image locks, checksums and exact game/card images; validates the completed backup; then restarts and runs readiness. Copy the printed backup directory off-host privately, preserving all files. The private file inventory is limited to `config.yaml`, `database.env`, `game.env`, `cards.env`, `catalog.json` and optional `test-account.json`. Extra operator notes, password input files and unfinished setup key files stay in place and need their own private backup if you require them. Restore only a trusted operator backup. Checksums detect corruption, not an untrusted party rewriting the backup and manifest together.

Add `--keep-stopped` to take a consistent snapshot without restarting writers. This is required during an unfinished legacy migration or ordinary upgrade after candidate staging completes. Such snapshots retain the private `migration.pending.json` or `upgrade.pending.json` fence. Finish partial ordinary staging or rollback before taking another backup; its original consistent backup is retained. Automatic restore and empty-target recovery reject unfinished-operation snapshots rather than discard that fence; retain the current-state snapshot, marker and original pre-migration backup together for qualified recovery. This limitation does not prevent ordinary completed-installation backups and recovery.

For an existing healthy installation at its recorded path:

```sh
python3 tools/cosmic.py --directory /absolute/install restore --backup /private/offhost/backup
```

Restore verifies checksums, mandatory SQLite databases and their integrity/schema, archive paths, project identity and database credentials before replacing data, then makes a safety backup. It replaces only the managed `cosmic` database as an exact snapshot, including removal of tables introduced after that backup. Restored services first validate with all public ingress closed. A failure at that stage attempts the safety backup; if rollback also fails, the error names the preserved backup. Once ingress reopens, a later fault stops services and preserves current data instead of discarding a new transaction. A restore intentionally replaces transactions after the chosen backup; schedule maintenance and verify its timestamp first.

State validation accepts the legacy framework schema and normalized schemas 2 through 8, and bridge journal schemas through 2. Normalized framework layouts must match the declared public schema exactly, including strict holder membership, its foreign keys and indexes in schema 3. Future versions and incomplete or mixed framework schemas are rejected. Encrypted payloads remain opaque during archive inspection; the restored image authenticates them with the original private keys during readiness. Activating recovery retries inspect private temporary copies of each primary database and its existing WAL, leaving the original volume read-only while rebuilding SQLite's temporary WAL index.

Managed backups also include validated `media.tar.gz`: ordinary raster files only, at most 32 MiB per file and 512 MiB total, with no symbolic links or traversal. Older backups without this optional archive remain restorable. Keep media changes paused during backup. See [static images](cosmic-setup.md#original-static-images).

Manual installations need the equivalent stopped-writer backup of MySQL, the entire private state directory, both environment files, active source/runtime version and any locally hosted media. Preserve ownership when restoring. Keep the real native inventory save boundary intact.

## Lost-installation recovery

Use this when the original installation and its Docker volumes are gone. Requirements are Linux/WSL2, Python 3.11+, Git, Docker Compose, a trusted complete off-host backup, enough disk for its images/state, and network access to the locked public MySQL/Node/nginx image digests. The backup carries the original private keys and exact game/card images; no original container or database credentials need to survive elsewhere.

Clone the public bridge tools, read the backup's `installation.json` project value without printing any environment files, and choose an empty absolute destination:

```sh
git clone https://github.com/zkkng/MapleStoryV83CardBridge.git
cd MapleStoryV83CardBridge
python3 tools/cosmic.py --directory /absolute/original/install recover --backup /private/offhost/backup --expect-project cosmic-cards-REPLACE_WITH_RECORDED_ID
```

Use the exact project from your trusted backup. Wrong identity, corruption, traversal, unexpected files, a nonempty destination or surviving project containers/volumes is rejected before target writes. This command never merges with another deployment. If recovering to a different path, add `--relocate`:

```sh
python3 tools/cosmic.py --directory /absolute/new/install recover --backup /private/offhost/backup --expect-project cosmic-cards-REPLACE_WITH_RECORDED_ID --relocate
```

Relocation changes only the recorded directory. The project identity, wallet/card ownership, keys, origin and advertised game address remain unchanged. Reconfigure host TLS/DNS/firewall separately if the host changed. Do not run the original and recovered deployments together. The command restores data before starting game/card writers, rebuilds the private proxy configuration, and checks readiness. A failed recovery stops services and preserves its partial directory and original backup for diagnosis; do not blindly delete unrelated Docker resources.

After recovery, verify `doctor`, administrator grants, known balances, cards and issued obligations. Repeated `smoke` verifies only its original receipt when the disposable account exists. A later upgrade fetches the recorded public Cosmic source revision if the recovered runtime has no source checkout. Custom source forks must restore their privately retained source separately before upgrade; their runtime images alone still support recovery.

An interrupted cold recovery leaves `recovery.pending.json`; ordinary `start` refuses to expose it. Repeat the exact recovery command with `--retry`, the same trusted backup, project, path and relocation option. The marker binds the backup's manifest digest and recovery phase. Before ingress has opened, retry can restore that exact backup. Once the marker records activation, retry instead verifies the existing runtime/volumes/card state and restarts using current databases and configuration; it never imports the backup again. This preserves transactions made after exposure, including across another failed private validation. The marker is cleared only after readiness and activation succeed. A different backup or runtime identity is rejected. Keep all current data and the original backup; do not delete the partial deployment to hide a possible new transaction.

## Internet access

Set the exact HTTPS `PUBLIC_ORIGIN`, such as `https://cards.example.org`, in the private bridge environment. Managed installs can set it initially through `install --origin`; normal service restart applies later configuration changes. The public proxy must preserve `Host` including any port and replace forwarding headers. For a managed deployment, include this in an existing TLS nginx server block with your issued certificate:

```nginx
server {
    listen 443 ssl;
    server_name cards.example.org;
    ssl_certificate /etc/letsencrypt/live/cards.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cards.example.org/privkey.pem;
    client_max_body_size 16k;
    location = /api/library/admin/preview {
        client_max_body_size 2m;
        proxy_pass http://127.0.0.1:8490;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 25s;
    }
    location / {
        proxy_pass http://127.0.0.1:8490;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 25s;
    }
}
```

For a manual backend on 8487, use the locations in `deploy/nginx.conf` and set `TRUST_PROXY=1`; that example replaces `X-Real-IP` directly. Test nginx configuration before reloading. Certificate issuance, DNS and router rules belong to your host. Allow public HTTPS and intended native game ports only; leave MySQL, game adapter 8486 and direct bridge 8487 private. Sign in from the exact HTTPS origin, verify the secure session cookie, sign out, and preview a large import to verify the 2 MiB administrator route while collector requests remain limited to 16 KiB.

## Upgrade

For a managed deployment, update the bridge source and use the [managed `upgrade` command](cosmic-setup.md#upgrade). It builds a candidate, takes a stopped-writer backup and qualifies the candidate with ingress closed before activation. `upgrade.pending.json` binds the original backup and old/new configuration identities to the exact candidate images. All configuration replacements are atomic. An interrupted operation keeps ordinary `start`, reward changes and another upgrade fenced. Resume its recorded staging, validation or rollback:

```sh
python3 tools/cosmic.py --directory /absolute/install upgrade --finish-upgrade
```

Before exposure, a candidate validation failure restores the consistent original backup. An interrupted rollback retains its marker for the same command. Once candidate or rollback activation is recorded, retry validates and starts the current databases; it never reimports the older snapshot after possible ingress exposure. Keep the marker, current volumes and original backup until completion. Do not run ordinary `start` or delete files to bypass recovery.

For a manual installation, use the following procedure.

Stop the bridge, reconcile any outstanding reward saves, and stop the game. Back up state and keys. Run the new installer's `--check`, inspect the game diff, install, rebuild, test and restart with the same persistent keys. Then start the bridge and verify an existing code history and an unopened pack. Preserve the game cash hooks whenever website debits are enabled.

## Legacy payment migration

An older framework installation may have paid orders that cannot be resolved by its installed runtime. Ordinary `upgrade` continues to require resolved orders and discovered orphan payments. To install the recovery runtime for legacy schema-1 state, use the explicit closed migration:

```sh
python3 tools/cosmic.py --directory /absolute/install upgrade --migrate-legacy-payments
```

This path verifies the candidate's actual public payment APIs and normalized framework schema 8 and journal schema 2 support, then stops and consistently backs up the old writers. It admits legacy state only; it cannot bypass modern unresolved financial orders. The old runtime image locks remain paired with that safety backup. Public game and website ports remain closed while the candidate loads the original state with unchanged keys, catalog and obligations.

`migration.pending.json` records the old/new configuration identities, original backup digest, source identity and exact candidate images. Interrupted staging resumes the recorded old/new files atomically, without deleting the marker or importing a database backup. Once migration has begun, failures preserve current databases and identities; they do not roll back new payment records. Ordinary start, reward changes and another upgrade cannot bypass the fence.

Resolve only payments for which the original delivery and ownership can be proven, using [legacy purchase repair](#proven-legacy-purchase-repair) where supported. Then repeat:

```sh
python3 tools/cosmic.py --directory /absolute/install upgrade --finish-legacy-migration
```

Finish validates privately and requires zero unresolved journal orders, zero discovered orphans, and empty authoritative framework/native pending-payment queries before exposure. It verifies the recorded keys/configuration and image identity again. After any possible exposure, another retry preserves current transactions and never imports the older backup. Delete no markers or volumes to force activation.

Historical native payments that contain only a numeric account ID may lack evidence of the original account incarnation after account deletion or reuse. Adoption without continuity proof fails with `LEGACY_IDENTITY_UNPROVEN`; automatic attribution is unavailable. The [managed attribution command](#retained-legacy-account-attribution) verifies the original retained backup for one supported payment. Unsupported or ambiguous cases remain closed. Retain continuity evidence and backups; do not infer ownership from the current account ID or submit manually fabricated evidence.

## Rollout checklist

The included site is a small collecting website for the pinned vanilla Cosmic baseline. This release is for evaluation with disposable test accounts and test cash. The pinned pair passes real MySQL financial tests and the default managed lifecycle, but concurrent-load qualification remains unresolved; do not take player funds. Use the following deployment checks as applicable:

1. Install into a clean copy of the supported Cosmic revision; run installer checks and the full game test suite.
2. Use a disposable real game account to sign in from the website. Verify each accepted balance independently and confirm that an insufficient selected balance never spends another balance.
3. Buy the default Shapes pack. Confirm eight collectible cards, no reward code, and no item delivery. Reload and restart both services; balances, unopened packs and the collection must persist.
4. Interrupt a purchase response after payment and recover the saved purchase. Confirm exactly one debit and one allocation. Complete pending orders before changing the catalog.
5. For optional client verification when rewards are enabled, redeem a configured code through a v83 client's Cash Shop. Verify account restrictions, inventory capacity, USED status, and replay rejection after a restart.
6. Serve the site behind TLS with the configured exact public origin. Verify secure cookies, sign-out, throttling and proxy headers. Keep the bridge/game private endpoints on loopback or a protected internal network.
7. Back up both services' persistent data and encryption keys together. Verify ownership, grants, active catalog and pending-order recovery after a controlled restore. Also test [empty-target recovery](#lost-installation-recovery) from an off-host copy.
8. Test the site on a phone and with keyboard navigation. Review imported catalogs and source receipts if using external scans. Keep the previous catalog and database backup available for a controlled rollback.

Automated qualification covers the documented pinned configuration. Client rendering, custom forks and the complete reward matrix are not established by the default lifecycle. Optional client checks can help assess your own configuration; they are separate from the automated release checks.
