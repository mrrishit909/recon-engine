"""Edge cases of the pure matcher: each test builds a handful of records and states what must come out."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from recon.matching import Config, convert, reconcile
from recon.models import Txn

T0 = datetime(2026, 8, 10, tzinfo=UTC)
AS_OF = T0 + timedelta(days=30)
_n = 0


def txn(kind: str, amount: int, day: float = 0, currency: str = "USD", **kw: object) -> Txn:
    global _n
    _n += 1
    source = "plaid" if kind == "deposit" else "stripe"
    return Txn(id=_n, source=source, external_id=kw.pop("external_id", f"x_{_n:06d}"), kind=kind, amount_minor=amount,  # type: ignore[arg-type]
               currency=currency, occurred_at=T0 + timedelta(days=day), **kw)  # type: ignore[arg-type]


def kinds(result: object) -> list[str]:
    return sorted(a.kind for a in result.anomalies)  # type: ignore[attr-defined]


def test_reference_match_beats_amount_and_ignores_the_window() -> None:
    payout = txn("payout", 50_000, reference="ST-1234567890")
    decoy = txn("deposit", 50_000, day=1, reference="ACH CREDIT")
    real = txn("deposit", 50_000, day=9, reference="stripe transfer st-1234567890")   # late, but it names the payout
    r = reconcile([payout], [decoy, real], [], AS_OF)
    assert [(m.ledger_id, m.bank_id, m.pass_name, m.confidence) for m in r.matches] == [(payout.id, real.id, "reference", 1.0)]
    assert kinds(r) == ["unmatched_deposit"]


def test_reference_by_payout_id() -> None:
    payout = txn("payout", 700, external_id="po_ABC12345")
    dep = txn("deposit", 700, reference="TRANSFER po_ABC12345 THANK YOU")
    assert reconcile([payout], [dep], [], AS_OF).matches[0].pass_name == "reference"


def test_reference_with_wrong_amount_goes_to_review_not_to_a_later_pass() -> None:
    payout = txn("payout", 50_000, reference="ST-1234567890")
    short = txn("deposit", 40_000, day=1, reference="STRIPE ST-1234567890")
    other = txn("deposit", 50_000, day=1)                 # would pair by amount if the payout were still in the pool
    r = reconcile([payout], [short, other], [], AS_OF)
    assert r.matches == []
    mismatch = next(a for a in r.anomalies if a.kind == "amount_mismatch")
    assert mismatch.txn_id == short.id and mismatch.detail == {"ledger_txn": payout.id, "expected_minor": 50_000, "actual_minor": 40_000}


def test_exact_amount_inside_three_day_window_only() -> None:
    p1, p2 = txn("payout", 10_000), txn("payout", 20_000)
    on_time = txn("deposit", 10_000, day=3)               # exactly 3 days: inside
    late = txn("deposit", 20_000, day=3.01)               # a few minutes over: outside
    r = reconcile([p1, p2], [on_time, late], [], AS_OF)
    assert [(m.ledger_id, m.bank_id, m.pass_name, m.confidence) for m in r.matches] == [(p1.id, on_time.id, "exact", 0.95)]
    assert kinds(r) == ["unmatched_deposit", "unmatched_payout"]


def test_ambiguous_equal_amounts_pair_by_nearest_date_with_lower_confidence() -> None:
    p1, p2 = txn("payout", 10_000, day=0), txn("payout", 10_000, day=2)
    d1, d2 = txn("deposit", 10_000, day=2.5), txn("deposit", 10_000, day=0.5)
    r = reconcile([p1, p2], [d1, d2], [], AS_OF)
    assert {(m.ledger_id, m.bank_id) for m in r.matches} == {(p1.id, d2.id), (p2.id, d1.id)}
    assert {m.confidence for m in r.matches} == {0.8}


def test_result_does_not_depend_on_input_order() -> None:
    ps = [txn("payout", 10_000, day=d) for d in (0, 1, 2)]
    ds = [txn("deposit", 10_000, day=d + 0.4) for d in (0, 1, 2)]
    a = reconcile(ps, ds, [], AS_OF).matches
    b = reconcile(ps[::-1], ds[::-1], [], AS_OF).matches
    assert sorted(a, key=lambda m: m.ledger_id) == sorted(b, key=lambda m: m.ledger_id)


def test_micro_tolerance_matches_and_records_the_difference() -> None:
    payout = txn("payout", 10_000)
    dep = txn("deposit", 10_002, day=1)                   # two cents over: allowed (2 minor units)
    r = reconcile([payout], [dep], [], AS_OF)
    assert [(m.pass_name, m.diff_minor, m.confidence) for m in r.matches] == [("tolerance", 2, 0.7)]
    far = txn("deposit", 10_003, day=1)                   # three cents over: not allowed
    assert reconcile([payout], [far], [], AS_OF).matches == []


def test_tolerance_scales_with_large_amounts() -> None:
    payout = txn("payout", 100_000_000)                   # 1,000,000.00: 1 bp = 100.00
    assert reconcile([payout], [txn("deposit", 100_009_000, day=1)], [], AS_OF).matches[0].pass_name == "tolerance"
    assert reconcile([payout], [txn("deposit", 100_011_000, day=1)], [], AS_OF).matches == []


def test_exact_pass_wins_over_tolerance_candidate() -> None:
    payout = txn("payout", 10_000)
    near, exact = txn("deposit", 10_001, day=0.1), txn("deposit", 10_000, day=2)
    r = reconcile([payout], [near, exact], [], AS_OF)
    assert [(m.bank_id, m.pass_name) for m in r.matches] == [(exact.id, "exact")]


def test_fee_subtraction_payout_equals_gross_minus_fees() -> None:
    payout = txn("payout", 9_680 + 4_828 - 1_900, external_id="po_FEES0001")
    parts = [txn("charge", 10_000, fee_minor=320, payout_id="po_FEES0001"),
             txn("charge", 5_000, fee_minor=172, payout_id="po_FEES0001"),
             txn("refund", -1_900, payout_id="po_FEES0001")]
    r = reconcile([payout], [txn("deposit", payout.amount_minor, day=1)], parts, AS_OF)
    assert len(r.matches) == 1 and r.anomalies == []


def test_payout_that_does_not_add_up_is_flagged_with_the_difference() -> None:
    payout = txn("payout", 9_000, external_id="po_SHORT001")
    parts = [txn("charge", 10_000, fee_minor=320, payout_id="po_SHORT001")]
    r = reconcile([payout], [], parts, T0)
    assert [(a.kind, a.detail) for a in r.anomalies] == [
        ("payout_composition_mismatch", {"expected_minor": 9_680, "actual_minor": 9_000, "diff_minor": -680})]


def test_chargeback_reduces_the_payout_and_is_listed_as_exposure() -> None:
    payout = txn("payout", 9_680 - 5_000 - 1_500, external_id="po_DISPUTE1")
    parts = [txn("charge", 10_000, fee_minor=320, payout_id="po_DISPUTE1"),
             txn("chargeback", -5_000, fee_minor=1_500, payout_id="po_DISPUTE1")]     # amount back + dispute fee
    r = reconcile([payout], [txn("deposit", 3_180, day=2)], parts, AS_OF)
    assert len(r.matches) == 1
    assert [(a.kind, a.detail) for a in r.anomalies] == [
        ("chargeback", {"amount_minor": 5_000, "fee_minor": 1_500, "currency": "USD"})]


def test_refund_larger_than_sales_gives_negative_composition() -> None:
    payout = txn("payout", -900, external_id="po_NEGATIVE")
    parts = [txn("charge", 1_000, fee_minor=59, payout_id="po_NEGATIVE"), txn("refund", -1_841, payout_id="po_NEGATIVE")]
    assert reconcile([payout], [], parts, T0).anomalies == []


def test_double_billing_same_customer_amount_within_ten_minutes() -> None:
    first = txn("charge", 4_900, customer="a@example.com")
    again = txn("charge", 4_900, day=5 / 1440, customer="a@example.com")
    later = txn("charge", 4_900, day=1, customer="a@example.com")           # next day: a normal repeat purchase
    other = txn("charge", 4_900, day=1 / 1440, customer="b@example.com")
    anonymous = [txn("charge", 100), txn("charge", 100)]                    # no customer: cannot tell
    r = reconcile([], [], [first, again, later, other, *anonymous], T0)
    assert [(a.kind, a.txn_id, a.detail) for a in r.anomalies] == [("double_billing", again.id, {"duplicate_of": first.id})]


def test_multi_currency_deposit_matches_at_the_known_rate() -> None:
    cfg = Config(fx_rates={("EUR", "USD"): Decimal("1.17")})
    payout = txn("payout", 100_000, currency="EUR", reference="ST-5550001111")
    by_ref = txn("deposit", 116_800, day=1, reference="STRIPE ST-5550001111")       # 0.17% under 1,170.00
    r = reconcile([payout], [by_ref], [], AS_OF, cfg)
    assert [(m.pass_name, m.diff_minor, m.confidence) for m in r.matches] == [("fx", -200, 0.9)]
    no_ref = txn("deposit", 117_500, day=2)
    r = reconcile([payout], [no_ref], [], AS_OF, cfg)
    assert [(m.pass_name, m.diff_minor, m.confidence) for m in r.matches] == [("fx", 500, 0.6)]


def test_currency_conversion_mismatch_goes_to_review() -> None:
    cfg = Config(fx_rates={("EUR", "USD"): Decimal("1.17")})
    payout = txn("payout", 100_000, currency="EUR", reference="ST-5550002222")
    off_rate = txn("deposit", 110_000, day=1, reference="ST-5550002222")            # 6% under the rate
    r = reconcile([payout], [off_rate], [], AS_OF, cfg)
    assert r.matches == [] and r.anomalies[0].kind == "currency_mismatch"
    assert r.anomalies[0].detail["expected_minor"] == 117_000
    unknown = txn("deposit", 85_000, day=1, currency="GBP", reference="ST-5550002222")   # no EUR->GBP rate on file
    r = reconcile([payout], [unknown], [], AS_OF, cfg)
    assert r.matches == [] and r.anomalies[0].detail["expected_minor"] is None
    assert reconcile([payout], [txn("deposit", 100_000, day=1, currency="GBP")], [], T0, cfg).matches == []


def test_zero_decimal_currency_conversion() -> None:
    cfg = Config(fx_rates={("JPY", "USD"): Decimal("0.0067"), ("USD", "JPY"): Decimal("149.5")})
    assert convert(100_000, "JPY", "USD", cfg) == 67_000          # 100,000 yen -> 670.00 dollars
    assert convert(10_000, "USD", "JPY", cfg) == 14_950           # 100.00 dollars -> 14,950 yen
    assert convert(1, "USD", "CHF", cfg) is None


def test_payout_in_one_currency_with_components_in_another() -> None:
    payout = txn("payout", 9_680, external_id="po_MIXED001")
    parts = [txn("charge", 10_000, fee_minor=320, currency="EUR", payout_id="po_MIXED001")]
    r = reconcile([payout], [], parts, T0)
    assert [(a.kind, a.detail["component_currencies"]) for a in r.anomalies] == [("currency_mismatch", ["EUR"])]


def test_recent_unmatched_records_are_pending_not_exceptions() -> None:
    r = reconcile([txn("payout", 5_000, day=0)], [txn("deposit", 7_000, day=0)], [], T0 + timedelta(days=2))
    assert r.matches == [] and r.anomalies == []
    r = reconcile([txn("payout", 5_000, day=0)], [txn("deposit", 7_000, day=0)], [], T0 + timedelta(days=4))
    assert kinds(r) == ["unmatched_deposit", "unmatched_payout"]


def test_one_deposit_cannot_be_matched_twice() -> None:
    p1, p2 = txn("payout", 10_000, day=0), txn("payout", 10_000, day=1)
    dep = txn("deposit", 10_000, day=1)
    r = reconcile([p1, p2], [dep], [], AS_OF)
    assert [(m.ledger_id, m.bank_id) for m in r.matches] == [(p2.id, dep.id)]     # the closer one in time
    assert kinds(r) == ["unmatched_payout"]


def test_bulk_run_is_fast() -> None:
    import time
    payouts = [txn("payout", 100_000 + 7 * i, day=i % 25) for i in range(20_000)]
    deposits = [txn("deposit", p.amount_minor + (i % 3 == 0), day=(i % 25) + 1) for i, p in enumerate(payouts)]
    start = time.perf_counter()
    r = reconcile(payouts, deposits, [], AS_OF)
    assert len(r.matches) == 20_000 and time.perf_counter() - start < 10
