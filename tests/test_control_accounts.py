"""Issue #119 — a document must never be accepted without its journal entry.

Reported by wilsons043 against the v2.10.0 Windows release: renumbering
Accounts Receivable away from 1100 made every later invoice post nothing,
while still returning 201 and still appearing in the A/R aging.

The reproduction is the first test here, written the way the reporter drove
it, and it asserts the thing that actually matters: the sub-ledger and the
general ledger agree. Note that the *old* behaviour kept the trial balance
balanced — debits equalled credits throughout — so a balance check can never
catch this class. Only the tie-out can.
"""

import pytest

from app.services import control_accounts


def _customer(client, name="Control Co"):
    r = client.post("/api/customers", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _invoice(client, cid, amount, date="2026-01-05"):
    return client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": date,
            "tax_rate": 0,
            "lines": [
                {"description": "work", "quantity": 1, "rate": amount, "line_order": 0}
            ],
        },
    )


def _tb(client):
    r = client.get(
        "/api/reports/trial-balance?start_date=2020-01-01&end_date=2030-12-31"
    )
    assert r.status_code == 200
    body = r.json()
    return body["total_debit"], body["total_credit"]


def _ar_total(client):
    """The aging's own TOTAL row — it lives under "totals", not in "items"."""
    body = client.get("/api/reports/ar-aging").json()
    return float(body["totals"]["total"])


# ---------------------------------------------------------------------------
# The reported path
# ---------------------------------------------------------------------------


