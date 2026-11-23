"""FastAPI: autentifikatsiya, buyruqlar, ma'lumotnoma va hisobotlar."""
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from backend.balances.rebuild import verify
from backend.cache.redis_impl import RedisBackend
from backend.core.clock import SystemClock
from backend.core.config import Settings
from backend.core.errors import DomainError
from backend.core.timing import local_date
from backend.ledger.db import Database, DatabaseProvider
from backend.ledger.journal import Journal
from backend.modules.auth.actor import Actor
from backend.modules.auth.permissions import Permissions
from backend.modules.auth.service import AuthService
from backend.modules.auth.tokens import TokenService
from backend.modules.sync.engine import Engine
from backend.modules.sync.registry import Command
from backend.modules.reference.queries import snapshot as reference_snapshot
from backend.modules.reporting import queries as reports

bearer = HTTPBearer(auto_error=False)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=120)
    device_label: str | None = Field(default=None, max_length=120)


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=120)
    payload: dict
    device_time: str = Field(min_length=1, max_length=64)
    est_server_time: str | None = Field(default=None, max_length=64)
    gps: dict | None = None

    def to_command(self) -> Command:
        return Command(**self.model_dump())


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[CommandBody] = Field(min_length=1, max_length=100)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: Settings = app.state.config_override or Settings.from_env()
    provider = app.state.provider_override or DatabaseProvider(
        settings.database_url, settings.pool_min, settings.pool_max)
    app.state.settings = settings
    app.state.db_provider = provider
    app.state.owns_db_provider = app.state.provider_override is None
    app.state.redis_backend = RedisBackend.from_url(settings.redis_url) if settings.redis_url else None
    try:
        yield
    finally:
        if app.state.redis_backend is not None:
            app.state.redis_backend.r.close()
        if app.state.owns_db_provider:
            provider.close()


