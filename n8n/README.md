# n8n de GadgetsY+

La instancia se ejecuta en local con Docker Compose y Caddy; su URL pública usa el dominio configurado en el `.env` de la raíz. El back-office y PostgreSQL permanecen en Render. Consulta [DEPLOY_LOCAL_WINDOWS.md](../DEPLOY_LOCAL_WINDOWS.md).

## Importar y configurar

Los archivos JSON se exportan **sin credenciales**. Tras importar cada workflow, asigna credenciales desde el editor, guarda y publica/activa cuando todo esté configurado.

### `workflows/gadgetsymas-integration.json`

1. **Webhook de la tienda**: Header Auth con nombre exacto `X-Webhook-Token`. El valor debe coincidir con `integration.webhook_secret` en `config.php` de la tienda.
2. **Upsert en back-office**, ambas herramientas HTTP, **Verifica propuesta guardada por agente**, **Guardar propuesta del agente** y **Guardar derivación humana**: Header Auth con nombre `X-API-Key`. El valor debe coincidir con `BACKOFFICE_API_TOKEN` en las variables de Render. No reutilices la credencial del webhook.
3. **Proveedor IA: Gemini**: asigna la credencial Google Gemini API de Google AI Studio configurada en el nivel gratuito. No actives facturación. La salida de Gemini se valida antes de guardarse; si no viene como JSON válido, el flujo guarda una propuesta conservadora para revisión humana.
4. Comprueba que las herramientas **Consultar pedido e historial** y **Proponer y guardar borrador** estén conectadas al puerto **Tool** del agente. Sus definiciones de entrada ya incluyen `case_id` y los campos de la propuesta. El `case_id` es `data.case_id` del evento; si no existe, se usa `event_id`, igual que en la creación del caso en el back-office.
5. Tras la acción del agente, n8n verifica en el back-office que el caso cambió a `pending_review`. Si el agente no guardó la propuesta, normaliza su salida y realiza un guardado de respaldo; si Gemini falla, crea una derivación humana. Solo devuelve 202 cuando el back-office confirma que guardó el caso para revisión.

### `workflows/telegram-approved.json`

1. **Webhook aprobación back-office**: Header Auth `X-Approval-Token`, cuyo valor debe coincidir con `N8N_APPROVAL_TOKEN` en Render. Debe ser un secreto distinto de `X-API-Key` y `X-Webhook-Token`.
2. Asigna la misma credencial `X-API-Key` a **Comprueba aprobación en back-office**, **Registra envío correcto** y **Registra fallo de Telegram**.
3. En **Envía al chat de prueba**, selecciona la credencial del bot de pruebas y reemplaza `REEMPLAZAR_CHAT_ID_DE_PRUEBA` por el ID numérico del chat privado de pruebas. Abre primero el chat y pulsa `/start`.
4. En Render, establece `N8N_APPROVAL_WEBHOOK_URL` como `https://<tu-dominio-n8n>/webhook/telegram-approved`. El flujo verifica que el caso figure como `approved` en el back-office antes de enviar. Después registra `sent` o `send_failed`.

Activa el registro de ejecuciones exitosas y fallidas en n8n. No exportes credenciales ni pegues secretos en los JSON, capturas o archivos del proyecto.

## Prueba completa

1. Envía desde la tienda una solicitud de soporte ficticia.
2. En **Executions**, abre el workflow de integración y comprueba consulta y guardado de la propuesta.
3. En `https://<tu-backoffice>.onrender.com/admin/incidents`, confirma que el caso aparece como `pending_review`.
4. Pulsa **Aprobar y enviar a Telegram**. Confirma que el mensaje llega al chat de pruebas y que el estado de Telegram aparece como `sent`.

Si el agente falla, abre la ejecución y revisa el primer nodo con error. El back-office conserva el estado `open` si no recibió una propuesta y el registro de eventos conserva el resultado de la integración.
