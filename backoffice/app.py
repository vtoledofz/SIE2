import hmac
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, MetaData, Numeric, String, Text, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./backoffice-dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
BACKOFFICE_SCHEMA = os.getenv("BACKOFFICE_DB_SCHEMA", "backoffice")
if DATABASE_URL.startswith("postgresql") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", BACKOFFICE_SCHEMA):
    raise RuntimeError("BACKOFFICE_DB_SCHEMA debe ser un identificador SQL simple")
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    metadata = MetaData(schema=BACKOFFICE_SCHEMA if DATABASE_URL.startswith("postgresql") else None)


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    result: Mapped[str] = mapped_column(String(24), default="accepted")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    order_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    customer_ref: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    order: Mapped[Order] = relationship(back_populates="items")


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_case_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True)
    customer_ref: Mapped[str] = mapped_column(String(100))
    order_code: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    case_type: Mapped[str] = mapped_column(String(60), default="sin_clasificar")
    priority: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    agent_proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    telegram_status: Mapped[str] = mapped_column(String(30), default="not_sent")


class OperationalChange(Base):
    __tablename__ = "operational_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_ref: Mapped[str] = mapped_column(String(100))
    before_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    after_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=80)
    occurred_at: datetime
    source: str = Field(min_length=1, max_length=80)
    schema_version: str = "1.0"
    correlation_id: str = Field(min_length=1, max_length=100)
    data: dict[str, Any]


class Proposal(BaseModel):
    case_type: str
    looked_up: dict[str, Any]
    proposed_action: str
    rationale: str
    confidence: float = Field(ge=0, le=1)
    priority: str
    warnings: list[str] = Field(default_factory=list)
    draft_message: str
    final_status: Literal["pending_review"] = "pending_review"


class TelegramResult(BaseModel):
    status: str = Field(pattern="^(sent|send_failed)$")


app = FastAPI(title="GadgetsY+ Back-office académico", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "local-only-change-me"), https_only=os.getenv("COOKIE_HTTPS_ONLY", "false").lower() == "true", same_site="lax")
security = HTTPBasic(auto_error=False)
if DATABASE_URL.startswith("postgresql"):
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{BACKOFFICE_SCHEMA}"'))
Base.metadata.create_all(engine)


def db_session():
    with SessionLocal() as session:
        yield session


