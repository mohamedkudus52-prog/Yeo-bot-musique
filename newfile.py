# -*- coding: utf-8 -*-
"""
Bot Telegram musical - version corrigée.

Variables d'environnement :
  TOKEN          (obligatoire) token du bot, donné par @BotFather
  PORT           port du mini serveur Flask (Render le fournit)
  MAX_DUREE_S    durée max d'un titre en secondes (défaut 900)
  MAX_PISTES     nombre max de pistes envoyées par album (défaut 10)
  COOLDOWN_S     délai minimum entre deux demandes d'un même chat (défaut 8)
  DL_SIMULTANES  téléchargements simultanés max (défaut 2)
  DATA_FILE      fichier JSON des prénoms (défaut utilisateurs.json)
  COOKIES_FILE   fichier cookies YouTube optionnel (défaut /etc/secrets/cookies.txt)
  ITUNES_PAYS    code pays pour la recherche d'albums (défaut FR)
"""
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager

import telebot
from flask import Flask
from telebot import apihelper
from telebot.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, match_filter_func

try:
    from PIL import Image
except ImportError:  # Pillow facultatif : sans lui, pas de miniature
    Image = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Si la variable d'environnement TOKEN existe (Render), elle est utilisée en priorité.
# Sinon, le token écrit ci-dessous sert de valeur par défaut.
TOKEN = os.environ.get("TOKEN") or "8803716438:AAEjyKCwbOkHnVbRgW4vGdYCG_uf92DNmWc"

PORT = int(os.environ.get("PORT", 8080))
MAX_OCTETS = 49 * 1024 * 1024          # limite Telegram bots : 50 Mo
MAX_DUREE_S = int(os.environ.get("MAX_DUREE_S", 15 * 60))
MAX_PISTES = int(os.environ.get("MAX_PISTES", 10))
COOLDOWN_S = int(os.environ.get("COOLDOWN_S", 8))
DL_SIMULTANES = int(os.environ.get("DL_SIMULTANES", 2))
DATA_FILE = os.environ.get("DATA_FILE", "utilisateurs.json")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
ITUNES_PAYS = os.environ.get("ITUNES_PAYS", "FR")
UA = "Mozilla/5.0 (compatible; MusicBot/2.0)"

EXT_AUDIO = {".m4a", ".mp3", ".opus", ".ogg", ".webm", ".aac", ".flac", ".wav", ".mp4"}
URL_YT = re.compile(r"^https?://((www|m|music)\.)?(youtube\.com|youtu\.be)/", re.I)
BRUIT_TITRE = re.compile(
    r"\s*[\(\[][^)\]]*\b(official|officiel|lyrics?|paroles|audio|video|vidéo|clip|hd|4k)\b[^)\]]*[\)\]]",
    re.I,
)

log = logging.getLogger("musicbot")

apihelper.RETRY_ON_ERROR = True
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

DL_SEM = threading.BoundedSemaphore(DL_SIMULTANES)


# ----------------------------------------------------------------------------
# Mini serveur Flask (pour Render + ping UptimeRobot)
# ----------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def accueil():
    return "Bot actif 24/7 !"


@app.route("/health")
def sante():
    return "ok"


def lancer_serveur():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT)


def keep_alive():
    threading.Thread(target=lancer_serveur, daemon=True).start()


# ----------------------------------------------------------------------------
# Données utilisateurs (JSON) et états en mémoire
# ----------------------------------------------------------------------------
_verrou_donnees = threading.Lock()


def _charger():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


prenoms = _charger()   # {"chat_id": "prénom"}
etats = {}             # {chat_id: "attente_nom"}


def definir_prenom(chat_id, prenom):
    with _verrou_donnees:
        prenoms[str(chat_id)] = prenom
        try:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prenoms, f, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
        except OSError:
            log.warning("Impossible d'écrire %s", DATA_FILE)


def obtenir_prenom(chat_id, defaut):
    return prenoms.get(str(chat_id)) or defaut or "ami"


