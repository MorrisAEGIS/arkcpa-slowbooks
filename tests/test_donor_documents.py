"""Donor documents: the donation receipt and pledge faces on the invoice
PDF, the IRS acknowledgment block, the donor fields on a customer, and
the recurring-template link on generated pledges."""

from decimal import Decimal

from app.services.donor_documents import irs_statement


def _nonprofit(client):
    assert (
        client.put("/api/settings", json={"company_type": "nonprofit"}).status_code
        == 200
    )
    client.post("/api/nonprofit/setup-accounts")


def _receipt(client, customer_id, **over):
    body = {
        "customer_id": customer_id,
        "date": "2026-05-09",
        "tax_rate": "0",
        "method": "Credit Card",
        "lines": [
            {"description": "Spring gala ticket", "quantity": 1, "rate": "150.00"}
        ],
    }
    body.update(over)
    return client.post("/api/sales-receipts", json=body)


# ---------------------------------------------------------------------------
# The IRS language, in one place
# ---------------------------------------------------------------------------


def test_irs_statement_variants():
    co = {"company_name": "Riverbend Community Arts", "company_tax_id": "12-3456789"}
    pure = irs_statement(co, 100)
    assert pure["deductible_amount"] == 100.0
    assert "No goods or services were provided" in pure["text"]
    assert "EIN 12-3456789" in pure["text"]

    quid = irs_statement(co, 150, 45, "gala dinner")
    assert quid["deductible_amount"] == 105.0
    assert "$45.00 (gala dinner)" in quid["text"]
    assert "limited to $105.00" in quid["text"]
    assert "No goods or services" not in quid["text"]

    prop = irs_statement(co, None, in_kind_descriptions=["Yamaha U1 upright piano"])
    assert prop["deductible_amount"] is None
    assert "Yamaha U1 upright piano" in prop["text"]
    assert "has not assigned a value" in prop["text"]
    assert "$" not in prop["text"]


# ---------------------------------------------------------------------------
# Donation receipt face
# ---------------------------------------------------------------------------


def test_receipt_prints_as_donation_receipt_with_deductible_portion(
    client, seed_accounts, seed_customer
):
    _nonprofit(client)
    r = _receipt(
        client,
        seed_customer.id,
        fair_value_amount="45",
        fair_value_description="gala dinner",
    )
    assert r.status_code in (200, 201), r.text
    inv = r.json()["invoice"]
    assert Decimal(inv["fair_value_amount"]) == Decimal("45")
    assert inv["fair_value_description"] == "gala dinner"

    html = client.get(f"/api/invoices/{inv['id']}/print-preview").text
    assert "DONATION RECEIPT" in html
    assert "SALES RECEIPT" not in html and "Sold To" not in html
    assert ">Donor<" in html
    assert "$45.00" in html and "gala dinner" in html
    assert "$105.00" in html  # the deductible portion
    assert "No goods or services" not in html
    assert "Received" in html and "Contribution" in html

    pdf = client.get(f"/api/invoices/{inv['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert (
        f"DonationReceipt_{inv['invoice_number']}.pdf"
        in pdf.headers["content-disposition"]
    )


def test_pure_gift_receipt_states_no_goods_or_services(
    client, seed_accounts, seed_customer
):
    _nonprofit(client)
    inv = _receipt(client, seed_customer.id).json()["invoice"]
    html = client.get(f"/api/invoices/{inv['id']}/print-preview").text
    assert "No goods or services were provided" in html
    assert "Deductible portion" not in html


def test_fair_value_is_validated_against_the_total(
    client, seed_accounts, seed_customer
):
    _nonprofit(client)
    assert (
        _receipt(client, seed_customer.id, fair_value_amount="151").status_code == 400
    )
    assert _receipt(client, seed_customer.id, fair_value_amount="-1").status_code == 400
    inv = _receipt(client, seed_customer.id, fair_value_amount="20").json()["invoice"]
    r = client.put(f"/api/invoices/{inv['id']}", json={"fair_value_amount": "999"})
    assert r.status_code == 409  # A posted receipt is immutable.
    draft = client.post(f"/api/invoices/{inv['id']}/duplicate")
    assert draft.status_code == 201, draft.text
    r = client.put(
        f"/api/invoices/{draft.json()['id']}", json={"fair_value_amount": "999"}
    )
    assert r.status_code == 400  # Editable drafts retain amount validation.


