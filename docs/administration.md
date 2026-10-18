# Administer the card website

This guide applies to the pinned Cosmic bridge deployment. The default Shapes pack contains collectibles only. No reward pool is enabled. Website administration is independent of game GM level.

## Grant access

Create an ordinary native account through your server's registration process, or use the managed account command. Load the installation environment for manual commands; do not paste private keys into a terminal command.

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer account create CardOwner
python3 tools/cosmic.py --directory ../CosmicCardServer admin grant CardOwner
python3 tools/cosmic.py --directory ../CosmicCardServer admin list
```

Account creation prompts for a password. The grant command resolves the native account and reports its numeric ID. Unknown or banned accounts cannot be granted access. Repeating a grant has no additional effect. A rename preserves access because grants belong to the numeric account ID.

For an existing-server installation:

```sh
sudo -u card-library /usr/bin/node --env-file=/etc/card-library/.env /opt/card-library/bridge/tools/admin.mjs grant CardOwner
sudo -u card-library /usr/bin/node --env-file=/etc/card-library/.env /opt/card-library/bridge/tools/admin.mjs list
```

Use the Node path installed by the [existing-server guide](install.md). The game adapter must be running. The commands never change game passwords, cash or GM level. Grants and history reside in the bridge database and are included in backups.

Sign in at your configured website origin with this native account: the default managed URL is `http://127.0.0.1:8490/library/`, and the default manual URL is `http://127.0.0.1:8487/library/`. **Administration** appears beside the collector navigation. A granted account can also buy and collect cards normally.

## Make cards and packs

Open **Administration → Cards & sets**. Add a set with a stable ID, name and description. Add a card to that set, then select its rarity and variant identity. Give it a short original symbol, or a supported image URL. Save the form into the draft. Errors retain the draft; saving a form does not change the live collection.

Static images use `/assets/library/filename.png`, `.jpg`, `.jpeg` or `.webp`. Place files beneath the configured `ASSET_ROOT`; the bridge checks real paths and refuses files outside that root. Keep images and their backups outside the source checkout. There is no upload service or arbitrary server-side image fetching. The optional [iTCG importer](itcg-import.md) uses its documented external source. Layered art, custom opening actions and owner data bindings are not rendered by this website and are rejected in website publication.

Open **Packs**. Select the set, enter a positive NX price, and choose a maximum purchase quantity. The largest checkout total cannot exceed 100,000,000. All three native balances use the same numerical price; the collector selects the wallet independently.

Add guaranteed draw slots with a count and positive variant weights. This website supports independent weighted draws with duplicates allowed. It rejects optional probability slots, pity and duplicate exclusion policies. Finite supply can change later draws; the contents dialog identifies weights rather than unconditional odds where supply is constrained.

To retire an offer, disable it and publish. Existing cards and unopened packs remain. Published card/set/variant identities cannot be deleted or reassigned; add new identities when changing an edition or rarity. Catalog and pack revisions advance automatically.

## Preview and publish

1. Choose **Preview changes**. Review added, changed and retired entries, warnings and the live version.
2. Choose **Publish reviewed changes**. Publication applies the reviewed digest, version and policy only.
3. Check the public pack list and export the active catalog if needed.

If another administrator publishes first, preview again against the current version. A changed digest, expired preview, changed policy or revoked grant cannot publish. Outstanding purchases must finish before catalog publication; retry them through Orders & activity. Closing the browser or signing out discards local drafts. The active catalog persists in framework state; editing the original bootstrap catalog file does not replace it on restart.

**Import & export** supports complete JSON catalogs up to 2 MiB, including the optional five-set import. Loading a file creates a draft; preview and publish are still required. Preview merges matching identities and retains published entries absent from the import; disable an offer explicitly to retire it. Export downloads the active catalog, not the bootstrap file. An export is not a full installation backup.

## Diagnose purchases and rewards

**Overview** reports game/callback/lease health, catalog version, storage state and unresolved work. **Orders & activity** searches exact native account names, account IDs or durable order IDs and shows cash type, amount, attempts, safe errors and timestamps. **Retry this order** resumes the original purchase identity. It cannot create a replacement debit, edit a balance or reveal a code.

Rewards are off until the [supported provider is explicitly enabled](cosmic-setup.md#install-and-verify). Rewards status describes the configured campaign; the website does not offer arbitrary item or meso editing. Registration retries use the same recovery engine. Only the owning collector can reveal a code. Game receipts control USED status; administrators cannot mark codes used or expose another player's plaintext.

## Revoke and recover access

```sh
python3 tools/cosmic.py --directory ../CosmicCardServer admin revoke CardOwner
```

Already-open sessions immediately lose permission on their next admin request. Native bans, expiry and deleted accounts also deny access. If every grant is removed, use the local grant command again; there is no public role-management endpoint. See [operations](operations.md) for backup and restoration, and [troubleshooting](troubleshooting.md) for unavailable accounts, stale previews and outstanding orders.
