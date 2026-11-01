# n8n de GadgetsY+ en Render

Incluye dos workflows exportables: recepción/sincronización y agente de incidencias; aprobación y Telegram de prueba. Importar ambos desde `workflows/` en una instancia n8n Community desplegada en Render.

## Despliegue y persistencia

Crear un servicio web Docker desde este directorio. `Dockerfile` utiliza la imagen oficial de n8n y expone el puerto `10000`, que Render espera por defecto. Configurar una instancia PostgreSQL gestionada en Render y guardar el estado n8n en el esquema `n8n_data`; el back-office utiliza el esquema separado `backoffice`. En Render, definir `DB_TYPE=postgresdb`, `DB_POSTGRESDB_HOST`, `DB_POSTGRESDB_PORT`, `DB_POSTGRESDB_DATABASE`, `DB_POSTGRESDB_USER`, `DB_POSTGRESDB_PASSWORD`, `DB_POSTGRESDB_SCHEMA=n8n_data`, `N8N_ENCRYPTION_KEY`, `N8N_HOST`, `N8N_PROTOCOL=https`, `N8N_PORT=10000`, `PORT=10000`, `N8N_EDITOR_BASE_URL`, `N8N_WEBHOOK_URL`, `N8N_PROXY_HOPS=1`, `N8N_SECURE_COOKIE=true`, `EXECUTIONS_DATA_SAVE_ON_SUCCESS=all` y `EXECUTIONS_DATA_SAVE_ON_ERROR=all`. Usa la URL interna de PostgreSQL y una clave de cifrado aleatoria estable; no guardes valores reales en Git. `N8N_WEBHOOK_URL` es la URL externa HTTPS que Render te asigna; la documentación actual de n8n marca `WEBHOOK_URL` como obsoleta en versiones recientes.

Los servicios gratuitos de Render pueden suspenderse y el PostgreSQL gratuito caduca a los 30 días. La instancia sirve para el prototipo; exporta workflows y registros de evidencia antes de que expire. No se utiliza SQLite ni disco efímero para datos persistentes.

## Configuración necesaria después de importar

1. En `Webhook de la tienda`, asigna una credencial **Header Auth** con nombre de cabecera `X-Webhook-Token`; el valor debe coincidir con `integration.webhook_secret` en `config.php` de la tienda.
2. En cada nodo que llame a la API del back-office, sustituye `https://BACKOFFICE_URL` por la URL Render y selecciona una credencial **Header Auth** para `X-API-Key`. Utiliza `BACKOFFICE_API_TOKEN`; los IDs ficticios de las referencias de credencial del export deben sustituirse por credenciales reales creadas dentro de n8n.
3. Conecta una credencial de modelo de chat en `Modelo de chat`. El workflow requiere un proveedor compatible y un secreto configurado dentro de n8n. El modelo no envía mensajes.
4. En `Webhook aprobación back-office`, asigna credencial Header Auth de `X-Approval-Token`; coincide con `N8N_APPROVAL_TOKEN` en el back-office. Sustituye igualmente la referencia ficticia por la credencial real.
5. Conecta el bot de Telegram de prueba en `Envía al chat de prueba`, cambia `REEMPLAZAR_CHAT_ID_DE_PRUEBA` por el identificador del chat y comprueba que el bot pertenece a ese chat.
6. Configura la URL del webhook de producción `telegram-approved` en `N8N_APPROVAL_WEBHOOK_URL` del back-office. Activa workflows solo después de completar las credenciales.
7. Activa almacenamiento de ejecuciones exitosas y fallidas; exporta de nuevo los workflows revisados para adjuntarlos a la entrega, sin incluir credenciales.

## Workflows

### `gadgetsymas-integration.json`

Webhook autenticado → validación del contrato → recepción/upsert en back-office. La rama `support.requested` ejecuta el agente, que dispone de herramientas HTTP de consulta y guardado de propuesta. El registro de propuesta siempre queda pendiente de revisión; no hay nodo de envío a Telegram en este flujo.

### `telegram-approved.json`

Webhook de aprobación autenticado → comprueba que la decisión es aprobada → envía al chat de prueba → registra el resultado en back-office. El panel solo llama al webhook tras pulsar «Aprobar y enviar a Telegram».

## Casos de demostración

- Pedido ficticio en estado `payment_pending`: debe aparecer una vez en `/admin/orders` y `event_id`/`correlation_id` deben coincidir con la ejecución n8n.
- Cambio de stock desde el panel eCommerce: debe crear evento en back-office.
- Incidencia ficticia: el agente consulta, guarda propuesta `pending_review`, un revisor la aprueba o rechaza desde el back-office; solo aprobada pasa al chat Telegram de prueba.
- Enviar un evento incompleto: se espera respuesta 422 y rama de validación fallida. Revisar historial de n8n y registro en el back-office.
