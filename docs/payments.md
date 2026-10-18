# Payment recovery

This guide applies to the pinned Cosmic deployment and its durable external-purchase protocol. Use the same bridge, framework and game adapter versions when recovering an installation. The readiness check requires `cosmic-payments/1`; an older game adapter cannot serve the new purchase protocol.

Each purchase binds its account, original cash type, amount, reviewed catalog and request identifier. NX Credit, Maple Points and NX Prepaid remain separate. Preparation validates allocation prerequisites and reserves storage for a terminal outcome. It does not reserve stock or execute random generators. After the game commits its debit, the framework either commits the complete delivery or records that the original payment requires a refund. A failed or lost response alone never authorizes a refund.

| Website status | Meaning | Operator action |
| --- | --- | --- |
| Payment pending | The original debit or cancellation is being reconciled | Restore service readiness and retry the same order |
| Finalizing delivered cards | Cards exist; native payment finalization is pending | Retry the same order; do not grant another pack |
| Delivered | Delivery and native finalization are complete | No recovery needed |
| Declined without payment | A durable cancellation prevents a later debit | The collector may review their balance and start a new purchase |
| Refund pending | Allocation failed; original-source refund is pending | Restore native/storage availability; inspect original refund capacity |
| Refunded to original balance | Native refund and framework confirmation agree | No cards were delivered for this order; the collector may start a new purchase |
| Operator reconciliation required | Historical payment evidence is inconsistent | Preserve all evidence and use only a provable recovery procedure |

The background worker discovers unresolved native and framework operations in bounded pages. It also adopts historical successful deliveries to release their native refund headroom. Retried operations preserve original keys, terms and delivered identities. Unknown outcomes remain pending. Unmatched identities without their journal evidence pause new payments and catalog publication; restore the paired journal from a verified consistent backup instead of reconstructing orders from guesses.

## Refund capacity

The game retains room in the original cash balance for unresolved refundable debits. Qualified native credits respect that room, including operator funding, coupons and trading proceeds. A legacy account may already lack this capacity. That obligation remains explicitly owed: the bridge cannot claim a refund until the original source was credited. Do not remove payment rows, change source, manually mark a refund or credit money while a recoverable order can still allocate.

Use Administration → Activity/orders to inspect the original order and retry it. The overview shows unmatched identities and readiness details without revealing code plaintext. Back up the entire paired installation before repairing historical data. See [operations](operations.md) for consistent backup, restoration and log commands.

## Adopt a proven historical delivery

An original native payment may lack a retained account incarnation. It must first pass the managed backup attribution procedure below. Attribution alone cannot authorize delivery, adoption or a refund.

One local repair is supported for a historical order whose original internal credit was immediately spent by another purchase key. The framework requires that credit to have started from zero, that the immediately following wallet debit bought exactly the originally reviewed product and quantity, and that the existing receipt, packs, cards and codes remain complete and unbound to another external purchase. The command preserves those identities and releases native refund headroom. It does not grant cards, alter balances or refund an uncertain payment. Other inconsistent evidence remains quarantined.

1. Record the original order ID and the existing purchase key from authoritative operator evidence.
2. Back up the installation. Stop the bridge writer and keep website ingress closed; keep the native game adapter running.
3. Load the installation's private environment and run the same attributed decision until it completes.

For the documented manual installation:

```sh
sudo systemctl stop card-library.service
sudo -u card-library /usr/bin/node --env-file=/etc/card-library/.env /opt/card-library/bridge/tools/payments.mjs resolve-legacy \
  --order-id ORIGINAL_64_HEX_ORDER_ID \
  --purchase-key EXISTING_PURCHASE_KEY \
  --decision-key UNIQUE_DECISION_KEY \
  --reason "Adopt the verified original-credit-funded delivery." \
  --operator-id LOCAL_OPERATOR_ID \
  --confirm-bridge-stopped
```

Use the Node path installed by the [manual guide](install.md). The operator ID attributes the decision locally; it does not grant a game role. Keep the reason free of credentials and code plaintext. A refusal leaves the original obligation intact. A response loss can leave the decision committed and finalization pending: repeat the exact original decision key, reason, operator ID and purchase key. Changing those terms cannot replay that decision.

When the command reports `complete` with the original purchase ID, restart the bridge and run readiness:

```sh
sudo systemctl start card-library.service
sudo -u card-library /usr/bin/node --env-file=/etc/card-library/.env /opt/card-library/bridge/tools/doctor.mjs
```

Verify the order, existing cards and all three balances before reopening ingress. Keep the pre-repair backup and the attributed decision record. See [administration](administration.md) for order search and [troubleshooting](troubleshooting.md) for unavailable services.

## Attribute a retained historical account

The managed legacy-migration profile can prove an unanchored historical payment from its original stopped-writer backup. It requires the recorded migration to remain closed in `migrating` or `validating`, the same installation and private keys, verified checksums for every retained file, and supported UTC creation timestamps in the original Cosmic SQL dump. A timestamp from the current account alone is insufficient. The original account creation must precede the payment and still match the retained native account.

Use the managed `attribute-legacy-payment` command described in [operations](operations.md). Keep the original order ID, decision key, operator ID and reason for an exact retry after response loss. The command keeps the card writer and website stopped and leaves migration ingress closed. It does not finish the migration or restart collectors automatically.

Success reports `attributed`: the native account identity and immutable attribution audit are anchored, with no changes to cash, cards, payment state, delivery, adoption or refund. Continue the original separately verified payment resolution, then run the recorded migration completion procedure. Unsupported dumps, unavailable original accounts, reused numeric IDs, changed terms and mismatched installation files refuse attribution.

The extractor supports the managed MySQL dump format with the `cosmic` database, extended `INSERT` statements, standard escaping and `NULL`, non-null zero-precision `TIMESTAMP` creation fields and `CURRENT_TIMESTAMP` defaults. Dumps requiring custom delimiters or exceeding 128 MiB are refused. Do not edit or execute a retained dump against the operating database to make it pass. Preserve the original evidence and closed migration marker for diagnosis.
