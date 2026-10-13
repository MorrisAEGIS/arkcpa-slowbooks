# CPA authority research controls

This is research infrastructure, not a completed US/Canadian accounting casebook,
professional qualification, or authorization to post, file, elect or move money.
The migration and software tests do not establish accounting correctness.

## Read and mutation boundaries

`GET /api/ark-cpa/authority-sources` is read-only. It never seeds or refreshes
records. Entity membership permits registry viewing; entity administration or
protected-approver status does **not** grant global research authority.

A human session needs an explicit, active, unexpired `refresh` grant in
`ark_research_permissions` to call the refresh endpoint. Reviews need the separate
`review` grant. Inactive users and revoked/expired grants fail closed on each
request. Token-only and mixed session/token requests cannot mutate this registry.
The web application has no grant/self-grant endpoint. An authorized operator must
provision grants outside the application under the existing access-change process;
this release provisions none. Do not grant permissions just to clear a blocked UI.

The scheduled Controller refuses `--refresh-tax` before starting any entity work.
Its existing financial posting permissions and entity holds are unchanged.
Research is currently an authenticated-human API workflow, not autonomous research.

## Register and capture

Source definitions are explicit operator configuration. The optional
`seed_authority_sources(db)` helper creates missing defaults only; it never
changes existing definitions and is never called by GET or refresh.

Every source starts with `capture_rights=unreviewed` and
`allow_model_context=false`. Before capture, a named rights reviewer must document
an applicable public-domain basis or licence, a rights expiry, and whether model
context use is allowed. Detailed rights notes are visible only through the global
reviewer endpoint, not the member-facing source catalog. A publicly accessible page or `license_note` alone is not
permission to reproduce or use it in models. FASB/CPA Handbook or similar paid
standards need the appropriate licence and separate usage review.

Retrieval requires an exact approved HTTPS host, no user information, no fragment,
and port 443. The client resolves all A/AAAA addresses, rejects private/special
addresses (including multicast, reserved and transition ranges), connects to a validated IP, and preserves the original TLS SNI and Host.
It verifies the connected peer in production. TLS verification stays enabled;
ambient proxies, cookies, credentials, compression and redirects are disabled.
The DNS worker pool is bounded; DNS waits are limited to five seconds. Responses
are streamed with a 5 MiB cap, ten-second read timeouts and a checked 30-second
capture budget (a blocking read can last until its read timeout).

HTML/text is parsed without executing scripts. Known challenge/error-page bodies
and titles are rejected, including HTTP-200 Request Access pages. This is a
conservative filter, not a claim to recognize every possible challenge. Every
accepted capture still requires human review. PDF bytes may be retained after
rights clearance, but PDF text extraction/review is not implemented and PDFs
cannot enter model context in this release.

Every authorized attempt commits an immutable `started` event before networking,
then appends one success or failure event. A process crash leaves a visible start
rather than inventing a completed check. Network/validation failures persist a
sanitized failure code. A database outage can prevent the terminal event; it does
not authorize accepting the source. Snapshot bytes, extracted text, metadata and
hashes are retained. Unchanged captures reuse the snapshot and append a new
retrieval event; their original capture date is never overwritten.

New snapshots always have `proposed` status and a draft research proposal. Even the
first capture has no automatic approval. A source row lock serializes publication
of identical concurrent captures. PostgreSQL/SQLite migration triggers prohibit
UPDATE/DELETE of snapshots, retrievals, reviews and passages; PostgreSQL also
blocks TRUNCATE. SQLite BEFORE INSERT guards also refuse PK/unique conflicts so
REPLACE cannot bypass DELETE triggers with recursive triggers disabled. These are application/database controls, not protection from a
privileged database administrator disabling triggers.

Source definitions and rights are hash-bound to captures. Material definition or
rights changes invalidate old approvals. Because the retained snapshot key is
`(source_id, content_hash)`, a definition change with identical content is refused;
register a reviewed new version under a new source code rather than silently
rewriting a prior definition or its history.

## Independent review

