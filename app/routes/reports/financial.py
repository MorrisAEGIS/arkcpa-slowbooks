from datetime import date
from decimal import Decimal

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, select

from app.database import get_db
from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine
from app.routes.reports._router import router
from app.services.terminology import Terms, terms_from_db

# Debit-normal account types. For these, natural balance = debit - credit.
# For the rest (liability, equity, income), natural balance = credit - debit.
from app.services.bank_register import DEBIT_NORMAL as _DEBIT_NORMAL  # noqa: E402


def _totals_by_account(db, acct_type, date_start=None, date_end=None):
    """Return a list of {account_id, account_name, account_number, amount}
    rows where amount is signed by the account type's natural balance
    (always positive for a normal-balance ledger).

    `account_id` is included so the SPA can drill into /account-transactions
    for any line — Phase 11 drill-down support.
    """
    q = (
        db.query(
            Account.id,
            Account.name,
            Account.account_number,
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .join(TransactionLine, TransactionLine.account_id == Account.id)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .filter(Account.account_type == acct_type)
    )
    if date_start is not None:
        q = q.filter(Transaction.date >= date_start)
    if date_end is not None:
        q = q.filter(Transaction.date <= date_end)
    q = q.group_by(Account.id, Account.name, Account.account_number)

    rows = []
    for acct_id, name, number, dr, cr in q.all():
        amount = (dr - cr) if acct_type in _DEBIT_NORMAL else (cr - dr)
        rows.append(
            {
                "account_id": acct_id,
                "account_name": name,
                "account_number": number,
                "amount": float(amount),
            }
        )
    return rows


@router.get("/profit-loss")
def profit_loss(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    income = _totals_by_account(db, AccountType.INCOME, start_date, end_date)
    cogs = _totals_by_account(db, AccountType.COGS, start_date, end_date)
    expenses = _totals_by_account(db, AccountType.EXPENSE, start_date, end_date)

    total_income = sum(i["amount"] for i in income)
    total_cogs = sum(c["amount"] for c in cogs)
    total_expenses = sum(e["amount"] for e in expenses)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "income": income,
        "cogs": cogs,
        "expenses": expenses,
        "total_income": total_income,
        "total_cogs": total_cogs,
        "gross_profit": total_income - total_cogs,
        "total_expenses": total_expenses,
        "net_income": total_income - total_cogs - total_expenses,
    }


@router.get("/balance-sheet")
def balance_sheet(
    as_of_date: date = Query(default=None), db: Session = Depends(get_db)
):
    t = terms_from_db(db)
    if not as_of_date:
        as_of_date = date.today()

    assets = _totals_by_account(db, AccountType.ASSET, date_end=as_of_date)
    liabilities = _totals_by_account(db, AccountType.LIABILITY, date_end=as_of_date)
    equity = _totals_by_account(db, AccountType.EQUITY, date_end=as_of_date)

    total_assets = sum(a["amount"] for a in assets)
    total_liabilities = sum(liab["amount"] for liab in liabilities)
    total_equity = sum(e["amount"] for e in equity)

    # Net income for all periods up to as_of_date flows into equity as retained
    # earnings. Without this, the balance sheet fails to balance whenever there
    # is income or expense activity that hasn't been formally closed into an
    # equity account (which is the normal state in this app — income/expense
    # accounts are never explicitly closed).
    income_rows = _totals_by_account(db, AccountType.INCOME, date_end=as_of_date)
    cogs_rows = _totals_by_account(db, AccountType.COGS, date_end=as_of_date)
    expense_rows = _totals_by_account(db, AccountType.EXPENSE, date_end=as_of_date)
    net_income = (
        sum(r["amount"] for r in income_rows)
        - sum(r["amount"] for r in cogs_rows)
        - sum(r["amount"] for r in expense_rows)
    )

    if net_income != 0:
        equity = list(equity) + [
            {
                "account_id": None,
                "account_name": f"{t('Net Income')} (current period)",
                "account_number": None,
                "amount": float(net_income),
            }
        ]
        total_equity += net_income

    return {
        "as_of_date": as_of_date.isoformat(),
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
    }


@router.get("/general-ledger")
def general_ledger(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    account_id: int = Query(default=None),
    db: Session = Depends(get_db),
):
    """General Ledger detail report."""
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    q = (
        db.query(TransactionLine, Transaction, Account)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
        .filter(Transaction.date >= start_date, Transaction.date <= end_date)
    )
    if account_id:
        q = q.filter(TransactionLine.account_id == account_id)

    q = q.order_by(Account.account_number, Transaction.date)
    results = q.all()

    entries_by_account = {}
    for tl, txn, acct in results:
        key = acct.id
        if key not in entries_by_account:
            entries_by_account[key] = {
                "account_id": acct.id,
                "account_number": acct.account_number,
                "account_name": acct.name,
                "account_type": acct.account_type.value,
                "entries": [],
                "total_debit": Decimal(0),
                "total_credit": Decimal(0),
            }
        entries_by_account[key]["entries"].append(
            {
                "date": txn.date.isoformat(),
                "description": txn.description or tl.description or "",
                "reference": txn.reference or "",
                "debit": float(tl.debit),
                "credit": float(tl.credit),
            }
        )
        entries_by_account[key]["total_debit"] += tl.debit
        entries_by_account[key]["total_credit"] += tl.credit

    accounts_list = list(entries_by_account.values())
    for a in accounts_list:
        a["total_debit"] = float(a["total_debit"])
        a["total_credit"] = float(a["total_credit"])

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "accounts": accounts_list,
    }


