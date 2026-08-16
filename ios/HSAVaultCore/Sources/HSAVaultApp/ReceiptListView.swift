import SwiftUI

import HSAVaultCore

/// The vault's main screen: the claimable balance, then the receipts behind it.
///
/// The balance is deliberately the first thing on the page. It is the number the
/// whole product exists to compute, and the one a user acts on years later.
public struct ReceiptListView: View {
    private let source: ReceiptSource

    @State private var receipts: [Receipt] = []
    @State private var loadError: String?
    @State private var isLoading = true

    public init(source: ReceiptSource = SampleSource()) {
        self.source = source
    }

    private var balance: Decimal { Ledger.unreimbursedBalance(receipts) }

    private var documented: Decimal {
        Ledger.active(receipts).reduce(Money.zero) { $0 + ($1.amount ?? Money.zero) }
    }

    public var body: some View {
        NavigationStack {
            List {
                Section {
                    BalanceHeader(balance: balance, documented: documented)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Color.clear)
                }

                if let loadError {
                    Section {
                        Label(loadError, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                    }
                }

                Section("Receipts") {
                    if isLoading {
                        ProgressView().frame(maxWidth: .infinity)
                    } else if receipts.isEmpty {
                        Text("No receipts yet.").foregroundStyle(.secondary)
                    } else {
                        ForEach(Ledger.active(receipts)) { receipt in
                            NavigationLink {
                                ReceiptDetailView(receipt: receipt)
                            } label: {
                                ReceiptRow(receipt: receipt)
                            }
                        }
                    }
                }
            }
            .navigationTitle("HSAVault")
            .refreshable { await load() }
        }
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            receipts = try await source.load()
            loadError = nil
        } catch {
            // Never blank the screen on a failed read: showing nothing is
            // indistinguishable from an empty vault, which is exactly the bug
            // the Python app shipped and had to fix.
            loadError = "Could not reach Google Sheets. Showing what was last loaded."
        }
    }
}

struct BalanceHeader: View {
    let balance: Decimal
    let documented: Decimal

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Unreimbursed claimable balance")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(MoneyFormat.string(balance))
                .font(.system(size: 40, weight: .semibold, design: .rounded))
                .monospacedDigit()
            Text("\(MoneyFormat.string(documented)) documented in total")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 12)
    }
}

struct ReceiptRow: View {
    let receipt: Receipt

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 3) {
                Text(receipt.provider.isEmpty ? "—" : receipt.provider)
                    .font(.body)
                HStack(spacing: 6) {
                    Text(receipt.serviceDate.map(Self.dateText) ?? "undated")
                    PaymentBadge(method: receipt.paymentMethod)
                    if receipt.pageCount > 1 {
                        Label("\(receipt.pageCount)", systemImage: "doc.on.doc")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text(MoneyFormat.string(receipt.amount ?? Money.zero))
                    .monospacedDigit()
                if receipt.claimable > Money.zero {
                    Text("\(MoneyFormat.string(receipt.claimable)) claimable")
                        .font(.caption)
                        .foregroundStyle(.green)
                }
            }
        }
    }

    static func dateText(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }
}

/// The distinction the whole product turns on, made visible at a glance.
struct PaymentBadge: View {
    let method: PaymentMethod

    var body: some View {
        switch method {
        case .hsaCard:
            Label("HSA card", systemImage: "creditcard")
        case .outOfPocket:
            Label("Out of pocket", systemImage: "banknote")
        }
    }
}
