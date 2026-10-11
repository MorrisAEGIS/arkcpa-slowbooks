# Enerteq field records and accounting boundary

Status: source implementation and synthetic acceptance; no live connector, company
provisioning, payroll certification or launch approval. The original electrical
construction overview is a requirements example. Enerteq's custom operations app
owns field records and review; CPA is an accounting destination under evaluation.

## Invoice lifecycle

Manual creation, duplicate, estimate conversion and recurring generation produce
an editable draft. Saving a draft writes the document and its audit history, with
no receivable journal, no stock movement and zero `balance_due`. `total` is the
proposed bill. Drafts are excluded from booked customer income/statements and
QBO/IIF financial exports. Issue must happen before email, payment allocation,
credit application or payment-link use.

`POST /api/invoices/{id}/issue` is the explicit office action that records the
receivable and tracked stock together. The existing `/send` URL is a compatibility
alias for the same action. The stored `sent` status means issued; neither action
delivers email. Email remains a separate action with its own failure reporting.
The UI calls this **Issue Invoice** and asks the operator to confirm the effects.

Issue locks the invoice row, validates stored totals and closing date, and writes
the journal, stock and status in one transaction. A retry for an already issued
invoice returns that invoice without another stock movement or receivable. A
failure must roll back the entire transaction. PostgreSQL row locks serialize
competing issue requests; SQLite tests alone do not certify this concurrency.

Issued documents are immutable through invoice PUT. To correct one, reverse any
applied payment/credit, void the invoice in an open period, duplicate into a new
draft, review and issue the replacement. Voiding reverses recorded inventory
journals at their original values and restores actual net quantities, including
older edit movements. Voiding an unissued draft adds no stock. Repeated voids are
safe. Separate late-fee journals reverse using their original amounts and dates.
Repeated payment allocations restore their combined amount on payment void.
The closed-period guard is retained; there is no automatic reopen.

Existing posted documents are preserved without a migration. In older versions a
`draft` could already have a journal: issue advances its status without posting
again, and direct editing is locked. A historical draft with stock/payment
activity but no linked journal is refused at issue for reconciliation. QBO/IIF
imports of issued historical records retain their existing import behavior.

Line amounts are the rounded, reviewed accounting amounts. Legacy invoice rate
and quantity columns retain their existing precision; issue does not recalculate
amounts from a rate that may have been rounded on persistence. A broader precision
migration is outside this repair. Financial reports/export are based on issued
documents, while draft previews remain available for review.

## Time approval and correction

Only approved time can post a job cost. Approval attribution comes from the
authenticated company session (`acting_username`), not an arbitrary name in the
request. The older `approved_by` request field is accepted for compatibility but
does not establish identity.

Once linked to a job cost, edit, delete, submit and approval changes are locked.
The rejection action is the correction path: it reverses the job cost in its
original open period and clears the link. Edit the rejected entry, submit, obtain
fresh approval and post the new cost. An edit to approved but uncosted time also
invalidates approval. PUT cannot promote status. Payroll-linked time cannot be
changed, rejected or have its linked job cost voided through these paths; correct
the payroll workflow first. Closing-date checks continue to protect cost reversal.

Repeated posting of already costed time is rejected without another job cost.
The existing `job_cost_id` is the receipt for a caller that lost the first response.
The repair does not implement payroll processing, bank submission or automatic
replacement of processed payroll.

## Proposed source-to-destination mapping

This is a connector contract to implement and qualify, not a shipped importer.
Never map identity solely by display name or repurpose invoice notes as an import
ledger. A reviewed batch requires these stable fields:

| Field | Contract |
|---|---|
| `company_id` | Approved immutable Enerteq company identifier and explicitly assigned CPA ledger |
| `source_system` | Fixed identifier for the custom operations application |
| `record_type`, `record_id` | Ticket, approved billing instruction or material-consumption record and immutable source ID |
| `revision` | Monotonically increasing reviewed source revision; never a browser timestamp alone |
| `operation_id` | Durable unique ID for this logical import action, retained through retries |
| `payload_sha256` | Hash of the canonical approved payload, including dimensions, amounts, units and attachments |
| `approved_by`, `approved_at` | Verified source approval identity and timestamp, with access checked at receipt |
| `employee_id`, `customer_id`, `job_id`, `cost_code_id`, `item_id` | Explicit versioned mappings to destination IDs; ambiguous/unmapped rows are rejected |
| `work_date`, `period`, `currency`, `units` | Explicit date/period, currency/rate and measurement basis; decimal strings for hours/quantity/money |
| `destination_id`, `transaction_id`, `job_cost_id` | Returned destination receipts recorded only after verified commit |
| `supersedes`, `correction_reason` | Link to original operation and approved correction; no silent replacement of posted values |

A future connector must atomically enforce a unique key on
`(company_id, source_system, record_type, record_id, revision, operation_kind)`.
The same key/hash returns the original receipt. The same key with different
content conflicts. Out-of-order revisions are rejected. A partial batch reports
each committed/rejected record and reconciles totals; retry only records whose
commit state is known. Network failure after commit must resolve from the durable
operation receipt, not generate a new invoice.

Current invoice-create is **not** globally idempotent and does not yet implement
that receipt table. An adapter must not blindly retry invoice-create after a lost
response. Issue retries by the already known invoice ID are supported. This is an
explicit remaining connector prerequisite, not an implicit promise from the new
issue endpoint.

Field warehouse/truck transfers do not represent a sale. Agree one consumption
event: do not decrement the accounting item both for a field takeout and again
when issuing its invoice. Accounting receives approved valuation/consumption;
the custom app retains custody/location, material-list snapshots, signatures,
photos, JSA evidence and record revisions.

## Synthetic acceptance before live use

The isolated fixtures cover saving/editing drafts without financial changes,
approved issue, repeated issue, tracked stock, balanced void, closed periods,
legacy posted drafts, correction locks, approved-time reversal and payroll locks.
The existing rounding, payroll preparation, job-cost and sales-receipt paths must
also pass at the integrated revision.

Before November, qualify a disposable company using a complete electrical job:
time revisions, approved labor/equipment/material/per-diem billing, warehouse/truck
custody, customer PDF, issue, payment, correction and period reconciliation. Use
the selected payroll destination and verify hours and totals independently. Test
PostgreSQL concurrency, signed-in company/role boundaries, actual field devices,
backup restore and the eventual durable import receipt. Owner/accountant approval
and production deployment are separate from source acceptance.
