"""Real PostgreSQL migration/concurrency acceptance on a disposable loopback DB.

Run only with TEST_AUTHORITY_DATABASE_URL=postgresql://.../authority_test_...
The harness creates a private network namespace and disposable database.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
from threading import Barrier
from types import SimpleNamespace
from datetime import date

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.arkcpa import (
    ArkAuthorityRetrieval,
    ArkAuthorityReview,
    ArkAuthorityPassage,
    ArkSourceSnapshot,
)
from app.services import arkcpa_tax as tax
from tests.test_arkcpa_tax import research_source, BODY, public_dns  # noqa: F401


@pytest.fixture(scope="module")
def pg_engine():
    url = os.getenv("TEST_AUTHORITY_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL URL not supplied")
    engine = create_engine(url)
    assert engine.url.host == "127.0.0.1" and engine.url.database.startswith(
        "authority_test_"
    )
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["database_url"] = url
    assert not engine.dialect.has_table(
        engine.connect(), "alembic_version"
    ), "Database must be fresh"
    command.upgrade(cfg, "f9a0b1c2d3e4")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ark_authority_sources(id,code,title,jurisdiction,authority_level,url,enabled,created_at) VALUES(9000,'LEGACY','Preserve title','Canada','law','https://www.canada.ca/legacy',true,now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ark_source_snapshots(id,source_id,content_hash,retrieved_at,status,snapshot_metadata) VALUES(9000,9000,:hash,now(),'current',:metadata)"
            ),
            {"hash": "a" * 64, "metadata": '{"legacy":true}'},
        )
    command.upgrade(cfg, "head")
    assert ScriptDirectory.from_config(cfg).get_current_head() == "aa1b2c3d4e5f"
    yield engine
    engine.dispose()


def test_migration_preserves_legacy_and_grants_nothing(pg_engine):
    with pg_engine.connect() as conn:
        row = conn.execute(
            text("SELECT title,capture_rights FROM ark_authority_sources WHERE id=9000")
        ).one()
        assert tuple(row) == ("Preserve title", "unreviewed")
        row = conn.execute(
            text(
                "SELECT status,content,extracted_text,snapshot_metadata FROM ark_source_snapshots WHERE id=9000"
            )
        ).one()
        assert tuple(row[:3]) == ("current", None, None) and row[3] == {"legacy": True}
        assert (
            conn.execute(text("SELECT count(*) FROM ark_research_permissions")).scalar()
            == 0
        )
    with sessionmaker(bind=pg_engine)() as db:
        assert (
            tax.authority_context(
                db,
                entity=SimpleNamespace(
                    jurisdiction="Canada", entity_type="ca_personal"
                ),
                transaction_date=date(2026, 5, 1),
            )["citations"]
            == []
        )


def test_database_prevents_update_delete_and_truncate(pg_engine):
    with sessionmaker(bind=pg_engine)() as db:
        event = ArkAuthorityRetrieval(
            attempt_id="immutable-test",
            source_id=9000,
            snapshot_id=9000,
            event="succeeded",
            requested_by="synthetic",
            source_definition={},
            detail={},
        )
        review = ArkAuthorityReview(
            snapshot_id=9000,
            reviewed_by="synthetic",
            decision="rejected",
            rationale="Fixture",
            definition_hash="b" * 64,
            applicability={},
        )
        db.add_all([event, review])
        db.flush()
        db.add(
            ArkAuthorityPassage(
                review_id=review.id,
                locator="fixture",
                text="Synthetic evidence only",
                content_hash="c" * 64,
            )
        )
        db.commit()
    for table in (
        "ark_source_snapshots",
        "ark_authority_retrievals",
        "ark_authority_reviews",
        "ark_authority_passages",
    ):
        for sql in (
            f"UPDATE {table} SET id=id",
            f"DELETE FROM {table}",
            f"TRUNCATE {table} CASCADE",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                with pg_engine.begin() as conn:
                    conn.execute(text(sql))
    with pytest.raises(DBAPIError, match="append-only|foreign key"):
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM ark_authority_sources WHERE id=9000"))


def test_concurrent_identical_refresh_keeps_two_attempts_one_capture(pg_engine):
    factory = sessionmaker(bind=pg_engine)
    with factory() as db:
        source = research_source(db)
        source_id = source.id
    barrier = Barrier(2)

    def one():
        def handler(request):
            barrier.wait(timeout=5)
            return httpx.Response(
                200, content=BODY, headers={"content-type": "text/html"}
            )

        from app.models.arkcpa import ArkAuthoritySource

        with factory() as db:
            return tax.refresh_authority_source(
                db,
                db.get(ArkAuthoritySource, source_id),
                requested_by="researcher",
                transport=httpx.MockTransport(handler),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(one)
        second = pool.submit(one)
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(r["status"] for r in results) == ["review_required", "unchanged"]
    assert len({r["snapshot_id"] for r in results}) == 1
    assert len({r["attempt_id"] for r in results}) == 2
    with factory() as db:
        assert db.query(ArkSourceSnapshot).filter_by(source_id=source_id).count() == 1
        assert (
            db.query(ArkAuthorityRetrieval).filter_by(source_id=source_id).count() == 4
        )


def test_sqlite_upgrade_enforces_immutable_history(tmp_path):
    import sqlite3

    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "authority.sqlite"
    cfg = Config(str(root / "alembic.ini"))
    cfg.attributes["database_url"] = "sqlite:///" + str(database)
    command.upgrade(cfg, "head")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO ark_authority_sources(id,code,title,jurisdiction,authority_level,url,enabled,created_at) VALUES(1,'SYNTHETIC','Synthetic','Canada','law','https://www.canada.ca/synthetic',1,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO ark_source_snapshots(id,source_id,content_hash,retrieved_at,status,snapshot_metadata) VALUES(1,1,?,CURRENT_TIMESTAMP,'proposed','{}')",
            ("a" * 64,),
        )
        conn.commit()
        assert conn.execute(
            "SELECT capture_rights,allow_model_context FROM ark_authority_sources"
        ).fetchone() == ("unreviewed", 0)
        for sql in (
            "UPDATE ark_source_snapshots SET status='current'",
            "DELETE FROM ark_source_snapshots",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        assert conn.execute("SELECT status FROM ark_source_snapshots").fetchone() == (
            "proposed",
        )
        assert conn.execute(
            "SELECT count(*) FROM ark_research_permissions"
        ).fetchone() == (0,)