Researcher-only API endpoints:

- `GET /api/ark-cpa/authority-sources/snapshots/{snapshot_id}` provides captured
  metadata and bounded extracted text for review.
- `POST /api/ark-cpa/authority-sources/snapshots/{snapshot_id}/reviews` appends an
  approval, rejection or revocation. No review UI is shipped yet; these deliberate
  backend endpoints have API permission and validation tests.

Approval requires an independent reviewer, a successful capture within seven
days, valid capture rights, verified content/extracted-text hashes, one to twelve
exact excerpts (20–3000 characters), source locators, rationale, an explicit legal
start date, any known end date, and a review expiry within 90 days. An omitted end
date means the reviewer supplied an open-ended interval; it is not inferred from
HTTP dates. Jurisdictions, entity types and topics must be explicit lists without
wildcards. Use the entity's exact stored jurisdiction/type; the system does not
infer equivalent jurisdiction labels.

Research approval appends a new record and leaves the draft rule proposal and
snapshot status unchanged. It does not promote a tax rule or modify a ledger.
Rejection/revocation appends another record; earlier approvals remain in history.

## Governed agent context

`build_agent_context` obtains citations by querying the control database. It does
not accept caller-supplied citation dictionaries. Eligibility requires all of:

- Source enabled, appropriate authority level, valid capture rights and explicit
  model-use permission; unchanged reviewed source-definition hash.
- Latest retrieval event successful and no more than seven days old. Failed or
  unfinished latest attempts exclude the source until a successful check.
- Latest snapshot review approved and unexpired, with a still-active reviewer
  holding an unexpired review grant.
- Verified capture, extracted-text and passage hashes.
- Exact jurisdiction/entity-type/topic match and a verified transaction date
  inside the reviewer-supplied effective interval. Conflicting verified facts from
  different document locations remain explicit alternatives; no last-row selection
  can hide conflicting dates.

Both Bookkeeper and Controller receive this bundle through all four existing
context construction paths. The initial proposal uses human-verified
`transaction_date` or `receipt.date` facts. Candidate review additionally requires
that the proposed date match the verified date. Missing/conflicting dates yield
no citations. Default governed posting contexts request the `bookkeeping` topic;
other topic bundles require an explicit caller scope. This release does not
expand autonomous tax reasoning to every topic in the registry.

The prompt labels excerpts as untrusted reference data, requires escalation for
unsupported conclusions, and includes source/passage hashes, URLs, locators,
effective dates, retrieval dates and review IDs in the context fingerprint. These
controls do not prove that a model's answer is correct or fully cite-check its
rationale. Existing deterministic posting gates remain independently necessary.

## Acceptance and activation

The tests use synthetic text and companies. They cover API permission denials,
source rights and model rights, redirects/private DNS, streaming limits, challenge
pages, immutable history, independent reviews, applicability/freshness/revocation,
and governed context redaction. A disposable PostgreSQL test runs the complete
migration chain through the previous head, inserts legacy rows, upgrades, proves
legacy preservation/no grants, checks SQL immutability, and races two captures.
A Chromium fixture operates the actual authority panel using synthetic HTTP.

Run the isolated tests before source acceptance. Deployment requires the additive
migration `aa1b2c3d4e5f` after `f9a0b1c2d3e4`, a verified backup/restore and normal
release review. New grants, rights clearance, real source captures, independent
accounting review, casebook execution and any financial activation remain separate
gates. No live financial/source refresh changes were made while implementing this.

## Primary implementation references

Read 2026-10-13, for implementation guidance; not imported as accounting evidence:

- [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html): allowlists, address validation and disabled redirects.
- [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html): deny by default and validate permissions on every request.
- [HTTPCore extensions](https://www.encode.io/httpcore/extensions/): explicit SNI hostname and connected-peer information.
- [HTTPX transports](https://www.python-httpx.org/advanced/transports/): isolated mock transports for meaningful network-policy tests.

These linked technical references are described, not copied into the product
source registry. They confer no licence to copy accounting standards.
