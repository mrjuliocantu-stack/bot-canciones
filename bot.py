"""
Bot de leads AislaTerm
- Sirve un formulario de cotización (index.html) en la raíz del dominio de Railway.
- Cada solicitud llega a Telegram con botón de WhatsApp y botones de estado.
- Guarda todo en Google Sheets (vía Apps Script) para reportes y Looker Studio.
- Recordatorio si un lead no se contacta a tiempo y reporte semanal automático.
"""
import asyncio
import datetime as dt
import html
import json
import logging
import os
import re
import time
import urllib.parse
from collections import Counter

import aiohttp
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from zoneinfo import ZoneInfo

# ---------------- Configuración (variables de Railway) ----------------
TZ = ZoneInfo(os.getenv("ZONA_HORARIA", "America/Monterrey"))
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_PIN = os.getenv("ADMIN_PIN", "")
ADMIN_IDS_ENV = os.getenv("ADMIN_CHAT_IDS", "")
SHEETS_URL = os.getenv("SHEETS_WEBHOOK", "")
SHEETS_KEY = os.getenv("SHEETS_KEY", "")
LEAD_API_KEY = os.getenv("LEAD_API_KEY", "")  # para que elBot (WhatsApp) mande leads
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "data")
RECORDATORIO_MIN = int(os.getenv("RECORDATORIO_MIN", "15"))
NEGOCIO = os.getenv("NEGOCIO", "AislaTerm")


def _precio(var):
    try:
        return float(os.getenv(var, "0") or 0)
    except ValueError:
        return 0.0


# El poliuretano aísla e impermeabiliza en la misma aplicación: un solo precio por m².
PRECIO_M2 = _precio("PRECIO_M2")
PRECIOS = {
    "impermeabilizacion": _precio("PRECIO_IMPER") or PRECIO_M2,
    "aislamiento": _precio("PRECIO_AISLANTE") or PRECIO_M2,
    "ambos": PRECIO_M2 or (_precio("PRECIO_IMPER") + _precio("PRECIO_AISLANTE")),
    "no_se": PRECIO_M2,
}

