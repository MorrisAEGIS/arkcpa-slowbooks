"""banking on the ledger (issue #114)

The bank register was a side ledger: register entries and imported
statement lines never posted, posting documents never appeared in the
register, and reconciliation ticked rows the general ledger had never
seen (discussion #112, a QuickBooks user asking why GL 1000 did not move).

From 2.10 the general ledger is the only ledger. This revision adds what
that needs and moves nothing of value:

- accounts.bank_kind ("bank" | "credit_card"): which chart accounts are
  bank or card accounts. Backfilled from the seeded numbers, from names,
  and from every account a bank feed already links to.
- transaction_lines.cleared / reconciliation_id: a line on a bank account
  is cleared by a matched statement line or a reconciliation tick, and
  carries the reconciliation that closed it.
- bank_accounts.balance -> legacy_balance: the register's own balance is
  retired; the number is kept for the one-time "post as opening balance /
  dismiss" banner. NO journal entry is posted here — a migration must not
  write ledger lines the user has not reviewed, and an install that also
  used Opening Balances would be double-counted.
- bank_accounts without a linked account get a bank-kind asset account
  created and linked (name = feed name, next free 10x0 number).
- bank_transactions.transaction_line_id: a statement line links to the
  exact line it matches (a transfer has two bank lines).
- reconciliations.account_id / beginning_balance / cleared_total;
  bank_account_id becomes nullable (pre-2.10 rows keep it).
- bank_transactions.match_status: rows ticked in the old side ledger
  become "excluded" (Restore brings one back); everything else "unmatched"
  (an old "auto" keeps the category a rule assigned).

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-09

"""

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f8a9b0c1d2e3"
# ARK's already-deployed OIDC identity migration owns e7f8a9b0c1d2.
# Upstream 2.10.1 independently chose the same revision id for banking.
# Preserve the live revision and serialize banking after it so both schema
# changes are applied exactly once on existing and new Ark CPA databases.
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BANKISH = re.compile(r"check|saving|cash|bank|petty", re.I)
_CARDISH = re.compile(r"credit card|visa|mastercard|amex|card", re.I)


