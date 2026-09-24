import html
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock, RLock, Semaphore, Thread
from typing import Any
from urllib.parse import quote_plus

import requests
import telebot
from flask import Flask, jsonify
from PIL import Image
from telebot import apihelper
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from yt_dlp import YoutubeDL

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

# ============================================================
# CONFIGURATION
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_ALBUMS = max(1, min(10, int(os.getenv("MAX_ALBUMS", "5"))))
MAX_ALBUM_TRACKS = max(1, min(30, int(os.getenv("MAX_ALBUM_TRACKS", "25"))))
CACHE_TTL_SECONDS = 30 * 60
REQUEST_TIMEOUT = (10, 30)
MB_USER_AGENT = os.getenv(
    "MUSICBRAINZ_USER_AGENT",
    "TelegramMusicBot/2.0 (contact@example.com)",
)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN est manquant. Ajoute-le dans les variables d'environnement de Render."
    )

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram_music_bot")

# ============================================================
# FLASK / HEALTH CHECK
# ============================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot Telegram actif.", 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "telegram_music_bot"}), 200


def run_web_server() -> None:
    app.run(host="0.0.0.0", port=PORT, threaded=True)


def keep_alive() -> None:
    Thread(target=run_web_server, daemon=True).start()


# ============================================================
# TELEGRAM
# ============================================================
bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    threaded=True,
    num_threads=4,
)

# Un verrou par utilisateur évite plusieurs téléchargements simultanés
# dans le même chat.
user_locks: dict[int, Lock] = defaultdict(Lock)
state_lock = RLock()
user_names: dict[int, str] = {}
user_states: dict[int, str] = {}
search_counts: dict[int, int] = defaultdict(int)
artist_history: dict[int, list[str]] = defaultdict(list)

# Limite globale pour ne pas surcharger Render/yt-dlp.
download_semaphore = Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Cache temporaire des boutons album.
album_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
album_cache_lock = RLock()

# Cache MusicBrainz simple en mémoire.
mb_cache: dict[str, tuple[float, Any]] = {}
mb_cache_lock = RLock()
mb_rate_lock = Lock()
mb_last_request = 0.0

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"

# ============================================================
# OUTILS GÉNÉRAUX
# ============================================================
def clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def escape_html(value: str) -> str:
    return html.escape(clean_text(value))


def truncate(value: str, max_len: int) -> str:
    value = clean_text(value)
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def safe_name(value: str, max_len: int = 80) -> str:
    value = unicodedata.normalize("NFKC", clean_text(value))
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return truncate(value or "audio", max_len)


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", clean_text(text), re.I))