SERVICIOS = {
    "impermeabilizacion": "Impermeabilización",
    "aislamiento": "Aislamiento térmico",
    "ambos": "Impermeabilización + aislamiento",
    "no_se": "No sabe / asesoría",
}
ESTADOS = {
    "nuevo": "🆕 Nuevo",
    "contactado": "✅ Contactado",
    "cotizado": "📄 Cotizado",
    "vendido": "💰 Vendido",
    "perdido": "❌ Perdido",
}
COLS = ["id", "fecha", "nombre", "telefono", "zona", "servicio", "superficie", "m2",
        "estimado", "comentarios", "origen", "estado", "actualizado", "por"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aislaterm")
os.makedirs(DATA_DIR, exist_ok=True)
LEADS_FILE = os.path.join(DATA_DIR, "leads.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
BASE = os.path.dirname(os.path.abspath(__file__))

_tareas = set()  # evita que asyncio borre tareas en segundo plano


# ---------------- Utilidades ----------------
def ahora():
    return dt.datetime.now(TZ)


def leer_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defecto


def guardar_json(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


def admins():
    ids = set(leer_json(ADMINS_FILE, []))
    ids.update(int(x) for x in re.findall(r"-?\d+", ADMIN_IDS_ENV))
    return ids


def num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def fmt_dinero(v):
    return f"${v:,.0f}"


def fecha_de(lead):
    """Acepta '2026-10-21 10:55', ISO con zona, o ISO UTC que regresa Google Sheets."""
    s = str(lead.get("fecha", "")).strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(TZ)


def tel_whatsapp(tel):
    d = re.sub(r"\D", "", str(tel))
    if len(d) == 10:
        d = "52" + d
    return d


def nuevo_id():
    return "L" + ahora().strftime("%y%m%d%H%M%S") + str(int(time.time() * 1000) % 1000).zfill(3)


# ---------------- Almacenamiento: local + Google Sheets ----------------
LEADS = {l["id"]: l for l in leer_json(LEADS_FILE, [])}


def guardar_local():
    guardar_json(LEADS_FILE, list(LEADS.values()))


async def sheets_post(payload):
    if not SHEETS_URL:
        return
    payload = dict(payload, k=SHEETS_KEY)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(SHEETS_URL, data=json.dumps(payload),
                              headers={"Content-Type": "text/plain"}) as r:
                txt = await r.text()
                if r.status >= 400:
                    log.warning("Sheets respondió %s: %s", r.status, txt[:200])
    except Exception as e:
        log.warning("No se pudo guardar en Sheets: %s", e)


async def todos_los_leads():
    """Usa Google Sheets como fuente principal (sobrevive a redeploys); si falla, usa lo local."""
    if SHEETS_URL:
        try:
            url = SHEETS_URL + ("&" if "?" in SHEETS_URL else "?") + "k=" + urllib.parse.quote(SHEETS_KEY)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(url) as r:
                    datos = json.loads(await r.text())
                    if isinstance(datos, list):
                        for l in datos:  # refresca la caché local
                            if l.get("id"):
                                LEADS.setdefault(str(l["id"]), l)
                        return datos
        except Exception as e:
            log.warning("No se pudo leer Sheets, uso datos locales: %s", e)
    return list(LEADS.values())


# ---------------- Mensajes ----------------
def texto_lead(l):
    e = html.escape
    m2 = num(l.get("m2"))
    est = num(l.get("estimado"))
    lineas = [
        f"🔥 <b>Nuevo lead</b> · {e(l.get('origen') or 'web')}",
        f"👤 <b>{e(l['nombre'])}</b>",
        f"📱 {e(l['telefono'])}",
    ]
    if l.get("zona"):
        lineas.append(f"📍 {e(l['zona'])}")
    serv = SERVICIOS.get(l.get("servicio"), l.get("servicio") or "")
    sup = f" · {e(l['superficie'])}" if l.get("superficie") else ""
    lineas.append(f"🛠 {e(serv)}{sup}")
    if m2:
        lineas.append(f"📐 {m2:,.0f} m²")
    if est:
        lineas.append(f"💲 Estimado: <b>{fmt_dinero(est)}</b>")
    if l.get("comentarios"):
        lineas.append(f"📝 {e(l['comentarios'])}")
    lineas.append(f"🕒 {fecha_de(l).strftime('%d/%m %H:%M') if fecha_de(l) else ''} · <code>{l['id']}</code>")
    return "\n".join(lineas)


def botones(l):
    serv = SERVICIOS.get(l.get("servicio"), "tu proyecto").lower()
    msg = f"Hola {l['nombre'].split()[0]}, te escribimos de {NEGOCIO} por la cotización de {serv}."
    wa = f"https://wa.me/{tel_whatsapp(l['telefono'])}?text={urllib.parse.quote(msg)}"
    i = l["id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Escribirle por WhatsApp", url=wa)],
        [InlineKeyboardButton("✅ Contactado", callback_data=f"st|{i}|contactado"),
         InlineKeyboardButton("📄 Cotizado", callback_data=f"st|{i}|cotizado")],
        [InlineKeyboardButton("💰 Vendido", callback_data=f"st|{i}|vendido"),
         InlineKeyboardButton("❌ Perdido", callback_data=f"st|{i}|perdido")],
    ])


async def enviar_a_admins(tg, texto, markup=None):
    ids = admins()
    if not ids:
        log.warning("No hay administradores registrados; manda /start PIN al bot.")
    for cid in ids:
        try:
            await tg.bot.send_message(cid, texto, parse_mode="HTML", reply_markup=markup,
                                      disable_web_page_preview=True)
        except Exception as e:
            log.warning("No pude enviar a %s: %s", cid, e)


def en_segundo_plano(coro):
    t = asyncio.create_task(coro)
    _tareas.add(t)
    t.add_done_callback(_tareas.discard)


async def recordatorio(tg, lid):
    await asyncio.sleep(RECORDATORIO_MIN * 60)
    l = LEADS.get(lid)
    if l and l.get("estado") == "nuevo":
        await enviar_a_admins(
            tg,
            f"⏰ <b>{html.escape(l['nombre'])}</b> lleva {RECORDATORIO_MIN} min sin contacto. "
            f"Entre más rápido, más probable cerrar.",
            botones(l),
        )