def require_api_token(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("BACKOFFICE_API_TOKEN", "")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Credencial de integración inválida")


def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    if request.session.get("reviewer"):
        return str(request.session["reviewer"])
    username = os.getenv("ADMIN_USERNAME", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if credentials and username and password and hmac.compare_digest(credentials.username, username) and hmac.compare_digest(credentials.password, password):
        request.session["reviewer"] = credentials.username
        request.session["csrf"] = secrets.token_urlsafe(24)
        return credentials.username
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"}, detail="Inicia sesión para revisar el back-office")


def html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · GadgetsY+ Back-office</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 18px;color:#172033;background:#f5f7fb}}h1,h2{{color:#173b64}}nav a{{margin-right:16px}}section,article{{background:white;border:1px solid #dce3ed;border-radius:10px;padding:16px;margin:14px 0}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:10px;border-bottom:1px solid #dce3ed;text-align:left;vertical-align:top}}small,.muted{{color:#58667a}}button{{padding:9px 14px;border:0;border-radius:6px;cursor:pointer}}.approve{{background:#126b46;color:white}}.reject{{background:#a72c35;color:white}}.pill{{background:#e8eef7;padding:3px 8px;border-radius:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f9;padding:12px}}</style>
<body><p class="muted">PROTOTIPO ACADÉMICO · Sin actividad comercial real</p><h1>{title}</h1><nav><a href="/admin">Resumen</a><a href="/admin/orders">Pedidos</a><a href="/admin/incidents">Incidencias</a><a href="/admin/events">Integraciones</a></nav>{body}</body></html>''')


@app.get("/health")
def health():
    return {"status": "ok", "service": "gadgetsymas-backoffice"}


def validate_event_data(event: EventEnvelope):
    d = event.data
    if event.event_type == "order.created":
        required = ["order_id", "order_code", "customer_ref", "status", "currency", "total", "items"]
        missing = [key for key in required if key not in d]
        if missing:
            raise HTTPException(status_code=422, detail={"code": "missing_fields", "fields": missing})
        if not isinstance(d["items"], list) or not d["items"]:
            raise HTTPException(status_code=422, detail={"code": "invalid_items", "fields": ["items"]})
        total = Decimal(str(d["total"]))
        if not total.is_finite() or total < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_total", "fields": ["total"]})
        for i, item in enumerate(d["items"]):
            price = Decimal(str(item.get("unit_price", -1)))
            if not item.get("sku") or int(item.get("quantity", 0)) < 1 or not price.is_finite() or price < 0:
                raise HTTPException(status_code=422, detail={"code": "invalid_item", "fields": [f"items[{i}]"]})
    elif event.event_type == "support.requested":
        if not d.get("customer_ref") or not str(d.get("message", "")).strip():
            raise HTTPException(status_code=422, detail={"code": "missing_fields", "fields": [k for k in ("customer_ref", "message") if not d.get(k)]})
    elif event.event_type == "payment.stripe_succeeded":
        if not d.get("order_id") or not d.get("order_code"):
            raise HTTPException(status_code=422, detail={"code": "missing_fields", "fields": ["order_id", "order_code"]})
    elif event.event_type == "inventory.stock_updated":
        if not d.get("product_id") or "new_stock" not in d:
            raise HTTPException(status_code=422, detail={"code": "missing_fields", "fields": ["product_id", "new_stock"]})
    else:
        raise HTTPException(status_code=422, detail={"code": "unsupported_event", "event_type": event.event_type})


@app.post("/api/events", dependencies=[Depends(require_api_token)])
def receive_event(event: EventEnvelope, session: Session = Depends(db_session)):
    duplicate = session.scalar(select(IntegrationEvent).where(IntegrationEvent.event_id == event.event_id))
    if duplicate:
        if duplicate.result != "accepted":
            raise HTTPException(status_code=409, detail={"code": "previous_attempt_failed", "error": duplicate.error})
        return {"accepted": True, "duplicate": True, "correlation_id": event.correlation_id}
    try:
        validate_event_data(event)
        record = IntegrationEvent(event_id=event.event_id, event_type=event.event_type, occurred_at=event.occurred_at,
            correlation_id=event.correlation_id, source=event.source, payload=event.model_dump(mode="json"))
        session.add(record)
        session.flush()
        d = event.data
        if event.event_type == "order.created":
            order = session.scalar(select(Order).where(Order.external_order_id == str(d["order_id"])))
            if not order:
                order = Order(external_order_id=str(d["order_id"]), order_code=str(d["order_code"]), customer_ref=str(d["customer_ref"]),
                    status=str(d["status"]), currency=str(d["currency"]).upper(), total=Decimal(str(d["total"])), created_at=event.occurred_at)
                order.items = [OrderItem(sku=str(item["sku"]), name=str(item.get("name", item["sku"])), quantity=int(item["quantity"]), unit_price=Decimal(str(item["unit_price"]))) for item in d["items"]]
                session.add(order)
            else:
                order.status = str(d["status"])
                order.total = Decimal(str(d["total"]))
                order.updated_at = datetime.now(timezone.utc)
        elif event.event_type == "payment.stripe_succeeded":
            order = session.scalar(select(Order).where(Order.external_order_id == str(d["order_id"])))
            if not order:
                raise HTTPException(status_code=409, detail={"code": "order_not_found", "order_id": str(d["order_id"])})
            order.status = "paid"
            order.updated_at = datetime.now(timezone.utc)
        elif event.event_type == "support.requested":
            case_id = str(d.get("case_id") or event.event_id)
            incident = Incident(external_case_id=case_id, event_id=event.event_id, customer_ref=str(d["customer_ref"]),
                order_code=d.get("order_code"), message=str(d["message"])[:2000])
            session.add(incident)
        elif event.event_type == "inventory.stock_updated":
            session.add(OperationalChange(event_id=event.event_id, entity_type="inventory", entity_ref=str(d["product_id"]),
                before_value=str(d.get("old_stock")) if d.get("old_stock") is not None else None, after_value=str(d["new_stock"])))
        session.commit()
    except HTTPException as exc:
        session.rollback()
        detail = json.dumps(exc.detail, ensure_ascii=False) if not isinstance(exc.detail, str) else exc.detail
        failed = IntegrationEvent(event_id=event.event_id, event_type=event.event_type, occurred_at=event.occurred_at,
            correlation_id=event.correlation_id, source=event.source, payload=event.model_dump(mode="json"), result="failed", error=detail)
        session.add(failed)
        try: session.commit()
        except Exception: session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        failed = IntegrationEvent(event_id=event.event_id, event_type=event.event_type, occurred_at=event.occurred_at,
            correlation_id=event.correlation_id, source=event.source, payload=event.model_dump(mode="json"),
            result="failed", error=type(exc).__name__)
        session.add(failed)
        try: session.commit()
        except Exception: session.rollback()
        raise HTTPException(status_code=500, detail={"code": "backoffice_write_failed", "message": type(exc).__name__}) from exc
    return {"accepted": True, "duplicate": False, "correlation_id": event.correlation_id}


@app.post("/api/incidents/{incident_id}/proposal", dependencies=[Depends(require_api_token)])
def save_proposal(incident_id: int, proposal: Proposal, session: Session = Depends(db_session)):
    incident = session.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    incident.case_type = proposal.case_type[:60]
    incident.priority = proposal.priority[:20]
    incident.agent_proposal = proposal.model_dump()
    incident.status = "pending_review"
    session.commit()
    return {"saved": True, "incident_id": incident.id, "status": incident.status}


@app.post("/api/incidents/{incident_id}/lookup", dependencies=[Depends(require_api_token)])
def incident_lookup(incident_id: int, session: Session = Depends(db_session)):
    incident = session.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    order = session.scalar(select(Order).where(Order.order_code == incident.order_code)) if incident.order_code else None
    history = list(session.scalars(select(Incident).where(Incident.customer_ref == incident.customer_ref, Incident.id != incident.id)))
    return {"customer_ref": incident.customer_ref, "order": ({"order_code": order.order_code, "status": order.status, "total": str(order.total), "currency": order.currency, "items": [{"sku": i.sku, "name": i.name, "quantity": i.quantity} for i in order.items]} if order else None), "prior_incidents": [{"case_id": h.external_case_id, "status": h.status, "case_type": h.case_type} for h in history]}


@app.get("/api/incidents/by-external/{case_id}", dependencies=[Depends(require_api_token)])
def get_incident_by_external(case_id: str, session: Session = Depends(db_session)):
    incident = session.scalar(select(Incident).where(Incident.external_case_id == case_id))
    if not incident:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    return {"id": incident.id, "case_id": incident.external_case_id, "customer_ref": incident.customer_ref,
        "order_code": incident.order_code, "message": incident.message, "status": incident.status}


@app.post("/api/incidents/by-external/{case_id}/lookup", dependencies=[Depends(require_api_token)])
def lookup_incident_by_external(case_id: str, session: Session = Depends(db_session)):
    incident = session.scalar(select(Incident).where(Incident.external_case_id == case_id))
    if not incident:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    return incident_lookup(incident.id, session)


@app.post("/api/incidents/by-external/{case_id}/proposal", dependencies=[Depends(require_api_token)])
def save_proposal_by_external(case_id: str, proposal: Proposal, session: Session = Depends(db_session)):
    incident = session.scalar(select(Incident).where(Incident.external_case_id == case_id))
    if not incident:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    incident.case_type = proposal.case_type[:60]
    incident.priority = proposal.priority[:20]
    incident.agent_proposal = proposal.model_dump()
    incident.status = "pending_review"
    session.commit()
    return {"saved": True, "incident_id": incident.id, "status": incident.status}


@app.post("/api/incidents/by-external/{case_id}/telegram-result", dependencies=[Depends(require_api_token)])
def set_telegram_result(case_id: str, result: TelegramResult, session: Session = Depends(db_session)):
    incident = session.scalar(select(Incident).where(Incident.external_case_id == case_id))
    if not incident or incident.review_decision != "approved":
        raise HTTPException(status_code=409, detail="No existe una aprobación registrada para este envío")
    incident.telegram_status = result.status
    session.commit()
    return {"recorded": True, "telegram_status": incident.telegram_status}


def csrf_input(request: Request) -> str:
    return f'<input type="hidden" name="csrf" value="{request.session.get("csrf", "")}">'


def check_csrf(request: Request, token: str):
    expected = str(request.session.get("csrf", ""))
    if not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="CSRF inválido; recarga la página")


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    orders = session.scalar(select(func.count()).select_from(Order)) or 0
    total = session.scalar(select(func.coalesce(func.sum(Order.total), 0))) or 0
    open_incidents = session.scalar(select(func.count()).select_from(Incident).where(Incident.status.in_(["open", "pending_review"]))) or 0
    success = session.scalar(select(func.count()).select_from(IntegrationEvent).where(IntegrationEvent.result == "accepted")) or 0
    failures = session.scalar(select(func.count()).select_from(IntegrationEvent).where(IntegrationEvent.result == "failed")) or 0
    pending = session.scalar(select(func.count()).select_from(Incident).where(Incident.status == "pending_review")) or 0
    agent_approved = session.scalar(select(func.count()).select_from(Incident).where(Incident.review_decision == "approved")) or 0
    daily = session.execute(select(func.date(Order.created_at), func.count()).group_by(func.date(Order.created_at)).order_by(func.date(Order.created_at)).limit(7)).all()
    max_count = max((int(row[1]) for row in daily), default=1) or 1
    bars = "".join(f'<rect x="{i*70+8}" y="{140-(int(count)*110/max_count):.0f}" width="42" height="{int(count)*110/max_count:.0f}" fill="#3977b8"><title>{day}: {count} pedidos</title></rect><text x="{i*70+8}" y="158" font-size="10">{str(day)[5:]}</text>' for i, (day, count) in enumerate(daily))
    success_rate = round(100 * success / (success + failures), 1) if success + failures else 0
    body = f'<p>Sesión de revisión: <b>{html.escape(reviewer)}</b>. <span class="pill">Datos del prototipo</span></p><section><h2>Métricas operativas</h2><p>Pedidos sincronizados: <b>{orders}</b> · Importe simulado: <b>{total} EUR</b> · Incidencias abiertas: <b>{open_incidents}</b> · Integraciones aceptadas: <b>{success}</b> · Fallidas: <b>{failures}</b> · Tasa de éxito: <b>{success_rate}%</b> · Propuestas aprobadas: <b>{agent_approved}</b> · Pendientes: <b>{pending}</b></p><h3>Pedidos por día</h3><svg role="img" aria-label="Pedidos por día" width="500" height="175" viewBox="0 0 500 175">{bars}</svg><p>Las cifras y el gráfico se calculan desde los registros del back-office.</p></section>'
    return html_page("Resumen operativo", body)


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    orders = list(session.scalars(select(Order).order_by(Order.created_at.desc()).limit(100)))
    rows = "".join(f'<tr><td>{html.escape(o.order_code)}<br><small>{html.escape(o.external_order_id)}</small></td><td>{html.escape(o.customer_ref)}</td><td>{o.created_at.isoformat()}</td><td>{o.total} {html.escape(o.currency)}</td><td>{html.escape(o.status)}</td><td>{", ".join(f"{html.escape(i.sku)} × {i.quantity}" for i in o.items)}</td></tr>' for o in orders)
    return html_page("Pedidos sincronizados", f'<table><tr><th>Pedido</th><th>Cliente de prueba</th><th>Fecha</th><th>Total</th><th>Estado</th><th>Líneas</th></tr>{rows}</table>')


@app.get("/admin/incidents", response_class=HTMLResponse)
def admin_incidents(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    incidents = list(session.scalars(select(Incident).order_by(Incident.created_at.desc()).limit(100)))
    cards = []
    for inc in incidents:
        proposal = inc.agent_proposal or {}
        if isinstance(proposal, str):
            try: proposal = json.loads(proposal)
            except ValueError: proposal = {}
        preview = html.escape(json.dumps(proposal, ensure_ascii=False, indent=2)) if proposal else "Aún no hay propuesta del agente."
        buttons = ""
        if inc.status == "pending_review" and proposal:
            buttons = f'''<form method="post" action="/admin/incidents/{inc.id}/decision">{csrf_input(request)}<button class="approve" name="decision" value="approved">Aprobar y enviar a Telegram</button> <button class="reject" name="decision" value="rejected">Rechazar</button></form>'''
        cards.append(f'<article><h2>{html.escape(inc.external_case_id)} · {html.escape(inc.case_type)} · {html.escape(inc.priority)}</h2><p><b>Estado:</b> {html.escape(inc.status)} · <b>Pedido:</b> {html.escape(inc.order_code or "sin asociar")} · <b>Cliente de prueba:</b> {html.escape(inc.customer_ref)}</p><p>{html.escape(inc.message)}</p><h3>Propuesta estructurada</h3><pre>{preview}</pre>{buttons}</article>')
    return html_page("Incidencias y revisión humana", "".join(cards) or "<p>No hay incidencias registradas.</p>")


@app.post("/admin/incidents/{incident_id}/decision")
def decide_incident(request: Request, incident_id: int, decision: str = Form(...), csrf: str = Form(...), reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    check_csrf(request, csrf)
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Decisión no válida")
    incident = session.get(Incident, incident_id)
    if not incident or incident.status != "pending_review" or not incident.agent_proposal:
        raise HTTPException(status_code=409, detail="La incidencia no tiene una propuesta pendiente")
    incident.review_decision = decision
    incident.reviewed_by = reviewer
    incident.reviewed_at = datetime.now(timezone.utc)
    incident.status = decision
    session.commit()
    if decision == "approved":
        hook = os.getenv("N8N_APPROVAL_WEBHOOK_URL", "")
        token = os.getenv("N8N_APPROVAL_TOKEN", "")
        if hook and token:
            import urllib.request
            payload = json.dumps({"incident_id": incident.id, "case_id": incident.external_case_id, "decision": decision, "reviewed_by": reviewer, "draft_message": (incident.agent_proposal or {}).get("draft_message", "")}).encode()
            req = urllib.request.Request(hook, data=payload, headers={"Content-Type": "application/json", "X-Approval-Token": token}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as response:
                    incident.telegram_status = "sent" if 200 <= response.status < 300 else "send_failed"
            except Exception:
                incident.telegram_status = "send_failed"
            session.commit()
    return RedirectResponse("/admin/incidents", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/events", response_class=HTMLResponse)
def admin_events(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    events = list(session.scalars(select(IntegrationEvent).order_by(IntegrationEvent.received_at.desc()).limit(100)))
    rows = "".join(f'<tr><td>{html.escape(e.event_id)}</td><td>{html.escape(e.event_type)}</td><td>{html.escape(e.correlation_id)}</td><td>{html.escape(e.result)}</td><td>{e.received_at.isoformat()}</td></tr>' for e in events)
    return html_page("Trazabilidad de integraciones", f'<table><tr><th>event_id</th><th>Evento</th><th>correlation_id</th><th>Resultado</th><th>Recibido</th></tr>{rows}</table>')
