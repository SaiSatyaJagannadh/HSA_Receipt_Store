import SwiftUI

import HSAVaultCore

/// One receipt, with the payment method spelled out rather than abbreviated.
///
/// "HSA card" and "out of pocket" look interchangeable to anyone who has not
/// read the IRS rules, and confusing them is how the same dollars get claimed
/// twice — so each says what it means for the balance, in full.
public struct ReceiptDetailView: View {
    let receipt: Receipt

    public init(receipt: Receipt) {
        self.receipt = receipt
    }

    public var body: some View {
        List {
            Section {
                LabeledContent("Provider", value: receipt.provider.isEmpty ? "—" : receipt.provider)
                LabeledContent(
                    "Service date",
                    value: receipt.serviceDate.map(ReceiptRow.dateText) ?? "undated")
                LabeledContent(
                    "Amount", value: MoneyFormat.string(receipt.amount ?? Money.zero))
            }

            Section("How it was paid") {
                PaymentBadge(method: receipt.paymentMethod)
                Text(explanation)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Section("Claim") {
                LabeledContent("Still claimable", value: MoneyFormat.string(receipt.claimable))
                if let reimbursed = receipt.reimbursementAmount, reimbursed > Money.zero {
                    LabeledContent(
                        "Already reimbursed", value: MoneyFormat.string(reimbursed))
                }
            }

            if receipt.pageCount > 1 {
                Section("Pages") {
                    Label(
                        "\(receipt.pageCount) images make up this receipt",
                        systemImage: "doc.on.doc")
                    Text(
                        "A receipt photographed in parts stays one record. The total is "
                            + "usually printed on the last page.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle(receipt.provider.isEmpty ? "Receipt" : receipt.provider)
    }

    private var explanation: String {
        switch receipt.paymentMethod {
        case .hsaCard:
            return
                "Already paid from the HSA at the register. This is audit documentation "
                + "and never counts toward your claimable balance."
        case .outOfPocket:
            return
                "Paid with other money, so it adds to your claimable balance and can be "
                + "reimbursed to yourself later — even years from now."
        }
    }
}
