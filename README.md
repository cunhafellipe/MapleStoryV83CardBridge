# MapleStory v83 Card Bridge

Add an iTCG-style card system to your MapleStory private server. Players sign in with their game accounts, buy digital packs, and collect cards. **Enable rewards to distribute code cards redeemable in the game's existing Cash Shop code-entry field.** The bridge handles code issuance, account ownership, and redemption status, so you can connect collecting to in-game rewards without building that workflow yourself.

Built on [DigitalCardFramework](https://github.com/zkkng/DigitalCardFramework), it includes a card website and installer for vanilla [Cosmic GMS v83](https://github.com/P0nk/Cosmic). NX Credit, Maple Points, and NX Prepaid work by default. Other MapleStory servers and currencies, including vote points, require adapter changes.

Start with a sample Shapes pack and no reward pool configured. Add your own cards, enable a reward campaign, or optionally import scans from five original iTCG sets.

## Current release limits

Use this release with disposable test accounts and test cash. The website and administration tools are usable, and allocation failures now have a durable original-balance refund protocol. Production qualification remains incomplete: concurrent HTTP load exceeds acceptable response times. Real MySQL payment tests and the managed fresh-install, restart, upgrade and failed-candidate rollback checks pass for the pinned framework runtime. Do not use this release to take player funds. See [compatibility](docs/compatibility.md) and [payment recovery](docs/payments.md).

## Get started

You need Linux or WSL2, Python 3.11+, Git, and Docker with Compose:

```sh
git clone https://github.com/zkkng/MapleStoryV83CardBridge.git
cd MapleStoryV83CardBridge
python3 tools/cosmic.py --directory ../CosmicCardServer install --test-account
python3 tools/cosmic.py --directory ../CosmicCardServer account create CardOwner
python3 tools/cosmic.py --directory ../CosmicCardServer admin grant CardOwner
```

This installs Cosmic, the database, bridge and website. Account creation prompts for a password. Open `http://127.0.0.1:8490/library/` and sign in as CardOwner. In **Administration → Packs**, change Shapes to 1,200 NX, save the draft, preview and publish. Sign out and use the separately funded test collector to buy and open a pack; its credentials are in `../CosmicCardServer/private/test-account.json`. The test account is never promoted automatically.

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer doctor
```

Follow the [setup guide](docs/cosmic-setup.md) for configuration and operation, or the [adapter guide](docs/install.md) to connect an existing Cosmic deployment. Game client files are supplied separately.

## Make it your own

- **Cards and packs:** create sets, cards, prices and weighted packs through [Administration](docs/administration.md). The [collector website](docs/website.md) includes pack previews, persistent balance display, collection search, filters, duplicate counts and set completion. Opened packs become cards; no opened-pack history is required. The default interface uses original CSS illustrations and system fonts.
- **Reward codes:** the optional [Series One profile](docs/cosmic-setup.md#install-and-verify) includes a ready-made reward campaign. [Code rules](docs/code-rules.md) explain redemption and the supported rewards; custom rewards need a matching game-side provider.
- **Original iTCG sets:** the [optional importer](docs/itcg-import.md) creates five set-based packs using externally sourced scans. No artwork is bundled and no rewards are enabled by the import.
- **Other servers or currencies:** adapt the [server protocol](docs/protocol.md) to your account, payment, and reward systems.

Use [operations](docs/operations.md) for Internet access, backups, lost-installation recovery and upgrades, or [troubleshooting](docs/troubleshooting.md) when a check fails. Before inviting players, check [compatibility](docs/compatibility.md) and complete the [rollout checks](docs/operations.md#rollout-checklist) with your separately supplied game client and database.

Source: AGPL-3.0-only. Third-party artwork retains its original rights.