async def registrar_lead(tg, datos, origen_defecto="web"):
    """Valida, guarda, avisa por Telegram. Regresa (ok, error)."""
    nombre = str(datos.get("nombre", "")).strip()[:80]
    tel = re.sub(r"[^\d+]", "", str(datos.get("telefono", "")))[:15]
    if len(nombre) < 2:
        return None, "Escribe tu nombre."
    if len(re.sub(r"\D", "", tel)) < 10:
        return None, "Escribe un teléfono de 10 dígitos."
    servicio = str(datos.get("servicio", "no_se"))
    if servicio not in SERVICIOS:
        servicio = "no_se"
    m2 = max(0.0, min(num(datos.get("m2")), 100000))
    precio = PRECIOS.get(servicio, 0)
    l = {
        "id": nuevo_id(),
        "fecha": ahora().strftime("%Y-%m-%d %H:%M"),
        "nombre": nombre,
        "telefono": tel,
        "zona": str(datos.get("zona", "")).strip()[:60],
        "servicio": servicio,
        "superficie": str(datos.get("superficie", "")).strip()[:40],
        "m2": round(m2, 1) if m2 else "",
        "estimado": round(m2 * precio) if (m2 and precio) else "",
        "comentarios": str(datos.get("comentarios", "")).strip()[:500],
        "origen": re.sub(r"[^\w-]", "", str(datos.get("origen") or origen_defecto))[:20] or origen_defecto,
        "estado": "nuevo",
        "actualizado": "",
        "por": "",
    }
    LEADS[l["id"]] = l
    guardar_local()
    await enviar_a_admins(tg, texto_lead(l), botones(l))
    en_segundo_plano(sheets_post({"action": "add", "lead": l}))
    if RECORDATORIO_MIN > 0:
        en_segundo_plano(recordatorio(tg, l["id"]))
    log.info("Lead %s de %s", l["id"], l["origen"])
    return l, None


# ---------------- Reportes ----------------
def resumen(leads, desde, titulo):
    ls = [l for l in leads if (fecha_de(l) or dt.datetime.min.replace(tzinfo=TZ)) >= desde]
    if not ls:
        return f"📊 <b>{titulo}</b>\nSin leads en este periodo."
    est = Counter((l.get("estado") or "nuevo") for l in ls)
    org = Counter((l.get("origen") or "web") for l in ls)
    serv = Counter(SERVICIOS.get(l.get("servicio"), "Otro") for l in ls)
    vendidos = [l for l in ls if l.get("estado") == "vendido"]
    m2_total = sum(num(l.get("m2")) for l in ls)
    m2_vend = sum(num(l.get("m2")) for l in vendidos)
    est_total = sum(num(l.get("estimado")) for l in ls)
    est_vend = sum(num(l.get("estimado")) for l in vendidos)
    conv = len(vendidos) / len(ls) * 100
    r = [f"📊 <b>{titulo}</b>", f"Leads: <b>{len(ls)}</b> · Vendidos: <b>{len(vendidos)}</b> ({conv:.0f}%)", ""]
    r += [f"{ESTADOS.get(k, k)}: {v}" for k, v in est.most_common()]
    r += ["", "<b>Por origen</b>"] + [f"• {html.escape(k)}: {v}" for k, v in org.most_common()]
    r += ["", "<b>Por servicio</b>"] + [f"• {k}: {v}" for k, v in serv.most_common()]
    r += ["", f"📐 m² solicitados: {m2_total:,.0f} · vendidos: {m2_vend:,.0f}"]
    if est_total:
        r.append(f"💲 Estimado solicitado: {fmt_dinero(est_total)} · vendido: {fmt_dinero(est_vend)}")
    sin = est.get("nuevo", 0)
    if sin:
        r += ["", f"⚠️ {sin} lead(s) siguen sin contactar. Usa /pendientes"]
    return "\n".join(r)


def inicio_dia(d):
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


async def reporte_semanal_loop(tg):
    enviado = None
    while True:
        try:
            n = ahora()
            if n.weekday() == 0 and n.hour == 8 and enviado != n.date():
                enviado = n.date()
                desde = inicio_dia(n) - dt.timedelta(days=7)
                await enviar_a_admins(tg, resumen(await todos_los_leads(), desde, "Reporte semanal (últimos 7 días)"))
        except Exception as e:
            log.warning("Error en reporte semanal: %s", e)
        await asyncio.sleep(60)


