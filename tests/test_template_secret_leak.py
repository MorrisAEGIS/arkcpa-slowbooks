"""Editable templates cannot read decrypted company credentials."""

import pytest

from app.models.email_templates import EmailTemplate
from app.services.settings_service import (
    ENCRYPTED_SETTINGS_KEYS,
    SECRET_PLACEHOLDER,
    get_all_settings,
    redact_secrets,
    set_setting,
)


CANARIES = {
    "smtp_password": "smtp-CANARY-secret",
    "stripe_secret_key": "stripe-CANARY-secret",
    "qbo_refresh_token": "qbo-CANARY-secret",
    "simplefin_access_url": "https://CANARY@simplefin.example/x",
}


@pytest.fixture
def secrets(db_session):
    for key, value in CANARIES.items():
        set_setting(db_session, key, value)
    db_session.commit()
    return CANARIES


def _set_template(db_session, name, body):
    template = EmailTemplate(
        name=name,
        template_type="acknowledgment",
        subject_template="{{ company.smtp_password }}",
        body_template=body,
    )
    db_session.add(template)
    db_session.commit()


def test_redaction_covers_every_encrypted_key():
    raw = {key: "live-value" for key in ENCRYPTED_SETTINGS_KEYS}
    raw["company_name"] = "Acme"
    redacted = redact_secrets(raw)
    assert redacted["company_name"] == "Acme"
    for key in ENCRYPTED_SETTINGS_KEYS:
        assert redacted[key] == SECRET_PLACEHOLDER


def test_empty_secret_stays_empty():
    assert redact_secrets({"smtp_password": ""})["smtp_password"] == ""


def test_api_and_encryption_use_the_same_secret_key_set():
    from app.routes import settings as settings_routes

    assert settings_routes.SECRET_KEYS is ENCRYPTED_SETTINGS_KEYS


def test_editable_template_sink_redacts_raw_company_context(db_session, secrets):
    from app.services.email_service import render_template_from_db

    _set_template(
        db_session,
        "credential_probe",
        "{{ company.smtp_password }}|{{ company.stripe_secret_key }}|{{ company }}",
    )
    subject, body = render_template_from_db(
        db_session,
        "credential_probe",
        {"company": get_all_settings(db_session)},
    )
    for value in secrets.values():
        assert value not in subject
        assert value not in body
    assert SECRET_PLACEHOLDER in subject
    assert SECRET_PLACEHOLDER in body


def test_acknowledgment_template_cannot_read_secrets(
    client, db_session, seed_accounts, secrets
):
    from datetime import date

    from app.models.contacts import Customer
    from app.services.donor_documents import ACK_TEMPLATE_NAME, render_acknowledgment

    _set_template(
        db_session,
        ACK_TEMPLATE_NAME,
        "{{ company.smtp_password }}|{{ company.qbo_refresh_token }}|{{ company }}",
    )
    customer_json = client.post(
        "/api/customers", json={"name": "Donor", "email": "donor@example.com"}
    ).json()
    customer = (
        db_session.query(Customer).filter(Customer.id == customer_json["id"]).first()
    )
    gift = {
        "id": 1,
        "number": "G-1",
        "date": date(2026, 6, 1),
        "amount": 100,
        "description": "",
        "fair_value_amount": None,
        "fair_value_description": None,
        "in_kind_lines": [],
        "customer_id": customer.id,
    }

    subject, body = render_acknowledgment(
        db_session, get_all_settings(db_session), customer, gift
    )
    for value in secrets.values():
        assert value not in subject
        assert value not in body
    assert SECRET_PLACEHOLDER in subject
    assert SECRET_PLACEHOLDER in body