def _type_name(value) -> str:
    """account_type is stored as the enum NAME on every engine ('ASSET');
    tolerate a lower-case value from an unusual import."""
    return str(value or "").upper()


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("bank_kind", sa.String(length=20), nullable=True))

    with op.batch_alter_table("transaction_lines") as batch:
        batch.add_column(
            sa.Column(
                "cleared", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("reconciliation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_transaction_lines_reconciliation_id",
            "reconciliations",
            ["reconciliation_id"],
            ["id"],
        )

    with op.batch_alter_table("bank_accounts") as batch:
        batch.alter_column(
            "balance",
            new_column_name="legacy_balance",
            existing_type=sa.Numeric(12, 2),
            existing_nullable=True,
        )

    with op.batch_alter_table("bank_transactions") as batch:
        batch.add_column(sa.Column("transaction_line_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_bank_transactions_transaction_line_id",
            "transaction_lines",
            ["transaction_line_id"],
            ["id"],
        )

    with op.batch_alter_table("reconciliations") as batch:
        batch.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_reconciliations_account_id", "accounts", ["account_id"], ["id"]
        )
        batch.add_column(
            sa.Column(
                "beginning_balance",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("cleared_total", sa.Numeric(12, 2), nullable=True))
        batch.alter_column("bank_account_id", existing_type=sa.Integer(), nullable=True)

    bind = op.get_bind()

    # 1. bank_kind from the seeded numbers and from names
    rows = bind.execute(
        sa.text("SELECT id, account_number, name, account_type FROM accounts")
    ).fetchall()
    for r in rows:
        kind = None
        t = _type_name(r.account_type)
        num = (r.account_number or "").strip()
        name = r.name or ""
        if t == "ASSET" and (num in ("1000", "1010") or _BANKISH.search(name)):
            kind = "bank"
        elif t == "LIABILITY" and (num == "2100" or _CARDISH.search(name)):
            kind = "credit_card"
        if kind:
            bind.execute(
                sa.text("UPDATE accounts SET bank_kind = :k WHERE id = :id"),
                {"k": kind, "id": r.id},
            )
    # ...and every account a feed already links to, whatever its name
    linked = bind.execute(
        sa.text(
            "SELECT a.id, a.account_type FROM accounts a "
            "JOIN bank_accounts b ON b.account_id = a.id"
        )
    ).fetchall()
    for r in linked:
        kind = "credit_card" if _type_name(r.account_type) == "LIABILITY" else "bank"
        bind.execute(
            sa.text("UPDATE accounts SET bank_kind = :k WHERE id = :id"),
            {"k": kind, "id": r.id},
        )

    # 2. an unlinked feed gets its own bank-kind asset account (no posting)
    used = {
        (r[0] or "").strip()
        for r in bind.execute(sa.text("SELECT account_number FROM accounts")).fetchall()
    }
    unlinked = bind.execute(
        sa.text(
            "SELECT id, name, is_active FROM bank_accounts WHERE account_id IS NULL"
        )
    ).fetchall()
    for r in unlinked:
        number = next(
            (str(n) for n in range(1000, 1100, 10) if str(n) not in used), None
        )
        if number:
            used.add(number)
        bind.execute(
            sa.text(
                "INSERT INTO accounts (name, account_number, account_type, bank_kind, "
                "is_active, is_system, balance) "
                "VALUES (:name, :number, 'ASSET', 'bank', :active, :sys, 0)"
            ),
            {
                "name": r.name,
                "number": number,
                "active": bool(r.is_active) if r.is_active is not None else True,
                "sys": False,
            },
        )
        acct_id = bind.execute(
            sa.text("SELECT id FROM accounts WHERE name = :name ORDER BY id DESC"),
            {"name": r.name},
        ).fetchone()[0]
        bind.execute(
            sa.text("UPDATE bank_accounts SET account_id = :a WHERE id = :id"),
            {"a": acct_id, "id": r.id},
        )

    # 3. reconciliations are keyed by the GL account now
    bind.execute(
        sa.text(
            "UPDATE reconciliations SET account_id = ("
            "SELECT account_id FROM bank_accounts "
            "WHERE bank_accounts.id = reconciliations.bank_account_id) "
            "WHERE account_id IS NULL"
        )
    )

    # 4. statement lines: old ticks leave the queue, everything else waits in it
    bind.execute(
        sa.text(
            "UPDATE bank_transactions SET match_status = 'excluded' "
            "WHERE reconciled = :t"
        ),
        {"t": True},
    )
    bind.execute(
        sa.text(
            "UPDATE bank_transactions SET match_status = 'unmatched' "
            "WHERE match_status IS NULL OR match_status NOT IN ('excluded')"
        )
    )


def downgrade() -> None:
    # Accounts created for unlinked feeds in upgrade() stay: dropping ledger
    # accounts is not something a downgrade should do on its own.
    with op.batch_alter_table("reconciliations") as batch:
        batch.alter_column(
            "bank_account_id", existing_type=sa.Integer(), nullable=False
        )
        batch.drop_column("cleared_total")
        batch.drop_column("beginning_balance")
        batch.drop_constraint("fk_reconciliations_account_id", type_="foreignkey")
        batch.drop_column("account_id")

    with op.batch_alter_table("bank_transactions") as batch:
        batch.drop_constraint(
            "fk_bank_transactions_transaction_line_id", type_="foreignkey"
        )
        batch.drop_column("transaction_line_id")

    with op.batch_alter_table("bank_accounts") as batch:
        batch.alter_column(
            "legacy_balance",
            new_column_name="balance",
            existing_type=sa.Numeric(12, 2),
            existing_nullable=True,
        )

    with op.batch_alter_table("transaction_lines") as batch:
        batch.drop_constraint(
            "fk_transaction_lines_reconciliation_id", type_="foreignkey"
        )
        batch.drop_column("reconciliation_id")
        batch.drop_column("cleared")

    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("bank_kind")
