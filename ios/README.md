# iOS

A native client is viable, and this is where it starts.

Nothing in `hsa_vault/` ports — Streamlit is a server-rendered Python web
framework with no iOS target, so all eight pages are throwaway. What survives is
the **contract**, and the piece of it that cannot be approximated is the
claimable-balance rule. That is what lives here.

## Why the balance rule first, and not a screen

Drive holds the files and Sheets holds the index, so an iOS app is simply
another client of two Google REST APIs — there is no backend to build and no
data to migrate. The screens are ordinary work.

The rule is not. A client that disagrees with the Python app about the claimable
balance is worse than no client at all: both would show a confident number for
the same Sheet, and the difference between them is medical dollars claimed from
the HSA twice. `hsa_vault/tests/test_ledger.py` is the specification;
`Sources/HSAVaultCoreChecks/main.swift` mirrors it case for case.

## Running the checks

```sh
cd ios/HSAVaultCore
swift run HSAVaultCoreChecks
```

No Xcode required — this is deliberate. The package is pure Swift with no
SwiftUI, UIKit, or Google SDK, so the rule can be verified with the Command Line
Tools alone, before anyone installs a 17 GB IDE or writes a view.

The checks are a plain executable rather than a test target for the same reason:
XCTest and swift-testing both ship with Xcode, and neither exists in a bare CLT
install. Convert to XCTest once Xcode is on the machine.

Verified by reverting the fix: deleting the `hsa_card` guard from `claimable`
fails three checks, including *"a withdrawal was applied to a receipt the HSA
already paid — double-claimed"*.

## What is still missing

| Piece | Notes |
|---|---|
| Google Sign-In | On-device OAuth into the Keychain. Removes the refresh-token-in-secrets problem the web deploy has entirely. |
| Sheets + Drive clients | REST. `RECEIPT_COLUMNS` order is the wire format — reads are positional. |
| The N+1 write order | The withdrawal row must be written **before** any receipt is marked. See `hsa_vault/tests/test_reimbursement_write_order.py`. |
| Concurrent writes | Two clients on one Sheet makes the read-modify-write race in `sheets.row_number_of` → `update_row` real rather than theoretical. |
| Screens | The ordinary part. |

## Before shipping to the App Store

The `auth/drive` scope is *restricted*. Distribution requires Google
verification plus a CASA security assessment — a serious gate for a personal
app. Sideloading to your own device with an Apple Developer account avoids it.

Note also that an NVIDIA API key must never be embedded in the binary; either
the user supplies their own or extraction goes through a proxy.
