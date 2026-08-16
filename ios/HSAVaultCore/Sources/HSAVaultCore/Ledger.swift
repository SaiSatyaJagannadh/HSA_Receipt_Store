import Foundation

/// The claimable-balance rule, ported from `hsa_vault/core/ledger.py` and
/// `Receipt.claimable` in `core/models.py`.
///
/// This is the one part of an iOS client that cannot be approximated. Getting it
/// wrong does not produce a visibly broken app — it produces a plausible number
/// that lets the same medical dollars be claimed from the HSA twice. The Python
/// suite in `tests/test_ledger.py` is the specification; the tests beside this
/// file mirror it case for case.

// MARK: - Money

/// Two-decimal, half-up money. Never `Double`.
///
/// `Decimal` is used rather than a binary float because 0.1 + 0.2 != 0.3 in
/// binary floating point, and these values are dollars people reclaim years
/// later. Python's `models.money()` quantizes with ROUND_HALF_UP rather than
/// Decimal's banker's-rounding default, because a half-cent on a receipt should
/// round the way a register does; `.plain` is Foundation's equivalent.
public enum Money {
    public static let zero = Decimal(0)

    public static func quantized(_ value: Decimal) -> Decimal {
        var input = value
        var result = Decimal()
        NSDecimalRound(&result, &input, 2, .plain)
        return result
    }
}

// MARK: - Model

public enum PaymentMethod: String, Codable, Sendable {
    /// Paid at the register with the HSA card. Audit documentation only — the
    /// HSA has already paid, so this can never add to the claimable balance.
    case hsaCard = "hsa_card"
    /// Paid with other money, and therefore reclaimable later.
    case outOfPocket = "out_of_pocket"
}

public struct Receipt: Identifiable, Sendable {
    public let id: String
    public var serviceDate: Date?
    public var amount: Decimal?
    public var paymentMethod: PaymentMethod
    public var reimbursed: Bool
    public var reimbursementAmount: Decimal?
    public var deleted: Bool

    public init(
        id: String,
        serviceDate: Date? = nil,
        amount: Decimal? = nil,
        paymentMethod: PaymentMethod = .outOfPocket,
        reimbursed: Bool = false,
        reimbursementAmount: Decimal? = nil,
        deleted: Bool = false
    ) {
        self.id = id
        self.serviceDate = serviceDate
        self.amount = amount
        self.paymentMethod = paymentMethod
        self.reimbursed = reimbursed
        self.reimbursementAmount = reimbursementAmount
        self.deleted = deleted
    }

    /// Dollars still claimable from the HSA for this receipt.
    ///
    /// Zero for deleted, hsa_card, or fully-reimbursed receipts. A partial
    /// reimbursement leaves the remainder claimable.
    public var claimable: Decimal {
        if deleted || paymentMethod != .outOfPocket || reimbursed { return Money.zero }
        guard let amount else { return Money.zero }
        return Money.quantized(amount - (reimbursementAmount ?? Money.zero))
    }
}

// MARK: - Ledger

public enum Ledger {
    public static func active(_ receipts: [Receipt]) -> [Receipt] {
        receipts.filter { !$0.deleted }
    }

    /// The headline number: out-of-pocket dollars not yet withdrawn from the HSA.
    public static func unreimbursedBalance(_ receipts: [Receipt]) -> Decimal {
        Money.quantized(active(receipts).reduce(Money.zero) { $0 + $1.claimable })
    }

    public struct Allocation: Sendable {
        public let receiptID: String
        public let applied: Decimal
        public let fullyCovered: Bool
    }

    /// Spread a withdrawal over receipts, oldest service date first.
    ///
    /// A withdrawal smaller than the selected total leaves the last receipt it
    /// touches partially reimbursed and still claimable for the remainder;
    /// receipts after that get nothing.
    public static func allocateReimbursement(
        _ receipts: [Receipt], withdrawal: Decimal
    ) -> [Allocation] {
        var remaining = Money.quantized(withdrawal)
        // Ties broken by id so the order is deterministic, matching the Python
        // sort key of (service_date or date.max, receipt_id). An undated receipt
        // sorts last rather than first.
        let ordered = receipts.sorted {
            let left = $0.serviceDate ?? .distantFuture
            let right = $1.serviceDate ?? .distantFuture
            return left == right ? $0.id < $1.id : left < right
        }

        var allocations: [Allocation] = []
        for receipt in ordered {
            if remaining <= Money.zero { break }
            let claim = receipt.claimable
            if claim <= Money.zero { continue }
            let applied = min(claim, remaining)
            remaining -= applied
            allocations.append(
                Allocation(
                    receiptID: receipt.id,
                    applied: Money.quantized(applied),
                    fullyCovered: applied == claim
                )
            )
        }
        return allocations
    }
}
