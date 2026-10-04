from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from recon.files import FileRejected, parse

HEADER = "external_id,kind,amount,fee,currency,occurred_at,reference,payout_id,customer\n"


def test_valid_csv_rows_become_minor_units() -> None:
    csv = HEADER + "a1,charge,12.34,0.66,USD,2026-08-01T10:00:00Z,,po_1,ann@example.com\n" \
                   "a2,refund,-5.00,0,USD,2026-08-02,,po_1,\n" \
                   "a3,deposit,5000,0,JPY,2026-08-03,WIRE,,\n"
    a, b, c = parse("bank.csv", csv.encode())
    assert (a.amount_minor, a.fee_minor, a.net_minor, a.customer) == (1234, 66, 1168, "ann@example.com")
    assert (b.amount_minor, b.reference, b.occurred_at.tzinfo is not None) == (-500, None, True)
    assert c.amount_minor == 5000                                    # yen has no minor unit


@pytest.mark.parametrize("line, column", [
    ("a1,charge,12.345,0,USD,2026-08-01,,,\n", None),                # three decimals in a two-decimal currency
    ("a1,charge,abc,0,USD,2026-08-01,,,\n", "amount"),
    ("a1,charge,NaN,0,USD,2026-08-01,,,\n", "amount"),
    ("a1,transfer,1,0,USD,2026-08-01,,,\n", "kind"),
    ("a1,charge,1,0,usd,2026-08-01,,,\n", "currency"),
    ("a1,charge,1,0,USD,yesterday,,,\n", "occurred_at"),
    (",charge,1,0,USD,2026-08-01,,,\n", "external_id"),
])
def test_bad_rows_reject_the_whole_file_and_name_the_row(line: str, column: str | None) -> None:
    good = "ok,charge,1.00,0,USD,2026-08-01,,,\n"
    with pytest.raises(FileRejected) as exc:
        parse("f.csv", (HEADER + good + line).encode())
    assert [(e["row"], e.get("column")) for e in exc.value.errors] == [(3, column)]


def test_unknown_column_empty_file_and_wrong_type_are_rejected() -> None:
    with pytest.raises(FileRejected):
        parse("f.csv", b"external_id,kind,amount,currency,occurred_at,surprise\na,charge,1,USD,2026-08-01,x\n")
    with pytest.raises(FileRejected, match="1 invalid"):
        parse("f.csv", HEADER.encode())
    with pytest.raises(FileRejected):
        parse("f.pdf", b"%PDF")


def test_xlsx_float_noise_is_dropped_but_real_extra_decimals_are_refused() -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["external_id", "kind", "amount", "currency", "occurred_at", "reference"])
    ws.append(["x1", "deposit", 0.1 + 0.2, "USD", "2026-08-05", None])          # 0.30000000000000004 in binary
    ws.append(["x2", "deposit", 19.99, "USD", "2026-08-06", "WIRE 77"])
    ws.append([None, None, None, None, None, None])                             # blank rows are skipped
    buf = io.BytesIO()
    wb.save(buf)
    a, b = parse("f.xlsx", buf.getvalue())
    assert (a.amount_minor, b.amount_minor, b.reference) == (30, 1999, "WIRE 77")
    ws.append(["x3", "deposit", 12.345, "USD", "2026-08-06", None])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(FileRejected) as exc:
        parse("f.xlsx", buf.getvalue())
    assert exc.value.errors[0]["row"] == 5
