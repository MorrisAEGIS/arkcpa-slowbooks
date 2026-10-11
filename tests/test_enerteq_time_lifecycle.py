"""Approved time may cost a job; corrections reverse before editing."""

from datetime import date

import pytest

from app.models.payroll import PayRun
from app.models.settings import Settings
from app.models.time_entries import TimeEntry
from app.models.transactions import Transaction


@pytest.fixture
def ticket(client, seed_accounts, seed_customer):
    assert client.post("/api/cost-types/setup-offsets").status_code == 200
    job = client.post(
        "/api/jobs",
        json={"customer_id": seed_customer.id, "name": "Synthetic Enerteq job"},
    ).json()
    employee = client.post(
        "/api/employees",
        json={
            "first_name": "Synthetic",
            "last_name": "Worker",
            "pay_type": "hourly",
            "pay_rate": 30,
        },
    ).json()
    response = client.post(
        "/api/time-entries",
        json={
            "employee_id": employee["id"],
            "job_id": job["id"],
            "date": "2026-10-10",
            "hours_regular": 8,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, ticket):
    response = client.post(
        f"/api/time-entries/{ticket['id']}/approve",
        json={"approved_by": "synthetic-reviewer"},
    )
    assert response.status_code == 200, response.text


def post(client, ticket):
    approve(client, ticket)
    response = client.post(f"/api/time-entries/{ticket['id']}/post-to-job")
    assert response.status_code == 200, response.text
    return response.json()


def test_submitted_time_cannot_post_individually_or_in_bulk(client, db_session, ticket):
    assert client.post(f"/api/time-entries/{ticket['id']}/submit").status_code == 200
    assert (
        client.post(f"/api/time-entries/{ticket['id']}/post-to-job").status_code == 400
    )
    batch = client.post("/api/time-entries/post-to-job", json={"ids": [ticket["id"]]})
    assert batch.json()["posted"] == 0
    assert db_session.query(Transaction).count() == 0


def test_costed_time_locks_edit_delete_and_resubmit_until_reversed(
    client, db_session, ticket
):
    result = post(client, ticket)
    assert result["total"] == 240
    before = db_session.query(Transaction).count()
    assert (
        client.put(
            f"/api/time-entries/{ticket['id']}", json={"hours_regular": 10}
        ).status_code
        == 409
    )
    assert client.delete(f"/api/time-entries/{ticket['id']}").status_code == 409
    assert client.post(f"/api/time-entries/{ticket['id']}/submit").status_code == 409
    assert (
        client.post(f"/api/time-entries/{ticket['id']}/post-to-job").status_code == 400
    )
    db_session.expire_all()
    assert db_session.query(Transaction).count() == before
    assert db_session.get(TimeEntry, ticket["id"]).hours_regular == 8
    rejected = client.post(f"/api/time-entries/{ticket['id']}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["job_cost_id"] is None
    edit = client.put(f"/api/time-entries/{ticket['id']}", json={"hours_regular": 10})
    assert edit.status_code == 200 and edit.json()["status"] == "draft"
    revised = post(client, ticket)
    assert revised["total"] == 300 and revised["job_cost_id"] != result["job_cost_id"]
    summary = client.get(f"/api/jobs/{ticket['job_id']}").json()["summary"]
    assert summary["total_costs"] == 300


def test_editing_approved_uncosted_time_requires_fresh_approval(client, ticket):
    approve(client, ticket)
    stored = next(
        row
        for row in client.get("/api/time-entries").json()
        if row["id"] == ticket["id"]
    )
    assert stored["approved_by"] and stored["approved_by"] != "synthetic-reviewer"
    response = client.put(
        f"/api/time-entries/{ticket['id']}", json={"hours_regular": 9}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert (
        response.json()["approved_by"] is None
        and response.json()["approved_at"] is None
    )
    assert (
        client.put(
            f"/api/time-entries/{ticket['id']}", json={"status": "approved"}
        ).status_code
        == 409
    )
    assert (
        client.post(f"/api/time-entries/{ticket['id']}/post-to-job").status_code == 400
    )


def test_payroll_lock_blocks_all_time_changes_and_direct_cost_void(
    client, db_session, ticket
):
    result = post(client, ticket)
    run = PayRun(
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 14),
        pay_date=date(2026, 10, 16),
    )
    db_session.add(run)
    db_session.flush()
    db_session.get(TimeEntry, ticket["id"]).pay_run_id = run.id
    db_session.commit()
    for action in ("submit", "reject"):
        assert (
            client.post(f"/api/time-entries/{ticket['id']}/{action}").status_code == 400
        )
    assert (
        client.post(
            f"/api/time-entries/{ticket['id']}/approve", json={"approved_by": "another"}
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/time-entries/{ticket['id']}", json={"hours_regular": 10}
        ).status_code
        == 400
    )
    assert client.delete(f"/api/time-entries/{ticket['id']}").status_code == 400
    assert (
        client.post(f"/api/job-costs/{result['job_cost_id']}/void").status_code == 400
    )
    db_session.expire_all()
    assert db_session.get(TimeEntry, ticket["id"]).job_cost_id == result["job_cost_id"]


def test_closed_period_prevents_rejecting_posted_time(client, db_session, ticket):
    result = post(client, ticket)
    db_session.add(Settings(key="closing_date", value="2026-10-10"))
    db_session.commit()
    response = client.post(f"/api/time-entries/{ticket['id']}/reject")
    assert response.status_code == 403, response.text
    db_session.expire_all()
    assert db_session.get(TimeEntry, ticket["id"]).job_cost_id == result["job_cost_id"]
