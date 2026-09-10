# ============================================================================
# Database layer — SQLAlchemy engine + session factory.
# PostgreSQL in server mode, per-company SQLite files in desktop mode.
# ============================================================================

import os
import re
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.requests import HTTPConnection

from app.config import DATABASE_URL

# Pool tuning rationale (Phase 9.6 perf pass):
#   pool_size=10        small base pool; most requests are short-lived
#   max_overflow=20     burst capacity when analytics + concurrent users hit
#   pool_recycle=1800   recycle every 30 min to avoid stale TCP idle kills
#   pool_pre_ping=True  cheap SELECT 1 before each checkout; catches dead conns
#   pool_use_lifo=True  reuse hottest conn first -> better CPU cache locality
# SQLite URLs skip pool_size/max_overflow since SQLite uses a different strategy.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_engine_kwargs = dict(pool_pre_ping=True)
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_use_lifo=True,
    )
engine = create_engine(DATABASE_URL, **_engine_kwargs)


def enable_sqlite_tuning(target_engine) -> None:
    """Concurrency PRAGMAs for SQLite engines (Server Edition groundwork).

    WAL lets readers proceed while one writer commits — the difference
    between "works for an office" and "database is locked" the moment a
    second person opens a report mid-save. busy_timeout makes brief lock
    contention wait instead of erroring; NORMAL sync is the recommended
    pairing with WAL. Harmless no-ops on :memory: databases.
    """
    from sqlalchemy import event

    @event.listens_for(target_engine, "connect")
    def _tune(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


if _is_sqlite:
    enable_sqlite_tuning(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_ENTITY_DATABASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_entity_sessions: dict[str, sessionmaker] = {}
_entity_sessions_lock = Lock()


def entity_mode_enabled() -> bool:
    """Whether authenticated accounting requests route to entity databases."""
    return os.getenv("ARKCPA_ENTITY_MODE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def entity_database_url(database_name: str) -> str:
    """Build a sibling PostgreSQL URL after strict identifier validation."""
    if not _ENTITY_DATABASE_RE.fullmatch(database_name or ""):
        raise ValueError("Invalid Ark CPA entity database name")
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Ark CPA entity databases require PostgreSQL")
    return url.set(database=database_name).render_as_string(hide_password=False)


def entity_session_factory(database_name: str):
    """Return a small, cached session factory for one governed entity ledger."""
    if database_name in _entity_sessions:
        return _entity_sessions[database_name]
    with _entity_sessions_lock:
        if database_name in _entity_sessions:
            return _entity_sessions[database_name]
        entity_engine = create_engine(
            entity_database_url(database_name),
            pool_pre_ping=True,
            pool_size=4,
            max_overflow=8,
            pool_recycle=1800,
            pool_use_lifo=True,
        )
        factory = sessionmaker(autocommit=False, autoflush=False, bind=entity_engine)
        _entity_sessions[database_name] = factory
        return factory


def _request_username(request: HTTPConnection | None) -> str | None:
    if request is None:
        return None
    try:
        session = getattr(request, "session", None)
        if isinstance(session, dict) and session.get("authenticated") is True:
            return session.get("username") or None
        principal = getattr(getattr(request, "state", None), "token_principal", None)
        if isinstance(principal, dict):
            return principal.get("username") or None
    except Exception:
        return None
    return None


def _entity_database_for_request(request: HTTPConnection | None) -> str | None:
    """Resolve an authorized entity; never trust the cookie/header alone."""
    if not entity_mode_enabled() or request is None or _is_sqlite:
        return None
    username = _request_username(request)
    if not username:
        return None

    from app.models.api_tokens import ApiToken
    from app.models.arkcpa import ArkAgentGrant, ArkEntity, ArkEntityAccess
    from app.models.users import User

    requested = request.headers.get("x-ark-entity")
    request_session = request.scope.get("session")
    if not requested and isinstance(request_session, dict):
        requested = request_session.get("active_entity_slug")

    control = SessionLocal()
    try:
        if username.startswith("token:"):
            if not requested:
                raise HTTPException(
                    status_code=400,
                    detail="Agent requests require an explicit X-Ark-Entity header",
                )
            label = username.removeprefix("token:")
            query = (
                control.query(ArkEntity, ArkAgentGrant)
                .join(ArkAgentGrant, ArkAgentGrant.entity_id == ArkEntity.id)
                .join(ApiToken, ApiToken.id == ArkAgentGrant.token_id)
                .filter(
                    ApiToken.label == label,
                    ApiToken.is_active,
                    ArkAgentGrant.is_active,
                    ArkEntity.status != "archived",
                )
            )
        else:
            query = (
                control.query(ArkEntity, ArkEntityAccess)
                .join(ArkEntityAccess, ArkEntityAccess.entity_id == ArkEntity.id)
                .join(User, User.id == ArkEntityAccess.user_id)
                .filter(
                    User.username == username,
                    User.is_active,
                    ArkEntity.status != "archived",
                )
            )
        row = None
        if requested:
            row = query.filter(ArkEntity.slug == requested).first()
            if row is None:
                raise HTTPException(
                    status_code=403,
                    detail="Principal is not granted to the requested Ark CPA entity",
                )
        else:
            if username.startswith("token:"):
                row = query.order_by(ArkEntity.id).first()
            else:
                row = query.order_by(
                    ArkEntityAccess.is_default.desc(), ArkEntity.id
                ).first()
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="No Ark CPA legal entity is configured for this account",
            )
        entity, _access = row
        if isinstance(request_session, dict):
            request_session["active_entity_id"] = entity.id
            request_session["active_entity_slug"] = entity.slug
            request_session["active_entity_name"] = entity.name
        return entity.database_name
    finally:
        control.close()


def _stamp_actor(db, request: HTTPConnection | None) -> None:
    username = _request_username(request)
    if username:
        db.info["acting_username"] = username


def get_control_db(request: HTTPConnection = None):
    """Request-scoped session for identity and Ark CPA control-plane state."""
    db = SessionLocal()
    _stamp_actor(db, request)
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db(request: HTTPConnection = None):
    """Request-scoped DB session (FastAPI dependency).

    When FastAPI injects the request, the acting username is stamped onto
    the Session's own info dict — the audit hooks receive this exact
    object at flush time, so attribution travels WITH the session instead
    of relying on contextvar propagation (which proved unreliable on the
    frozen Windows runtime; the contextvar remains as a fallback for
    sessions created outside the request cycle)."""
    database_name = _entity_database_for_request(request)
    db = (
        entity_session_factory(database_name)()
        if database_name is not None
        else SessionLocal()
    )
    _stamp_actor(db, request)
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
