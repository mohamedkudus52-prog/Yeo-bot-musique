# -*- coding: utf-8 -*-
"""
MusicBot V2 - Telegram + Render + PostgreSQL

IMPORTANT:
- Put BOT_TOKEN only in Render Environment Variables.
- This bot uses yt-dlp for URLs/searches. Users are responsible for
  respecting copyright, platform terms and applicable law.
"""
import html
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from flask import Flask
from PIL import Image
import io
import telebot
from telebot import types
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# -------------------- Configuration --------------------
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")

PORT = int(os.environ.get("PORT", "10000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "49"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_DURATION = int(os.environ.get("MAX_DURATION", "900"))
MAX_ALBUM_TRACKS = int(os.environ.get("MAX_ALBUM_TRACKS", "10"))
COOLDOWN = float(os.environ.get("COOLDOWN_SECONDS", "5"))
WORKERS = int(os.environ.get("WORKERS", "4"))
ITUNES_COUNTRY = os.environ.get("ITUNES_COUNTRY", "FR")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Connect a Render PostgreSQL database.")

log = logging.getLogger("musicbot")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
executor = ThreadPoolExecutor(max_workers=WORKERS)
app = Flask(__name__)

# Per-chat conversation state. It is intentionally ephemeral.
state_lock = threading.Lock()
states = {}
cooldown_lock = threading.Lock()
last_request = {}

# -------------------- Database --------------------
def db():
    return psycopg.connect(DATABASE_URL, connect_timeout=10)

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        conn.commit()

def save_user(message):
    u = message.from_user
    with db() as conn:
        conn.execute("""
            INSERT INTO users(chat_id, first_name, username)
            VALUES (%s, %s, %s)
            ON CONFLICT(chat_id) DO UPDATE SET
                first_name = EXCLUDED.first_name,
                username = EXCLUDED.username,
                updated_at = NOW()
        """, (message.chat.id, u.first_name if u else None, u.username if u else None))
        conn.commit()

def save_search(chat_id, query, artist=None, title=None, album=None):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO searches(chat_id, query, artist, title, album) VALUES (%s,%s,%s,%s,%s)",
                (chat_id, query, artist, title, album),
            )
            conn.commit()
    except Exception:
        log.exception("Could not save search")

# -------------------- Web health --------------------
@app.get("/")
def home():
    return "MusicBot V2 is running", 200

@app.get("/health")
def health():
    return "ok", 200

def start_web():
    # Render web services must listen on 0.0.0.0 and the supplied PORT.
    app.run(host="0.0.0.0", port=PORT, threaded=True)

# -------------------- UI helpers --------------------
def esc(value):
    return html.escape(str(value or ""), quote=False)

def send(chat_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    return bot.send_message(chat_id, text, **kwargs)

def edit(chat_id, message_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        return None

def answer(call, text="", alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass

def music_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎵 MUSIC", callback_data="mode_music"),
        types.InlineKeyboardButton("🎬 VIDEO", callback_data="mode_video"),
    )
    return kb

def cancel_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Annuler", callback_data="cancel"))
    return kb

def result_keyboard(album_id=None):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("⬇️ Télécharger", callback_data="download_track"))
    if album_id:
        kb.add(types.InlineKeyboardButton("💿 Télécharger l'album", callback_data=f"album:{album_id}"))
    kb.add(types.InlineKeyboardButton("🔎 Nouvelle recherche", callback_data="mode_music"))
    return kb

def cooldown_ok(chat_id):
    now = __import__("time").monotonic()
    with cooldown_lock:
        previous = last_request.get(chat_id, 0)
        if now - previous < COOLDOWN:
            return False, max(1, int(COOLDOWN - (now - previous)) + 1)
        last_request[chat_id] = now
    return True, 0

def set_state(chat_id, **values):
    with state_lock:
        current = states.get(chat_id, {})
        current.update(values)
        states[chat_id] = current

def get_state(chat_id):
    with state_lock:
        return dict(states.get(chat_id, {}))

def clear_state(chat_id):
    with state_lock:
        states.pop(chat_id, None)

# -------------------- iTunes metadata --------------------
def itunes(endpoint, params):
    url = "https://itunes.apple.com/" + endpoint + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "MusicBotV2/1.0"})
    with urlopen(req, timeout=12) as r:
        import json
        return json.loads(r.read().decode("utf-8"))

def search_itunes(query, artist=None):
    term = f"{artist} {query}" if artist else query
    data = itunes("search", {
        "term": term,
        "media": "music",
        "entity": "song",
        "limit": 20,
        "country": ITUNES_COUNTRY,
    })
    results = data.get("results", [])
    if artist:
        target = artist.lower()
        results.sort(key=lambda x: 0 if target in (x.get("artistName") or "").lower() else 1)
    return results

