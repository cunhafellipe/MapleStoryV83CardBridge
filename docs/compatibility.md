# Compatibility

Bridge 0.1 targets Cosmic GMS v83 revision `fec53bc7714dc0f1ae3f50b2986cdf2727e0912a` from [P0nk/Cosmic](https://github.com/P0nk/Cosmic/tree/fec53bc7714dc0f1ae3f50b2986cdf2727e0912a).

The framework runtime archive and integrity are pinned in package.json and the lockfile; the exact source revision is in data/framework-runtime.json. See [the runtime dependency](framework-runtime.md). The dependency includes synchronous generated-code pools and the server-only registration-material API; an older framework checkout lacks those methods.

## Qualified source and contract checks

- Installer anchors, repeat installation and Java 21 compilation against the pinned unmodified upstream source.
- The pinned Cosmic source passes 1,917 tests across twelve suites, including 31 adapter contract cases.
- SQL-backed reward/item/USED/outbox commit, failed-save retry, account ownership and immutable registration.
- Independent spending from all three cash balances, changed-type replay rejection and insufficient selected funds.
- Native account-session creation, lookup, revocation and password compatibility.
- Node bridge purchase, encrypted restart, generated code and signed HTTP contract tests.
- The standalone collecting website is independent of custom server websites. Browser fixtures cover Shapes packs without rewards, a separate reward-enabled catalog, mobile layout, purchase confirmation, and session isolation.

The adapter was also installed and compiled against a Cosmic-derived downstream checkout. This does not establish compatibility with every HeavenMS/OdinMS fork.

## Managed deployment qualification

The scripted Linux amd64 profile was exercised with Docker 29.7.2, Compose 5.5.0, MySQL 8.4.11, Java 21 and Node.js 24.19.0. A fresh database completed Cosmic migrations, native account login, a selected Prepaid debit and retry, eight collectibles plus one registered code, reveal and logout. Independent game restart, maintenance refusal for a connected account, funding idempotence, consistent backup, restoration of an intentionally changed balance, and upgrade retention of keys, cards and codes also passed. The CI workflow repeats installation and the persistent lifecycle on a disposable fresh stack.

These backend checks do not establish real-client coupon rendering or redemption. The managed installer runs on Linux; WSL2 is an available Linux execution route, rather than a separately qualified Windows-native profile.

## Runtime checks for your server

Most SQL contract tests use H2 in MySQL compatibility mode and mocked packet/character boundaries. Sixteen separate MySQL 8.4.11/Connector-J 9.3.0 checks cover existing-account receipt barriers, claim acknowledgement reconciliation and wallet retry under repeatable-read and read-committed isolation. These checks do not establish full connection-pool, character inventory, process supervision or real-client behavior.

Before enabling player purchases, complete the starter workflow with a disposable real account and v83 client: all accepted balances, the reward-free Shapes pack, game restart, bridge restart and purchase recovery. If enabling rewards, also check ordinary and pet rewards, full inventory rejection, repeat redemption and a temporarily unavailable callback. Confirm that each saved item and USED receipt agree. Keep your installed code revision and configuration together when diagnosing an issue.

The process model is one bridge writer and one enabled Cosmic process per installation. Distributed game writers are outside this profile. The framework uses normalized encrypted SQLite storage with bounded capacity. Economic commands still materialize state synchronously; normalized storage does not establish distributed transactions or large-installation throughput. Do not run two bridge services against the same state files.

Production payment qualification remains incomplete. Successful game debits can precede allocation failures caused by stock, generators or storage capacity; the current bridge preserves pending work but has no automatic compensation protocol. Use test accounts and test cash until that boundary is resolved. Current bounded collection responses still materialize whole collections internally and are not a large-installation throughput qualification.
