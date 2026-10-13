# ============================================================================
# Database layer — SQLAlchemy engine + session factory.
# PostgreSQL in server mode, per-company SQLite files in desktop mode.
# ============================================================================

import os
import re
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
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
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
# These POSTs change control-plane review metadata, never the entity ledger.
_LEDGER_REVIEW_POST = re.compile(
    r"^/api/ark-cpa/candidates/[0-9]+/(evaluate|controller-run)$"
)
_CANDIDATE_LEDGER_PATH = re.compile(
    r"^/api/ark-cpa/candidates/([0-9]+)/(evaluate|controller-run|post)$"
)


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


def validate_browser_context(request: HTTPConnection | None) -> None:
    """An old tab must not submit work as a newly signed-in human."""
    if request is None:
        return
    expected = request.headers.get("x-ark-principal")
    query_expected = request.query_params.get("_ark_principal")
    if len(request.query_params.getlist("_ark_principal")) > 1:
        raise HTTPException(status_code=400, detail="Ambiguous sign-in context")
    if expected and query_expected and expected != query_expected:
        raise HTTPException(status_code=409, detail="Sign-in context changed")
    expected = expected or query_expected
    if expected and expected != _request_username(request):
        raise HTTPException(
            status_code=409,
            detail="Sign-in changed; reopen this page before continuing",
        )


def _entity_database_for_request(request: HTTPConnection | None) -> str | None:
    """Resolve an authorized entity; never trust the cookie/header alone."""
    if not entity_mode_enabled() or request is None:
        return None
    username = _request_username(request)
    if not username:
        raise HTTPException(
            status_code=401, detail="An authorized entity principal is required"
        )
    validate_browser_context(request)

    from app.models.api_tokens import ApiToken
    from app.models.arkcpa import (
        ArkAgentGrant,
        ArkEntity,
        ArkEntityAccess,
        ArkPostingCandidate,
    )
    from app.models.users import User

    requested = request.headers.get("x-ark-entity")
    query_entity = request.query_params.get("_ark_entity")
    method = request.scope.get("method", "GET")
    if len(request.query_params.getlist("_ark_entity")) > 1:
        raise HTTPException(status_code=400, detail="Ambiguous company context")
    if requested and query_entity and requested != query_entity:
        raise HTTPException(status_code=409, detail="Company context changed")
    # Native PDF/download navigation cannot set headers. Only safe methods
    # accept URL context; mutations require an explicit entity header.
    if method in _READ_METHODS:
        requested = requested or query_entity
    elif not requested:
        raise HTTPException(
            status_code=400,
            detail="Ledger changes require an explicit X-Ark-Entity header",
        )
    request_session = request.scope.get("session")
    if not requested and isinstance(request_session, dict):
        requested = request_session.get("active_entity_slug")

    control = SessionLocal()
    try:
        if username.startswith("token:"):
            if not request.headers.get("x-ark-entity"):
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
        entity, access = row
        if not username.startswith("token:") and access.role not in {
            "admin",
            "bookkeeper",
            "readonly",
        }:
            raise HTTPException(status_code=403, detail="Invalid entity access role")
        if request.url.path.startswith("/api/ark-cpa/"):
            entity_id = request.query_params.get("entity_id")
            if entity_id is not None and entity_id != str(entity.id):
                raise HTTPException(
                    status_code=409,
                    detail="Activate the requested entity before ledger review",
                )
            candidate_path = _CANDIDATE_LEDGER_PATH.fullmatch(request.url.path)
            if candidate_path:
                candidate = control.get(
                    ArkPostingCandidate, int(candidate_path.group(1))
                )
                if candidate is not None and candidate.entity_id != entity.id:
                    raise HTTPException(
                        status_code=409,
                        detail="Activate the candidate's entity before ledger review",
                    )
        method = request.scope.get("method", "GET")
        if not username.startswith("token:"):
            from app.main import _role_allows

            if not _role_allows(access.role, method, request.url.path):
                raise HTTPException(
                    status_code=403,
                    detail="Your entity role does not allow this action",
                )
            # Legacy route serializers read the session role to redact HR data.
            # Narrow that request-local view; middleware reloads global authority
            # from the user table on the next request (including control routes).
            levels = {"readonly": 0, "bookkeeper": 1, "admin": 2}
            if (
                isinstance(request_session, dict)
                and request_session.get("role") in levels
            ):
                request_session["role"] = min(
                    (request_session["role"], access.role), key=levels.get
                )
        review_only = method == "POST" and bool(
            _LEDGER_REVIEW_POST.fullmatch(request.url.path)
        )
        ledger_write = method not in _READ_METHODS and not review_only
        role_readonly = not username.startswith("token:") and access.role == "readonly"
        frozen = entity.posting_mode == "frozen" or entity.facts_status == "hold"
        if ledger_write and role_readonly:
            raise HTTPException(status_code=403, detail="Read-only entity access")
        if ledger_write and frozen:
            raise HTTPException(
                status_code=409, detail="Entity ledger is frozen or on hold"
            )
        # Safe-method and review dependencies are read-only at the database
        # boundary too, preventing accidental writes behind a GET/review path.
        request.state.ark_ledger_readonly = not ledger_write or role_readonly or frozen
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
    validate_browser_context(request)
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
    if database_name is not None and request.state.ark_ledger_readonly:
        _restrict_ledger_session(db)
    _stamp_actor(db, request)
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _restrict_ledger_session(db):
    """Keep reviewed/read-only dependencies read-only across every transaction."""

    def prohibit_flush(session, _context, _instances):
        if session.new or session.dirty or session.deleted:
            raise HTTPException(
                status_code=403, detail="This ledger request is read-only"
            )

    def read_only_transaction(session, transaction, connection):
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))

    event.listen(db, "before_flush", prohibit_flush)
    event.listen(db, "after_begin", read_only_transaction)
