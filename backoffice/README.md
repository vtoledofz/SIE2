# Back-office de GadgetsY+

API y panel operativo independientes del eCommerce, desplegados como servicio web FastAPI en Render y conectados a PostgreSQL de Render. El panel muestra datos del prototipo académico y no realiza ventas reales.

## Configuración en Render

Configura en el servicio web `gadgetsymas-backoffice` las variables `DATABASE_URL`, `BACKOFFICE_DB_SCHEMA=backoffice`, `BACKOFFICE_API_TOKEN`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `N8N_APPROVAL_WEBHOOK_URL` y `N8N_APPROVAL_TOKEN`. Usa secretos aleatorios y no los incluyas en Git, exports ni capturas.

El panel está disponible en `/admin`; sus secciones son `/admin/orders`, `/admin/incidents` y `/admin/events`. Requiere autenticación de administrador. Desde incidencias, una persona puede aprobar o rechazar propuestas. Solo al aprobar se llama al webhook autenticado de n8n para Telegram.

El resumen obtiene sus indicadores y gráficas de PostgreSQL: número e importe de pedidos, incidencias abiertas, resultados de integración, pedidos por estado, pedidos por día, unidades por producto y conversión del embudo. El embudo usa sesiones seudonimizadas distintas durante los últimos 30 días y cuenta solo eventos aceptados.

## API de integración

Todas las rutas `/api` requieren `X-API-Key` igual a `BACKOFFICE_API_TOKEN`.

- `POST /api/events`: recibe eventos `product.viewed`, `cart.item_added`, `checkout.started`, `order.created`, `payment.stripe_succeeded`, `inventory.stock_updated` y `support.requested`; valida campos, registra el resultado y aplica idempotencia por `event_id`.
- `POST /api/incidents/by-external/{case_id}/lookup`: consulta el pedido asociado e historial de incidencias. Es de solo lectura aunque actualmente use POST.
- `POST /api/incidents/by-external/{case_id}/proposal`: guarda la propuesta estructurada en estado `pending_review`.
- `GET /api/incidents/by-external/{case_id}`: consulta estado y datos básicos de una incidencia.
- `POST /api/incidents/by-external/{case_id}/telegram-result`: registra `sent` o `send_failed` después de una aprobación humana.

Usa solo identificadores y mensajes ficticios como `customer-demo-42` y `VG-DEMO-*`. No incluyas datos personales reales.