class CacheCourt:
    """Stocke des valeurs longues derrière un jeton court (callback_data <= 64 octets)."""

    def __init__(self, taille=500):
        self._d = OrderedDict()
        self._taille = taille
        self._v = threading.Lock()

    def ajouter(self, valeur):
        cle = secrets.token_urlsafe(6)
        with self._v:
            self._d[cle] = valeur
            while len(self._d) > self._taille:
                self._d.popitem(last=False)
        return cle

    def lire(self, cle):
        with self._v:
            return self._d.get(cle)


cache_artistes = CacheCourt()

# --- Anti-spam et protection contre les demandes simultanées ---
_derniere = {}
_verrou_cd = threading.Lock()
_occupes = set()
_verrou_occ = threading.Lock()


def trop_rapide(chat_id, delai=COOLDOWN_S):
    """Retourne 0 si la demande est autorisée, sinon le nombre de secondes à attendre."""
    maintenant = time.monotonic()
    with _verrou_cd:
        reste = delai - (maintenant - _derniere.get(chat_id, -delai))
        if reste > 0:
            return int(reste) + 1
        _derniere[chat_id] = maintenant
    return 0


@contextmanager
def occupation(chat_id):
    """Empêche un même chat de lancer deux téléchargements en parallèle."""
    with _verrou_occ:
        libre = chat_id not in _occupes
        if libre:
            _occupes.add(chat_id)
    try:
        yield libre
    finally:
        if libre:
            with _verrou_occ:
                _occupes.discard(chat_id)


# ----------------------------------------------------------------------------
# Utilitaires Telegram
# ----------------------------------------------------------------------------
def e(texte):
    """Échappe le texte pour parse_mode HTML."""
    return html.escape(str(texte), quote=False)


def repondre(chat_id, texte, **kw):
    return bot.send_message(chat_id, texte, parse_mode="HTML", **kw)


def supprimer(chat_id, msg):
    if msg is None:
        return
    try:
        bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass


def modifier(chat_id, message_id, texte):
    try:
        bot.edit_message_text(texte, chat_id, message_id, parse_mode="HTML")
    except Exception:
        pass


def repondre_callback(call, texte="", alerte=False):
    try:
        bot.answer_callback_query(call.id, texte, show_alert=alerte)
    except Exception:
        pass


def envoyer_audio(chat_id, chemin, cover, **kw):
    """Envoie un audio avec miniature optionnelle (compatible thumb / thumbnail)."""
    kw.setdefault("parse_mode", "HTML")
    kw.setdefault("timeout", 180)
    with open(chemin, "rb") as audio:
        if cover and os.path.exists(cover):
            with open(cover, "rb") as vignette:
                try:
                    return bot.send_audio(chat_id, audio, thumbnail=vignette, **kw)
                except TypeError:  # anciennes versions de pyTelegramBotAPI
                    audio.seek(0)
                    vignette.seek(0)
                    return bot.send_audio(chat_id, audio, thumb=vignette, **kw)
        return bot.send_audio(chat_id, audio, **kw)