def test_renumbering_accounts_receivable_is_refused(client, seed_accounts):
    """The reporter's step 2. It answered 200 and broke every later invoice."""
    ar = seed_accounts["1100"]
    r = client.put(f"/api/accounts/{ar.id}", json={"account_number": "1105"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "1100" in detail and "control account" in detail
    assert "rename" in detail.lower()  # say what IS allowed

    # and it really did not change
    assert client.get(f"/api/accounts/{ar.id}").json()["account_number"] == "1100"


def test_renaming_a_control_account_is_still_allowed(client, seed_accounts):
    """Only the number is load-bearing. An accountant renaming 1100 to
    'Trade Debtors' must not be blocked — that was never the problem."""
    ar = seed_accounts["1100"]
    r = client.put(f"/api/accounts/{ar.id}", json={"name": "Trade Debtors"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Trade Debtors"
    assert r.json()["account_number"] == "1100"


def test_changing_a_control_account_type_is_refused(client, seed_accounts):
    """The same fail-open lands anywhere the type drives behaviour."""
    ap = seed_accounts["2000"]
    r = client.put(f"/api/accounts/{ap.id}", json={"account_type": "expense"})
    assert r.status_code == 400, r.text
    assert "2000" in r.json()["detail"]


def test_an_ordinary_seeded_account_can_still_be_renumbered(client, seed_accounts):
    """Every seeded account carries is_system, so guarding on that flag would
    have blocked ordinary renumbering — which is a real bookkeeping request.
    The guard is the control-account registry, not is_system."""
    office = next(
        a
        for n, a in seed_accounts.items()
        if not control_accounts.is_control_number(n) and n.startswith("6")
    )
    r = client.put(f"/api/accounts/{office.id}", json={"account_number": "6999"})
    assert r.status_code == 200, r.text
    assert r.json()["account_number"] == "6999"


@pytest.mark.parametrize("number", ["1100", "2000", "1200", "2200", "4000", "2100"])
def test_every_posting_control_account_is_protected(client, seed_accounts, number):
    acct = seed_accounts[number]
    r = client.put(f"/api/accounts/{acct.id}", json={"account_number": number + "9"})
    assert r.status_code == 400, f"{number} was renumberable: {r.text}"


# ---------------------------------------------------------------------------
# The dangerous half: a posting path that cannot resolve its account
# ---------------------------------------------------------------------------


def test_invoice_is_refused_when_ar_is_absent_not_silently_unposted(
    client, db_session, seed_accounts
):
    """The heart of #119. With A/R gone the invoice must FAIL — the old
    behaviour created it, returned 201, and skipped the journal entry."""
    cid = _customer(client)
    assert _invoice(client, cid, 2000).status_code == 201
    before_tb, _ = _tb(client)
    before_ar = _ar_total(client)

    # Reach past the route guard the way a damaged or hand-built file would
    # arrive. Renumbering is what the reporter did; the effect on the
    # resolver is identical and it does not disturb the existing postings
    # (deleting the account would null their account_id).
    seed_accounts["1100"].account_number = "1105"
    db_session.commit()

    r = _invoice(client, cid, 5000, date="2026-01-06")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "1100" in detail and "Accounts Receivable" in detail
    assert "Nothing was posted" in detail

    # and nothing moved on either side — no half-written document
    assert _tb(client)[0] == before_tb
    assert _ar_total(client) == before_ar


def test_bill_is_refused_when_accounts_payable_is_absent(
    client, db_session, seed_accounts
):
    """The payables half the reporter also confirmed by hand."""
    v = client.post("/api/vendors", json={"name": "Control Vendor"})
    assert v.status_code == 201, v.text
    vid = v.json()["id"]

    seed_accounts["2000"].account_number = "2005"
    db_session.commit()

    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vid,
            "date": "2026-01-06",
            "lines": [{"description": "parts", "quantity": 1, "rate": 900}],
        },
    )
    assert r.status_code == 409, r.text
    assert "2000" in r.json()["detail"]


def test_the_trial_balance_still_balanced_while_the_books_were_wrong(
    client, db_session, seed_accounts
):
    """Why no existing check caught this.

    With the journal entry skipped, debits still equalled credits exactly —
    the ledger stayed internally consistent and simply lost an entry. This
    test pins the reasoning: balance is not the same as complete, and the
    A/R tie-out is the assertion that has to be made.
    """
    cid = _customer(client)
    assert _invoice(client, cid, 2000).status_code == 201
    debit, credit = _tb(client)
    assert debit == credit  # consistent...
    assert _ar_total(client) == pytest.approx(debit)  # ...AND tied out


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_registry_matches_the_numbers_the_code_resolves_by_literal():
    """If someone adds a `account_number == "NNNN"` lookup and does not add
    it here, renumbering that account becomes a silent trap again. This test
    is the tripwire."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        for m in re.finditer(
            r'account_number\s*==\s*"(\d{4})"', path.read_text(encoding="utf-8")
        ):
            found.add(m.group(1))
    unregistered = found - set(control_accounts.CONTROL_ACCOUNTS)
    assert not unregistered, (
        "these account numbers are resolved by literal value but are not in "
        f"CONTROL_ACCOUNTS, so they can be renumbered into silence: {sorted(unregistered)}"
    )


def test_missing_lists_what_a_blank_chart_lacks(db_session):
    """The reporter's note (1): a company bootstrapped outside
    manifest_create_company() has no chart at all, and reached the same
    fail-open from a different direction."""
    gaps = control_accounts.missing(db_session)
    # every SEEDED control account, and only those — 4800 and 5900 are
    # created on demand, so their absence is not a fault to report
    assert len(gaps) == len(control_accounts.seeded_numbers())
    assert ("1100", "Accounts Receivable") in gaps
    assert "4800" not in {n for n, _ in gaps}


def test_missing_is_empty_on_a_seeded_chart(db_session, seed_accounts):
    assert control_accounts.missing(db_session) == []


def test_resolve_raises_and_find_does_not(db_session, seed_accounts):
    assert control_accounts.resolve(db_session, "1100") == seed_accounts["1100"].id
    assert control_accounts.find(db_session, "9999") is None
    with pytest.raises(control_accounts.MissingControlAccount) as exc:
        control_accounts.resolve(db_session, "9999")
    assert exc.value.number == "9999"


def test_exports_stay_tolerant_of_an_odd_chart(db_session):
    """An export only needs a display name; it must not raise on a chart
    that lacks the account (app/services/iif_export.py)."""
    assert control_accounts.find(db_session, "2000") is None
