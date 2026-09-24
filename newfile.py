import os
import time
import urllib.request
import telebot
from collections import Counter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from yt_dlp import YoutubeDL
from threading import Thread
from flask import Flask

# --- Mini serveur Flask pour maintenir Render en ligne ---
app = Flask('')

@app.route('/')
def home():
    return "Bot actif 24/7 !"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()
# --------------------------------------------------------

TOKEN = "8803716438:AAGesgLwzKNOt1VGFER90wkFWypPdr6GmQk"

# Initialisation TeleBot
bot = telebot.TeleBot(TOKEN)

prenoms_utilisateurs = {}
etats_utilisateurs = {}
historique_artistes = {}
compteur_recherches = {}

@bot.message_handler(commands=['start'])
def message_bienvenue(message):
    chat_id = message.chat.id
    etats_utilisateurs[chat_id] = "attente_nom"
    bot.send_message(
        chat_id, 
        "👋 **Bienvenue sur ton assistant musical IA !**\n\nComment dois-je t'appeler ?",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: True)
def gestion_messages(message):
    chat_id = message.chat.id
    texte_recu = message.text.strip()
    
    # Sécurité anti-boucle pour /start
    if texte_recu.startswith('/start'):
        return

    # Capture du prénom
    if etats_utilisateurs.get(chat_id) == "attente_nom":
        prenoms_utilisateurs[chat_id] = texte_recu
        etats_utilisateurs[chat_id] = "actif"
        historique_artistes[chat_id] = []
        compteur_recherches[chat_id] = 0
        
        bot.send_message(
            chat_id, 
            f"Enchanté **{texte_recu}** ! 🎧\nEnvoyez-moi le nom d'un titre, d'un album ou d'un artiste.",
            parse_mode="Markdown"
        )
        return

    nom_user = prenoms_utilisateurs.get(chat_id, message.from_user.first_name)
    bot.send_chat_action(chat_id, 'upload_document')

    # CAS 1 : Recherche explicite d'albums
    if "album" in texte_recu.lower():
        rechercher_3_derniers_albums(chat_id, nom_user, texte_recu)
        return

    # CAS 2 : Recherche de morceau individuel
    msg_patienter = bot.send_message(
        chat_id, 
        f"⚡ *Analyse & téléchargement pour {nom_user}...*", 
        parse_mode="Markdown"
    )
    
    options = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'default_search': 'ytsearch1:',
        'outtmpl': '%(id)s.%(ext)s',
        'nopart': True,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'writethumbnail': True
    }
    
    fichier_telecharge = None
    pochette_img = None
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(texte_recu, download=True)
            
            if not info or ('entries' in info and not info['entries']):
                bot.delete_message(chat_id, msg_patienter.message_id)
                bot.send_message(
                    chat_id, 
                    f"⚠️ Oups **{nom_user}**, aucun titre trouvé. Vérifie l'orthographe !",
                    parse_mode="Markdown"
                )
                return

            if 'entries' in info and info['entries']:
                info_piste = info['entries'][0]
            else:
                info_piste = info

            fichier_telecharge = ydl.prepare_filename(info_piste)
            titre_chanson = info_piste.get('title', texte_recu)
            nom_artiste = info_piste.get('artist', info_piste.get('uploader', 'Artiste Inconnu'))
            nom_album = info_piste.get('album', None)

            # Pochette d'album HD
            thumb_url = info_piste.get('thumbnail', None)
            if thumb_url:
                pochette_img = f"{info_piste['id']}.jpg"
                try:
                    urllib.request.urlretrieve(thumb_url, pochette_img)
                except Exception:
                    pochette_img = None

        if chat_id not in historique_artistes:
            historique_artistes[chat_id] = []
            compteur_recherches[chat_id] = 0
            
        historique_artistes[chat_id].append(nom_artiste)
        compteur_recherches[chat_id] += 1

        markup = InlineKeyboardMarkup()
        url_paroles = f"https://www.google.com/search?q=paroles+{titre_chanson.replace(' ', '+')}"
        markup.add(InlineKeyboardButton("📄 Paroles", url=url_paroles))

        markup.add(InlineKeyboardButton(
            f"💿 Chercher les albums de {nom_artiste[:15]}", 
            callback_data=f"listalbums_{nom_artiste[:25]}"
        ))

        caption_texte = f"🎵 **{titre_chanson}**\n👤 **Artiste :** {nom_artiste}"
        if nom_album:
            caption_texte += f"\n💿 **Album :** {nom_album}"

        with open(fichier_telecharge, 'rb') as audio:
            if pochette_img and os.path.exists(pochette_img):
                with open(pochette_img, 'rb') as thumb:
                    bot.send_audio(
                        chat_id, audio, title=titre_chanson, performer=nom_artiste,
                        caption=caption_texte, parse_mode="Markdown", reply_markup=markup,
                        thumb=thumb
                    )
            else:
                bot.send_audio(
                    chat_id, audio, title=titre_chanson, performer=nom_artiste,
                    caption=caption_texte, parse_mode="Markdown", reply_markup=markup
                )
            
        bot.delete_message(chat_id, msg_patienter.message_id)

    except Exception as e:
        if msg_patienter:
            try:
                bot.delete_message(chat_id, msg_patienter.message_id)
            except Exception:
                pass
        bot.send_message(
            chat_id, 
            f"❌ **{nom_user}**, titre introuvable ou erreur de saisie. Réessaie avec le nom exact !", 
            parse_mode="Markdown"
        )
    
    finally:
        if fichier_telecharge and os.path.exists(fichier_telecharge):
            try:
                os.remove(fichier_telecharge)
            except Exception:
                pass
        if pochette_img and os.path.exists(pochette_img):
            try:
                os.remove(pochette_img)
            except Exception:
                pass

