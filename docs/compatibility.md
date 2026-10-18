# Compatibility

Bridge 0.1 targets Cosmic GMS v83 revision `fec53bc7714dc0f1ae3f50b2986cdf2727e0912a` from [P0nk/Cosmic](https://github.com/P0nk/Cosmic/tree/fec53bc7714dc0f1ae3f50b2986cdf2727e0912a).

The framework runtime archive and integrity are pinned in package.json and the lockfile; the exact source revision is in data/framework-runtime.json. See [the runtime dependency](framework-runtime.md). The dependency includes generated-code pools, trusted registration material, durable external purchases, indexed collection reads and normalized storage. The native adapter must advertise `cosmic-payments/1`; an older adapter cannot pass readiness.

## Qualified source and contract checks

- Installer anchors, repeat installation and Java 21 compilation against the pinned unmodified upstream source.
- The installed source passes the full game test/package build, including the regression check that native pending-payment responses preserve an explicit terminal cursor.
- SQL-backed reward/item/USED/outbox commit, failed-save retry, account ownership and immutable registration.
- Independent spending from all three cash balances, changed-type replay rejection and insufficient selected funds.
- Native account-session creation, lookup, revocation and password compatibility.
- Node bridge purchase preparation, atomic allocation, original-source refunds, response-loss recovery, encrypted restart, generated codes and signed HTTP contracts. Future journal versions present only in WAL are rejected without altering original files.
- The standalone collecting website is independent of custom server websites. Browser fixtures cover Shapes without rewards, an optional reward catalog, selected-balance confirmation, lost-response and rate-limit recovery, 211-variant collection pagination, mobile layout, keyboard controls and session isolation. It bundles no artwork.

The adapter was also installed and compiled against a Cosmic-derived downstream checkout. This does not establish compatibility with every HeavenMS/OdinMS fork.

## Managed deployment qualification

The current bridge with framework commit `2423fa6f700e2e7627c31f075a7ddd80d641e203` passed a real Linux amd64 Compose deployment: fresh installation and acceptance, restart, same-source upgrade, and deliberately failed candidate rollback. The reward-free Shapes workflow purchased and opened packs using cash types 1, 2 and 4. Wallets, packs, opened cards, catalog and native payment state remained consistent across the lifecycle; rollback restored the preceding runtime images.

This qualification covers the pinned pair and the default reward-free profile. It does not establish the complete reward/custom-catalog matrix, every recovery scenario or compatibility with another framework revision. The CI workflow runs default, reward and custom-catalog deployments plus broader lifecycle checks; inspect the result for the exact revision you install.

These backend checks do not establish real-client coupon rendering or redemption. The managed installer runs on Linux; WSL2 is an available Linux execution route, rather than a separately qualified Windows-native profile.

## Runtime checks for your server

Most SQL contract tests use H2 in MySQL compatibility mode and mocked packet/character boundaries. Six real MySQL 8.4.11 tests passed without failures or skips, covering debit/cancel races, finalize/refund races and replay after a lost commit acknowledgement under repeatable-read and read-committed isolation. Each scenario exercises cash types 1, 2 and 4. These checks do not establish every connection-pool, character-inventory or client behavior.

Optional checks for your deployment include the starter workflow with a disposable account and v83 client: all accepted balances, the reward-free Shapes pack, game restart, bridge restart and purchase recovery. For a custom reward configuration, useful additional checks include ordinary and pet rewards, full inventory rejection, repeat redemption and a temporarily unavailable callback. Confirm that each saved item and USED receipt agree. Keep your installed code revision and configuration together when diagnosing an issue.

The process model is one bridge writer and one enabled Cosmic process per installation. Distributed game writers are outside this profile. The framework uses normalized encrypted SQLite records with bounded capacity and indexed code history for current and former holders. Economic commands still materialize whole state synchronously; normalized storage alone does not establish scalable transactions. Do not run two bridge services against the same state files. The qualified native configuration requires MTS disabled; enabling `USE_MTS` is rejected because the stock listing and withdrawal lifecycle has not been qualified with bridge payments.

Production qualification remains incomplete. An actual loopback HTTP workload with 20 readers, 800 existing card copies and two scheduled purchases per second produced many 18-second request timeouts. Native wallets in that test were synthetic, so the failure already occurs without real game-database latency. Passing single-user and mocked transaction checks does not override this failed load gate. Use test accounts and test cash while throughput qualification remains unresolved. Automated backend results do not certify client rendering or every custom reward configuration. Historical unanchored payments also require authoritative ownership evidence before adoption; unprovable identities remain quarantined. See [payments](payments.md) before migrating an existing installation.