def album_tracks(album_id):
    data = itunes("lookup", {
        "id": album_id,
        "entity": "song",
        "country": ITUNES_COUNTRY,
    })
    results = data.get("results", [])
    album = next((x for x in results if x.get("wrapperType") == "collection"), {})
    tracks = [x for x in results if x.get("wrapperType") == "track" and x.get("kind") == "song"]
    tracks.sort(key=lambda x: (x.get("discNumber", 1), x.get("trackNumber", 0)))
    return album, tracks

# -------------------- Download helpers --------------------
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".webm", ".aac", ".flac", ".wav", ".mp4"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

def ydl_opts(folder, video=False):
    if video:
        fmt = "best[ext=mp4][height<=1080]/best[ext=mp4]/best"
    else:
        fmt = "bestaudio[ext=m4a]/bestaudio/best"

    return {
        "format": fmt,
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILE_BYTES,
        "overwrites": False,
    }

def find_media(folder, video=False):
    allowed = VIDEO_EXTS if video else AUDIO_EXTS
    files = [
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in allowed
    ]
    return max(files, key=os.path.getsize) if files else None

def prepare_cover(url, path):
    if not url:
        return None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            raw = r.read(5 * 1024 * 1024)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w-side)//2, (h-side)//2, (w+side)//2, (h+side)//2))
        img = img.resize((320, 320))
        img.save(path, "JPEG", quality=82, optimize=True)
        return path
    except Exception:
        log.exception("Cover preparation failed")
        return None

def download_audio(query):
    with tempfile.TemporaryDirectory(prefix="music_") as folder:
        target = f"ytsearch1:{query}"
        try:
            with YoutubeDL(ydl_opts(folder, video=False)) as ydl:
                info = ydl.extract_info(target, download=True)
        except DownloadError as exc:
            raise RuntimeError("Le service vidéo n'a pas pu récupérer ce titre pour le moment.") from exc

        if info and "entries" in info:
            entries = [x for x in info.get("entries") or [] if x]
            info = entries[0] if entries else None
        path = find_media(folder, video=False)
        if not info or not path:
            raise RuntimeError("Aucun résultat exploitable n'a été trouvé.")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise RuntimeError(f"Le fichier dépasse la limite configurée de {MAX_FILE_MB} Mo.")

        # Telegram upload happens before TemporaryDirectory is destroyed.
        return {
            "bytes": open(path, "rb").read(),
            "title": (info.get("title") or query)[:200],
            "artist": (info.get("artist") or info.get("creator") or info.get("uploader") or "Artiste inconnu")[:100],
            "album": info.get("album"),
            "duration": int(info["duration"]) if info.get("duration") else None,
            "thumbnail": info.get("thumbnail"),
        }

def download_video(url):
    with tempfile.TemporaryDirectory(prefix="video_") as folder:
        try:
            with YoutubeDL(ydl_opts(folder, video=True)) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadError as exc:
            raise RuntimeError("Je n'ai pas pu récupérer ce média. Vérifie le lien puis réessaie.") from exc
        path = find_media(folder, video=True)
        if not info or not path:
            raise RuntimeError("Aucun fichier vidéo exploitable n'a été trouvé.")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise RuntimeError(f"Le fichier dépasse la limite configurée de {MAX_FILE_MB} Mo.")
        return {
            "bytes": open(path, "rb").read(),
            "title": (info.get("title") or "Vidéo")[:200],
            "duration": int(info["duration"]) if info.get("duration") else None,
        }

# -------------------- Music flow --------------------
def search_and_show(chat_id):
    st = get_state(chat_id)
    title = st.get("title", "").strip()
    artist = st.get("artist", "").strip()
    status = send(chat_id, f"🔎 <i>Je recherche <b>{esc(title)}</b>{' de ' + esc(artist) if artist else ''}…</i>")
    try:
        results = search_itunes(title, artist)
    except Exception:
        log.exception("iTunes search failed")
        edit(chat_id, status.message_id, "⚠️ Le service de recherche est momentanément indisponible. Réessaie dans quelques minutes.")
        return

    if not results:
        edit(chat_id, status.message_id, "⚠️ Je n'ai trouvé aucun résultat. Essaie avec le titre et l'artiste.")
        return

    # Pick a metadata match. We keep alternatives available if several results exist.
    best = results[0]
    album_id = best.get("collectionId")
    set_state(
        chat_id,
        mode="music",
        title=best.get("trackName") or title,
        artist=best.get("artistName") or artist,
        album=best.get("collectionName"),
        album_id=str(album_id) if album_id else None,
        release_date=(best.get("releaseDate") or "")[:4],
        search_results=results[:8],
    )
    save_search(
        chat_id,
        title,
        best.get("artistName"),
        best.get("trackName"),
        best.get("collectionName"),
    )
    year = (best.get("releaseDate") or "")[:4]
    album = best.get("collectionName") or "Album inconnu"
    text = (
        f"🎵 <b>{esc(best.get('trackName') or title)}</b>\n"
        f"👤 <b>{esc(best.get('artistName') or artist or 'Artiste inconnu')}</b>\n"
        f"💿 <b>{esc(album)}</b>"
        + (f"\n📅 {year}" if year else "")
        + "\n\n🫴 Voilà ce que j'ai trouvé."
    )
    edit(chat_id, status.message_id, text, reply_markup=result_keyboard(album_id))