# ---------------- Comandos de Telegram ----------------
def solo_admin(fn):
    async def envoltura(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id not in admins():
            await update.effective_message.reply_text("🔒 Bot privado. Escribe /start seguido del PIN.")
            return
        return await fn(update, ctx)
    return envoltura


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in admins():
        await update.message.reply_text(f"✅ Ya estás registrado.\n\n{AYUDA}", parse_mode="HTML")
        return
    pin = ctx.args[0] if ctx.args else ""
    if not ADMIN_PIN or pin != ADMIN_PIN:
        await update.message.reply_text("🔒 Escribe: /start TU_PIN")
        return
    ids = set(leer_json(ADMINS_FILE, []))
    ids.add(cid)
    guardar_json(ADMINS_FILE, sorted(ids))
    await update.message.reply_text(
        f"✅ Registrado. Aquí te llegarán los leads.\n\n"
        f"Para que no se borre al actualizar Railway, agrega este número en la variable "
        f"<b>ADMIN_CHAT_IDS</b>: <code>{cid}</code>\n\n{AYUDA}",
        parse_mode="HTML",
    )


AYUDA = (
    "<b>Comandos</b>\n"
    "/hoy – resumen de hoy\n"
    "/semana – últimos 7 días\n"
    "/mes – este mes\n"
    "/leads – últimos 10 leads\n"
    "/pendientes – leads sin contactar\n"
    "/formulario – link del formulario por red social\n"
    "/exportar – todos los leads en archivo para Excel\n"
    "/id – tu número de chat"
)


@solo_admin
async def cmd_ayuda(update: Update, ctx):
    await update.message.reply_text(AYUDA, parse_mode="HTML")


async def cmd_id(update: Update, ctx):
    await update.message.reply_text(f"Tu chat id: <code>{update.effective_chat.id}</code>", parse_mode="HTML")


@solo_admin
async def cmd_hoy(update: Update, ctx):
    await update.message.reply_text(resumen(await todos_los_leads(), inicio_dia(ahora()), "Hoy"), parse_mode="HTML")


@solo_admin
async def cmd_semana(update: Update, ctx):
    desde = inicio_dia(ahora()) - dt.timedelta(days=6)
    await update.message.reply_text(resumen(await todos_los_leads(), desde, "Últimos 7 días"), parse_mode="HTML")


@solo_admin
async def cmd_mes(update: Update, ctx):
    desde = inicio_dia(ahora()).replace(day=1)
    await update.message.reply_text(resumen(await todos_los_leads(), desde, "Este mes"), parse_mode="HTML")


def linea_corta(l):
    f = fecha_de(l)
    return (f"{ESTADOS.get(l.get('estado') or 'nuevo', '')} · <b>{html.escape(str(l.get('nombre', '')))}</b> · "
            f"{num(l.get('m2')):,.0f} m² · {f.strftime('%d/%m') if f else ''} · {html.escape(str(l.get('origen', '')))}")


@solo_admin
async def cmd_leads(update: Update, ctx):
    ls = sorted(await todos_los_leads(), key=lambda l: fecha_de(l) or dt.datetime.min.replace(tzinfo=TZ), reverse=True)[:10]
    txt = "\n".join(linea_corta(l) for l in ls) or "Aún no hay leads."
    await update.message.reply_text("<b>Últimos leads</b>\n" + txt, parse_mode="HTML")


@solo_admin
async def cmd_pendientes(update: Update, ctx):
    ls = [l for l in await todos_los_leads() if (l.get("estado") or "nuevo") == "nuevo"]
    if not ls:
        await update.message.reply_text("🎉 No hay leads pendientes.")
        return
    for l in ls[-10:]:
        l = LEADS.get(str(l["id"]), l)
        await update.message.reply_text(texto_lead(l), parse_mode="HTML", reply_markup=botones(l),
                                        disable_web_page_preview=True)


@solo_admin
async def cmd_exportar(update: Update, ctx):
    import csv
    import io
    ls = sorted(await todos_los_leads(), key=lambda l: fecha_de(l) or dt.datetime.min.replace(tzinfo=TZ))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    for l in ls:
        w.writerow({c: l.get(c, "") for c in COLS})
    datos = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # utf-8-sig para que Excel lea acentos
    await update.message.reply_document(document=datos, filename=f"leads_{ahora():%Y-%m-%d}.csv",
                                        caption=f"{len(ls)} leads")


@solo_admin
async def cmd_formulario(update: Update, ctx):
    dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not dom:
        await update.message.reply_text("Primero genera el dominio en Railway (Settings → Networking → Generate Domain).")
        return
    base = f"https://{dom}/"
    txt = "<b>Links del formulario</b> (cada uno marca de dónde llegó el cliente)\n\n" + "\n".join(
        f"{red}: {base}?o={red.lower()}" for red in ["Facebook", "Instagram", "TikTok", "WhatsApp", "Google", "Volante"]
    )
    await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)


