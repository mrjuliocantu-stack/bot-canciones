# Bot de leads AislaTerm

Formulario de cotización + avisos a Telegram con botones de estado, recordatorio y reportes.

## Comandos
/hoy · /semana · /mes · /leads · /pendientes · /formulario · /exportar · /id

## Variables en Railway
- TELEGRAM_TOKEN – token del bot
- ADMIN_PIN – PIN para registrarse con `/start PIN`
- PRECIO_M2 – precio por m² de aplicación (aísla e impermeabiliza), ej. 160
- DATA_DIR=/app/data – con un Volume montado en /app/data (los datos no se borran)
- Opcional: RECORDATORIO_MIN (15), LEAD_API_KEY (para que elBot mande leads a POST /lead con header X-Api-Key)
- Opcional Google Sheets: SHEETS_WEBHOOK y SHEETS_KEY (ver apps_script.gs)

## Links del formulario
`https://TU-DOMINIO/?o=facebook` (cambia facebook por instagram, tiktok, whatsapp, etc.). `/formulario` te los da.