def ask_for_artist(chat_id, title):
    set_state(chat_id, mode="music", step="waiting_artist", title=title)
    send(
        chat_id,
        f"🎵 <b>{esc(title)}</b> — excellent choix !\n\n"
        "Pour être plus précis, <b>qui chante ce titre ?</b>\n"
        "👤 Donne-moi simplement le nom de l'artiste.",
        reply_markup=cancel_keyboard(),
    )

def process_track(chat_id):
    st = get_state(chat_id)
    title = st.get("title", "")
    artist = st.get("artist", "")
    status = send(chat_id, f"⏳ <i>Je prépare <b>{esc(title)}</b> de {esc(artist)}…</i>")
    try:
        data = download_audio(f"{artist} {title}".strip())
        audio = io.BytesIO(data["bytes"])
        audio.name = "audio.m4a"
        caption = f"🎵 <b>{esc(title)}</b>\n👤 {esc(artist)}"
        if data.get("album"):
            caption += f"\n💿 {esc(data['album'])}"
        bot.send_audio(
            chat_id,
            audio,
            title=title[:200],
            performer=artist[:100],
            duration=data.get("duration"),
            caption=caption[:1000],
            parse_mode="HTML",
        )
        edit(chat_id, status.message_id, "✅ Voilà pour toi !")
    except Exception as exc:
        log.exception("Track failed")
        edit(chat_id, status.message_id, f"⚠️ Je n'ai pas pu récupérer ce titre pour le moment.\n<i>{esc(str(exc))}</i>")

def process_album(chat_id, album_id):
    status = send(chat_id, "💿 <i>Je récupère la liste des pistes…</i>")
    try:
        album, tracks = album_tracks(album_id)
    except Exception:
        log.exception("Album lookup failed")
        edit(chat_id, status.message_id, "⚠️ Je n'arrive pas à récupérer cet album pour le moment. Réessaie dans quelques minutes.")
        return

    if not tracks:
        edit(chat_id, status.message_id, "⚠️ Cet album ne contient aucune piste exploitable.")
        return

    tracks = tracks[:MAX_ALBUM_TRACKS]
    total = len(tracks)
    ok = 0
    for index, track in enumerate(tracks, 1):
        title = track.get("trackName") or f"Piste {index}"
        artist = track.get("artistName") or album.get("artistName") or "Artiste inconnu"
        edit(chat_id, status.message_id, f"💿 <b>{esc(album.get('collectionName') or 'Album')}</b>\n⏬ Piste <b>{index}/{total}</b> : {esc(title)}")
        try:
            data = download_audio(f"{artist} {title}")
            audio = io.BytesIO(data["bytes"])
            audio.name = "audio.m4a"
            bot.send_audio(
                chat_id,
                audio,
                title=title[:200],
                performer=artist[:100],
                duration=data.get("duration"),
                caption=f"💿 <b>Piste #{index}</b> — {esc(title)}\n👤 {esc(artist)}"[:1000],
                parse_mode="HTML",
            )
            ok += 1
        except Exception:
            log.exception("Album track failed: %s", title)

    edit(
        chat_id,
        status.message_id,
        f"✅ Album terminé : <b>{ok}/{total}</b> piste(s) envoyée(s).",
    )

# -------------------- Video flow --------------------
def process_video(chat_id, url):
    status = send(chat_id, "🎬 <i>Je traite ton lien…</i>")
    try:
        data = download_video(url)
        video = io.BytesIO(data["bytes"])
        video.name = "video.mp4"
        bot.send_video(
            chat_id,
            video,
            caption=f"🎬 <b>{esc(data['title'])}</b>"[:1000],
            duration=data.get("duration"),
            supports_streaming=True,
            parse_mode="HTML",
        )
        edit(chat_id, status.message_id, "✅ Voilà ta vidéo.")
    except Exception as exc:
        log.exception("Video failed")
        edit(chat_id, status.message_id, f"⚠️ Je n'ai pas pu traiter ce lien.\n<i>{esc(str(exc))}</i>")

