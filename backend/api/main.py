"""FastAPI: autentifikatsiya, buyruqlar, ma'lumotnoma va hisobotlar."""
from contextlib import asynccontextmanager
from datetime import date, timedelta
import os
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from backend.balances.rebuild import verify
from backend.cache.redis_impl import RedisBackend
from backend.core.clock import SystemClock
from backend.core.config import Settings
from backend.core.errors import DomainError, Forbidden
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
from backend.modules.reference.queries import get as get_reference, list_all as list_references
from backend.modules.reporting import queries as reports
from backend.modules.sales.queries import (
    get_sale, list_sales, get_dispute, list_disputes, list_payments,
    get_payment, available_custody_batches,
)
from backend.modules.purchase.queries import (
    get_purchase, list_purchases, list_supplier_payments, supplier_balance,
)
from backend.modules.packaging.queries import get_batch, list_batches, shrink_summary
from backend.modules.warehouse.queries import (
    pending_loadings, get_stock_count, list_stock_counts, custody_inventory,
)
from backend.modules.returns.queries import (
    get_return, list_returns, get_exchange, list_writeoffs,
)
from backend.modules.closing.queries import (
    get_closing, list_closings, get_cash_handover, pending_cash_handovers,
    balance_snapshot,
)
from backend.modules.pricing.queries import history as price_history
from backend.modules.sync.inbox import Inbox

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


class BootstrapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    tasks: list[str] = Field(min_length=1, max_length=20)
    phone: str | None = Field(default=None, max_length=40)


class TasksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[str] = Field(min_length=1, max_length=20)


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


class ChangePasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class RevokeDeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=120)