def delete_message_safe(chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def send_error(chat_id: int, user_name: str, public_message: str) -> None:
    try:
        bot.send_message(
            chat_id,
            f"❌ <b>{escape_html(user_name)}</b>, {escape_html(public_message)}",
        )
    except Exception:
        logger.exception("Impossible d'envoyer le message d'erreur au chat %s", chat_id)


def prune_album_cache() -> None:
    now = time.time()
    with album_cache_lock:
        expired = [
            key
            for key, (created, _) in album_cache.items()
            if now - created > CACHE_TTL_SECONDS
        ]
        for key in expired:
            album_cache.pop(key, None)


def cache_album(chat_id: int, album: dict[str, Any]) -> str:
    prune_album_cache()
    key = secrets.token_hex(6)
    with album_cache_lock:
        album_cache[(chat_id, key)] = (time.time(), album)
    return key


def get_cached_album(chat_id: int, key: str) -> dict[str, Any] | None:
    with album_cache_lock:
        item = album_cache.get((chat_id, key))
    if not item:
        return None
    created, album = item
    if time.time() - created > CACHE_TTL_SECONDS:
        with album_cache_lock:
            album_cache.pop((chat_id, key), None)
        return None
    return album


def choose_audio_file(directory: Path) -> Path | None:
    # La post-production peut modifier l'extension : ne jamais dépendre de
    # ydl.prepare_filename() pour le fichier final.
    preferred = ["*.m4a", "*.mp3", "*.opus", "*.aac", "*.webm"]
    for pattern in preferred:
        files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    return None


# ============================================================
# MUSICBRAINZ
# ============================================================
def musicbrainz_get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET MusicBrainz avec User-Agent et cadence <= 1 requête/s par bot."""
    global mb_last_request

    cache_key = endpoint + "?" + repr(sorted(params.items()))
    with mb_cache_lock:
        cached = mb_cache.get(cache_key)
    if cached and time.time() - cached[0] < 10 * 60:
        return cached[1]

    with mb_rate_lock:
        elapsed = time.monotonic() - mb_last_request
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        headers = {
            "User-Agent": MB_USER_AGENT,
            "Accept": "application/json",
        }
        response = requests.get(
            f"{MB_BASE}/{endpoint.lstrip('/')}",
            params={**params, "fmt": "json"},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        mb_last_request = time.monotonic()

    response.raise_for_status()
    data = response.json()
    with mb_cache_lock:
        mb_cache[cache_key] = (time.time(), data)
    return data


def search_best_artist(artist_query: str) -> dict[str, Any] | None:
    data = musicbrainz_get(
        "artist",
        {"query": f'artist:"{artist_query}"', "limit": 8},
    )
    artists = data.get("artists") or []
    if not artists:
        return None

    return max(
        artists,
        key=lambda artist: (
            similarity(artist_query, clean_text(artist.get("name"))),
            int(artist.get("score") or 0),
        ),
    )


def search_artist_albums(artist_query: str, limit: int = MAX_ALBUMS) -> list[dict[str, Any]]:
    artist = search_best_artist(artist_query)
    if not artist:
        return []

    mbid = clean_text(artist.get("id"))
    if not mbid:
        return []

    data = musicbrainz_get(
        "release-group",
        {
            "artist": mbid,
            "type": "album|ep",
            "release-group-status": "website-default",
            "limit": 100,
        },
    )
    groups = data.get("release-groups") or []

    # Dédoublonnage sur le titre : MusicBrainz sépare les versions dans des
    # release groups distincts uniquement quand le contenu est réellement différent.
    unique: dict[str, dict[str, Any]] = {}
    for group in groups:
        title = clean_text(group.get("title"))
        if not title:
            continue
        key = normalize_text(title)
        current = unique.get(key)
        if not current:
            unique[key] = group
            continue
        old_date = clean_text(current.get("first-release-date"))
        new_date = clean_text(group.get("first-release-date"))
        if new_date > old_date:
            unique[key] = group

    albums = sorted(
        unique.values(),
        key=lambda x: clean_text(x.get("first-release-date")),
        reverse=True,
    )

    result = []
    for group in albums[:limit]:
        result.append(
            {
                "artist": clean_text(artist.get("name"), artist_query),
                "artist_mbid": mbid,
                "release_group_mbid": clean_text(group.get("id")),
                "title": clean_text(group.get("title"), "Album sans titre"),
                "date": clean_text(group.get("first-release-date"), "Date inconnue"),
                "type": clean_text(group.get("primary-type"), "Album"),
            }
        )
    return result


def get_official_release_for_group(release_group_mbid: str) -> dict[str, Any] | None:
    data = musicbrainz_get(
        "release",
        {
            "release-group": release_group_mbid,
            "status": "official",
            "inc": "media+recordings+artist-credits",
            "limit": 100,
        },
    )
    releases = data.get("releases") or []
    if not releases:
        return None

    with_media = [r for r in releases if r.get("media")]
    candidates = with_media or releases

    # On privilégie la sortie officielle la plus récente qui possède une tracklist.
    return max(
        candidates,
        key=lambda release: clean_text(release.get("date")),
    )


def get_album_details(release_group_mbid: str) -> dict[str, Any] | None:
    release = get_official_release_for_group(release_group_mbid)
    if not release:
        return None

    tracks: list[dict[str, Any]] = []
    for media in release.get("media") or []:
        disc_number = media.get("position") or 1
        for track in media.get("tracks") or []:
            recording = track.get("recording") or {}
            title = clean_text(track.get("title")) or clean_text(recording.get("title"))
            if not title:
                continue
            tracks.append(
                {
                    "disc": int(disc_number),
                    "position": int(track.get("position") or len(tracks) + 1),
                    "title": title,
                }
            )

    if not tracks:
        return None

    artist_credit = release.get("artist-credit") or []
    artist_name = ""
    for item in artist_credit:
        if isinstance(item, dict):
            name = clean_text((item.get("artist") or {}).get("name"))
            join = clean_text(item.get("joinphrase"))
            artist_name += name + join
    artist_name = artist_name.strip()

    return {
        "artist": artist_name or "Artiste inconnu",
        "title": clean_text(release.get("title"), "Album"),
        "date": clean_text(release.get("date"), "Date inconnue"),
        "release_mbid": clean_text(release.get("id")),
        "release_group_mbid": release_group_mbid,
        "tracks": tracks,
    }


def download_cover_art(release_mbid: str, destination: Path) -> Path | None:
    if not release_mbid:
        return None

    # Cover Art Archive : petite image suffisamment légère pour Telegram.
    url = f"{CAA_BASE}/release/{release_mbid}/front-250"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        temp_source = destination.with_suffix(".source")
        temp_source.write_bytes(response.content)
        with Image.open(temp_source) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320))
            image.save(destination, "JPEG", quality=82, optimize=True)
        temp_source.unlink(missing_ok=True)

        # Telegram demande < 200 kB pour la miniature.
        if destination.stat().st_size > 190_000:
            with Image.open(destination) as image:
                image.save(destination, "JPEG", quality=68, optimize=True)
        return destination if destination.exists() and destination.stat().st_size < 200_000 else None
    except Exception:
        logger.exception("Échec récupération pochette %s", release_mbid)
        return None


# ============================================================
# YT-DLP
# ============================================================
def ffmpeg_location() -> str | None:
    if imageio_ffmpeg is not None:
        try:
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and Path(exe).exists():
                return exe
        except Exception:
            logger.exception("Impossible de localiser ffmpeg via imageio-ffmpeg")

    return shutil.which("ffmpeg")


def build_ydl_options(output_dir: Path) -> dict[str, Any]:
    opts: dict[str, Any] = {
        # Telegram sendAudio attend du MP3 ou du M4A.
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(output_dir / "audio.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 4,
        "fragment_retries": 4,
        "file_access_retries": 3,
        "socket_timeout": 25,
        "overwrites": True,
        "continuedl": True,
        "nopart": False,
        "windowsfilenames": True,
        "restrictfilenames": True,
        # Métadonnées sans dépendre d'un chemin de fichier fragile.
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            },
            {"key": "FFmpegMetadata"},
        ],
    }

    ffmpeg = ffmpeg_location()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    else:
        logger.warning("ffmpeg non trouvé : la conversion audio peut échouer")

    return opts


def extract_youtube_candidates(query: str, limit: int = 8) -> list[dict[str, Any]]:
    search_url = f"ytsearch{limit}:{query}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "retries": 2,
        "socket_timeout": 20,
    }

    with download_semaphore:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

    entries = info.get("entries") if info else None
    if not entries:
        return []

    candidates = []
    for entry in entries:
        if not entry:
            continue
        webpage_url = clean_text(entry.get("webpage_url"))
        if not webpage_url:
            entry_id = clean_text(entry.get("id"))
            extractor_key = clean_text(entry.get("ie_key")) or clean_text(entry.get("extractor_key"))
            if entry_id and "youtube" in extractor_key.lower():
                webpage_url = f"https://www.youtube.com/watch?v={entry_id}"
        if not webpage_url.startswith("http"):
            continue
        candidates.append(
            {
                "title": clean_text(entry.get("title")),
                "url": webpage_url,
                "uploader": clean_text(entry.get("uploader") or entry.get("channel")),
                "duration": entry.get("duration"),
            }
        )
    return candidates


def choose_best_video(candidates: list[dict[str, Any]], expected_title: str, expected_artist: str = "") -> dict[str, Any] | None:
    if not candidates:
        return None

    penalties = {
        "karaoke": 0.40,
        "instrumental": 0.25,
        "slowed": 0.25,
        "speed up": 0.25,
        "sped up": 0.25,
        "remix": 0.15,
        "live": 0.10,
        "cover": 0.30,
    }

    def score(candidate: dict[str, Any]) -> float:
        title = candidate["title"]
        title_norm = normalize_text(title)
        score_value = similarity(expected_title, title)
        if expected_artist:
            score_value += 0.45 * similarity(expected_artist, candidate.get("uploader", ""))
            if normalize_text(expected_artist) and normalize_text(expected_artist) in title_norm:
                score_value += 0.20
        for bad, penalty in penalties.items():
            if bad in title_norm:
                score_value -= penalty
        return score_value

    return max(candidates, key=score)


def search_track(query: str, expected_title: str, expected_artist: str = "") -> dict[str, Any] | None:
    candidates = extract_youtube_candidates(query, limit=8)
    return choose_best_video(candidates, expected_title, expected_artist)


def download_video_to_m4a(url: str, output_dir: Path) -> tuple[Path, dict[str, Any]]:
    opts = build_ydl_options(output_dir)

    with download_semaphore:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp n'a retourné aucune information")

    audio_path = choose_audio_file(output_dir)
    if not audio_path or not audio_path.exists():
        raise RuntimeError("Le fichier audio final est introuvable après le téléchargement")

    return audio_path, info


# ============================================================
# TÉLÉGRAM : ENVOI AUDIO
# ============================================================
def prepare_thumbnail(source_url: str | None, destination: Path) -> Path | None:
    if not source_url:
        return None
    try:
        response = requests.get(source_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        temp_source = destination.with_suffix(".source")
        temp_source.write_bytes(response.content)
        with Image.open(temp_source) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320))
            image.save(destination, "JPEG", quality=80, optimize=True)
        temp_source.unlink(missing_ok=True)
        if destination.stat().st_size >= 200_000:
            with Image.open(destination) as image:
                image.save(destination, "JPEG", quality=65, optimize=True)
        return destination if destination.stat().st_size < 200_000 else None
    except Exception:
        logger.warning("Impossible de préparer la miniature", exc_info=True)
        return None


def build_track_keyboard(title: str, artist: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    lyrics_url = f"https://www.google.com/search?q={quote_plus('paroles ' + title + ' ' + artist)}"
    markup.add(InlineKeyboardButton("📄 Paroles", url=lyrics_url))
    return markup


def send_audio_file(
    chat_id: int,
    audio_path: Path,
    info: dict[str, Any],
    artist: str,
    title: str,
    album: str | None = None,
    cover_path: Path | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    max_size = 50 * 1024 * 1024
    size = audio_path.stat().st_size
    if size > max_size:
        raise RuntimeError(
            f"Le fichier fait {size / (1024 * 1024):.1f} Mo, au-dessus de la limite Telegram de 50 Mo."
        )

    caption = f"🎵 <b>{escape_html(title)}</b>\n👤 <b>Artiste :</b> {escape_html(artist)}"
    if album:
        caption += f"\n💿 <b>Album :</b> {escape_html(album)}"
    caption = caption[:1024]

    duration = info.get("duration")
    title_for_telegram = truncate(title, 100)
    performer_for_telegram = truncate(artist, 100)

    with audio_path.open("rb") as audio_file:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "audio": audio_file,
            "title": title_for_telegram,
            "performer": performer_for_telegram,
            "caption": caption,
            "reply_markup": reply_markup,
        }
        if isinstance(duration, (int, float)) and duration > 0:
            kwargs["duration"] = int(duration)

        if cover_path and cover_path.exists() and cover_path.stat().st_size < 200_000:
            with cover_path.open("rb") as thumb:
                kwargs["thumbnail"] = InputFile(thumb)
                bot.send_audio(**kwargs)
        else:
            bot.send_audio(**kwargs)


# ============================================================
# LOGIQUE SINGLE TRACK
# ============================================================
def handle_single_track(chat_id: int, user_name: str, query: str) -> None:
    waiting = bot.send_message(
        chat_id,
        f"⚡ <b>Recherche du morceau pour {escape_html(user_name)}…</b>",
    )

    try:
        direct_url = query if is_url(query) else None

        with tempfile.TemporaryDirectory(prefix="tg_music_") as temp_dir:
            workdir = Path(temp_dir)
            cover = workdir / "cover.jpg"

            if direct_url:
                selected = {"url": direct_url, "title": query, "uploader": ""}
            else:
                selected = search_track(query, expected_title=query)
                if not selected:
                    raise RuntimeError("Aucun résultat vidéo exploitable n'a été trouvé")

            logger.info(
                "Téléchargement single: chat=%s query=%r url=%s",
                chat_id,
                query,
                selected["url"],
            )

            audio_path, info = download_video_to_m4a(selected["url"], workdir)

            title = clean_text(info.get("track")) or clean_text(info.get("title"), query)
            artist = (
                clean_text(info.get("artist"))
                or clean_text(info.get("creator"))
                or clean_text(info.get("uploader"))
                or "Artiste inconnu"
            )
            album = clean_text(info.get("album")) or None
            thumb_url = clean_text(info.get("thumbnail"))
            prepare_thumbnail(thumb_url, cover)

            markup = build_track_keyboard(title, artist)
            send_audio_file(
                chat_id,
                audio_path,
                info,
                artist,
                title,
                album=album,
                cover_path=cover if cover.exists() else None,
                reply_markup=markup,
            )

            with state_lock:
                search_counts[chat_id] += 1
                artist_history[chat_id].append(artist)
                if len(artist_history[chat_id]) > 20:
                    artist_history[chat_id] = artist_history[chat_id][-20:]

        delete_message_safe(chat_id, waiting.message_id)
        bot.send_message(chat_id, "✅ Morceau envoyé avec succès.")

    except Exception:
        logger.exception("Erreur single track | chat=%s | query=%r", chat_id, query)
        delete_message_safe(chat_id, waiting.message_id)
        send_error(chat_id, user_name, "je n’ai pas réussi à récupérer ce morceau. Réessaie avec un titre plus précis ou une URL directe.")


# ============================================================
# LOGIQUE ALBUMS
# ============================================================
def show_artist_albums(chat_id: int, user_name: str, artist_query: str) -> None:
    waiting = bot.send_message(
        chat_id,
        f"🔎 <b>Recherche des albums de {escape_html(artist_query)}…</b>",
    )

    try:
        albums = search_artist_albums(artist_query, MAX_ALBUMS)
        delete_message_safe(chat_id, waiting.message_id)

        if not albums:
            bot.send_message(
                chat_id,
                f"⚠️ Aucun album trouvé pour <b>{escape_html(artist_query)}</b>.",
            )
            return

        markup = InlineKeyboardMarkup()
        for index, album in enumerate(albums, start=1):
            key = cache_album(chat_id, album)
            label = f"💿 {truncate(album['title'], 40)}"
            markup.add(
                InlineKeyboardButton(
                    f"{index}. {label}",
                    callback_data=f"alb:{key}",
                )
            )

        text = f"💿 <b>Albums trouvés pour {escape_html(albums[0]['artist'])}</b>\n\n"
        for index, album in enumerate(albums, start=1):
            text += f"{index}. {escape_html(truncate(album['title'], 45))} — {escape_html(album['date'][:10])}\n"
        text += "\nChoisis un album :"
        bot.send_message(chat_id, text, reply_markup=markup)

    except requests.RequestException:
        logger.exception("MusicBrainz indisponible | chat=%s | query=%r", chat_id, artist_query)
        delete_message_safe(chat_id, waiting.message_id)
        send_error(chat_id, user_name, "le service de recherche musicale est temporairement indisponible.")
    except Exception:
        logger.exception("Erreur recherche albums | chat=%s | query=%r", chat_id, artist_query)
        delete_message_safe(chat_id, waiting.message_id)
        send_error(chat_id, user_name, "une erreur est survenue pendant la recherche des albums.")


def download_selected_album(chat_id: int, user_name: str, album: dict[str, Any]) -> None:
    lock = user_locks[chat_id]
    if not lock.acquire(blocking=False):
        bot.send_message(chat_id, "⏳ Un autre téléchargement est déjà en cours pour toi. Attends sa fin.")
        return

    waiting = bot.send_message(
        chat_id,
        f"💿 <b>Préparation de {escape_html(album['title'])}…</b>",
    )

    try:
        details = get_album_details(album["release_group_mbid"])
        if not details:
            raise RuntimeError("Impossible d'obtenir la tracklist MusicBrainz")

        tracks = details["tracks"][:MAX_ALBUM_TRACKS]
        if not tracks:
            raise RuntimeError("Tracklist vide")

        with tempfile.TemporaryDirectory(prefix="tg_album_") as temp_dir:
            workdir = Path(temp_dir)
            cover = workdir / "album_cover.jpg"
            download_cover_art(details.get("release_mbid", ""), cover)

            delete_message_safe(chat_id, waiting.message_id)
            waiting = None

            bot.send_message(
                chat_id,
                f"🎧 <b>{escape_html(details['artist'])} — {escape_html(details['title'])}</b>\n"
                f"{len(tracks)} piste(s) vont être traitées. Les pistes introuvables seront simplement ignorées.",
            )

            sent = 0
            failed = 0
            for index, track in enumerate(tracks, start=1):
                title = track["title"]
                progress = bot.send_message(
                    chat_id,
                    f"⏳ <b>Piste {index}/{len(tracks)}</b> — {escape_html(truncate(title, 70))}",
                )
                try:
                    query = f"{details['artist']} {title}"
                    selected = search_track(query, expected_title=title, expected_artist=details["artist"])
                    if not selected:
                        raise RuntimeError("Aucun résultat exploitable")

                    track_dir = workdir / f"track_{index:02d}"
                    track_dir.mkdir(parents=True, exist_ok=True)
                    audio_path, info = download_video_to_m4a(selected["url"], track_dir)

                    send_audio_file(
                        chat_id=chat_id,
                        audio_path=audio_path,
                        info=info,
                        artist=details["artist"],
                        title=title,
                        album=details["title"],
                        cover_path=cover if cover.exists() else None,
                        reply_markup=build_track_keyboard(title, details["artist"]),
                    )
                    sent += 1
                    time.sleep(0.6)
                except Exception:
                    failed += 1
                    logger.exception(
                        "Piste album échouée | chat=%s | album=%r | track=%r",
                        chat_id,
                        details["title"],
                        title,
                    )
                finally:
                    delete_message_safe(chat_id, progress.message_id)

            bot.send_message(
                chat_id,
                f"✅ <b>Album terminé</b>\n"
                f"Envoyées : <b>{sent}</b>\n"
                f"Échecs : <b>{failed}</b>",
            )

    except requests.RequestException:
        logger.exception("Erreur MusicBrainz/Cover Art | chat=%s", chat_id)
        if waiting:
            delete_message_safe(chat_id, waiting.message_id)
        send_error(chat_id, user_name, "le service de métadonnées musicales est temporairement indisponible.")
    except Exception:
        logger.exception("Erreur album | chat=%s | album=%r", chat_id, album)
        if waiting:
            delete_message_safe(chat_id, waiting.message_id)
        send_error(chat_id, user_name, "je n’ai pas réussi à traiter cet album.")
    finally:
        lock.release()


# ============================================================
# HANDLERS TELEGRAM
# ============================================================
@bot.message_handler(commands=["start"])
def message_bienvenue(message):
    chat_id = message.chat.id
    with state_lock:
        user_states[chat_id] = "attente_nom"
    bot.send_message(
        chat_id,
        "👋 <b>Bienvenue sur ton assistant musical !</b>\n\nComment dois-je t’appeler ?",
    )


@bot.message_handler(commands=["help"])
def help_command(message):
    bot.send_message(
        message.chat.id,
        "<b>Commandes</b>\n"
        "• /start — démarrer\n"
        "• /albums Artiste — chercher les albums\n"
        "• Envoie un titre, un artiste, ou une URL directe pour un morceau.\n\n"
        "Utilise le téléchargement uniquement lorsque tu as le droit de récupérer le contenu concerné.",
    )


@bot.message_handler(commands=["albums"])
def albums_command(message):
    chat_id = message.chat.id
    text = clean_text(message.text)
    artist_query = re.sub(r"^/albums(?:@\w+)?\s*", "", text, flags=re.I).strip()
    user_name = user_names.get(chat_id, clean_text(message.from_user.first_name, "Ami"))

    if not artist_query:
        bot.send_message(chat_id, "Exemple : <code>/albums Burna Boy</code>")
        return
    show_artist_albums(chat_id, user_name, artist_query)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    text = clean_text(message.text)
    if not text:
        return

    if text.startswith("/"):
        return

    with state_lock:
        state = user_states.get(chat_id)

    if state == "attente_nom":
        name = truncate(text, 50)
        with state_lock:
            user_names[chat_id] = name
            user_states[chat_id] = "actif"
            artist_history[chat_id] = []
            search_counts[chat_id] = 0
        bot.send_message(
            chat_id,
            f"Enchanté <b>{escape_html(name)}</b> ! 🎧\n\n"
            "Envoie-moi le nom d'un titre ou utilise <code>/albums Artiste</code>.",
        )
        return

    user_name = user_names.get(chat_id, clean_text(message.from_user.first_name, "Ami"))
    lower = text.lower()

    # Compatibilité avec l'ancien usage : "album Artiste" ou "albums Artiste".
    if re.match(r"^albums?\b", lower):
        artist_query = re.sub(r"^albums?\s*[:\-]?\s*", "", text, flags=re.I).strip()
        if artist_query:
            show_artist_albums(chat_id, user_name, artist_query)
            return

    lock = user_locks[chat_id]
    if not lock.acquire(blocking=False):
        bot.send_message(chat_id, "⏳ Un téléchargement est déjà en cours pour toi. Attends sa fin.")
        return

    try:
        handle_single_track(chat_id, user_name, text)
    finally:
        lock.release()


@bot.callback_query_handler(func=lambda call: call.data.startswith("alb:"))
def callback_album(call):
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    album = get_cached_album(chat_id, key)
    user_name = user_names.get(chat_id, clean_text(call.from_user.first_name, "Ami"))

    if not album:
        bot.answer_callback_query(call.id, "Cette sélection a expiré. Relance /albums.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Album sélectionné.")
    Thread(
        target=download_selected_album,
        args=(chat_id, user_name, album),
        daemon=True,
    ).start()


# ============================================================
# DÉMARRAGE
# ============================================================
def start_bot() -> None:
    keep_alive()

    # Évite qu'un ancien webhook empêche le long polling.
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.warning("delete_webhook a échoué", exc_info=True)

    logger.info("Bot Telegram démarré")
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
        except apihelper.ApiTelegramException as exc:
            if getattr(exc, "error_code", None) == 409:
                logger.error(
                    "409 Conflict : une autre instance du bot utilise probablement le même token."
                )
                time.sleep(15)
            else:
                logger.exception("Erreur Telegram API")
                time.sleep(5)
        except Exception:
            logger.exception("Erreur polling inattendue")
            time.sleep(5)


if __name__ == "__main__":
    start_bot()