# -------------------- Commands --------------------
@bot.message_handler(commands=["start"])
def start(message):
    save_user(message)
    clear_state(message.chat.id)
    name = message.from_user.first_name if message.from_user else "ami"
    send(
        message.chat.id,
        f"👋 <b>Bonjour {esc(name)} !</b>\n\n"
        "🎧 Bienvenue sur ton assistant multimédia.\n"
        "Choisis un mode :",
        reply_markup=music_keyboard(),
    )

@bot.message_handler(commands=["aide", "help"])
def help_cmd(message):
    send(
        message.chat.id,
        "🎧 <b>Aide</b>\n\n"
        "🎵 <b>Music</b> : recherche un titre avec son artiste.\n"
        "🎬 <b>Video</b> : traite un lien média compatible.\n\n"
        "Tu peux aussi utiliser /start pour revenir au menu."
    )

# -------------------- Callbacks --------------------
@bot.callback_query_handler(func=lambda c: c.data == "mode_music")
def cb_music(call):
    answer(call)
    set_state(call.message.chat.id, mode="music", step="waiting_title")
    send(
        call.message.chat.id,
        "🎵 <b>Mode Music</b>\n\n"
        "Quel titre ou album souhaites-tu écouter aujourd'hui ?",
        reply_markup=cancel_keyboard(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "mode_video")
def cb_video(call):
    answer(call)
    set_state(call.message.chat.id, mode="video", step="waiting_url")
    send(
        call.message.chat.id,
        "🎬 <b>Mode Video</b>\n\n"
        "Envoie-moi le lien du média que tu veux traiter.",
        reply_markup=cancel_keyboard(),
    )

@bot.callback_query_handler(func=lambda c: c.data == "cancel")
def cb_cancel(call):
    answer(call, "Annulé")
    clear_state(call.message.chat.id)
    send(call.message.chat.id, "D'accord 👍", reply_markup=music_keyboard())

@bot.callback_query_handler(func=lambda c: c.data == "download_track")
def cb_download(call):
    answer(call, "Préparation…")
    chat_id = call.message.chat.id
    st = get_state(chat_id)
    if not st.get("title") or not st.get("artist"):
        send(chat_id, "⚠️ La recherche a expiré. Recommence avec /start.")
        return
    ok, wait = cooldown_ok(chat_id)
    if not ok:
        send(chat_id, f"⏳ Patiente encore {wait}s.")
        return
    executor.submit(process_track, chat_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("album:"))
def cb_album(call):
    chat_id = call.message.chat.id
    album_id = call.data.split(":", 1)[1]
    if not album_id.isdigit():
        answer(call, "Album invalide.", alert=True)
        return
    ok, wait = cooldown_ok(chat_id)
    if not ok:
        answer(call, f"Patiente {wait}s.", alert=True)
        return
    answer(call, "Album lancé…")
    executor.submit(process_album, chat_id, album_id)

# -------------------- Text messages --------------------
@bot.message_handler(content_types=["text"])
def text_message(message):
    save_user(message)
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    st = get_state(chat_id)
    mode = st.get("mode")
    step = st.get("step")

    if mode == "video" and step == "waiting_url":
        if not re.match(r"^https?://", text, re.I):
            send(chat_id, "⚠️ Envoie-moi un lien commençant par http:// ou https://.")
            return
        clear_state(chat_id)
        executor.submit(process_video, chat_id, text[:2000])
        return

    if mode == "music" and step == "waiting_artist":
        artist = text[:100]
        set_state(chat_id, artist=artist, step="searching")
        send(chat_id, f"👊 Je vois ! <b>{esc(st.get('title'))}</b> de <b>{esc(artist)}</b>.\n⏳ Je lance la recherche…")
        executor.submit(search_and_show, chat_id)
        return

    # Default: treat a normal text as a music title, but ask artist first.
    ask_for_artist(chat_id, text[:200])

# -------------------- Main --------------------
def main():
    init_db()
    threading.Thread(target=start_web, daemon=True).start()

    try:
        bot.remove_webhook()
    except Exception:
        pass

    try:
        bot.set_my_commands([
            types.BotCommand("start", "Ouvrir le menu"),
            types.BotCommand("aide", "Afficher l'aide"),
        ])
    except Exception:
        log.exception("Could not set commands")

    log.info("MusicBot V2 starting")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=20,
        skip_pending=True,
        allowed_updates=["message", "callback_query"],
    )

if __name__ == "__main__":
    main()
