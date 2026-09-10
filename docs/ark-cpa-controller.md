# Ark CPA controller architecture

Ark CPA is one SlowBooks-based browser application at
`arkcpa.magaenergy.ai`. It has multiple Authentik-backed users and one isolated
ledger database per legal entity. The product name is always written **Ark
CPA**.

## Trust boundaries

The control database stores users, entity memberships, agent-token grants,
evidence hashes, posting candidates, independent agent decisions, compliance
obligations, and protected actions. It is not the general ledger.

Each personal, corporation, trust, and LLC entity receives a separate
PostgreSQL database. The selected slug in a signed session or
`X-Ark-Entity` header is only a request; `app.database` resolves it against a
current user membership or token grant before opening the ledger database.
Missing or archived relationships fail closed. Dormant entities remain
reviewable but cannot pass the autonomous posting gate.

Ark Files is mounted read-only at `/ark-files`. The canonical entity layout is:

```text
_shared/Ark CPA/<entity>/
  Inbox/
  Statements/
  Receipts/
  Sales/
  Tax/
  Legal/
  Exports/
  Archive/
```

The scanner does not follow symlinks. It rejects unsupported, empty, and
oversized files, hashes acceptable files with SHA-256, and stores only metadata
in the control database. Raw bytes and extracted text are not copied to general
agent memory or training data.

## Posting authority

The posting path is evidence -> candidate -> Bookkeeper decision -> Controller
decision -> deterministic checks -> ledger. An ordinary entry can post at any
amount only when all of these are true:

- the evidence is verified and still has its source hash;
- the entity is active and candidate currency matches the entity;
- every line has one positive side, all referenced accounts are active, and
  exact decimal debits equal credits;
- the tax code is allowed by the entity rule pack;
- Ark Bookkeeper and Ark Controller both approve at 98% or higher;
- the agents use different model families;
- the action is an ordinary ledger entry and the period is open.

The separate-database commit is recoverable and idempotent. A candidate is
marked `posting` before the ledger commit. A retry searches for
`source_type=arkcpa_agent` and the candidate ID before creating a second entry.

Filings, tax elections, money movement, credential changes, final close or
reopen, model promotion, and payroll are protected actions. The system records
the request for a human; it does not execute it. Payroll remains evidence and
reminders only.

## Jurisdiction packs

The first governed packs classify work for:

- Canadian personal: T1 and T1135;
- Alberta corporations: T2, Alberta AT1, GST/HST place-of-supply and ITC
  support;
- Alberta family trust: T3 and Schedule 15;
- dormant Texas LLC: Texas PIR/OIR, IRS Form 5472 with pro forma Form 1120
  trigger review, Canadian T1134/T1135 mapping, and an effective-dated BOI
  applicability monitor.

These are obligation and evidence contracts, not legal conclusions. Every
filing output remains a human-reviewed workpaper.

## Model and memory contract

Ark Bookkeeper and Ark Controller are roles with independent prompts, model
families, evaluation sets, and grants. They are not two labels on one model.
Until both candidates pass the sealed accounting evaluation, decisions remain
draft-only. Any adapter promotion is a protected action.

Accounting state is authoritative in Ark CPA PostgreSQL and Ark Files. Approved
abstractions may be published to the existing ARK Memory Fabric; raw financial
documents, credentials, and full extracted text may not. Cloud fallback is
redacted-only. A generic Memory Fabric health claim never substitutes for an
Ark CPA evidence hash or ledger tie-out.

## Upgrade and release gates

SlowBooks v2.10.1 is the base. ARK's OIDC migration already used revision
`e7f8a9b0c1d2`, which upstream independently reused for v2.10 banking. Ark CPA
preserves the deployed OIDC revision and gives banking `f8a9b0c1d2e3`, ordered
after OIDC. The Ark CPA control plane follows at `a9b0c1d2e3f4`.

Before production:

1. back up and restore-test the control and every entity database;
2. run the full upstream and Ark CPA test suites;
3. run migration upgrade tests from the live pre-release revision;
4. prove Jay admin and Maesa bookkeeper membership boundaries;
5. prove a token cannot read or post outside its entity grant;
6. prove a protected action cannot reach ledger posting;
7. prove desktop and mobile UI, keyboard focus, reduced motion, console, and
   network behavior;
8. stage on a separate port and compare trial balances before changing port
   3333;
9. deploy the reviewed immutable image and retain the encrypted pre-deploy
   exports.

The rejected legacy Ark Accounting extraction database may be used only as a
comparison source during reconstruction. It is not ledger truth and must not
become training data without evidence re-linking and human approval.