@router.get("/account-transactions")
def account_transactions(
    account_id: int = Query(..., description="Account to drill into"),
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """Phase 11: drill-down support. Every journal entry line hitting a
    given account in the date range, with source document linkage so the
    UI can jump from a P&L row straight to the underlying invoice/bill/JE.
    The register service (bank_register.account_register) does the work —
    the bank register is the same view."""
    from app.services.bank_register import account_register

    acct = db.query(Account).filter(Account.id == account_id).first()
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()
    out = account_register(db, acct, start_date, end_date)
    out["start_date"] = start_date.isoformat()
    out["end_date"] = end_date.isoformat()
    return out


# ============================================================================
# Phase 10: Quick Wins — Trial Balance, Cash Flow, Batch Email,
#            Collection Letters, 1099 Summary
# ============================================================================


@router.get("/trial-balance")
def trial_balance(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """Trial Balance: sum all debits/credits per account for a date range."""
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    results = (
        db.query(
            Account.id,
            Account.account_number,
            Account.name,
            Account.account_type,
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .join(TransactionLine, TransactionLine.account_id == Account.id)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .filter(Transaction.date >= start_date, Transaction.date <= end_date)
        .group_by(
            Account.id, Account.account_number, Account.name, Account.account_type
        )
        .order_by(Account.account_number)
        .all()
    )

    items = []
    total_debit = Decimal(0)
    total_credit = Decimal(0)
    for acct_id, acct_num, acct_name, acct_type, debit, credit in results:
        total_debit += debit
        total_credit += credit
        items.append(
            {
                "account_id": acct_id,
                "account_number": acct_num or "",
                "account_name": acct_name,
                "account_type": acct_type.value,
                "total_debit": float(debit),
                "total_credit": float(credit),
                "net_balance": float(debit - credit),
            }
        )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "items": items,
        "total_debit": float(total_debit),
        "total_credit": float(total_credit),
        "difference": float(total_debit - total_credit),
    }


@router.get("/cash-flow")
def cash_flow(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """Cash flow from the non-cash side of journals that move linked cash."""
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    # Map account types to cash flow sections
    section_map = {
        AccountType.INCOME: "operating",
        AccountType.EXPENSE: "operating",
        AccountType.COGS: "operating",
        AccountType.ASSET: "investing",
        AccountType.LIABILITY: "financing",
        AccountType.EQUITY: "financing",
    }

    # Cash = the chart's bank accounts (2.10: Account.bank_kind), whether or
    # not a feed is linked; a card is a liability, not cash.
    cash_account_ids = select(Account.id).where(Account.bank_kind == "bank")
    cash_transaction_ids = select(TransactionLine.transaction_id).where(
        TransactionLine.account_id.in_(cash_account_ids)
    )

    results = (
        db.query(
            Account.name,
            Account.account_number,
            Account.account_type,
            sqlfunc.coalesce(
                sqlfunc.sum(TransactionLine.credit - TransactionLine.debit), 0
            ),
        )
        .join(TransactionLine, TransactionLine.account_id == Account.id)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .filter(
            Transaction.date >= start_date,
            Transaction.date <= end_date,
            Transaction.id.in_(cash_transaction_ids),
            ~TransactionLine.account_id.in_(cash_account_ids),
            sqlfunc.coalesce(Transaction.source_type, "") != "opening_balance",
        )
        .group_by(
            Account.id, Account.name, Account.account_number, Account.account_type
        )
        .order_by(Account.account_number)
        .all()
    )

    sections = {"operating": [], "investing": [], "financing": []}
    totals = {"operating": Decimal(0), "investing": Decimal(0), "financing": Decimal(0)}

    for acct_name, acct_num, acct_type, net_change in results:
        section = section_map.get(acct_type, "operating")
        amount = float(net_change)
        sections[section].append(
            {
                "account_name": acct_name,
                "account_number": acct_num or "",
                "amount": amount,
            }
        )
        totals[section] += Decimal(str(amount))

    net_change = totals["operating"] + totals["investing"] + totals["financing"]

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "operating": sections["operating"],
        "investing": sections["investing"],
        "financing": sections["financing"],
        "total_operating": float(totals["operating"]),
        "total_investing": float(totals["investing"]),
        "total_financing": float(totals["financing"]),
        "net_change": float(net_change),
    }


@router.get("/profit-loss-by-class")
def profit_loss_by_class(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """P&L split by the class dimension on each posted line.

    A line's own class wins, then the transaction header's; untagged
    activity groups with the system-default "Uncategorized" class so every
    posting is accounted for and the column totals reconcile with the
    plain Profit & Loss.
    """
    from app.models.classes import TxnClass
    from app.services.classes_service import class_attribution, uncategorized_class_id

    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    uncat_id = uncategorized_class_id(db)
    db.commit()

    pl_types = (AccountType.INCOME, AccountType.COGS, AccountType.EXPENSE)
    rows = (
        db.query(
            class_attribution(uncat_id).label("cls"),
            Account.account_type,
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .select_from(Transaction)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .join(Account, TransactionLine.account_id == Account.id)
        .filter(
            Account.account_type.in_(pl_types),
            Transaction.date >= start_date,
            Transaction.date <= end_date,
        )
        .group_by("cls", Account.account_type)
        .all()
    )

    class_names = {c.id: c.name for c in db.query(TxnClass).all()}
    by_class: dict[int, dict] = {}
    for cls_id, acct_type, dr, cr in rows:
        bucket = by_class.setdefault(
            cls_id,
            {
                "class_id": cls_id,
                "class_name": class_names.get(cls_id, "Unknown"),
                "income": Decimal("0"),
                "cogs": Decimal("0"),
                "expenses": Decimal("0"),
            },
        )
        if acct_type == AccountType.INCOME:
            bucket["income"] += cr - dr
        elif acct_type == AccountType.COGS:
            bucket["cogs"] += dr - cr
        else:
            bucket["expenses"] += dr - cr

    columns = []
    for bucket in sorted(
        by_class.values(),
        key=lambda b: (b["class_id"] != uncat_id, b["class_name"].lower()),
    ):
        income, cogs, expenses = bucket["income"], bucket["cogs"], bucket["expenses"]
        columns.append(
            {
                "class_id": bucket["class_id"],
                "class_name": bucket["class_name"],
                "income": float(income),
                "cogs": float(cogs),
                "gross_profit": float(income - cogs),
                "expenses": float(expenses),
                "net_income": float(income - cogs - expenses),
            }
        )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "classes": columns,
        "total_income": sum(c["income"] for c in columns),
        "total_expenses": sum(c["expenses"] for c in columns),
        "total_net_income": sum(c["net_income"] for c in columns),
    }


@router.get("/job-profitability")
def job_profitability_report(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    customer_id: int = Query(default=None),
    db: Session = Depends(get_db),
):
    """Income, costs and margin per job from posted lines.

    A line's job is its own job_id, else its transaction's; untagged
    activity is the "No job" row, so the totals equal the plain Profit &
    Loss for the same period (the same reconciliation promise as P&L by
    Class).
    """
    from app.services.jobs_service import job_profitability

    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()
    rows = job_profitability(db, start_date, end_date, customer_id=customer_id)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "jobs": rows,
        "total_income": sum(r["income"] for r in rows),
        "total_costs": sum(r["total_costs"] for r in rows),
        "total_net_income": sum(r["net_income"] for r in rows),
    }


# ── Financial-report PDFs ────────────────────────────────────────────────
# One shared renderer (report_pdf.html + _report_theme.html) with the
# pdf_paper_size setting (letter default, a4 selectable). The statements
# pack bundles P&L + Balance Sheet + Trial Balance into one document.


def _money(value) -> str:
    amount = Decimal(str(value or 0))
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _pl_section(data: dict, t=None) -> dict:
    """P&L rows for the PDF; t (Terms) picks the company's words —
    "Statement of Activities" / "Revenue & Support" for a nonprofit."""
    t = t or Terms()
    rows = []
    for label, key, total_key in (
        (t("Income"), "income", "total_income"),
        ("Cost of Goods Sold", "cogs", "total_cogs"),
    ):
        rows.append({"cells": [label, ""], "style": "subtotal"})
        for item in data[key]:
            rows.append(
                {"cells": [f"  {item['account_name']}", _money(item["amount"])]}
            )
        rows.append(
            {"cells": [f"Total {label}", _money(data[total_key])], "style": "subtotal"}
        )
    rows.append(
        {"cells": ["Gross Profit", _money(data["gross_profit"])], "style": "subtotal"}
    )
    rows.append({"cells": ["Expenses", ""], "style": "subtotal"})
    for item in data["expenses"]:
        rows.append({"cells": [f"  {item['account_name']}", _money(item["amount"])]})
    rows.append(
        {
            "cells": ["Total Expenses", _money(data["total_expenses"])],
            "style": "subtotal",
        }
    )
    rows.append(
        {"cells": [t("Net Income"), _money(data["net_income"])], "style": "grand-total"}
    )
    return {
        "title": t("Profit & Loss"),
        "period": f"{data['start_date']} — {data['end_date']}",
        "columns": ["", "Amount"],
        "rows": rows,
    }


def _bs_section(data: dict, t=None) -> dict:
    t = t or Terms()
    rows = []
    for label, key, total_key in (
        ("Assets", "assets", "total_assets"),
        ("Liabilities", "liabilities", "total_liabilities"),
        (t("Equity"), "equity", "total_equity"),
    ):
        rows.append({"cells": [label, ""], "style": "subtotal"})
        for item in data[key]:
            rows.append(
                {"cells": [f"  {item['account_name']}", _money(item["amount"])]}
            )
        rows.append(
            {"cells": [f"Total {label}", _money(data[total_key])], "style": "subtotal"}
        )
    rows.append(
        {
            "cells": [
                t("Liabilities + Equity"),
                _money(data["total_liabilities"] + data["total_equity"]),
            ],
            "style": "grand-total",
        }
    )
    return {
        "title": t("Balance Sheet"),
        "period": f"As of {data['as_of_date']}",
        "columns": ["", "Amount"],
        "rows": rows,
    }


def _tb_section(data: dict) -> dict:
    rows = [
        {
            "cells": [
                f"{item['account_number']} {item['account_name']}".strip(),
                _money(item["total_debit"]),
                _money(item["total_credit"]),
            ]
        }
        for item in data["items"]
    ]
    rows.append(
        {
            "cells": [
                "Total",
                _money(data["total_debit"]),
                _money(data["total_credit"]),
            ],
            "style": "grand-total",
        }
    )
    return {
        "title": "Trial Balance",
        "period": f"{data['start_date']} — {data['end_date']}",
        "columns": ["Account", "Debit", "Credit"],
        "rows": rows,
    }


def _pdf_response(sections, db, filename: str):
    from fastapi.responses import Response
    from app.services.pdf_service import generate_report_pdf
    from app.services.settings_service import get_all_settings

    pdf_bytes = generate_report_pdf(sections, get_all_settings(db))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/profit-loss/pdf")
def profit_loss_pdf(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    data = profit_loss(start_date, end_date, db)
    t = terms_from_db(db)
    return _pdf_response(
        [_pl_section(data, t)],
        db,
        f"{t.slug('Profit & Loss')}_{data['start_date']}_{data['end_date']}.pdf",
    )


@router.get("/balance-sheet/pdf")
def balance_sheet_pdf(
    as_of_date: date = Query(default=None), db: Session = Depends(get_db)
):
    data = balance_sheet(as_of_date, db)
    t = terms_from_db(db)
    return _pdf_response(
        [_bs_section(data, t)],
        db,
        f"{t.slug('Balance Sheet')}_{data['as_of_date']}.pdf",
    )


@router.get("/financial-statements/pdf")
def financial_statements_pdf(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """The statements pack: P&L, Balance Sheet, and Trial Balance in one
    audit-ready document (each statement on its own page)."""
    pl = profit_loss(start_date, end_date, db)
    bs = balance_sheet(date.fromisoformat(pl["end_date"]), db)
    tb = trial_balance(
        date.fromisoformat(pl["start_date"]), date.fromisoformat(pl["end_date"]), db
    )
    t = terms_from_db(db)
    if t.is_nonprofit:
        from app.routes.reports.nonprofit import nonprofit_statement_sections

        sections = nonprofit_statement_sections(
            db, date.fromisoformat(pl["start_date"]), date.fromisoformat(pl["end_date"])
        ) + [_tb_section(tb)]
    else:
        sections = [_pl_section(pl, t), _bs_section(bs, t), _tb_section(tb)]
    return _pdf_response(
        sections, db, f"financial-statements_{pl['start_date']}_{pl['end_date']}.pdf"
    )