def test_business_mode_receipt_is_untouched(client, seed_accounts, seed_customer):
    inv = _receipt(client, seed_customer.id, fair_value_amount="45").json()["invoice"]
    html = client.get(f"/api/invoices/{inv['id']}/print-preview").text
    assert "SALES RECEIPT" in html and "DONATION" not in html
    assert "Sold To" in html and "Acknowledgment" not in html
    pdf = client.get(f"/api/invoices/{inv['id']}/pdf")
    assert (
        f"SalesReceipt_{inv['invoice_number']}.pdf"
        in pdf.headers["content-disposition"]
    )


# ---------------------------------------------------------------------------
# Pledge face
# ---------------------------------------------------------------------------


def test_pledge_and_invoice_faces(client, seed_accounts, seed_customer):
    _nonprofit(client)
    body = {
        "customer_id": seed_customer.id,
        "date": "2026-01-01",
        "terms": "Net 30",
        "tax_rate": "0",
        "lines": [{"description": "Monthly pledge", "quantity": 1, "rate": "100"}],
    }
    pledge = client.post("/api/invoices", json={**body, "is_pledge": True}).json()
    assert pledge["is_pledge"] is True
    html = client.get(f"/api/invoices/{pledge['id']}/print-preview").text
    assert (
        ">PLEDGE<" in html
        and "Pledge Amount" in html
        and "deductible when paid" in html
    )
    assert "Terms:" not in html
    pdf = client.get(f"/api/invoices/{pledge['id']}/pdf")
    assert (
        f"Pledge_{pledge['invoice_number']}.pdf" in pdf.headers["content-disposition"]
    )

    # a program fee is still an invoice for a nonprofit
    fee = client.post("/api/invoices", json={**body, "is_pledge": False}).json()
    html = client.get(f"/api/invoices/{fee['id']}/print-preview").text
    assert ">INVOICE<" in html and "PLEDGE" not in html
    assert (
        f"Invoice_{fee['invoice_number']}.pdf"
        in client.get(f"/api/invoices/{fee['id']}/pdf").headers["content-disposition"]
    )

    dup = client.post(f"/api/invoices/{pledge['id']}/duplicate").json()
    assert dup["is_pledge"] is True


# ---------------------------------------------------------------------------
# Donor record
# ---------------------------------------------------------------------------


