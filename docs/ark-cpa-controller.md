# Ark CPA Controller architecture

Ark CPA is one SlowBooks-based browser application at
`arkcpa.magaenergy.ai`. It has multiple Authentik-backed users and one isolated
ledger database per legal entity. The product name is always written **Ark
CPA**.

## Trust boundaries

The control database stores users, entity memberships, approval authority,
import manifests, evidence hashes and facts, posting candidates, append-only
agent decisions, Controller runs and issues, authority-source snapshots, rule
proposals, compliance obligations, workpaper manifests, and protected actions.
It is not the general ledger.

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

The scanner does not follow symlinks. It rejects unsupported, empty, oversized,
executable, extension-mismatched, changing, and embedded-instruction inputs.
Acceptable files are hashed with SHA-256. OCR and PDF text are transient; the
database receives only the extraction hash plus small structured facts with a
source hash, confidence, and page or document locator. A human verifies those
facts before either model can use them. Raw bytes and full extracted text are
not copied to agent memory or training data.

## Posting authority

The posting path is evidence -> verified facts -> Bookkeeper candidate ->
Controller re-performance -> deterministic checks -> review or ledger. Both
model calls use only structured context through an allowlisted internal ARK
gateway. Every call writes an immutable receipt containing model and family,
prompt and policy versions, evidence and context hashes, confidence, decision,
and response and rationale hashes.

An ordinary entry can be manually posted at any amount when all of these are
true:

- the evidence is verified and still has its source hash;
- the entity is active and candidate currency matches the entity;
- every line has one positive side, all referenced accounts are active, and
  exact decimal debits equal credits;
- the tax code is allowed by the entity rule pack;
- Ark Bookkeeper and Ark Controller both approve at 98% or higher;
- the agents use different model families;
- the action is an ordinary ledger entry and the period is open.

Autonomous posting adds three gates and has no fixed dollar threshold:

- the protected owner has set the entity to `autopost_ordinary`;
- the entity facts profile is verified;
- at least three prior posted entries match normalized description, account
  mapping, and tax code, and the new amount is within 20% of the recent median.

Anything novel, dormant, incomplete, frozen, low-confidence, cross-entity, or
out of pattern remains blocked for review. The scheduled worker is opt-in and
ships with autonomous posting disabled.

The separate-database commit is recoverable and idempotent. A candidate is
marked `posting` before the ledger commit. Posting takes an evidence-level
ledger lock, and a retry searches for `source_type=arkcpa_evidence` plus the
verified evidence ID before creating another entry. Different candidates for
the same source document therefore converge on one ledger transaction.

Filings, tax elections, money movement, credential changes, final close or
reopen, model promotion, and payroll are protected actions. Jay is configured
as the sole protected approver; Maesa can have full administration inside each
assigned Ark CPA ledger without inheriting protected approval or global
user/system administration. Approval writes a hash-bound decision record
and still does not execute the action. Payroll remains evidence and reminders
only.

## Jurisdiction packs

The first governed packs classify work for:

- Canadian personal: T1 and T1135;
- Alberta corporations: T2, Alberta AT1, GST/HST place-of-supply and ITC
  support;
- Alberta family trust: T3 and Schedule 15;
- dormant Texas LLC: Texas PIR/OIR, IRS Form 5472 with pro forma Form 1120
  trigger review, Canadian T1134/T1135 mapping, and an effective-dated BOI
  applicability monitor.

These are obligation and evidence contracts, not legal conclusions. No-revenue
or dormant status never suppresses an obligation automatically. Every filing
output remains a human-reviewed workpaper.

## Tax research and workpapers

The authority library starts from official federal, Alberta, Texas, IRS, and
FinCEN sources. Refreshes enforce HTTPS and an explicit hostname allowlist. A
new source creates a baseline hash. A changed source creates a proposed
snapshot and a draft rule proposal while the prior snapshot remains current.
Web content never rewrites posting or tax policy automatically. Test evidence,
protected approval, and a reviewed code release are required before a proposal
can be promoted.

Workpaper packages contain only verified evidence IDs, hashes, and source paths
from one entity. The package manifest is hashed and explicitly records that no
submission occurred. It is an internal preparation artifact for Jay and a
licensed Canadian or US professional, not a filed return or an assertion that
an obligation is not applicable.

## Model and memory contract

Ark Bookkeeper and Ark Controller are roles with independent prompts, model
families, evaluation sets, and grants. They are not two labels on one model.
Until both candidates pass the sealed accounting evaluation, decisions remain
draft-only. Any adapter promotion is a protected action. Route labels are not
proof of model independence; the runtime families in the completion receipts
must differ.

Accounting state is authoritative in Ark CPA PostgreSQL and Ark Files. Approved
abstractions may be published to the existing ARK Memory Fabric; raw financial
documents, credentials, exact legal entity names, and full extracted text may
not. Agent context uses an opaque entity reference and an allowlist of
non-identifying operating facts. Cloud fallback is redacted-only. A generic
Memory Fabric health claim never substitutes for an Ark CPA evidence hash or
ledger tie-out.

## Upgrade and release gates

SlowBooks v2.10.1 is the base. ARK's OIDC migration already used revision
`e7f8a9b0c1d2`, which upstream independently reused for v2.10 banking. Ark CPA
preserves the deployed OIDC revision and gives banking `f8a9b0c1d2e3`, ordered
after OIDC. The original control plane follows at `a9b0c1d2e3f4`; private
Controller governance and provenance are added by `b0c1d2e3f4a5`.

Before production:

1. back up and restore-test the control and every entity database;
2. run the full upstream and Ark CPA test suites;
3. run migration upgrade and downgrade tests from the live pre-release
   revision;
4. prove Jay and Maesa entity-admin membership plus Jay-only protected
   approval;
5. prove a token cannot read or post outside its entity grant;
6. prove a protected action cannot reach ledger posting;
7. prove the two configured model routes complete, return strict JSON, and
   resolve to different families;
8. prove desktop and mobile UI, keyboard focus, reduced motion, console, and
   network behavior;
9. stage on a separate port and compare trial balances before changing port
   3333;
10. deploy the reviewed immutable image and retain the encrypted pre-deploy
    exports.

Production activation is three separate decisions: deploy the UI and schema,
activate the non-posting Controller worker, and enable routine-pattern posting.
Do not combine them into one flag change. Roll back by disabling the worker
profile or `ARKCPA_CONTROLLER_ALLOW_POSTING`, restoring the immutable prior
image, and restoring all databases only from the matched pre-deploy backup set.

The rejected legacy Ark Accounting extraction database may be used only as a
comparison source during reconstruction. It is not ledger truth and must not
become training data without evidence re-linking and human approval.
