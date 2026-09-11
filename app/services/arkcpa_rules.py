"""Versioned jurisdiction and protected-action contract for Ark CPA.

This is workflow classification, not a tax calculation engine.  Filing
outputs remain reviewable workpapers until a human signs and submits them.
"""

from datetime import date
import json
import re

POLICY_VERSION = "arkcpa-policy-2026-09-10.2"
MIN_AUTONOMOUS_CONFIDENCE = 0.98

ENTITY_TYPES = {
    "ca_personal": {
        "label": "Canadian personal",
        "jurisdiction": "Canada / Alberta",
        "currency": "CAD",
        "tax_codes": ("none", "gst_itc", "gst_collected", "personal_deduction"),
    },
    "ca_ab_corporation": {
        "label": "Alberta corporation",
        "jurisdiction": "Canada / Alberta",
        "currency": "CAD",
        "tax_codes": ("none", "gst_itc", "gst_collected", "zero_rated", "exempt"),
    },
    "ca_ab_family_trust": {
        "label": "Alberta family trust",
        "jurisdiction": "Canada / Alberta",
        "currency": "CAD",
        "tax_codes": ("none", "gst_itc", "gst_collected", "trust_allocation"),
    },
    "us_tx_llc": {
        "label": "Texas LLC",
        "jurisdiction": "United States / Texas",
        "currency": "USD",
        "tax_codes": ("none", "us_sales_tax", "cross_border"),
    },
}

PROTECTED_ACTIONS = {
    "filing": "Government filing or submission",
    "tax_election": "Tax election or entity classification choice",
    "money_movement": "Payment, transfer, refund, or other movement of money",
    "credential_change": "Credential, token, bank connection, or access change",
    "final_close": "Final close, reopen, or locked-period override",
    "model_promotion": "Promotion of a model or adapter into posting authority",
    "payroll": "Payroll calculation, remittance, or filing",
}

ORDINARY_ACTION = "ordinary_entry"

SENSITIVE_PROFILE_KEY_PARTS = {
    "sin",
    "ssn",
    "ein",
    "taxid",
    "taxnumber",
    "accountnumber",
    "routingnumber",
    "password",
    "secret",
    "token",
    "email",
    "address",
}

OBLIGATION_PACKS = {
    "ca_personal": (
        ("CRA_T1", "T1 personal income tax return", "Canada Revenue Agency"),
        (
            "CRA_T1135",
            "T1135 foreign income verification statement",
            "Canada Revenue Agency",
        ),
    ),
    "ca_ab_corporation": (
        ("CRA_T2", "T2 corporation income tax return", "Canada Revenue Agency"),
        (
            "AB_AT1",
            "Alberta corporate income tax return",
            "Alberta Tax and Revenue Administration",
        ),
        ("CRA_GST_HST", "GST/HST return and ITC support", "Canada Revenue Agency"),
    ),
    "ca_ab_family_trust": (
        (
            "CRA_T3",
            "T3 trust income tax and information return",
            "Canada Revenue Agency",
        ),
        (
            "CRA_T3_S15",
            "T3 Schedule 15 beneficial ownership information",
            "Canada Revenue Agency",
        ),
    ),
    "us_tx_llc": (
        (
            "TX_PIR_OIR",
            "Texas Public or Ownership Information Report",
            "Texas Comptroller",
        ),
        (
            "IRS_5472_1120",
            "Form 5472 and pro forma Form 1120 trigger review",
            "Internal Revenue Service",
        ),
        (
            "CRA_T1134",
            "T1134 foreign affiliate reporting trigger review",
            "Canada Revenue Agency",
        ),
        (
            "CRA_T1135",
            "T1135 specified foreign property mapping",
            "Canada Revenue Agency",
        ),
        ("FINCEN_BOI", "BOI effective-date applicability monitor", "FinCEN"),
    ),
}


def obligations_for(entity_type: str, tax_year: int | None = None) -> list[dict]:
    year = tax_year or date.today().year
    return [
        {
            "code": code,
            "label": label,
            "authority": authority,
            "tax_year": year,
            "status": "monitoring",
            "human_required": True,
        }
        for code, label, authority in OBLIGATION_PACKS.get(entity_type, ())
    ]


def is_protected_action(action_type: str) -> bool:
    return action_type in PROTECTED_ACTIONS


def validate_entity_profile(profile: dict) -> dict:
    """Reject identifiers and secrets from the non-sensitive facts profile."""
    if not isinstance(profile, dict):
        raise ValueError("Entity profile must be an object")

    def walk(value, depth: int = 0) -> None:
        if depth > 5:
            raise ValueError("Entity profile nesting is too deep")
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if any(part in normalized for part in SENSITIVE_PROFILE_KEY_PARTS):
                    raise ValueError(f"Sensitive entity profile field is forbidden: {key}")
                walk(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 100:
                raise ValueError("Entity profile list is too large")
            for child in value:
                walk(child, depth + 1)
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("Entity profile values must be JSON scalars")

    walk(profile)
    if len(json.dumps(profile, sort_keys=True)) > 20000:
        raise ValueError("Entity profile is too large")
    return profile
