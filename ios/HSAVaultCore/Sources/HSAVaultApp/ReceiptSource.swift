import Foundation

import HSAVaultCore

/// Where receipts come from.
///
/// The screens are written against this, not against Google. Sheets is the
/// source of truth, so the real implementation is a REST client — but keeping it
/// behind a protocol means the UI is complete and navigable before any auth
/// exists, and the network layer can be dropped in without touching a view.
public protocol ReceiptSource: Sendable {
    func load() async throws -> [Receipt]
}

/// Enough real-shaped data to exercise every branch of the UI: an out-of-pocket
/// receipt, an HSA-card one that must never appear as claimable, a partially
/// reimbursed one, and a multi-page receipt.
public struct SampleSource: ReceiptSource {
    public init() {}

    public func load() async throws -> [Receipt] {
        func day(_ y: Int, _ m: Int, _ d: Int) -> Date {
            var c = DateComponents()
            c.year = y
            c.month = m
            c.day = d
            return Calendar(identifier: .gregorian).date(from: c)!
        }
        return [
            Receipt(
                id: "1", provider: "Pledge Financial MD LLC", serviceDate: day(2026, 8, 16),
                amount: Decimal(string: "294.94"), paymentMethod: .hsaCard, pageCount: 3),
            Receipt(
                id: "2", provider: "CVS Pharmacy", serviceDate: day(2026, 3, 14),
                amount: Decimal(string: "42.18"), paymentMethod: .outOfPocket),
            Receipt(
                id: "3", provider: "Dr. Ruiz", serviceDate: day(2026, 2, 2),
                amount: Decimal(string: "310.00"), paymentMethod: .outOfPocket,
                reimbursementAmount: Decimal(string: "50.00")),
            Receipt(
                id: "4", provider: "eyebuydirect.com", serviceDate: day(2026, 6, 23),
                amount: Decimal(string: "27.40"), paymentMethod: .outOfPocket),
        ]
    }
}

/// Formats money the way the receipt does — two decimals, grouped, no guessing.
public enum MoneyFormat {
    public static func string(_ value: Decimal) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = "USD"
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        return formatter.string(from: value as NSDecimalNumber) ?? "$0.00"
    }
}