def create_app(settings: Settings | None = None,
               provider: DatabaseProvider | None = None) -> FastAPI:
    app = FastAPI(
        title="TERA API",
        version="1.0.0",
        description="For everybody — token bilan kirgan barcha rollar; buyruqlarda amal ruxsati alohida tekshiriladi.",
        openapi_tags=[
            {"name": "System", "description": "Ochiq xizmat holati."},
            {"name": "Auth", "description": "Kirish va vazifa almashtirish."},
            {"name": "Commands", "description": "Buyruqlar; ruxsat buyruq turiga qarab tekshiriladi."},
            {"name": "References", "description": "Ma'lumotnoma nusxalari."},
            {"name": "Reports", "description": "Rolga qarab himoyalangan hisobotlar."},
        ],
        lifespan=_lifespan,
    )
    app.state.config_override = settings
    app.state.provider_override = provider

    def get_db(request: Request) -> Iterator[Database]:
        with request.app.state.db_provider.session() as db:
            yield db

    def auth_service(request: Request, db: Database) -> AuthService:
        clock = SystemClock()
        return AuthService(
            Journal(db, clock),
            TokenService(request.app.state.settings.secret, clock,
                         request.app.state.settings.token_ttl_seconds),
            Permissions.default(),
            limiter=request.app.state.redis_backend,
        )

    def current_actor(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        db: Database = Depends(get_db),
    ) -> Actor:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "Bearer token talab qilinadi",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            return auth_service(request, db).authenticate(credentials.credentials)
        except DomainError as exc:
            raise HTTPException(401, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.get(
        "/health",
        tags=["System"],
        summary="For everybody — health check",
        description="For everybody. No authentication required.",
    )
    def health(db: Database = Depends(get_db)) -> dict:
        row = db.one("SELECT 1 AS ok")
        return {"status": "ok" if row and row["ok"] == 1 else "error"}

    @app.post(
        "/api/auth/login",
        tags=["Auth"],
        summary="For everybody — login",
        description="For everybody. No authentication required.",
    )
    def login(body: LoginBody, request: Request, db: Database = Depends(get_db)) -> dict:
        service = auth_service(request, db)
        try:
            result = service.login(body.login, body.password, body.device_id, body.device_label,
                                   request.client.host if request.client else None)
        except DomainError as exc:
            status = 401 if exc.code == "invalid_credentials" else 423 if exc.code == "account_locked" else 429 \
                if exc.code == "rate_limited" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return {"access_token": result.token, "token_type": "bearer",
                "user_id": result.user.user_id, "task": result.task}

    @app.post(
        "/api/auth/switch-task",
        tags=["Auth"],
        summary="For everybody (valid token) — switch task",
        description="For everybody with a valid bearer token. Users can switch only to a task assigned to their account.",
    )
    def switch_task(body: dict, request: Request, actor: Actor = Depends(current_actor),
                    db: Database = Depends(get_db)) -> dict:
        task = body.get("task")
        if not isinstance(task, str) or not task.strip():
            raise HTTPException(422, "task maydoni kiritilishi kerak")
        service = auth_service(request, db)
        try:
            token = service.switch_task(actor, task.strip())
        except DomainError as exc:
            raise HTTPException(403, detail={"code": exc.code, "message": str(exc),
                                             "details": exc.details}) from exc
        return {"access_token": token, "token_type": "bearer", "task": task.strip()}

    @app.post(
        "/api/commands",
        tags=["Commands"],
        summary="For everybody (valid token) — execute one command",
        description="For everybody with a valid bearer token. Permission for the requested command is checked by role.",
    )
    def execute_command(body: CommandBody, actor: Actor = Depends(current_actor),
                        db: Database = Depends(get_db)) -> dict:
        result = Engine(Journal(db), Permissions.default()).execute(body.to_command(), actor)
        return result.to_json()

    @app.post(
        "/api/commands/batch",
        tags=["Commands"],
        summary="For everybody (valid token) — execute command batch",
        description="For everybody with a valid bearer token. Each command is authorized separately by role.",
    )
    def execute_batch(body: BatchBody, actor: Actor = Depends(current_actor),
                      db: Database = Depends(get_db)) -> dict:
        results = Engine(Journal(db), Permissions.default()).execute_batch(
            [item.to_command() for item in body.commands], actor)
        return {"results": [result.to_json() for result in results]}

    @app.get(
        "/api/reference/snapshot",
        tags=["References"],
        summary="For everybody (valid token) — reference snapshot",
        description="For everybody with a valid bearer token. Agents only receive data for their assigned stores.",
    )
    def get_reference_snapshot(actor: Actor = Depends(current_actor),
                               db: Database = Depends(get_db)) -> dict:
        journal = Journal(db)
        agent_id = actor.user_id if actor.task == "agent" else None
        return reference_snapshot(journal, Engine(journal).current_policy(), agent_id)

    @app.get(
        "/api/reports/sales",
        tags=["Reports"],
        summary="Allowed: owner, accountant, agent — sales report",
        description="Allowed roles: owner, accountant, agent. Agents are limited to their assigned stores.",
    )
    def sales_report(
        from_date: date,
        through_date: date,
        store_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        _validate_period(from_date, through_date)
        store_ids = _agent_store_ids(db, actor) if actor.task == "agent" else None
        if store_ids is not None and store_id is not None and store_id not in store_ids:
            raise HTTPException(403, "Bu do'kon bo'yicha hisobot olishga ruxsat yo'q")
        if store_id is not None:
            store_ids = {store_id}
        return reports.sales_summary(Journal(db).docs, from_date.isoformat(),
                                     through_date.isoformat(), store_ids)

    @app.get(
        "/api/reports/debts",
        tags=["Reports"],
        summary="Allowed: owner, accountant, agent — debt report",
        description="Allowed roles: owner, accountant, agent. Agents are limited to their assigned stores.",
    )
    def debt_report(
        as_of: date | None = None,
        store_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        store_ids = _agent_store_ids(db, actor) if actor.task == "agent" else None
        if store_ids is not None and store_id is not None and store_id not in store_ids:
            raise HTTPException(403, "Bu do'kon qarzi bo'yicha hisobot olishga ruxsat yo'q")
        if store_id is not None:
            store_ids = {store_id}
        report_date = as_of or local_date(SystemClock().now())
        return reports.debt_report(Journal(db).bal, Journal(db).docs,
                                   report_date.isoformat(), store_ids)

    @app.get(
        "/api/reports/inventory",
        tags=["Reports"],
        summary="Allowed: owner, warehouse, agent — inventory report",
        description="Allowed roles: owner, warehouse, agent. Agents only see inventory in their custody.",
    )
    def inventory_report(actor: Actor = Depends(current_actor),
                         db: Database = Depends(get_db)) -> dict:
        _require_roles(actor, {"owner", "warehouse", "agent"})
        agent_id = actor.user_id if actor.task == "agent" else None
        return reports.inventory_summary(Journal(db).bal, Journal(db).docs, agent_id)

    @app.get(
        "/api/reports/expiring",
        tags=["Reports"],
        summary="Allowed: owner, warehouse — expiring stock report",
        description="Allowed roles: owner, warehouse.",
    )
    def expiring_report(
        days: int = Query(default=14, ge=0, le=365),
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse"})
        from_date = local_date(SystemClock().now())
        through_date = from_date + timedelta(days=days)
        return {"from_date": from_date.isoformat(), "through_date": through_date.isoformat(),
                "batches": reports.expiring_inventory(
                    Journal(db).bal, Journal(db).docs, from_date.isoformat(), through_date.isoformat())}

    @app.get(
        "/api/reports/audit",
        tags=["Reports"],
        summary="Allowed: owner — audit report",
        description="Allowed roles: owner.",
    )
    def audit_report(actor: Actor = Depends(current_actor),
                     db: Database = Depends(get_db)) -> dict:
        _require_roles(actor, {"owner"})
        report = verify(db)
        return {
            "ok": report.ok,
            "events": report.events,
            "chain": {"ok": report.chain.ok, "broken_seq": report.chain.broken_seq,
                      "reason": report.chain.reason},
            "balance_diffs": report.balance_diffs,
            "doc_diffs": report.doc_diffs,
        }

    return app


def _require_roles(actor: Actor, allowed: set[str]) -> None:
    if actor.task not in allowed:
        raise HTTPException(403, "Bu hisobot sizning vazifangiz uchun ruxsat etilmagan")


def _agent_store_ids(db: Database, actor: Actor) -> set[str]:
    return {
        store.doc_id.split(":", 1)[1]
        for store in Journal(db).docs.query("store")
        if store.data.get("agent_id") == actor.user_id
    }


def _validate_period(from_date: date, through_date: date) -> None:
    if through_date < from_date:
        raise HTTPException(422, "through_date from_date dan oldin bo'lmasligi kerak")
    if (through_date - from_date).days > 366:
        raise HTTPException(422, "Hisobot oralig'i 366 kundan oshmasligi kerak")


app = create_app()
