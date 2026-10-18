# Complete Cosmic setup and operation

Applies to bridge 0.1 and Cosmic revision `fec53bc7714dc0f1ae3f50b2986cdf2727e0912a`. The managed Linux profile creates a complete isolated game, MySQL database, card service and default website. It requires Python 3.11+, Git and a running Docker Engine with Compose. Use WSL2 with Docker integration on Windows. A v83 client, DNS, router configuration and public TLS are supplied separately.

On WSL2, keep the managed deployment inside the WSL home directory so private file permissions and Docker bind mounts behave as expected.

- [Install and verify](#install-and-verify)
- [Start, stop and diagnose](#start-stop-and-diagnose)
- [Accounts and funding](#accounts-and-funding)
- [Website administrators](#website-administrators)
- [Customize the catalog](#customize-the-catalog)
- [Private state and backups](#private-state-and-backups)
- [Upgrade](#upgrade)
- [Remote players](#remote-players)

## Install and verify

Clone [the bridge repository](https://github.com/zkkng/MapleStoryV83CardBridge) and run:

```sh
git clone https://github.com/zkkng/MapleStoryV83CardBridge.git
cd MapleStoryV83CardBridge
python3 tools/cosmic.py --directory ../CosmicCardServer install --test-account
```

The installation directory must be separate from the source repository and empty. Setup validates Cosmic's hooks before changing its deployment copy, builds with Java 21, runs the Cosmic and adapter tests, installs the pinned framework dependency with Node.js 24, initializes the game database through Cosmic's migrations, and starts the game, database, card service, proxy and private network container. Runtime container images are resolved to digests and retained in `images.lock.json`. Subsequent starts and upgrades use those locked runtimes.

Open `http://127.0.0.1:8490/library/`. Setup's final readiness check proves the signed game request, the signed provider callback, matching authentication mode and accepted cash types, and the website proxy. It fails when these disagree. The public catalog remains readable during a game outage, but the readiness endpoint returns failure.

Service readiness does not finish owner setup. Installation prints **Owner setup incomplete** until an ordinary native account receives an explicit website administrator grant. Follow [Website administrators](#website-administrators). The disposable test account remains a collector.

`--test-account` creates a disposable `CardTest` native game account with 5,000 NX Prepaid. Setup purchases one 1,000-unit Shapes pack, retries the same purchase, opens its eight collectibles and signs out. Credentials stay in `CosmicCardServer/private/test-account.json`; they are never printed. Keep this account for local testing only. The normal install omits it and grants no cash. The upstream demonstration administrator is disabled during the seed migration.

For a pack with exactly one dedicated Series One code insert, enable its complete profile explicitly:

```sh
python3 tools/cosmic.py --directory ../CosmicRewardsServer install --series-one --test-account
```

This adds the insert to each default pack and sets `ENABLE_SERIES_ONE_REWARDS=1`. Verification opens eight collectibles plus one code card, checks its game registration and reveals its code. The profile does not download card scans. A normal install leaves the reward provider disabled and issues no item codes.

For an existing reward-free installation, use the separate [selected-pack reward activation procedure](code-rules.md#enable-rewards-on-an-existing-managed-installation). It backs up and stops writers before changing the active catalog; `install --series-one` cannot be reapplied to an existing deployment.

An interrupted install leaves `setup.pending.json`. Repeat the **same command and options** to resume without replacing persistent keys, duplicating funding or buying another verification pack. A completed installation refuses a second install; use `start` or `upgrade` instead. Do not delete the marker, keys or volumes to recover an interrupted purchase.

To use a clean Cosmic-derived source checkout:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer install --cosmic-directory ../MyCosmic
```

This copies the checkout into the managed deployment and rejects unknown hooks or conflicting dependencies. It creates a new isolated database; it does not import or overwrite an existing game's database. For an existing operated installation, follow [the adapter guide](install.md). Fork compatibility still requires the [gameplay checks](compatibility.md).

## Start, stop and diagnose

Keep `--directory` before the command in every invocation:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer doctor
python3 tools/cosmic.py --directory ../CosmicCardServer stop
python3 tools/cosmic.py --directory ../CosmicCardServer start
python3 tools/cosmic.py --directory ../CosmicCardServer smoke
```

`smoke` requires the optional setup account. Its private state receipt freezes the verification quote before payment and retains only that purchase's pack/card identities. Repeating it replays the same order/opening and tolerates additional purchases without consuming another pack or charging again. It checks only its own collectibles and optional code registration. Stop refuses while a game account is logged in. Containers restart automatically after host or process failure; volumes retain the game database and encrypted card state. Start waits for database, adapter, callback and proxy readiness. Docker logs rotate at 10 MiB with three files per service.

For startup diagnostics:

```sh
docker compose -f ../CosmicCardServer/compose.json ps
docker compose -f ../CosmicCardServer/compose.json logs --tail 100 cosmic cards web
```

Do not publish environment dumps, SQL backups or code-reveal responses. Normal lifecycle commands require the recorded directory. Use [explicit disaster recovery](operations.md#lost-installation-recovery) to relocate a lost installation; copying a live directory does not move its Docker volumes.

## Accounts and funding

Create a native account with a privately prompted password:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer account create Collector
python3 tools/cosmic.py --directory ../CosmicCardServer account fund Collector --cash-type 4 --amount 5000 --request-id opening-credit-001
```

Use 3–13 letters or digits for the account name and 8–72 UTF-8 bytes for the password. `--password-file` accepts a private UTF-8 file for unattended provisioning. Passwords enter Java over stdin and are hashed with BCrypt. Recreating an account with the same password succeeds; a different password is rejected without changing it.

Cash types are 1=NX Credit, 2=Maple Points and 4=NX Prepaid. Funding requires a request ID. Retry an uncertain credit with the same ID, cash type and amount; its SQL receipt prevents duplicate credit. Reusing an ID with different terms is rejected. Use a new ID for an intentional additional credit. These commands are operator tools, not public website routes. Funding receipts live in `card_bridge_operator_funding`.

## Website administrators

Create or select an ordinary native account and grant website administration:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer account create Owner
python3 tools/cosmic.py --directory ../CosmicCardServer admin grant Owner
python3 tools/cosmic.py --directory ../CosmicCardServer admin list
```

Sign in at `http://127.0.0.1:8490/library/` with that native password and open Administration. Edit a card or pack, preview the change, then publish it. Use a separate collector account for purchases. Website grants use verified numeric game account IDs; they do not change GM level, passwords or cash. Unknown and banned accounts are refused. Repeating a grant or revoke is safe.

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer admin revoke Owner
```

Revocation applies to existing website sessions. The local CLI also recovers an installation with no remaining administrator. Preserve the journal containing grants in backups. See [administration](administration.md) for content and diagnostic controls.

For a banned/deleted identity, revoke its remembered grant using the verified numeric ID from `admin list`:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer admin revoke --account-id 123
```

Replace 123 with the grant ID. This form avoids eligible-account resolution and is safe to repeat. The managed cards container must still be running; the manual CLI can perform the same numeric revoke directly against local state when the game is unavailable.

## Customize the catalog

For a new installation, `install --catalog path/to/catalog.json` installs your collectible catalog. The optional [iTCG importer](itcg-import.md) generates separate packs whose images load directly from the source website, requiring no asset mount. Keep the input catalog outside the managed installation directory. Setup records its hash and refuses to resume with changed contents. Reward-bearing input catalogs are rejected; `--series-one` explicitly adds the supplied insert. With `--test-account`, the first pack must cost no more than its 5,000 NX Prepaid starting balance.

`CosmicCardServer/private/catalog.json` is the read-only bootstrap seed for an empty installation. After initialization the durable framework catalog is authoritative. Editing the seed and restarting does not replace a published catalog. Use Administration to edit cards, sets, rarity weights and pack prices, or explicitly preview/publish a JSON import. Publication validates revisions and conflicts before changing active content. Unresolved purchases must finish before incompatible content changes.

For the Series One profile, retain the generated insert's pool ID `v83.series-one`; game rewards remain constrained by the adapter whitelist. Imported scans do not enable this provider or create code inserts. Backup and upgrade preserve active catalog, grants and issued obligations. The [framework runtime](framework-runtime.md) supplies card contracts; game assets and your artwork remain separate inputs.

### Original static images

The managed `media/` folder is mounted read-only as the card service's `/assets`. To use a raster image you own or have permission to serve:

```sh
install -m 644 /path/to/your/own.png ../CosmicCardServer/media/own.png
```

Set the card form's image to `/assets/library/own.png`, preview and publish. Subfolders use letters, numbers, underscores or hyphens; files must be PNG, JPEG or WebP with a matching lowercase extension. No upload control or bundled third-party art is provided. Keep images at most 32 MiB each and managed media at most 512 MiB total. Managed backup, restore and disaster recovery include these files. Imported remote scans need no media copy; see [the scan importer](itcg-import.md).

## Private state and backups

`private/` contains generated service keys, database credentials, the runtime game configuration and catalog. Its parent directory is owner-only. Keys are not baked into container images. The game and card containers run as separate unprivileged users. MySQL and adapter HTTP ports are never published. Card and proxy roots are read-only; writable state has its own named volume.

Take a consistent backup while no game accounts are connected:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer backup
```

The command stops game, card and website writers, dumps MySQL, archives the stopped SQLite state, and saves private keys, catalog, configuration, checksums and the exact game/card Docker images. It restarts and checks the installation afterward. Backups contain account data and credentials; store copies privately off the host. They can be large because runtime images are included. Keep free space for an additional safety backup before a restore or upgrade.

Restore one of this installation's backups:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer restore --backup ../CosmicCardServer/backups/REPLACE_WITH_BACKUP_NAME
```

Restore verifies checksums, SQLite integrity/schema and allowed state paths, requires the original installation identity and database credentials, and takes a safety backup before replacing data. It loads and verifies the saved runtime images, replaces the owned MySQL schema and card state, and validates restored services with game/web ingress closed. A private validation failure attempts the safety backup. A later activation failure preserves current data to avoid losing a new transaction; see [operations](operations.md#consistent-backups-and-in-place-restore). Restore accepts trusted operator backups only. It does not merge databases, move installations or restore a different server's backup.

If the original installation and volumes are lost, use the separate [empty-target recovery command](operations.md#lost-installation-recovery). It needs only the trusted off-host backup, the public bridge tools, Docker and public locked runtime images. It refuses nonempty destinations, wrong project identities and surviving project resources. Explicit relocation changes the directory while retaining the installation project and keys.

## Upgrade

Update this bridge source checkout to the desired reviewed release, then run:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer upgrade
```

Upgrade refuses connected game accounts and unresolved purchases, validates and installs the adapter into the managed game copy, builds and tests the new images, makes a consistent safety backup, then starts and checks the candidate. A failed candidate restores the previous images, keys and data. Issued codes, the customized catalog and persistent encryption/index keys are retained. Locked base images and the Cosmic source revision are not silently changed. Major Cosmic/runtime migrations require separate qualification.

Candidate validation temporarily removes all published game/web ports while retaining the database service. Internal readiness passes before those ports reopen. A staging failure can safely restore the previous backup because players could not transact with the candidate. If final ingress activation fails after reopening ports, services stop and candidate data is retained; the error preserves the recorded operation and recommends correcting configuration and running `upgrade --finish-upgrade`. Ordinary `start` remains fenced until the upgrade completes. Automatic rollback at that point could discard a new transaction, so it is deliberately refused.

For older paid orders that the installed runtime cannot reconcile, use the separate [closed legacy payment migration](operations.md#legacy-payment-migration): `upgrade --migrate-legacy-payments`, followed by `upgrade --finish-legacy-migration` only after authoritative closure. Ordinary upgrade remains resolved-only. Migration retains a fence, bound backup and exact images; it does not infer historical account ownership or automatically refund unverified payments.

## Remote players

For a remotely reachable game, supply the IPv4 address advertised to the v83 client, the host interface, and the website's real HTTPS origin:

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer install --game-host 192.0.2.10 --bind 0.0.0.0 --origin https://cards.example.org
```

The addresses above are examples. Configure your host TLS reverse proxy to forward that origin to `127.0.0.1:8490`. The HTTP listener remains bound to loopback even when game ports use `--bind 0.0.0.0`. Setup rejects non-loopback HTTP origins. Public DNS, certificate issuance and router/firewall rules are host-specific; the installer does not change them. See [Docker's firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/) when publishing container ports.

Have the TLS proxy replace `X-Forwarded-For` with the verified client address. The managed proxy trusts only loopback and its generated Docker host gateway when resolving that header, then replaces `X-Real-IP` for the card service. This keeps request limits separate for remote players. Do not forward an unchecked client-supplied forwarding header.

Defaults publish website port 8490, game login 8484 and channels 7575–7577, bound to loopback. `--web-port` and `--login-port` select different host ports; the client must use the chosen login port. Channel ports are fixed in this profile. The game, card backend and proxy join a stable private network container, so callbacks use loopback without LAN relays and the game can restart independently. All three cash types use the same example pack price; debits never combine balances.

Before accepting player purchases, use a real v83 client to qualify ordinary and pet redemption, inventory-full rejection, repeat redemption and saved-item/USED agreement after restarts. Automated setup tests the backend account-to-code flow; it does not automate a licensed game client.
