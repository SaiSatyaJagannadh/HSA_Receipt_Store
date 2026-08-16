import Foundation

import HSAVaultCore

/// Mirrors `hsa_vault/tests/test_ledger.py` case for case.
///
/// A Swift client that disagrees with the Python app about the claimable balance
/// is worse than no client at all: both would show confident, different numbers
/// for the same Sheet, and the difference is money claimed from the HSA twice.
///
/// Run with `swift run HSAVaultCoreChecks`. Exits non-zero on any failure.

var failures: [String] = []

func check(_ condition: Bool, _ message: String) {
    if !condition { failures.append(message) }
}

func check(_ actual: Decimal, _ expected: Decimal, _ message: String) {
    if actual != expected { failures.append("\(message) — expected \(expected), got \(actual)") }
}

func dec(_ value: String) -> Decimal { Decimal(string: value)! }

func day(_ year: Int, _ month: Int, _ dayOfMonth: Int) -> Date {
    var components = DateComponents()
    components.year = year
    components.month = month
    components.day = dayOfMonth
    return Calendar(identifier: .gregorian).date(from: components)!
}

// MARK: - The rule that protects against double-claiming

check(
    Receipt(id: "a", amount: dec("42.18"), paymentMethod: .hsaCard).claimable, 0,
    "an HSA-card receipt was counted as claimable")

check(
    Receipt(id: "a", amount: dec("42.18"), paymentMethod: .outOfPocket).claimable, dec("42.18"),
    "an out-of-pocket receipt was not claimable")

check(
    Receipt(id: "a", amount: dec("42.18"), reimbursed: true, reimbursementAmount: dec("42.18"))
        .claimable, 0,
    "a fully reimbursed receipt was still claimable")

check(
    Receipt(id: "a", amount: dec("100.00"), reimbursementAmount: dec("40.00")).claimable,
    dec("60.00"),
    "a partial reimbursement lost its remainder")

check(
    Receipt(id: "a", amount: dec("10.00"), deleted: true).claimable, 0,
    "a deleted receipt was claimable")

check(
    Receipt(id: "a", amount: nil).claimable, 0,
    "a receipt with no amount was claimable")

check(
    Ledger.unreimbursedBalance([
        Receipt(id: "a", amount: dec("50.00"), paymentMethod: .outOfPocket),
        Receipt(id: "b", amount: dec("80.06"), paymentMethod: .hsaCard),
        Receipt(id: "c", amount: dec("10.00"), deleted: true),
    ]), dec("50.00"),
    "HSA-card or deleted receipts leaked into the claimable balance")

// MARK: - Money

// ROUND_HALF_UP, not banker's rounding: 0.125 -> 0.13, not 0.12.
check(Money.quantized(dec("0.125")), dec("0.13"), "half-cent rounded down (banker's rounding?)")
check(Money.quantized(dec("2.345")), dec("2.35"), "half-cent rounded down (banker's rounding?)")

check(
    (0..<10).reduce(Money.zero) { total, _ in total + dec("0.10") }, dec("1.00"),
    "money drifted over ten additions — is this a Double?")

// MARK: - Allocation

let ordered = Ledger.allocateReimbursement(
    [
        Receipt(id: "new", serviceDate: day(2026, 6, 1), amount: dec("60.00")),
        Receipt(id: "old", serviceDate: day(2026, 1, 1), amount: dec("40.00")),
    ], withdrawal: dec("100.00"))
check(ordered.map(\.receiptID) == ["old", "new"], "a withdrawal was not applied oldest-first")
check(ordered.allSatisfy(\.fullyCovered), "a fully covered receipt was marked partial")

let short = Ledger.allocateReimbursement(
    [
        Receipt(id: "a", serviceDate: day(2026, 1, 1), amount: dec("40.00")),
        Receipt(id: "b", serviceDate: day(2026, 2, 1), amount: dec("60.00")),
    ], withdrawal: dec("70.00"))
check(short.count == 2, "a short withdrawal did not reach the second receipt")
check(short[0].applied, dec("40.00"), "the oldest receipt got the wrong share")
check(short[1].applied, dec("30.00"), "the remainder was misallocated")
check(
    short[1].fullyCovered == false,
    "a partly covered receipt was marked fully reimbursed — the remainder is lost")

check(
    Ledger.allocateReimbursement(
        [
            Receipt(
                id: "card", serviceDate: day(2026, 1, 1), amount: dec("40.00"),
                paymentMethod: .hsaCard)
        ], withdrawal: dec("40.00")
    ).isEmpty,
    "a withdrawal was applied to a receipt the HSA already paid — double-claimed")

check(
    Ledger.allocateReimbursement(
        [
            Receipt(id: "undated", serviceDate: nil, amount: dec("10.00")),
            Receipt(id: "dated", serviceDate: day(2026, 1, 1), amount: dec("10.00")),
        ], withdrawal: dec("10.00")
    ).map(\.receiptID) == ["dated"],
    "an undated receipt sorted before a dated one")

// MARK: - Report

if failures.isEmpty {
    print("HSAVaultCore: all balance-rule checks passed")
} else {
    for failure in failures { print("FAIL: \(failure)") }
    print("\(failures.count) check(s) failed")
    exit(1)
}
