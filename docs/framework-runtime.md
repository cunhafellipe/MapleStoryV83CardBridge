# Framework runtime dependency

The bridge depends directly on the public [DigitalCardFramework repository](https://github.com/zkkng/DigitalCardFramework). Bridge 0.1 pins its MIT licensed 0.2.0 indexed-storage-8 edition to [commit `2423fa6f700e2e7627c31f075a7ddd80d641e203`](https://github.com/zkkng/DigitalCardFramework/tree/2423fa6f700e2e7627c31f075a7ddd80d641e203).

`npm ci` downloads that exact source archive from GitHub. No GitHub account, SSH key, local framework checkout or bridge-hosted framework copy is required. The pinned commit includes generated-code pools, the trusted server-only `codeRegistrationMaterial` API, durable external-purchase preparation and compensation, indexed collection and current/former-owner code-history reads, and normalized encrypted storage schema 8. An older framework revision lacks these contracts. Upgrade the native adapter together with the bridge; readiness requires `cosmic-payments/1`.

At startup the bridge uses the framework's idempotent completion-reservation backfill before recovering existing purchases. Storage migration does not enable new catalog capabilities: existing installations still require an explicit capability preview and publish through the administrator catalog workflow. The supplied starter catalogs declare the pack-collection profile with issuance and settlement enabled. Backup validation recognizes exact normalized schemas 2 through 8 and rejects unknown or inconsistent layouts.

`package-lock.json` pins the archive URL and SHA-512 integrity. [The runtime descriptor](../data/framework-runtime.json) records the source repository, commit, SHA-256, archive size and integrity. These identifiers must change together when qualifying a framework upgrade.

The framework supplies generic card behavior and public APIs. This bridge supplies Cosmic identity, cash balances and game rewards. Custom frontends use the same public bridge APIs. Use the framework repository for its source, license, contracts and development history.

After installation, inspect the dependency's `src/`, `docs/openapi.json` and `docs/card-types-and-codes.md` files. Its MIT software license does not cover separately supplied artwork. Qualify local runtime modifications before changing an installed bridge.