async def cb_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id not in admins():
        await q.answer("Sin permiso")
        return
    _, lid, estado = q.data.split("|")
    quien = q.from_user.first_name or "admin"
    cuando = ahora().strftime("%d/%m %H:%M")
    l = LEADS.get(lid)
    if l:
        l.update(estado=estado, actualizado=ahora().strftime("%Y-%m-%d %H:%M"), por=quien)
        guardar_local()
    en_segundo_plano(sheets_post({"action": "estado", "id": lid, "estado": estado,
                                  "actualizado": ahora().strftime("%Y-%m-%d %H:%M"), "por": quien}))
    base = re.split(r"\n\n<i>Estado:", q.message.text_html)[0]
    try:
        await q.edit_message_text(f"{base}\n\n<i>Estado: {ESTADOS[estado]} · {html.escape(quien)} · {cuando}</i>",
                                  parse_mode="HTML", reply_markup=q.message.reply_markup,
                                  disable_web_page_preview=True)
    except Exception:
        pass
    await q.answer(f"Marcado como {ESTADOS[estado]}")


# ---------------- Servidor web (formulario) ----------------
_intentos = {}


def ip_de(req):
    return (req.headers.get("X-Forwarded-For", "").split(",")[0].strip() or req.remote or "?")


async def pagina(req):
    return web.FileResponse(os.path.join(BASE, "index.html"))


async def salud(req):
    return web.Response(text="ok")


async def api_lead(req):
    if req.content_type == "application/json":
        datos = await req.json()
    else:
        datos = dict(await req.post())
    # elBot u otro sistema puede mandar leads con la clave
    clave_ok = LEAD_API_KEY and req.headers.get("X-Api-Key") == LEAD_API_KEY
    if not clave_ok:
        if datos.get("website"):  # trampa para robots
            return web.json_response({"ok": True})
        ip = ip_de(req)
        hora = time.time()
        lista = [t for t in _intentos.get(ip, []) if hora - t < 3600]
        if len(lista) >= 5:
            return web.json_response({"ok": False, "error": "Demasiados envíos, intenta más tarde."}, status=429)
        lista.append(hora)
        _intentos[ip] = lista
    l, err = await registrar_lead(req.app["tg"], datos, "api" if clave_ok else "web")
    if err:
        return web.json_response({"ok": False, "error": err}, status=400)
    return web.json_response({"ok": True, "id": l["id"]})


def crear_web(tg):
    app = web.Application()
    app["tg"] = tg
    app.router.add_get("/", pagina)
    app.router.add_get("/salud", salud)
    app.router.add_post("/lead", api_lead)
    return app


# ---------------- Arranque ----------------
async def main():
    if not TOKEN:
        raise SystemExit("Falta la variable TELEGRAM_TOKEN en Railway.")
    tg = Application.builder().token(TOKEN).build()
    tg.add_handler(CommandHandler("start", cmd_start))
    tg.add_handler(CommandHandler(["ayuda", "help"], cmd_ayuda))
    tg.add_handler(CommandHandler("id", cmd_id))
    tg.add_handler(CommandHandler("hoy", cmd_hoy))
    tg.add_handler(CommandHandler("semana", cmd_semana))
    tg.add_handler(CommandHandler("mes", cmd_mes))
    tg.add_handler(CommandHandler("leads", cmd_leads))
    tg.add_handler(CommandHandler("pendientes", cmd_pendientes))
    tg.add_handler(CommandHandler("formulario", cmd_formulario))
    tg.add_handler(CommandHandler("exportar", cmd_exportar))
    tg.add_handler(CallbackQueryHandler(cb_estado, pattern=r"^st\|"))

    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=True)

    runner = web.AppRunner(crear_web(tg))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Formulario en puerto %s · admins: %s", PORT, admins() or "ninguno")

    en_segundo_plano(reporte_semanal_loop(tg))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