# ----------------------------------------------------------------------------
# Images : pochette JPEG carrée < 200 Ko, 320x320 max (exigence Telegram)
# ----------------------------------------------------------------------------
def preparer_pochette(url, sortie):
    if Image is None or not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(5 * 1024 * 1024)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        c = min(w, h)
        img = img.crop(((w - c) // 2, (h - c) // 2, (w + c) // 2, (h + c) // 2))
        img = img.resize((320, 320))
        img.save(sortie, "JPEG", quality=85, optimize=True)
        if os.path.getsize(sortie) > 200 * 1024:
            img.save(sortie, "JPEG", quality=60)
        return sortie
    except Exception as exc:
        log.debug("Pochette ignorée : %s", exc)
        return None


# ----------------------------------------------------------------------------
# Téléchargement (yt-dlp)
# ----------------------------------------------------------------------------
class ErreurUtilisateur(Exception):
    """Erreur dont le message peut être montré tel quel à l'utilisateur."""


def nettoyer_titre(titre):
    propre = BRUIT_TITRE.sub("", titre).strip(" -–")
    return propre or titre


def options_ydl(dossier):
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(dossier, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "nopart": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_OCTETS,
        "match_filter": match_filter_func(f"!is_live & duration <=? {MAX_DUREE_S}"),
        "socket_timeout": 20,
        "retries": 3,
    }
    if os.path.isfile(COOKIES_FILE):
        # copie dans le dossier temporaire : yt-dlp réécrit le fichier de cookies
        copie = os.path.join(dossier, "cookies.txt")
        shutil.copy(COOKIES_FILE, copie)
        opts["cookiefile"] = copie
    return opts


def trouver_audio(dossier):
    candidats = [
        os.path.join(dossier, f)
        for f in os.listdir(dossier)
        if os.path.splitext(f)[1].lower() in EXT_AUDIO
    ]
    return max(candidats, key=os.path.getsize) if candidats else None


def telecharger_piste(requete, dossier):
    """Télécharge un titre dans `dossier`. Retourne un dict d'infos."""
    # Seules les URL YouTube sont acceptées ; tout le reste est une recherche.
    cible = requete if URL_YT.match(requete) else f"ytsearch1:{requete}"

    try:
        with YoutubeDL(options_ydl(dossier)) as ydl:
            info = ydl.extract_info(cible, download=True)
    except DownloadError as exc:
        msg = str(exc)
        if "not a bot" in msg or "Sign in" in msg:
            log.error("YouTube bloque le serveur : %s", msg)
            raise ErreurUtilisateur(
                "YouTube bloque temporairement le serveur. Réessaie un peu plus tard."
            ) from exc
        raise

    if info and "entries" in info:
        entrees = [x for x in (info.get("entries") or []) if x]
        info = entrees[0] if entrees else None
    if not info:
        raise ErreurUtilisateur(
            f"aucun titre trouvé (ou durée supérieure à {MAX_DUREE_S // 60} min). "
            "Vérifie l'orthographe !"
        )

    fichier = trouver_audio(dossier)
    if not fichier:
        raise ErreurUtilisateur(
            "ce titre est trop long ou trop lourd pour être envoyé par Telegram (50 Mo max)."
        )
    if os.path.getsize(fichier) > MAX_OCTETS:
        raise ErreurUtilisateur("le fichier dépasse la limite de 50 Mo de Telegram.")

    artiste = info.get("artist") or info.get("creator") or info.get("uploader") or "Artiste inconnu"
    artiste = re.sub(r"\s*-\s*Topic$", "", artiste).strip() or "Artiste inconnu"
    duree = info.get("duration")
    return {
        "fichier": fichier,
        "titre": nettoyer_titre(info.get("title") or requete)[:200],
        "artiste": artiste[:100],
        "album": info.get("album"),
        "duree": int(duree) if duree else None,
        "cover_url": info.get("thumbnail"),
    }


# ----------------------------------------------------------------------------
# Recherche d'albums (API iTunes, gratuite et sans clé)
# ----------------------------------------------------------------------------
def itunes(endpoint, params):
    url = f"https://itunes.apple.com/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def chercher_albums(requete, par_artiste=False, n=3):
    params = {
        "term": requete,
        "media": "music",
        "entity": "album",
        "limit": 25,
        "country": ITUNES_PAYS,
    }
    if par_artiste:
        params["attribute"] = "artistTerm"
    resultats = itunes("search", params).get("results", [])
    albums = [r for r in resultats if r.get("collectionId") and r.get("collectionName")]
    if par_artiste:  # les plus récents d'abord
        albums.sort(key=lambda r: r.get("releaseDate", ""), reverse=True)

    vus, retenus = set(), []
    for a in albums:
        norm = re.sub(r"\s*[\(\[].*?[\)\]]", "", a["collectionName"]).lower().strip()
        if norm in vus or norm.endswith("- single"):
            continue
        vus.add(norm)
        retenus.append(a)
        if len(retenus) == n:
            break
    return retenus


def pistes_album(album_id):
    data = itunes("lookup", {"id": album_id, "entity": "song", "country": ITUNES_PAYS})
    res = data.get("results", [])
    album = next((r for r in res if r.get("wrapperType") == "collection"), {})
    pistes = [r for r in res if r.get("wrapperType") == "track" and r.get("kind") == "song"]
    pistes.sort(key=lambda r: (r.get("discNumber", 1), r.get("trackNumber", 0)))
    return album, pistes


def afficher_albums(chat_id, nom, requete, par_artiste=False):
    attente = repondre(chat_id, f"🔎 <i>Recherche des albums pour {e(nom)}…</i>")
    try:
        albums = chercher_albums(requete, par_artiste)
    except Exception:
        log.exception("Recherche d'albums échouée pour %r", requete)
        supprimer(chat_id, attente)
        repondre(chat_id, "⚠️ Le service de recherche d'albums est indisponible. Réessaie plus tard.")
        return
    supprimer(chat_id, attente)

    if not albums:
        repondre(chat_id, f"⚠️ Aucun album trouvé pour : <i>{e(requete)}</i>")
        return

    markup = InlineKeyboardMarkup()
    for a in albums:
        annee = (a.get("releaseDate") or "")[:4]
        libelle = a["collectionName"][:32] + (f" ({annee})" if annee else "")
        markup.add(InlineKeyboardButton(f"💿 {libelle}", callback_data=f"alb_{a['collectionId']}"))

    repondre(
        chat_id,
        f"💿 <b>Albums trouvés pour {e(nom)} :</b>\n"
        f"Clique sur celui que tu veux (max {MAX_PISTES} pistes envoyées).",
        reply_markup=markup,
    )


# ----------------------------------------------------------------------------
# Envoi d'un titre / d'un album
# ----------------------------------------------------------------------------
def clavier_titre(titre, artiste):
    markup = InlineKeyboardMarkup()
    recherche = urllib.parse.quote_plus(f"paroles {artiste[:60]} {titre[:100]}")
    markup.add(InlineKeyboardButton("📄 Paroles", url=f"https://www.google.com/search?q={recherche}"))
    if artiste != "Artiste inconnu":
        cle = cache_artistes.ajouter(artiste)
        markup.add(InlineKeyboardButton(f"💿 Albums de {artiste[:20]}", callback_data=f"art_{cle}"))
    return markup


def legende(titre, artiste, album=None):
    texte = f"🎵 <b>{e(titre)}</b>\n👤 <b>Artiste :</b> {e(artiste)}"
    if album:
        texte += f"\n💿 <b>Album :</b> {e(album)}"
    return texte[:1000]


def envoyer_titre(chat_id, nom, requete):
    bot.send_chat_action(chat_id, "upload_voice")
    attente = repondre(chat_id, f"⚡ <i>Analyse &amp; téléchargement pour {e(nom)}…</i>")
    try:
        with tempfile.TemporaryDirectory(prefix="bot_") as tmp:
            with DL_SEM:
                piste = telecharger_piste(requete, tmp)
            cover = preparer_pochette(piste["cover_url"], os.path.join(tmp, "cover.jpg"))
            envoyer_audio(
                chat_id,
                piste["fichier"],
                cover,
                title=piste["titre"],
                performer=piste["artiste"],
                duration=piste["duree"],
                caption=legende(piste["titre"], piste["artiste"], piste["album"]),
                reply_markup=clavier_titre(piste["titre"], piste["artiste"]),
            )
    except ErreurUtilisateur as err:
        repondre(chat_id, f"⚠️ {e(nom)}, {e(err)}")
    except Exception:
        log.exception("Erreur pour la requête %r", requete)
        repondre(chat_id, f"❌ {e(nom)}, une erreur est survenue. Réessaie avec un autre nom de titre.")
    finally:
        supprimer(chat_id, attente)


def telecharger_album(chat_id, nom, album_id):
    try:
        album, pistes = pistes_album(album_id)
    except Exception:
        log.exception("Lookup album %s échoué", album_id)
        repondre(chat_id, "⚠️ Impossible de récupérer la liste des pistes de cet album.")
        return
    if not pistes:
        repondre(chat_id, "⚠️ Aucune piste trouvée pour cet album.")
        return

    total = len(pistes)
    pistes = pistes[:MAX_PISTES]
    nom_album = album.get("collectionName", "Album")
    artiste_album = album.get("artistName", "")
    url_cover = (album.get("artworkUrl100") or "").replace("100x100", "600x600")

    suivi = repondre(
        chat_id,
        f"💿 <b>{e(nom_album)}</b>\nPréparation de {len(pistes)} piste(s)…",
    )
    envoyes = 0

    with tempfile.TemporaryDirectory(prefix="album_") as dossier_album:
        cover = preparer_pochette(url_cover, os.path.join(dossier_album, "cover.jpg"))

        for i, p in enumerate(pistes, 1):
            titre = p.get("trackName") or f"Piste {i}"
            artiste = p.get("artistName") or artiste_album or "Artiste inconnu"
            modifier(
                chat_id,
                suivi.message_id,
                f"💿 <b>{e(nom_album)}</b>\n⏬ Piste {i}/{len(pistes)} : {e(titre)}",
            )
            try:
                bot.send_chat_action(chat_id, "upload_voice")
                with tempfile.TemporaryDirectory(dir=dossier_album) as tmp:
                    with DL_SEM:
                        piste = telecharger_piste(f"{artiste} {titre}", tmp)
                    envoyer_audio(
                        chat_id,
                        piste["fichier"],
                        cover,
                        title=titre[:200],
                        performer=artiste[:100],
                        duration=piste["duree"],
                        caption=f"💿 <b>Piste #{i}</b> - {e(titre)}\n👤 {e(artiste)}"[:1000],
                    )
                envoyes += 1
            except ErreurUtilisateur as err:
                log.warning("Piste %r ignorée : %s", titre, err)
            except Exception:
                log.exception("Piste %r échouée", titre)

    supprimer(chat_id, suivi)
    note = f"\n<i>(L'album compte {total} pistes, seules les {len(pistes)} premières sont envoyées.)</i>" if total > len(pistes) else ""
    repondre(chat_id, f"✅ {envoyes}/{len(pistes)} pistes envoyées pour <b>{e(nom)}</b> !{note}")


# ----------------------------------------------------------------------------
# Commandes
# ----------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    prenom = prenoms.get(str(chat_id))
    if prenom:
        etats.pop(chat_id, None)
        repondre(
            chat_id,
            f"👋 Re-bonjour <b>{e(prenom)}</b> ! Envoie-moi un titre ou un artiste.\n"
            "Tape /aide pour voir les commandes.",
        )
        return
    etats[chat_id] = "attente_nom"
    repondre(chat_id, "👋 <b>Bienvenue sur ton assistant musical !</b>\n\nComment dois-je t'appeler ?")


@bot.message_handler(commands=["nom"])
def cmd_nom(message):
    etats[message.chat.id] = "attente_nom"
    repondre(message.chat.id, "✏️ Quel nom veux-tu utiliser ?")


@bot.message_handler(commands=["aide", "help"])
def cmd_aide(message):
    repondre(
        message.chat.id,
        "🎧 <b>Comment ça marche</b>\n\n"
        "• Envoie un titre ou un artiste : je t'envoie le morceau.\n"
        "• <code>/album nom</code> : cherche les albums d'un artiste.\n"
        "• <code>/nom</code> : change ton prénom.\n\n"
        f"Limites : {MAX_DUREE_S // 60} min et 50 Mo par titre.",
    )


@bot.message_handler(commands=["album"])
def cmd_album(message):
    chat_id = message.chat.id
    parties = (message.text or "").split(maxsplit=1)
    if len(parties) < 2 or not parties[1].strip():
        repondre(chat_id, "Utilisation : <code>/album nom de l'artiste ou de l'album</code>")
        return
    nom = obtenir_prenom(chat_id, message.from_user.first_name if message.from_user else None)
    attente = trop_rapide(chat_id)
    if attente:
        repondre(chat_id, f"⏳ Patiente {attente}s avant la prochaine demande.")
        return
    afficher_albums(chat_id, nom, parties[1].strip()[:100])


# ----------------------------------------------------------------------------
# Messages texte
# ----------------------------------------------------------------------------
@bot.message_handler(content_types=["text"])
def gestion_messages(message):
    chat_id = message.chat.id
    texte = (message.text or "").strip()
    if not texte:
        return

    if texte.startswith("/"):
        repondre(chat_id, "Commande inconnue. Tape /aide pour voir les commandes.")
        return

    # Capture du prénom
    if etats.get(chat_id) == "attente_nom":
        prenom = texte[:30]
        definir_prenom(chat_id, prenom)
        etats.pop(chat_id, None)
        repondre(
            chat_id,
            f"Enchanté <b>{e(prenom)}</b> ! 🎧\n"
            "Envoie-moi le nom d'un titre ou d'un artiste. "
            "Pour les albums : <code>/album nom</code>.",
        )
        return

    nom = obtenir_prenom(chat_id, message.from_user.first_name if message.from_user else None)

    with occupation(chat_id) as libre:
        if not libre:
            repondre(chat_id, "⏳ Ta demande précédente est encore en cours, patiente un instant.")
            return
        attente = trop_rapide(chat_id)
        if attente:
            repondre(chat_id, f"⏳ Patiente {attente}s avant la prochaine demande.")
            return
        envoyer_titre(chat_id, nom, texte[:200])


# ----------------------------------------------------------------------------
# Boutons (callbacks)
# ----------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("art_"))
def cb_albums_artiste(call):
    chat_id = call.message.chat.id
    artiste = cache_artistes.lire(call.data[4:])
    if not artiste:
        repondre_callback(call, "Ce bouton a expiré, refais ta recherche.", alerte=True)
        return
    attente = trop_rapide(chat_id)
    if attente:
        repondre_callback(call, f"Patiente {attente}s…")
        return
    repondre_callback(call, "Recherche des albums…")
    nom = obtenir_prenom(chat_id, call.from_user.first_name)
    afficher_albums(chat_id, nom, artiste, par_artiste=True)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("alb_"))
def cb_telecharger_album(call):
    chat_id = call.message.chat.id
    album_id = call.data[4:]
    if not album_id.isdigit():
        repondre_callback(call)
        return
    nom = obtenir_prenom(chat_id, call.from_user.first_name)

    with occupation(chat_id) as libre:
        if not libre:
            repondre_callback(call, "Un envoi est déjà en cours, patiente un peu.", alerte=True)
            return
        repondre_callback(call, "Téléchargement de l'album en cours…")
        telecharger_album(chat_id, nom, album_id)


# ----------------------------------------------------------------------------
# Lancement
# ----------------------------------------------------------------------------
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    keep_alive()

    try:
        bot.remove_webhook()  # évite les conflits webhook / polling
    except Exception:
        pass
    try:
        bot.set_my_commands([
            BotCommand("start", "Démarrer le bot"),
            BotCommand("album", "Chercher des albums"),
            BotCommand("nom", "Changer mon prénom"),
            BotCommand("aide", "Aide"),
        ])
    except Exception:
        pass

    log.info("Démarrage du bot Telegram…")
    # infinity_polling relance déjà automatiquement en cas d'erreur (dont le 409)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=20,
        skip_pending=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