def test_customer_donor_fields_round_trip(client):
    r = client.post(
        "/api/customers",
        json={
            "name": "Maria Okafor",
            "donor_type": "individual",
            "salutation": "Dear Maria",
            "send_year_end_statement": False,
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["donor_type"], c["salutation"], c["send_year_end_statement"]) == (
        "individual",
        "Dear Maria",
        False,
    )
    plain = client.post("/api/customers", json={"name": "Plain Co"}).json()
    assert plain["donor_type"] is None and plain["send_year_end_statement"] is True
    r = client.put(
        f"/api/customers/{c['id']}",
        json={"send_year_end_statement": True, "donor_type": None},
    )
    assert r.status_code == 200 and r.json()["send_year_end_statement"] is True
    assert r.json()["donor_type"] is None


# ---------------------------------------------------------------------------
# Recurring pledges remember their template (and their grant)
# ---------------------------------------------------------------------------


def test_generated_invoices_link_to_template_and_carry_job(
    client, seed_accounts, seed_customer
):
    _nonprofit(client)
    job = client.post(
        "/api/jobs", json={"customer_id": seed_customer.id, "name": "Capital Campaign"}
    ).json()
    rec = client.post(
        "/api/recurring",
        json={
            "customer_id": seed_customer.id,
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "job_id": job["id"],
            "lines": [{"description": "Monthly pledge", "quantity": 1, "rate": "100"}],
        },
    )
    assert rec.status_code in (200, 201), rec.text
    r = client.post("/api/recurring/generate?as_of=2026-02-01")
    assert r.status_code in (200, 201), r.text
    invoices = client.get("/api/invoices?is_sales_receipt=false").json()
    generated = [i for i in invoices if i["recurring_invoice_id"] == rec.json()["id"]]
    assert len(generated) >= 1, invoices
    for inv in generated:
        assert inv["is_pledge"] is True
        assert inv["job_id"] == job["id"]


# ---------------------------------------------------------------------------
# Acknowledgment letters
# ---------------------------------------------------------------------------


def test_acknowledgment_letter_uses_the_editable_template(
    client, seed_accounts, seed_customer
):
    _nonprofit(client)
    client.put(
        f"/api/customers/{seed_customer.id}",
        json={"salutation": "Dear Friend", "email": "friend@example.org"},
    )
    inv = _receipt(
        client,
        seed_customer.id,
        fair_value_amount="45",
        fair_value_description="gala dinner",
    ).json()["invoice"]

    # built-in text before the template row exists
    r = client.get(f"/api/donors/gifts/invoice/{inv['id']}/acknowledgment/pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert (
        f"Acknowledgment_{inv['invoice_number']}.pdf"
        in r.headers["content-disposition"]
    )

    # seed, edit the template, and the letter body follows it
    assert client.post("/api/email-templates/seed-defaults").status_code in (200, 201)
    tpl = next(
        t
        for t in client.get("/api/email-templates").json()
        if t["name"] == "donation_acknowledgment"
    )
    r = client.put(
        f"/api/email-templates/{tpl['id']}",
        json={
            "subject_template": "Bless you, {{ donor_name }}",
            "body_template": "<p>{{ donor.salutation }}, your gift of {{ gift.amount | currency }} matters. {{ irs.text }}</p>",
        },
    )
    assert r.status_code == 200, r.text
    from app.models.contacts import Customer
    from app.services.donor_documents import load_gift, render_acknowledgment
    from app.database import SessionLocal  # noqa: F401

    # render through the same code path the routes use
    import app.routes.donors as donors_routes

    captured = {}

    def fake_send(db, to_email, subject, html_body, **kw):
        captured.update(to=to_email, subject=subject, body=html_body, kw=kw)
        return True

    donors_routes.send_email = fake_send
    try:
        r = client.post(
            f"/api/donors/gifts/invoice/{inv['id']}/acknowledgment/email", json={}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"sent": True, "recipient": "friend@example.org"}
    finally:
        from app.services.email_service import send_email as real_send

        donors_routes.send_email = real_send
    assert captured["subject"] == "Bless you, Test Customer"
    assert "Dear Friend, your gift of $150.00 matters." in captured["body"]
    assert "limited to $105.00" in captured["body"]
    assert (
        captured["kw"]["attachment_name"]
        == f"Acknowledgment_{inv['invoice_number']}.pdf"
    )
    assert captured["kw"]["entity_type"] == "acknowledgment_invoice"
    assert (
        isinstance(Customer, type)
        and callable(load_gift)
        and callable(render_acknowledgment)
    )


def test_acknowledgment_email_without_smtp_is_a_502_with_a_log_row(
    client, db_session, seed_accounts, seed_customer
):
    _nonprofit(client)
    client.put(f"/api/customers/{seed_customer.id}", json={"email": "d@example.org"})
    inv = _receipt(client, seed_customer.id).json()["invoice"]
    r = client.post(
        f"/api/donors/gifts/invoice/{inv['id']}/acknowledgment/email", json={}
    )
    assert r.status_code == 502
    from app.models.email_log import EmailLog

    assert (
        db_session.query(EmailLog)
        .filter(EmailLog.entity_type == "acknowledgment_invoice")
        .count()
        == 1
    )
    # no address anywhere -> 400
    client.put(f"/api/customers/{seed_customer.id}", json={"email": ""})
    r = client.post(
        f"/api/donors/gifts/invoice/{inv['id']}/acknowledgment/email", json={}
    )
    assert r.status_code == 400


def test_payment_acknowledgment_eligibility(client, seed_accounts, seed_customer):
    _nonprofit(client)
    # a receipt's own payment is not a separate gift
    sr = _receipt(client, seed_customer.id).json()
    pay_id = sr["payment"]["id"]
    r = client.get(f"/api/donors/gifts/payment/{pay_id}/acknowledgment/preview").json()
    assert r["eligible"] is False and "acknowledge the receipt" in r["reason"]
    assert (
        client.get(f"/api/donors/gifts/payment/{pay_id}/acknowledgment/pdf").status_code
        == 400
    )

    # an unapplied payment is a gift
    gift = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-06-01",
            "amount": "500",
            "method": "Check",
            "check_number": "1042",
        },
    )
    assert gift.status_code in (200, 201), gift.text
    r = client.get(
        f"/api/donors/gifts/payment/{gift.json()['id']}/acknowledgment/preview"
    ).json()
    assert r == {"eligible": True, "amount": 500.0, "reason": None}
    pdf = client.get(
        f"/api/donors/gifts/payment/{gift.json()['id']}/acknowledgment/pdf"
    )
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert "Acknowledgment_1042.pdf" in pdf.headers["content-disposition"]

    # a pledge is acknowledged when paid, not when issued
    pledge = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-01-01",
            "tax_rate": "0",
            "is_pledge": True,
            "lines": [{"description": "pledge", "quantity": 1, "rate": "100"}],
        },
    ).json()
    assert (
        client.get(
            f"/api/donors/gifts/invoice/{pledge['id']}/acknowledgment/pdf"
        ).status_code
        == 400
    )
    assert (
        client.get("/api/donors/gifts/banana/1/acknowledgment/pdf").status_code == 404
    )
    assert (
        client.get("/api/donors/gifts/invoice/99999/acknowledgment/pdf").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Year-end giving statements
# ---------------------------------------------------------------------------


def _giving_year(client, seed_accounts, seed_customer):
    _nonprofit(client)
    other = client.post(
        "/api/customers",
        json={
            "name": "Opted Out",
            "email": "out@example.org",
            "send_year_end_statement": False,
        },
    ).json()
    client.put(f"/api/customers/{seed_customer.id}", json={"email": "d@example.org"})
    checking = seed_accounts["1010"].id
    # gala receipt with a dinner, a pledge paid, an unapplied gift, a voided receipt, property
    _receipt(
        client,
        seed_customer.id,
        fair_value_amount="45",
        fair_value_description="dinner",
    )
    pledge = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-02-01",
            "tax_rate": "0",
            "is_pledge": True,
            "lines": [{"description": "pledge", "quantity": 1, "rate": "100"}],
        },
    ).json()
    issued = client.post(f"/api/invoices/{pledge['id']}/issue")
    assert issued.status_code == 200, issued.text
    paid = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-02-10",
            "amount": "100",
            "allocations": [{"invoice_id": pledge["id"], "amount": "100"}],
        },
    )
    assert paid.status_code == 201, paid.text
    client.post(
        "/api/payments",
        json={"customer_id": seed_customer.id, "date": "2026-03-01", "amount": "500"},
    )
    voided = _receipt(client, seed_customer.id, date="2026-03-15").json()
    # a receipt is voided the way the SPA does it: its payment first, then the invoice
    assert (
        client.post(f"/api/payments/{voided['payment']['id']}/void").status_code == 200
    )
    assert (
        client.post(f"/api/invoices/{voided['invoice']['id']}/void").status_code == 200
    )
    client.post(
        "/api/in-kind-gifts",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-04-20",
            "lines": [
                {
                    "description": "Upright piano",
                    "quantity": 1,
                    "fair_value": "6500",
                    "debit_account_id": checking,
                }
            ],
        },
    )
    _receipt(client, other["id"], date="2026-06-01")
    return other


