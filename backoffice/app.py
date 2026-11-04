import hmac
import html
import json
import logging
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
logger = logging.getLogger("gadgetsymas.backoffice")
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
    safe_title = html.escape(title)
    return HTMLResponse(f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#10243b"><title>{safe_title} · GadgetsY+ Back-office</title>
<style>
:root{{--ink:#172b42;--muted:#66758a;--line:#e4eaf1;--paper:#fff;--canvas:#f3f6fa;--navy:#10243b;--blue:#2463a6;--blue-soft:#eaf2fc;--green:#14734f;--green-soft:#e8f6ef;--red:#b53c44;--red-soft:#fff0f0;--amber:#916100;--amber-soft:#fff6df;--shadow:0 10px 28px rgba(25,48,76,.06)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}a{{color:var(--blue);text-decoration:none}}a:hover{{text-decoration:underline}}.topbar{{background:var(--navy);color:white;padding:18px max(22px,calc((100vw - 1180px)/2));display:flex;align-items:center;justify-content:space-between;gap:16px}}.brand{{display:flex;align-items:center;gap:12px;color:white;font-weight:750;letter-spacing:.01em}}.brand-mark{{width:36px;height:36px;border-radius:11px;background:#3c83d3;display:grid;place-items:center;font-weight:800}}.prototype{{font-size:11px;letter-spacing:.09em;font-weight:750;color:#cde1f8;border:1px solid #45617e;padding:6px 10px;border-radius:999px;white-space:nowrap}}.shell{{max-width:1180px;margin:28px auto;padding:0 22px 48px}}.nav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px;border-bottom:1px solid var(--line);padding-bottom:16px}}.nav a{{color:#50647c;padding:9px 13px;border-radius:9px;font-weight:650}}.nav a:hover,.nav a.active{{background:white;color:var(--blue);box-shadow:0 2px 10px rgba(24,47,74,.07);text-decoration:none}}h1{{font-size:clamp(25px,4vw,34px);line-height:1.15;letter-spacing:-.035em;margin:0 0 8px;color:var(--navy)}}h2{{font-size:19px;letter-spacing:-.015em;margin:0 0 14px;color:var(--navy)}}h3{{font-size:14px;margin:20px 0 8px;color:var(--navy)}}.page-heading{{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:24px}}.subhead,.muted{{color:var(--muted)}}.subhead{{margin:0}}.panel,.metric,.incident{{background:var(--paper);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}}.panel{{padding:22px;margin:18px 0}}.metrics{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px}}.metric{{padding:18px}}.metric-label{{font-size:12px;font-weight:650;color:var(--muted);margin-bottom:8px}}.metric-value{{font-size:28px;font-weight:760;letter-spacing:-.04em;color:var(--navy);line-height:1.1}}.metric-foot{{font-size:12px;color:var(--muted);margin-top:8px}}.metric.warn .metric-value{{color:var(--amber)}}.metric.good .metric-value{{color:var(--green)}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:12px;background:white}}table{{border-collapse:collapse;width:100%;min-width:720px;background:white}}th,td{{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{background:#f7f9fc;color:#53647a;font-size:11px;letter-spacing:.065em;text-transform:uppercase;white-space:nowrap}}tbody tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:#fafcff}}td{{font-size:14px}}.secondary{{color:var(--muted);font-size:12px}}.badge{{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;background:#edf1f6;color:#45586d;white-space:nowrap}}.badge::before{{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}}.badge.good{{background:var(--green-soft);color:var(--green)}}.badge.bad{{background:var(--red-soft);color:var(--red)}}.badge.pending{{background:var(--amber-soft);color:var(--amber)}}.badge.info{{background:var(--blue-soft);color:var(--blue)}}.incident-list{{display:grid;gap:16px}}.incident{{padding:20px}}.incident-head{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:15px}}.incident-head h2{{margin:0 0 5px;font-size:17px}}.incident-id{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);font-size:12px}}.detail-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0 16px}}.detail{{background:#f7f9fc;border-radius:10px;padding:10px 12px;min-width:0}}.detail-label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}}.detail-value{{font-weight:650;overflow-wrap:anywhere;margin-top:3px}}.message{{border-left:3px solid #9bbbe0;padding:10px 13px;background:#f6f9fd;border-radius:0 9px 9px 0;white-space:pre-wrap}}.proposal{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fc;border:1px solid var(--line);border-radius:10px;padding:14px;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;max-height:320px;overflow:auto}}.actions{{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}}button{{font:inherit;font-weight:700;padding:10px 15px;border:1px solid transparent;border-radius:9px;cursor:pointer;transition:transform .12s,background .12s}}button:hover{{transform:translateY(-1px)}}button:focus-visible,a:focus-visible{{outline:3px solid #8cbcf1;outline-offset:2px}}.approve{{background:var(--green);color:white}}.approve:hover{{background:#0f6242}}.reject{{background:white;color:var(--red);border-color:#efc6c8}}.reject:hover{{background:var(--red-soft)}}.empty{{padding:36px 22px;text-align:center;color:var(--muted)}}.empty strong{{display:block;color:var(--ink);font-size:16px;margin-bottom:4px}}.chart{{overflow:auto}}.chart svg{{max-width:100%;height:auto}}.chart-note{{font-size:12px;color:var(--muted);margin:8px 0 0}}.charts-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.charts-grid .panel{{min-width:0;margin:0}}.bar-list{{display:grid;gap:13px;margin-top:18px}}.bar-row{{display:grid;grid-template-columns:minmax(90px,140px) minmax(60px,1fr) 34px;align-items:center;gap:10px;font-size:12px}}.bar-label{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#52647a}}.bar-track{{height:10px;border-radius:99px;background:#edf1f6;overflow:hidden}}.bar-fill{{height:100%;border-radius:inherit;background:linear-gradient(90deg,#438bd4,#2463a6);min-width:2px}}.bar-value{{text-align:right;font-weight:750;color:var(--navy)}}.chart-empty{{color:var(--muted);font-size:13px;padding:16px 0}}.footer{{margin-top:36px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
@media(max-width:1000px){{.metrics{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
@media(max-width:800px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.topbar{{padding-left:20px;padding-right:20px}}.shell{{margin-top:22px}}}}
@media(max-width:700px){{.charts-grid{{grid-template-columns:1fr}}}}
@media(max-width:560px){{.topbar{{align-items:flex-start;flex-direction:column}}.shell{{padding-left:14px;padding-right:14px}}.nav{{gap:4px}}.nav a{{padding:8px 10px;font-size:13px}}.metrics{{gap:9px}}.metric{{padding:14px}}.metric-value{{font-size:23px}}.panel{{padding:16px}}.detail-grid{{grid-template-columns:1fr 1fr}}.incident{{padding:15px}}.incident-head{{flex-direction:column}}.bar-row{{grid-template-columns:90px minmax(55px,1fr) 25px;gap:7px}}}}
</style></head><body><header class="topbar"><a class="brand" href="/admin"><span class="brand-mark">G+</span><span>GadgetsY+ <span style="font-weight:450;color:#c2d3e5">/ Back-office</span></span></a><span class="prototype">PROTOTIPO ACADÉMICO · SIN VENTAS REALES</span></header><main class="shell"><nav class="nav" aria-label="Navegación principal"><a href="/admin">Resumen</a><a href="/admin/orders">Pedidos</a><a href="/admin/incidents">Incidencias</a><a href="/admin/events">Integraciones</a></nav>{body}<footer class="footer">Entorno de demostración académica · Los datos mostrados son registros del prototipo.</footer></main></body></html>''')


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
    daily = session.execute(select(func.date(Order.created_at), func.count()).group_by(func.date(Order.created_at)).order_by(func.date(Order.created_at).desc()).limit(7)).all()
    daily = list(reversed(daily))
    status_rows = session.execute(select(Order.status, func.count(Order.id)).group_by(Order.status).order_by(func.count(Order.id).desc())).all()
    product_rows = session.execute(select(OrderItem.sku, OrderItem.name, func.sum(OrderItem.quantity)).group_by(OrderItem.sku, OrderItem.name).order_by(func.sum(OrderItem.quantity).desc()).limit(5)).all()
    max_count = max((int(row[1]) for row in daily), default=1) or 1
    bars = "".join(f'<rect x="{i*70+8}" y="{140-(int(count)*110/max_count):.0f}" width="42" height="{int(count)*110/max_count:.0f}" fill="#3977b8"><title>{day}: {count} pedidos</title></rect><text x="{i*70+8}" y="158" font-size="10">{str(day)[5:]}</text>' for i, (day, count) in enumerate(daily))
    max_status = max((int(count) for _, count in status_rows), default=1) or 1
    status_bars = "".join(f'<div class="bar-row"><span class="bar-label">{html.escape(str(label))}</span><div class="bar-track"><div class="bar-fill" style="width:{max(2, int(count) * 100 / max_status):.1f}%"></div></div><span class="bar-value">{int(count)}</span></div>' for label, count in status_rows)
    max_product = max((int(quantity) for _, _, quantity in product_rows), default=1) or 1
    product_bars = "".join(f'<div class="bar-row"><span class="bar-label" title="{html.escape(str(sku))}">{html.escape(str(name))}</span><div class="bar-track"><div class="bar-fill" style="width:{max(2, int(quantity) * 100 / max_product):.1f}%"></div></div><span class="bar-value">{int(quantity)}</span></div>' for sku, name, quantity in product_rows)
    success_rate = round(100 * success / (success + failures), 1) if success + failures else 0
    metrics = ''.join([
        f'<div class="metric"><div class="metric-label">Pedidos sincronizados</div><div class="metric-value">{orders}</div><div class="metric-foot">Registros recibidos</div></div>',
        f'<div class="metric"><div class="metric-label">Importe simulado</div><div class="metric-value">{total} <span style="font-size:15px">EUR</span></div><div class="metric-foot">Total de pedidos</div></div>',
        f'<div class="metric warn"><div class="metric-label">Incidencias abiertas</div><div class="metric-value">{open_incidents}</div><div class="metric-foot">{pending} pendientes de revisión</div></div>',
        f'<div class="metric good"><div class="metric-label">Integraciones correctas</div><div class="metric-value">{success}</div><div class="metric-foot">{failures} fallidas · {success_rate}% de éxito</div></div>',
        f'<div class="metric"><div class="metric-label">Propuestas aprobadas</div><div class="metric-value">{agent_approved}</div><div class="metric-foot">Decisión registrada por revisión humana</div></div>'
    ])
    status_content = status_bars or '<p class="chart-empty">Todavía no hay pedidos para clasificar.</p>'
    product_content = product_bars or '<p class="chart-empty">Todavía no hay líneas de pedido para comparar.</p>'
    body = f'<div class="page-heading"><div><h1>Resumen operativo</h1><p class="subhead">Actividad reciente del canal digital y sus procesos internos.</p></div><span class="badge info">Revisor: {html.escape(reviewer)}</span></div><section class="metrics" aria-label="Indicadores">{metrics}</section><div class="charts-grid"><section class="panel chart"><h2>Pedidos por día</h2><svg role="img" aria-label="Pedidos por día" width="500" height="175" viewBox="0 0 500 175"><line x1="0" y1="140" x2="490" y2="140" stroke="#dce4ed"/>{bars}</svg><p class="chart-note">Pedidos registrados por fecha de creación.</p></section><section class="panel"><h2>Pedidos por estado</h2><div class="bar-list" role="img" aria-label="Cantidad de pedidos agrupados por estado">{status_content}</div><p class="chart-note">Recuento de pedidos almacenados en cada estado.</p></section><section class="panel"><h2>Productos más solicitados</h2><div class="bar-list" role="img" aria-label="Cinco productos con más unidades solicitadas">{product_content}</div><p class="chart-note">Unidades solicitadas, agrupadas por SKU.</p></section></div>'
    return html_page("Resumen operativo", body)


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    orders = list(session.scalars(select(Order).order_by(Order.created_at.desc()).limit(100)))
    rows = "".join(f'<tr><td><strong>{html.escape(o.order_code)}</strong><br><span class="secondary">ID {html.escape(o.external_order_id)}</span></td><td>{html.escape(o.customer_ref)}</td><td>{o.created_at.strftime("%d/%m/%Y %H:%M UTC")}</td><td><strong>{o.total} {html.escape(o.currency)}</strong></td><td><span class="badge info">{html.escape(o.status)}</span></td><td>{", ".join(f"{html.escape(i.sku)} × {i.quantity}" for i in o.items)}</td></tr>' for o in orders)
    table = f'<div class="table-wrap"><table><thead><tr><th>Pedido</th><th>Cliente de prueba</th><th>Fecha</th><th>Importe</th><th>Estado</th><th>Artículos</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else '<div class="panel empty"><strong>Aún no hay pedidos</strong>Cuando la tienda envíe el primer evento order.created, aparecerá aquí.</div>'
    return html_page("Pedidos sincronizados", f'<div class="page-heading"><div><h1>Pedidos</h1><p class="subhead">Pedidos recibidos automáticamente desde la tienda.</p></div><span class="badge info">{len(orders)} registros</span></div>{table}')


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
            buttons = f'''<form class="actions" method="post" action="/admin/incidents/{inc.id}/decision">{csrf_input(request)}<button class="approve" name="decision" value="approved">Aprobar y enviar a Telegram</button><button class="reject" name="decision" value="rejected">Rechazar propuesta</button></form>'''
        status_class = "pending" if inc.status == "pending_review" else ("good" if inc.status == "approved" else ("bad" if inc.status == "rejected" else "info"))
        decision = f'<span class="secondary">Decisión: {html.escape(inc.review_decision or "sin revisar")}</span>'
        telegram = f'<span class="secondary">Telegram: {html.escape(inc.telegram_status)}</span>'
        cards.append(f'<article class="incident"><header class="incident-head"><div><h2>{html.escape(inc.case_type.replace("_", " ").title())} · prioridad {html.escape(inc.priority)}</h2><span class="incident-id">Caso {html.escape(inc.external_case_id)}</span></div><span class="badge {status_class}">{html.escape(inc.status.replace("_", " "))}</span></header><div class="detail-grid"><div class="detail"><div class="detail-label">Cliente</div><div class="detail-value">{html.escape(inc.customer_ref)}</div></div><div class="detail"><div class="detail-label">Pedido asociado</div><div class="detail-value">{html.escape(inc.order_code or "Sin asociar")}</div></div><div class="detail"><div class="detail-label">Recibida</div><div class="detail-value">{inc.created_at.strftime("%d/%m/%Y %H:%M UTC")}</div></div></div><h3>Consulta del cliente</h3><div class="message">{html.escape(inc.message)}</div><h3>Propuesta del agente</h3><pre class="proposal">{preview}</pre><div class="actions">{decision}{telegram}</div>{buttons}</article>')
    content = "".join(cards) or '<div class="panel empty"><strong>No hay incidencias registradas</strong>Las solicitudes de soporte recibidas desde la tienda aparecerán aquí.</div>'
    return html_page("Incidencias", f'<div class="page-heading"><div><h1>Incidencias</h1><p class="subhead">Revisa la propuesta del agente y decide si debe enviarse por Telegram.</p></div><span class="badge pending">{len(incidents)} casos recientes</span></div><div class="incident-list">{content}</div>')


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
                with urllib.request.urlopen(req, timeout=20) as response:
                    incident.telegram_status = "sent" if 200 <= response.status < 300 else "send_failed"
            except urllib.error.HTTPError as exc:
                logger.warning("n8n approval webhook returned HTTP %s", exc.code)
                incident.telegram_status = "send_failed"
            except urllib.error.URLError as exc:
                logger.warning("n8n approval webhook connection failed (%s)", type(exc.reason).__name__)
                incident.telegram_status = "send_failed"
            except Exception as exc:
                logger.exception("n8n approval webhook failed (%s)", type(exc).__name__)
                incident.telegram_status = "send_failed"
            session.commit()
    return RedirectResponse("/admin/incidents", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/events", response_class=HTMLResponse)
def admin_events(request: Request, reviewer: str = Depends(require_admin), session: Session = Depends(db_session)):
    events = list(session.scalars(select(IntegrationEvent).order_by(IntegrationEvent.received_at.desc()).limit(100)))
    rows = "".join(f'<tr><td><code>{html.escape(e.event_id)}</code></td><td>{html.escape(e.event_type)}</td><td><code>{html.escape(e.correlation_id)}</code></td><td><span class="badge {"good" if e.result == "accepted" else "bad"}">{html.escape(e.result)}</span></td><td>{e.received_at.strftime("%d/%m/%Y %H:%M UTC")}</td><td>{html.escape(e.error or "—")}</td></tr>' for e in events)
    table = f'<div class="table-wrap"><table><thead><tr><th>Evento</th><th>Tipo</th><th>Correlación</th><th>Resultado</th><th>Recibido</th><th>Detalle de error</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else '<div class="panel empty"><strong>Aún no hay eventos</strong>Cuando n8n reciba una operación desde la tienda, su trazabilidad aparecerá aquí.</div>'
    return html_page("Integraciones", f'<div class="page-heading"><div><h1>Integraciones</h1><p class="subhead">Registro técnico de eventos aceptados y fallidos.</p></div><span class="badge info">{len(events)} eventos recientes</span></div>{table}')