# Recherche des 3 derniers albums
def rechercher_3_derniers_albums(chat_id, nom_user, requete):
    msg = bot.send_message(chat_id, f"🔎 *Recherche des albums pour {nom_user}...*", parse_mode="Markdown")
    
    options_recherche = {
        'extract_flat': True,
        'skip_download': True,
        'default_search': f'ytsearch3:{requete} full album',
        'quiet': True
    }
    
    try:
        with YoutubeDL(options_recherche) as ydl:
            info = ydl.extract_info(f"{requete} full album", download=False)
            entries = info.get('entries', []) if info else []
            
            if not entries:
                bot.delete_message(chat_id, msg.message_id)
                bot.send_message(chat_id, f"⚠️ Aucun album trouvé pour : *{requete}*", parse_mode="Markdown")
                return

            markup = InlineKeyboardMarkup()
            for idx, entry in enumerate(entries[:3], 1):
                titre_album = entry.get('title', f'Album {idx}')
                nom_clean = titre_album.replace('Full Album', '').replace('Album', '').strip()[:35]
                # On stocke le titre complet nettoyé dans le callback_data (limité à 64 octets max pour Telegram)
                callback_payload = nom_clean[:50]
                markup.add(InlineKeyboardButton(
                    f"💿 {nom_clean}", 
                    callback_data=f"dlalbum_{callback_payload}"
                ))

            bot.delete_message(chat_id, msg.message_id)
            bot.send_message(
                chat_id, 
                f"💿 **Voici les albums trouvés pour {nom_user} :**\nClique sur celui que tu veux télécharger :",
                parse_mode="Markdown",
                reply_markup=markup
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur lors de la recherche des albums : {str(e)}")

# Callbacks
@bot.callback_query_handler(func=lambda call: call.data.startswith('listalbums_'))
def callback_liste_albums(call):
    chat_id = call.message.chat.id
    nom_user = prenoms_utilisateurs.get(chat_id, call.from_user.first_name)
    artiste = call.data.replace('listalbums_', '')
    bot.answer_callback_query(call.id, "Recherche des albums...")
    rechercher_3_derniers_albums(chat_id, nom_user, f"album {artiste}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('dlalbum_'))
def callback_telecharger_album_selectionne(call):
    chat_id = call.message.chat.id
    nom_user = prenoms_utilisateurs.get(chat_id, call.from_user.first_name)
    target_album = call.data.replace('dlalbum_', '')
    
    bot.answer_callback_query(call.id, "Téléchargement de l'album en cours...")
    msg = bot.send_message(chat_id, f"💿 *Téléchargement de l'album '{target_album}'...*", parse_mode="Markdown")
    
    options_dl = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'default_search': f'ytsearch5:{target_album} playlist',
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': True,
        'noplaylist': False
    }
    
    try:
        with YoutubeDL(options_dl) as ydl:
            info = ydl.extract_info(f"{target_album} album songs", download=True)
            entries = info.get('entries', []) if info else []

            if not entries:
                bot.delete_message(chat_id, msg.message_id)
                bot.send_message(chat_id, f"⚠️ Impossible de récupérer les pistes de cet album.", parse_mode="Markdown")
                return

            count_envoyes = 0
            for index, entry in enumerate(entries[:5], 1): # Limité à 5 pistes principales pour éviter le timeout
                if not entry:
                    continue
                f_path = ydl.prepare_filename(entry)
                titre = entry.get('title', f"Piste {index}")

                if os.path.exists(f_path):
                    try:
                        with open(f_path, 'rb') as audio:
                            bot.send_audio(chat_id, audio, title=titre, caption=f"💿 **Piste #{index}** - {titre}", parse_mode="Markdown")
                        count_envoyes += 1
                    except Exception:
                        pass
                    finally:
                        if os.path.exists(f_path):
                            os.remove(f_path)
                    
        bot.delete_message(chat_id, msg.message_id)
        bot.send_message(chat_id, f"✅ {count_envoyes} pistes envoyées avec succès pour **{nom_user}** !", parse_mode="Markdown")
        
    except Exception as e:
        if msg:
            try:
                bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
        bot.send_message(chat_id, f"⚠️ Erreur lors du téléchargement de l'album : {str(e)}")

# Boucle principale d'exécution sécurisée contre le conflit 409
while True:
    try:
        print("Démarrage du bot Telegram...")
        bot.infinity_polling(timeout=30, long_polling_timeout=20, skip_pending=True)
    except Exception as e:
        print(f"Erreur de polling : {e}")
        time.sleep(5)
