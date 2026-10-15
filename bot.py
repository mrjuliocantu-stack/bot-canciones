import os
import html
import requests
from urllib.parse import urlencode
from flask import Flask, request, redirect

app = Flask(__name__)

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
G_ID = os.environ.get("GOOGLE_CLIENT_ID")
G_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
G_REFRESH = os.environ.get("GOOGLE_REFRESH_TOKEN")
PLAYLIST_ID = os.environ.get("YOUTUBE_PLAYLIST_ID")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # https://tu-app.onrender.com

agregadas = set()  # evita duplicados mientras el servidor siga encendido


# ---------- YouTube ----------
def yt_token():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": G_REFRESH,
            "client_id": G_ID,
            "client_secret": G_SECRET,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def agregar_cancion(consulta):
    h = {"Authorization": f"Bearer {yt_token()}"}
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        headers=h,
        params={
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",  # música
            "maxResults": 1,
            "q": consulta,
        },
        timeout=15,
    )
    items = r.json().get("items", [])
    if not items:
        return "No encontré esa canción 😕 Intenta con 'artista - canción'."
    video_id = items[0]["id"]["videoId"]
    titulo = html.unescape(items[0]["snippet"]["title"])
    if video_id in agregadas:
        return f"Ya estaba en la playlist: {titulo}"
    a = requests.post(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        headers=h,
        params={"part": "snippet"},
        json={
            "snippet": {
                "playlistId": PLAYLIST_ID,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
        timeout=15,
    )
    if a.status_code in (200, 201):
        agregadas.add(video_id)
        return f"✅ Agregada: {titulo}"
    print("Error YouTube:", a.status_code, a.text)
    return f"No pude agregarla (error {a.status_code})."


# ---------- Telegram ----------
def responder(chat_id, texto):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": texto},
        timeout=15,
    )


@app.post("/telegram")
def telegram():
    msg = (request.json or {}).get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    texto = (msg.get("text") or "").strip()
    if not chat_id or not texto:
        return "ok"
    if texto.startswith("/start"):
        responder(chat_id, "¡Hola! 🎶 Mándame 'artista - canción' y la agrego a la playlist.")
        return "ok"
    try:
        responder(chat_id, agregar_cancion(texto))
    except Exception as e:
        print("Error:", e)
        responder(chat_id, "Hubo un error, intenta de nuevo.")
    return "ok"


# ---------- Login de Google (se usa UNA sola vez) ----------
@app.get("/login")
def login():
    q = urlencode({
        "client_id": G_ID,
        "response_type": "code",
        "redirect_uri": f"{BASE_URL}/callback",
        "scope": "https://www.googleapis.com/auth/youtube",
        "access_type": "offline",
        "prompt": "consent",
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{q}")


@app.get("/callback")
def callback():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "code": request.args.get("code"),
            "redirect_uri": f"{BASE_URL}/callback",
            "client_id": G_ID,
            "client_secret": G_SECRET,
        },
        timeout=15,
    )
    rt = r.json().get("refresh_token", "ERROR: " + r.text)
    return f"<h3>Copia este valor en la variable GOOGLE_REFRESH_TOKEN:</h3><textarea rows=5 cols=60>{rt}</textarea>"


@app.get("/")
def home():
    return "Bot de canciones activo 🎶"
