# Framework runtime dependency

The bridge depends directly on the public [DigitalCardFramework repository](https://github.com/zkkng/DigitalCardFramework). Bridge 0.1 pins its MIT licensed 0.2.0 indexed-storage-8 edition to [commit `2423fa6f700e2e7627c31f075a7ddd80d641e203`](https://github.com/zkkng/DigitalCardFramework/tree/2423fa6f700e2e7627c31f075a7ddd80d641e203).

`npm ci` downloads that exact source archive from GitHub. No GitHub account, SSH key, local framework checkout or bridge-hosted framework copy is required. The pinned commit includes synchronous generated-code pools and the trusted server-only `codeRegistrationMaterial` API. It uses normalized encrypted storage schema 8. Startup backfills completion reservations for existing work through the public framework maintenance API. The starter catalogs explicitly enable the pack-collection profile with issuance and settlement. Existing catalogs require an explicit administrator capability preview and publish; storage migration does not enable capabilities automatically. Backup and recovery validation recognize exact framework schemas 2 through 8 and retain the bridge journal version 1 boundary.

`package-lock.json` pins the archive URL and SHA-512 integrity. [The runtime descriptor](../data/framework-runtime.json) records the source repository, commit, SHA-256, archive size and integrity. These identifiers must change together when qualifying a framework upgrade.

The framework supplies generic card behavior and public APIs. This bridge supplies Cosmic identity, cash balances and game rewards. Custom frontends use the same public bridge APIs. Use the framework repository for its source, license, contracts and development history.

After installation, inspect the dependency's `src/`, `docs/openapi.json` and `docs/card-types-and-codes.md` files. Its MIT software license does not cover separately supplied artwork. Qualify local runtime modifications before changing an installed bridge.
