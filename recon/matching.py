"""Multi-pass matcher. Pure functions: records in, matches and anomalies out. No database, no clock.

Passes run from most to least certain, and a record leaves the pool as soon as it is matched or sent to review:
  1. reference  the bank memo carries the payout id / trace id / payment reference
  2. exact      same currency, same amount, inside the clearing window
  3. tolerance  same currency, amount within a micro-tolerance, inside the window
  4. fx         different currency, amount agrees with the known rate within the FX tolerance
Then the checks that do not pair anything: payout composition (gross - fees = net), double billing, chargebacks,
and records that stayed unmatched after the clearing window closed.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from .models import Anomaly, Match, Result, Txn, exponent

Pool = dict[int, Txn]


@dataclass(frozen=True)
class Config:
    window_days: int = 3                 # strict clearing window between a payout and its bank deposit
    tolerance_minor: int = 2             # micro-tolerance, in minor units (2 = two cents)
    tolerance_bps: int = 1               # ... or this many basis points of the amount, whichever is larger
    fx_tolerance_bps: int = 100          # 1%: how far a converted deposit may sit from the quoted rate
    duplicate_window: timedelta = timedelta(minutes=10)
    fx_rates: Mapping[tuple[str, str], Decimal] = field(default_factory=dict)  # (from, to) -> rate in major units
    # ponytail: one rate per currency pair; key by date as well once deposits span days with moving rates


def reconcile(ledger: Sequence[Txn], bank: Sequence[Txn], components: Sequence[Txn], as_of: datetime,
              cfg: Config = Config()) -> Result:
    result = Result()
    open_l: Pool = {t.id: t for t in ledger}
    open_b: Pool = {t.id: t for t in bank}
    _by_reference(open_l, open_b, cfg, result)
    _by_amount(open_l, open_b, cfg, result, "exact")
    _by_amount(open_l, open_b, cfg, result, "tolerance")
    _by_amount(open_l, open_b, cfg, result, "fx")
    _composition(ledger, components, result)
    _double_billing(components, cfg, result)
    result.anomalies += [Anomaly(c.id, "chargeback", {"amount_minor": -c.amount_minor, "fee_minor": c.fee_minor,
                                                      "currency": c.currency})
                         for c in components if c.kind == "chargeback"]
    cutoff = as_of - timedelta(days=cfg.window_days)
    result.anomalies += [Anomaly(t.id, "unmatched_payout") for t in open_l.values() if t.occurred_at < cutoff]
    result.anomalies += [Anomaly(t.id, "unmatched_deposit") for t in open_b.values() if t.occurred_at < cutoff]
    return result


def _tolerance(amount: int, cfg: Config) -> int:
    return max(cfg.tolerance_minor, abs(amount) * cfg.tolerance_bps // 10_000)


def convert(amount_minor: int, src: str, dst: str, cfg: Config) -> int | None:
    """Expected amount in `dst` minor units, or None when no rate is known."""
    rate = cfg.fx_rates.get((src, dst))
    if rate is None:
        return None
    major = Decimal(amount_minor).scaleb(-exponent(src)) * rate
    return int(major.scaleb(exponent(dst)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z0-9_\-]+", (text or "").upper()) if len(t) >= 6}


def _by_reference(open_l: Pool, open_b: Pool, cfg: Config, result: Result) -> None:
    keys: dict[str, int] = {}
    for t in open_l.values():
        for token in _tokens(t.external_id) | _tokens(t.reference):
            keys[token] = t.id
    for b in list(open_b.values()):
        hit = next((keys[token] for token in sorted(_tokens(b.reference)) if token in keys), None)
        if hit is None or hit not in open_l:
            continue
        left = open_l.pop(hit)
        del open_b[b.id]                 # either matched or sent to review: later passes must not re-pair them
        if left.currency == b.currency:
            diff = b.amount_minor - left.amount_minor
            if abs(diff) <= _tolerance(left.amount_minor, cfg):
                result.matches.append(Match(left.id, b.id, "reference", 1.0, diff))
            else:
                result.anomalies.append(Anomaly(b.id, "amount_mismatch", {
                    "ledger_txn": left.id, "expected_minor": left.amount_minor, "actual_minor": b.amount_minor}))
            continue
        expected = convert(left.amount_minor, left.currency, b.currency, cfg)
        if expected is not None and abs(b.amount_minor - expected) * 10_000 <= abs(expected) * cfg.fx_tolerance_bps:
            result.matches.append(Match(left.id, b.id, "fx", 0.9, b.amount_minor - expected))
        else:
            result.anomalies.append(Anomaly(b.id, "currency_mismatch", {
                "ledger_txn": left.id, "ledger_currency": left.currency, "bank_currency": b.currency,
                "expected_minor": expected, "actual_minor": b.amount_minor}))


def _by_amount(open_l: Pool, open_b: Pool, cfg: Config, result: Result, mode: str) -> None:
    """Pair by amount inside the clearing window. All candidate pairs are ranked (closest amount, then closest
    date, then ids) and taken greedily, so the outcome does not depend on input order."""
    window = timedelta(days=cfg.window_days)
    by_currency: dict[str, list[tuple[int, int]]] = defaultdict(list)   # currency -> sorted (amount, bank id)
    for b in open_b.values():
        by_currency[b.currency].append((b.amount_minor, b.id))
    for rows in by_currency.values():
        rows.sort()

    expected_in: Callable[[Txn, str], int | None]
    if mode == "fx":
        expected_in = lambda t, cur: convert(t.amount_minor, t.currency, cur, cfg) if cur != t.currency else None  # noqa: E731
    else:
        expected_in = lambda t, cur: t.amount_minor if cur == t.currency else None  # noqa: E731

    pairs: list[tuple[int, timedelta, int, int, int]] = []
    options: dict[int, int] = defaultdict(int)       # how many candidates each record had (drives confidence)
    for left in open_l.values():
        for currency, rows in by_currency.items():
            expected = expected_in(left, currency)
            if expected is None:
                continue
            slack = {"exact": 0, "tolerance": _tolerance(expected, cfg),
                     "fx": abs(expected) * cfg.fx_tolerance_bps // 10_000}[mode]
            lo, hi = bisect_left(rows, (expected - slack, -1)), bisect_right(rows, (expected + slack, 2**62))
            for amount, bank_id in rows[lo:hi]:
                gap = abs(open_b[bank_id].occurred_at - left.occurred_at)
                if gap <= window:
                    pairs.append((abs(amount - expected), gap, left.id, bank_id, amount - expected))
                    options[left.id] += 1
                    options[-bank_id] += 1
    for _, _, left_id, bank_id, diff in sorted(pairs):
        if left_id in open_l and bank_id in open_b:
            unique = options[left_id] == 1 and options[-bank_id] == 1
            confidence = {"exact": (0.95, 0.8), "tolerance": (0.7, 0.5), "fx": (0.6, 0.4)}[mode][0 if unique else 1]
            result.matches.append(Match(left_id, bank_id, mode, confidence, diff))
            del open_l[left_id], open_b[bank_id]


def _composition(ledger: Sequence[Txn], components: Sequence[Txn], result: Result) -> None:
    """Fee subtraction: a payout must equal the sum of (gross - fee) over the charges, refunds and chargebacks in it."""
    parts: dict[str, list[Txn]] = defaultdict(list)
    for c in components:
        if c.payout_id:
            parts[c.payout_id].append(c)
    for p in ledger:
        if p.kind != "payout" or p.external_id not in parts:
            continue
        foreign = sorted({c.currency for c in parts[p.external_id]} - {p.currency})
        if foreign:
            result.anomalies.append(Anomaly(p.id, "currency_mismatch", {"payout_currency": p.currency,
                                                                         "component_currencies": foreign}))
            continue
        expected = sum(c.net_minor for c in parts[p.external_id])
        if expected != p.amount_minor:
            result.anomalies.append(Anomaly(p.id, "payout_composition_mismatch", {
                "expected_minor": expected, "actual_minor": p.amount_minor, "diff_minor": p.amount_minor - expected}))


def _double_billing(components: Sequence[Txn], cfg: Config, result: Result) -> None:
    charges = sorted((c for c in components if c.kind == "charge" and c.customer),
                     key=lambda c: (c.customer or "", c.currency, c.amount_minor, c.occurred_at, c.id))
    for prev, cur in zip(charges, charges[1:]):
        same = (prev.customer, prev.currency, prev.amount_minor) == (cur.customer, cur.currency, cur.amount_minor)
        if same and cur.occurred_at - prev.occurred_at <= cfg.duplicate_window:
            result.anomalies.append(Anomaly(cur.id, "double_billing", {"duplicate_of": prev.id}))
