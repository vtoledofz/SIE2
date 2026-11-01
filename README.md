# Back-office independiente de GadgetsY+

API REST y panel académico separado del eCommerce. Usa FastAPI y PostgreSQL. Incluye los pedidos recibidos, incidencias, decisiones humanas y registro de integraciones; muestra aviso visible de prototipo.

## Despliegue en Render

1. Crea una base de datos PostgreSQL en Render (o utiliza una instancia existente con permisos para crear un esquema propio).
2. Crea un servicio web conectado a este directorio. `render.yaml` incluye el comando de arranque y comprobación de salud.
3. Configura `DATABASE_URL` con la URL interna PostgreSQL y el esquema exclusivo `backoffice`.
4. Genera secretos independientes para `BACKOFFICE_API_TOKEN`, `SESSION_SECRET`, `ADMIN_PASSWORD` y `N8N_APPROVAL_TOKEN`. Define `ADMIN_USERNAME`.
5. Define `N8N_APPROVAL_WEBHOOK_URL` como la URL de producción de `telegram-approved` de n8n. `N8N_APPROVAL_TOKEN` debe corresponder a la credencial Header Auth de ese webhook.
6. Activa cookies seguras con `COOKIE_HTTPS_ONLY=true`.
7. Revisa `/health`, inicia sesión en `/admin` y verifica el prototipo con pedidos/incidencias ficticios.

La primera versión crea tablas al arrancar. Para una entrega académica pequeña es suficiente; una evolución de producción debería añadir Alembic, gestión de roles y migraciones revisadas.

## API

Todas las rutas `/api` requieren `X-API-Key: <BACKOFFICE_API_TOKEN>`.

- `POST /api/events`: recibe eventos `order.created`, `payment.stripe_succeeded`, `inventory.stock_updated` y `support.requested`; valida campos, conserva evento/error y actualiza la entidad con idempotencia por `event_id`.
- `POST /api/incidents/by-external/{case_id}/lookup`: consulta el pedido asociado e historial de incidencias; herramienta de consulta del agente.
- `POST /api/incidents/by-external/{case_id}/proposal`: guarda salida estructurada del agente como `pending_review`; herramienta de propuesta del agente.
- `POST /api/incidents/by-external/{case_id}/telegram-result`: registra envío o fallo tras la aprobación.

Panel: `/admin`, `/admin/orders`, `/admin/incidents`, `/admin/events`.

## Datos

Usa identificadores como `customer-demo-42` y pedidos `VG-DEMO-*`. No metas nombres, correos, direcciones, mensajes ni tokens reales en ejemplos, capturas, fixtures o workflows.
