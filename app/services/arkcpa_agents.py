"""Specialized, redacted contracts for Ark Bookkeeper and Ark Controller."""

from copy import deepcopy
from hashlib import sha256
import json
import re

from app.services.arkcpa_rules import POLICY_VERSION

PROMPT_VERSION = "arkcpa-agents-2026-09-12.2"
_FORBIDDEN_CONTEXT_KEYS = {
    "document_bytes",
    "raw_document",
    "raw_text",
    "full_text",
    "credential",
    "password",
    "secret",
    "access_token",
    "refresh_token",
}
_REDACTED_KEYS = {
    "sin",
    "ssn",
    "tax_id",
    "ein",
    "bank_account_number",
    "routing_number",
    "email",
    "address",
}
_SAFE_HASH = re.compile(r"^[a-f0-9]{64}$")

BOOKKEEPER_SYSTEM_PROMPT = """You are Ark Bookkeeper, the evidence-first posting
specialist for Canadian personal accounts, Alberta corporations, Alberta family
trusts, and a dormant Texas LLC. Work only from the structured, redacted facts
and current entity chart supplied. Never invent a vendor, amount, tax treatment,
account, jurisdiction, or filing fact. Propose balanced ordinary journal lines
and an explicit confidence. Escalate ambiguity. You cannot file, move money,
change credentials, close a period, run payroll, or promote a model. Return only
the governed JSON decision schema."""

CONTROLLER_SYSTEM_PROMPT = """You are Ark Controller, an independent accounting
reviewer. Re-perform the evidence-to-ledger reasoning; do not defer to Ark
Bookkeeper. Test entity boundary, period, currency, account classification,
GST/HST or cross-border treatment, duplicate risk, and balanced lines. Reject or
escalate unsupported conclusions. You cannot file, elect, move money, change
credentials, close a period, run payroll, or promote a model. Return only the
governed JSON decision schema."""

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "confidence", "rationale", "checks"],
    "properties": {
        "decision": {"enum": ["approve", "reject", "escalate"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 20000},
        "checks": {"type": "object"},
    },
}

DECISION_RETURN_CONTRACT = """Return only one JSON object with exactly these
four keys and no others: \"decision\" (one of \"approve\", \"reject\", or
\"escalate\"), \"confidence\" (a number from 0 through 1), \"rationale\" (a
non-empty string), and \"checks\" (an object). Do not use a key named
\"fields\" and do not wrap the JSON in Markdown."""


def _sanitize(value, key: str | None = None):
    normalized = (key or "").strip().lower()
    if normalized in _FORBIDDEN_CONTEXT_KEYS:
        raise ValueError(f"Raw or secret field is forbidden in agent context: {key}")
    if normalized in _REDACTED_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_agent_context(
    *,
    agent_role: str,
    entity: dict,
    evidence_hash: str,
    extracted_facts: dict,
    chart: list[dict],
    candidate: dict | None = None,
) -> dict:
    if agent_role not in {"bookkeeper", "controller"}:
        raise ValueError("Unknown Ark CPA agent role")
    if not _SAFE_HASH.fullmatch(evidence_hash or ""):
        raise ValueError("A verified SHA-256 evidence hash is required")
    context = {
        "contract": {
            "product": "Ark CPA",
            "agent_role": agent_role,
            "prompt_version": PROMPT_VERSION,
            "policy_version": POLICY_VERSION,
            "raw_documents_allowed": False,
            "cloud_fallback": "redacted-only",
        },
        "entity": deepcopy(entity),
        "evidence": {"sha256": evidence_hash, "facts": deepcopy(extracted_facts)},
        "chart": deepcopy(chart),
    }
    if candidate is not None:
        context["posting_candidate"] = deepcopy(candidate)
    if agent_role == "controller" and candidate is None:
        raise ValueError("Ark Controller requires the Bookkeeper candidate")
    return _sanitize(context)


def context_fingerprint(context: dict) -> str:
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def system_prompt(agent_role: str) -> str:
    if agent_role == "bookkeeper":
        role_prompt = BOOKKEEPER_SYSTEM_PROMPT
    elif agent_role == "controller":
        role_prompt = CONTROLLER_SYSTEM_PROMPT
    else:
        raise ValueError("Unknown Ark CPA agent role")
    return f"{role_prompt}\n\n{DECISION_RETURN_CONTRACT}"