def _user_json(user) -> dict:
    return {
        "user_id": user.user_id,
        "login": user.login,
        "full_name": user.full_name,
        "phone": user.phone,
        "tasks": user.tasks,
        "active": user.active,
    }


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
    allowed_origins = settings.allowed_origins if settings is not None else tuple(
        origin.strip().rstrip("/")
        for origin in (os.environ.get("TERA_ALLOWED_ORIGINS") or "").split(",")
        if origin.strip()
    )
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
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
        "/api/auth/bootstrap",
        tags=["Auth"],
        summary="For everybody — bootstrap the first owner",
    )
    def bootstrap_owner(body: BootstrapBody, request: Request,
                        db: Database = Depends(get_db)) -> dict:
        service = auth_service(request, db)
        try:
            user = service.bootstrap_owner(body.login, body.password, body.full_name, body.phone)
        except DomainError as exc:
            raise HTTPException(409 if exc.code == "already_initialized" else 400,
                                detail={"code": exc.code, "message": str(exc),
                                        "details": exc.details}) from exc
        return _user_json(user)

    @app.get(
        "/api/auth/users",
        tags=["Auth"],
        summary="Allowed: owner — list users",
    )
    def list_users(request: Request, actor: Actor = Depends(current_actor),
                   db: Database = Depends(get_db)) -> dict:
        service = auth_service(request, db)
        try:
            if not service.perms.allowed(actor.task, "user.manage"):
                raise Forbidden("forbidden", "Foydalanuvchilarni faqat egasi boshqaradi")
            users = service.users.all_users()
        except (DomainError, Forbidden) as exc:
            raise HTTPException(403, detail={"code": exc.code, "message": str(exc),
                                             "details": getattr(exc, "details", {})}) from exc
        return {"users": [_user_json(user) for user in users]}

    @app.post(
        "/api/auth/users",
        tags=["Auth"],
        summary="Allowed: owner — create user",
    )
    def create_user(body: CreateUserBody, request: Request, actor: Actor = Depends(current_actor),
                    db: Database = Depends(get_db)) -> dict:
        service = auth_service(request, db)
        try:
            user = service.create_user(actor, body.login, body.password, body.full_name,
                                       body.tasks, body.phone)
        except DomainError as exc:
            status = 403 if exc.code == "forbidden" else 409 if exc.code == "login_taken" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return _user_json(user)

    @app.patch(
        "/api/auth/users/{user_id}/tasks",
        tags=["Auth"],
        summary="Allowed: owner — change user tasks",
    )
    def set_user_tasks(user_id: str, body: TasksBody, request: Request,
                       actor: Actor = Depends(current_actor), db: Database = Depends(get_db)) -> dict:
        try:
            user = auth_service(request, db).set_tasks(actor, user_id, body.tasks)
        except DomainError as exc:
            status = 403 if exc.code in {"forbidden", "actor_not_allowed"} else 404 if exc.code == "not_found" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return _user_json(user)

    @app.patch(
        "/api/auth/users/{user_id}/active",
        tags=["Auth"],
        summary="Allowed: owner — activate or deactivate user",
    )
    def set_user_active(user_id: str, body: ActiveBody, request: Request,
                        actor: Actor = Depends(current_actor), db: Database = Depends(get_db)) -> dict:
        try:
            user = auth_service(request, db).set_active(actor, user_id, body.active)
        except DomainError as exc:
            status = 403 if exc.code in {"forbidden", "actor_not_allowed"} else 404 if exc.code == "not_found" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return _user_json(user)

    @app.post(
        "/api/auth/change-password",
        tags=["Auth"],
        summary="For valid user — change own password",
    )
    def change_password(body: ChangePasswordBody, request: Request,
                        actor: Actor = Depends(current_actor), db: Database = Depends(get_db)) -> dict:
        try:
            auth_service(request, db).change_password(actor.user_id, body.old_password, body.new_password)
        except DomainError as exc:
            status = 401 if exc.code == "invalid_credentials" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return {"status": "ok"}

    @app.post(
        "/api/auth/revoke-device",
        tags=["Auth"],
        summary="Allowed: owner — revoke device",
    )
    def revoke_device(body: RevokeDeviceBody, request: Request,
                      actor: Actor = Depends(current_actor), db: Database = Depends(get_db)) -> dict:
        try:
            auth_service(request, db).revoke_device(actor, body.device_id)
        except DomainError as exc:
            status = 403 if exc.code == "forbidden" else 404 if exc.code == "not_found" else 400
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc),
                                                "details": exc.details}) from exc
        return {"status": "ok"}

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
        "/api/references/{kind}",
        tags=["References"],
        summary="Allowed: all valid roles — list reference records",
    )
    def list_reference_records(
        kind: str,
        active_only: bool = True,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        if kind not in {"material", "packmaterial", "product", "supplier", "store"}:
            raise HTTPException(404, "Noma'lum ma'lumotnoma turi")
        records = list_references(Journal(db).docs, kind, active_only)
        if actor.task == "agent" and kind == "store":
            records = [record for record in records if record.get("agent_id") == actor.user_id]
        return {"kind": kind, "records": records}

    @app.get(
        "/api/references/{kind}/{reference_id}",
        tags=["References"],
        summary="Allowed: all valid roles — get reference record",
    )
    def get_reference_record(
        kind: str,
        reference_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        record = get_reference(Journal(db).docs, kind, reference_id)
        if record is None:
            raise HTTPException(404, "Ma'lumotnoma yozuvi topilmadi")
        if actor.task == "agent" and kind == "store" and record.get("agent_id") != actor.user_id:
            raise HTTPException(403, "Bu do'kon sizga biriktirilmagan")
        return record

    @app.get(
        "/api/sales",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — list sales",
    )
    def list_sale_records(
        store_id: str | None = None,
        status: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        store_ids = _agent_store_ids(db, actor) if actor.task == "agent" else None
        if store_id is not None:
            if store_ids is not None and store_id not in store_ids:
                raise HTTPException(403, "Bu do'kon bo'yicha sotuv ko'rishga ruxsat yo'q")
            store_ids = {store_id}
        docs = Journal(db).docs
        sales = list_sales(docs, store_id, status)
        if store_ids is not None:
            sales = [sale for sale in sales if sale["store_id"] in store_ids]
        return {"sales": sales}

    @app.get(
        "/api/sales/{sale_id}",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — get sale",
    )
    def get_sale_record(
        sale_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        sale = get_sale(Journal(db).docs, sale_id)
        if sale is None:
            raise HTTPException(404, "Sotuv topilmadi")
        if actor.task == "agent":
            store_ids = _agent_store_ids(db, actor)
            if sale["store_id"] not in store_ids:
                raise HTTPException(403, "Bu sotuv sizga biriktirilmagan")
        return sale

    @app.get(
        "/api/purchases",
        tags=["Purchases"],
        summary="Allowed: owner, accountant — list purchases",
    )
    def list_purchase_records(
        supplier_id: str | None = None,
        purchase_type: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant"})
        return {"purchases": list_purchases(Journal(db).docs, supplier_id, purchase_type)}

    @app.get(
        "/api/purchases/{purchase_id}",
        tags=["Purchases"],
        summary="Allowed: owner, accountant — get purchase",
    )
    def get_purchase_record(
        purchase_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant"})
        purchase = get_purchase(Journal(db).docs, purchase_id)
        if purchase is None:
            raise HTTPException(404, "Xarid topilmadi")
        return purchase

    @app.get(
        "/api/batches",
        tags=["Packaging"],
        summary="Allowed: owner, warehouse, packer — list batches",
    )
    def list_packaging_batches(
        material_id: str | None = None,
        product_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "packer"})
        return {"batches": list_batches(Journal(db).docs, material_id, product_id)}

    @app.get(
        "/api/batches/{batch_id}",
        tags=["Packaging"],
        summary="Allowed: owner, warehouse, packer — get batch",
    )
    def get_packaging_batch(
        batch_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "packer"})
        batch = get_batch(Journal(db).docs, batch_id)
        if batch is None:
            raise HTTPException(404, "Partiya topilmadi")
        return batch

    @app.get(
        "/api/warehouse/loadings",
        tags=["Warehouse"],
        summary="Allowed: owner, warehouse — list pending loadings",
    )
    def warehouse_loadings(
        agent_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse"})
        return {"loadings": pending_loadings(Journal(db).docs, agent_id)}

    @app.get(
        "/api/warehouse/stock-counts",
        tags=["Warehouse"],
        summary="Allowed: owner, warehouse — list stock counts",
    )
    def warehouse_stock_counts(
        status: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse"})
        return {"stock_counts": list_stock_counts(Journal(db).docs, status)}

    @app.get(
        "/api/returns",
        tags=["Returns"],
        summary="Allowed: owner, warehouse, agent — list returns",
    )
    def list_return_records(
        sale_id: str | None = None,
        status: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "agent"})
        return {"returns": list_returns(Journal(db).docs, sale_id, status)}

    @app.get(
        "/api/disputes",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — list disputes",
    )
    def list_dispute_records(
        store_id: str | None = None,
        status: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        disputes = list_disputes(Journal(db).docs, store_id, status)
        if actor.task == "agent":
            store_ids = _agent_store_ids(db, actor)
            disputes = [item for item in disputes if item["store_id"] in store_ids]
        return {"disputes": disputes}

    @app.get(
        "/api/closings",
        tags=["Closing"],
        summary="Allowed: owner, agent — list daily closings",
    )
    def list_closing_records(
        agent_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "agent"})
        if actor.task == "agent":
            agent_id = actor.user_id
        return {"closings": list_closings(Journal(db).docs, agent_id)}

    @app.get(
        "/api/inbox",
        tags=["Commands"],
        summary="Allowed: owner — list conflict inbox",
    )
    def conflict_inbox(
        status: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner"})
        engine = Engine(Journal(db), Permissions.default())
        return {"items": engine.list_inbox(actor, status)}

    @app.post(
        "/api/inbox/{event_id}/resolve",
        tags=["Commands"],
        summary="Allowed: owner — resolve conflict inbox item",
    )
    def resolve_conflict(
        event_id: str,
        body: dict,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner"})
        resolution = body.get("resolution")
        if resolution not in {"accept", "reject", "merge"}:
            raise HTTPException(422, "resolution accept, reject yoki merge bo'lishi kerak")
        engine = Engine(Journal(db), Permissions.default())
        try:
            engine.resolve_inbox(actor, event_id, resolution)
        except DomainError as exc:
            raise HTTPException(404 if exc.code == "not_found" else 400,
                                detail={"code": exc.code, "message": str(exc),
                                        "details": exc.details}) from exc
        return {"status": "ok", "event_id": event_id, "resolution": resolution}

    @app.get(
        "/api/payments",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — list store payments",
    )
    def list_store_payments(
        store_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        if actor.task == "agent" and store_id not in _agent_store_ids(db, actor):
            raise HTTPException(403, "Bu do'kon sizga biriktirilmagan")
        return {"payments": list_payments(Journal(db).docs, store_id)}

    @app.get(
        "/api/payments/{payment_id}",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — get store payment",
    )
    def get_store_payment(
        payment_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        payment = get_payment(Journal(db).docs, payment_id)
        if payment is None:
            raise HTTPException(404, "To'lov topilmadi")
        if actor.task == "agent" and payment["store_id"] not in _agent_store_ids(db, actor):
            raise HTTPException(403, "Bu to'lov sizga biriktirilmagan")
        return payment

    @app.get(
        "/api/disputes/{dispute_id}",
        tags=["Sales"],
        summary="Allowed: owner, accountant, agent — get dispute",
    )
    def get_dispute_record(
        dispute_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant", "agent"})
        dispute = get_dispute(Journal(db).docs, dispute_id)
        if dispute is None:
            raise HTTPException(404, "Bahs topilmadi")
        if actor.task == "agent" and dispute["store_id"] not in _agent_store_ids(db, actor):
            raise HTTPException(403, "Bu bahs sizga biriktirilmagan")
        return dispute

    @app.get(
        "/api/suppliers/{supplier_id}/payments",
        tags=["Purchases"],
        summary="Allowed: owner, accountant — supplier payments",
    )
    def supplier_payment_records(
        supplier_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant"})
        journal = Journal(db)
        return {
            "supplier_id": supplier_id,
            "balance": supplier_balance(journal.bal, supplier_id),
            "payments": list_supplier_payments(journal.docs, supplier_id),
        }

    @app.get(
        "/api/prices/{product_id}/history",
        tags=["References"],
        summary="Allowed: owner, accountant — price history",
    )
    def product_price_history(
        product_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "accountant"})
        return {"product_id": product_id, "history": price_history(Journal(db).docs, product_id)}

    @app.get(
        "/api/batches/{batch_id}/shrink",
        tags=["Packaging"],
        summary="Allowed: owner, warehouse, packer — shrink statistics",
    )
    def batch_shrink_summary(
        batch_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "packer"})
        journal = Journal(db)
        batch = get_batch(journal.docs, batch_id)
        if batch is None:
            raise HTTPException(404, "Partiya topilmadi")
        material_id = batch["material_id"]
        policy = Engine(journal).current_policy()
        return shrink_summary(journal.docs, material_id, policy)

    @app.get(
        "/api/warehouse/custody",
        tags=["Warehouse"],
        summary="Allowed: owner, warehouse, agent — custody stock",
    )
    def warehouse_custody(
        user_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "agent"})
        if actor.task == "agent":
            user_id = actor.user_id
        return {"items": custody_inventory(Journal(db).bal, user_id)}

    @app.get(
        "/api/returns/{return_id}",
        tags=["Returns"],
        summary="Allowed: owner, warehouse, agent — get return",
    )
    def get_return_record(
        return_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "agent"})
        record = get_return(Journal(db).docs, return_id)
        if record is None:
            raise HTTPException(404, "Qaytim topilmadi")
        return record

    @app.get(
        "/api/exchanges/{exchange_id}",
        tags=["Returns"],
        summary="Allowed: owner, warehouse, agent — get exchange",
    )
    def get_exchange_record(
        exchange_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse", "agent"})
        record = get_exchange(Journal(db).docs, exchange_id)
        if record is None:
            raise HTTPException(404, "Almashtirish topilmadi")
        return record

    @app.get(
        "/api/writeoffs",
        tags=["Returns"],
        summary="Allowed: owner, warehouse — list write-offs",
    )
    def list_writeoff_records(
        product_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "warehouse"})
        return {"writeoffs": list_writeoffs(Journal(db).docs, product_id)}

    @app.get(
        "/api/closings/{closing_id}",
        tags=["Closing"],
        summary="Allowed: owner, agent — get daily closing",
    )
    def get_closing_record(
        closing_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "agent"})
        record = get_closing(Journal(db).docs, closing_id)
        if record is None:
            raise HTTPException(404, "Kun yopish topilmadi")
        if actor.task == "agent" and record["agent_id"] != actor.user_id:
            raise HTTPException(403, "Bu kun yopish sizga biriktirilmagan")
        return record

    @app.get(
        "/api/cash-handovers",
        tags=["Closing"],
        summary="Allowed: owner, agent — list cash handovers",
    )
    def list_cash_handover_records(
        agent_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "agent"})
        if actor.task == "agent":
            agent_id = actor.user_id
        return {"handovers": pending_cash_handovers(Journal(db).docs, agent_id)}

    @app.get(
        "/api/cash-handovers/{transfer_id}",
        tags=["Closing"],
        summary="Allowed: owner, agent — get cash handover",
    )
    def get_cash_handover_record(
        transfer_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "agent"})
        record = get_cash_handover(Journal(db).docs, transfer_id)
        if record is None:
            raise HTTPException(404, "Kassa topshirig'i topilmadi")
        if actor.task == "agent" and record["agent_id"] != actor.user_id:
            raise HTTPException(403, "Bu topshirig' sizga biriktirilmagan")
        return record

    @app.get(
        "/api/closings/{agent_id}/balance-preview",
        tags=["Closing"],
        summary="Allowed: owner, agent — preview balances before daily closing",
    )
    def closing_balance_preview(
        agent_id: str,
        cash_counted: int = Query(ge=0),
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner", "agent"})
        if actor.task == "agent":
            agent_id = actor.user_id
        journal = Journal(db)
        return balance_snapshot(journal.bal, journal.docs, agent_id, cash_counted)

    @app.get(
        "/api/policy",
        tags=["Settings"],
        summary="Allowed: valid token — get effective business policy",
    )
    def get_policy(actor: Actor = Depends(current_actor), db: Database = Depends(get_db)) -> dict:
        return Engine(Journal(db), Permissions.default()).current_policy().to_dict()

    @app.get(
        "/api/auth/devices",
        tags=["Auth"],
        summary="Allowed: owner — list registered devices",
    )
    def list_devices(
        user_id: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner"})
        sql = "SELECT device_id, user_id, label, created_at, last_seen, revoked FROM devices"
        params: tuple = ()
        if user_id:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        sql += " ORDER BY created_at"
        return {"devices": [dict(row) for row in db.query(sql, params)]}

    @app.get(
        "/api/events",
        tags=["Manage"],
        summary="Allowed: owner — paginated event journal",
    )
    def event_journal(
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        type: str | None = None,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner"})
        journal = Journal(db)
        types = (type,) if type else None
        events = list(journal.ledger.iter_events(after_seq, types))[:limit]
        return {
            "events": [
                {
                    "seq": item.seq,
                    "event_id": item.event_id,
                    "type": item.type,
                    "actor_id": item.actor_id,
                    "device_id": item.device_id,
                    "occurred_at": item.occurred_at,
                    "recorded_at": item.recorded_at,
                    "time_flag": item.time_flag,
                    "payload": item.payload,
                    "gps": item.gps,
                    "corrects_event_id": item.corrects_event_id,
                }
                for item in events
            ],
            "last_seq": journal.ledger.last_seq(),
        }

    @app.get(
        "/api/events/{event_id}",
        tags=["Manage"],
        summary="Allowed: owner — get event",
    )
    def get_event_record(
        event_id: str,
        actor: Actor = Depends(current_actor),
        db: Database = Depends(get_db),
    ) -> dict:
        _require_roles(actor, {"owner"})
        item = Journal(db).ledger.get(event_id)
        if item is None:
            raise HTTPException(404, "Hodisa topilmadi")
        return {
            "seq": item.seq,
            "event_id": item.event_id,
            "type": item.type,
            "actor_id": item.actor_id,
            "device_id": item.device_id,
            "occurred_at": item.occurred_at,
            "recorded_at": item.recorded_at,
            "time_flag": item.time_flag,
            "payload": item.payload,
            "gps": item.gps,
            "corrects_event_id": item.corrects_event_id,
            "hash": item.hash,
            "prev_hash": item.prev_hash,
        }

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