def test_giving_statement_totals_and_batch(
    client, db_session, seed_accounts, seed_customer
):
    other = _giving_year(client, seed_accounts, seed_customer)
    from app.services.donor_documents import collect_gifts

    gifts = collect_gifts(db_session, seed_customer.id, 2026)
    amounts = sorted(float(g["amount"]) for g in gifts["cash"])
    # the voided receipt is out; the pledge payment and the unapplied gift are in;
    # the gala receipt's own payment is not counted twice
    assert amounts == [100.0, 150.0, 500.0]
    assert gifts["totals"] == {
        "amount": Decimal("750.00"),
        "fair_value": Decimal("45.00"),
        "deductible": Decimal("705.00"),
    }
    assert [g["in_kind_lines"][0]["description"] for g in gifts["in_kind"]] == [
        "Upright piano"
    ]
    assert collect_gifts(db_session, seed_customer.id, 2025)["cash"] == []

    one = client.get(f"/api/donors/{seed_customer.id}/giving-statement/pdf?year=2026")
    assert one.status_code == 200 and one.content[:5] == b"%PDF-"
    assert (
        "GivingStatement_2026_Test Customer.pdf" in one.headers["content-disposition"]
    )
    both = client.get("/api/donors/giving-statements/pdf?year=2026")
    assert both.status_code == 200 and both.content[:5] == b"%PDF-"
    assert "GivingStatements_2026.pdf" in both.headers["content-disposition"]
    assert (
        client.get("/api/donors/99999/giving-statement/pdf?year=2026").status_code
        == 404
    )

    import app.routes.donors as donors_routes

    sent_to = []
    donors_routes.send_email = (
        lambda db, to_email, **kw: sent_to.append(to_email) or True
    )
    try:
        r = client.post(
            "/api/donors/giving-statements/batch-email", json={"year": 2026}
        )
    finally:
        from app.services.email_service import send_email as real_send

        donors_routes.send_email = real_send
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": 1, "failed": 0, "skipped": 1, "errors": []}
    assert sent_to == ["d@example.org"]
    assert other["send_year_end_statement"] is False
