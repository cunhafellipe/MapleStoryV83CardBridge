# Optional iTCG scan import

The bridge uses an original Shapes collection by default. This optional tool reads the five English card-scan galleries from [the MapleStory Card Game Guide](https://maplestoryitcg.weebly.com/), preserving each gallery as a separate set and pack. Images load directly from the source website by default. It does not configure item rewards, generate game codes, or bundle scans in the source repository.

The source gallery order supplies stable scan numbers. Source pages do not provide machine-readable card names or rarity labels, so imported cards are named by set and number and use the rarity label Scanned card. You can replace these labels and adjust weights in the generated catalog. Every card initially has equal draw weight; eight draws per pack allow duplicates. These are digital collection packs, not historical booster-odds claims. Galleries may include alternate printings or extra cards.

## Inspect and import

Use Python 3 from the bridge directory:

```sh
python3 tools/import-itcg.py --inspect
python3 tools/import-itcg.py --output external/itcg --sets 1,2,3,4,5 --price 1000 --catalog-version 3
```

`--inspect` fetches only gallery HTML and lists scan counts. `--sets` accepts any subset of 1–5. The default import writes a catalog of source image URLs and a receipt containing gallery hashes and scan references, without downloading artwork. The website permits only the named HTTPS source, sends no referrer with image requests and shows an unavailable-image placeholder if a scan cannot load. The source controls availability of these images; cards and purchase receipts remain stored on your server.

To download local copies instead, add `--download`. Downloads run sequentially with a short delay, limit each file to 8 MiB, check JPEG/PNG signatures and record file hashes. An existing output directory is never overwritten. A failed download leaves an INCOMPLETE marker and no activatable catalog; retry into a new directory after correcting the source or network issue.

The output contains:

- `catalog.json`: the framework catalog with one pack per set.
- `assets/`: downloaded scans, only when `--download` is selected.
- `sources.json`: source URLs and gallery hashes; downloaded mode also records image byte counts and file hashes.

The importer accepts only scan links from the named source domain and refuses redirects outside it. If its page layout changes, it fails rather than substituting art. Keep generated catalogs and receipts outside public source control and in installation backups. Direct-source images are display assets; they never determine payment or pack allocation.

## Activate the catalog

For a fresh managed Cosmic installation, review the catalog and run:

```sh
python3 tools/cosmic.py --directory ../CosmicITCGServer install --catalog external/itcg/catalog.json --test-account
```

This copies the catalog into the managed private configuration; direct-source mode needs no artwork mount and keeps rewards disabled. Interrupted setup verifies the original catalog hash before resuming. For a fresh manual installation, set `CATALOG_PATH` to the absolute path of the reviewed `catalog.json` before starting an empty bridge state directory. Keep `ENABLE_SERIES_ONE_REWARDS=0`.

For an existing managed or manual installation:

1. Sign in as a website administrator and open **Administration → Import & export**.
2. Load the generated `catalog.json`, then select **Preview changes**. Review the additions, changes and any validation errors.
3. Select **Publish reviewed changes** after reviewing the preview. Finish or recover unresolved purchases first if publication reports a conflict.

The website manages catalog and product revisions. Import preserves published entries whose IDs are absent from the imported file, including Shapes and previously issued card identities. To stop selling Shapes, disable its pack in **Packs**, then preview and publish that edit. Existing cards and unopened packs remain available. Changing the bootstrap file or `CATALOG_PATH` and restarting does not replace a catalog already stored in the database. See [administration](administration.md) for conflicts and draft recovery.

For local-download mode, copy the contents of the generated `assets/` directory into the managed installation's `media/` directory, preserving its set subdirectories. Directories must be readable by the container and image files must be mode `644`. The managed service mounts `media/` read-only at `/assets`. For a manual deployment, set `ASSET_ROOT` to the absolute path of the generated `assets/` directory and ensure the service user can read it. Preserve these assets in backups; issued cards still refer to them.

Check each selected set in the pack shop, inspect contents, purchase with a test account, open a pack, and confirm images and duplicates appear in the collection.

The scan artwork is third-party material. Downloading it separately does not change its copyright or grant redistribution rights. Use it only where you have the necessary permission; the Shapes pack works without external artwork.
