import os
import telebot
from flask import Flask, render_template_string
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# Configuration de ton token (on récupère celui que tu as mis en place)
TOKEN = "8803716438:AAGesgLwzKNOt1VGFER90wkFWypPdr6GmQk"
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# Route Flask qui héberge l'interface style "Liquid Glass"
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="fr" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Abdoul Yeo Music</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            /* Style effet verre liquide (Glassmorphism) */
            .glass-card {
                background: rgba(255, 255, 255, 0.05);
                backdrop-filter: blur(25px);
                -webkit-backdrop-filter: blur(25px);
                border: 1px solid rgba(255, 255, 255, 0.1);
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
            }
            .glass-input {
                background: rgba(0, 0, 0, 0.3);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.08);
            }
        </style>
    </head>
    <body class="bg-gradient-to-br from-neutral-950 via-purple-950/40 to-neutral-950 text-neutral-100 min-h-screen flex flex-col items-center justify-center p-4 selection:bg-purple-500 selection:text-white">

        <div class="glass-card rounded-[32px] p-6 max-w-sm w-full space-y-6 text-center relative overflow-hidden">
            
            <div class="absolute -top-24 -left-24 w-48 h-48 bg-purple-600/30 rounded-full blur-3xl pointer-events-none"></div>
            <div class="absolute -bottom-24 -right-24 w-48 h-48 bg-pink-600/20 rounded-full blur-3xl pointer-events-none"></div>

            <div class="relative mx-auto w-20 h-20">
                <div class="absolute inset-0 bg-gradient-to-tr from-purple-600 to-pink-500 rounded-2xl blur-md opacity-75"></div>
                <div class="relative w-20 h-20 bg-neutral-900 border border-white/20 rounded-2xl flex items-center justify-center text-2xl text-white shadow-xl">
                    <i class="fa-solid fa-music"></i>
                </div>
            </div>

            <div class="space-y-1">
                <h1 class="font-bold text-xl tracking-tight text-white flex items-center justify-center gap-1.5">
                    𝔄𝔟𝔡𝔬𝔲𝔩 𝔜𝔢𝔬 𝔐𝔲𝔰𝔦𝔠 <i class="fa-solid fa-circle-check text-xs text-blue-400"></i>
                </h1>
                <p class="text-xs text-neutral-400 font-medium">Lecteur & Téléchargeur de musique</p>
            </div>

            <div class="relative">
                <span class="absolute inset-y-0 left-0 flex items-center pl-4 pointer-events-none text-neutral-400">
                    <i class="fa-solid fa-magnifying-glass text-xs"></i>
                </span>
                <input type="text" id="searchQuery" placeholder="Rechercher un titre ou un artiste..." class="glass-input w-full rounded-2xl pl-10 pr-4 py-3.5 text-xs text-white placeholder-neutral-500 focus:outline-none focus:border-purple-500/80 transition shadow-inner">
            </div>

            <button onclick="launchSearch()" class="w-full bg-gradient-to-r from-purple-600 to-pink-600 hover:from-purple-500 hover:to-pink-500 text-white font-semibold py-3.5 rounded-2xl text-xs transition duration-300 shadow-lg shadow-purple-600/30 flex items-center justify-center gap-2">
                <i class="fa-solid fa-download"></i> Lancer le téléchargement
            </button>

            <p class="text-[10px] text-neutral-500 tracking-wider uppercase">Propulsé par Render & Telegram</p>
        </div>

        <script>
            function launchSearch() {
                const query = document.getElementById('searchQuery').value;
                if(query.trim() !== "") {
                    alert("Recherche de : " + query + " en cours...");
                } else {
                    alert("Veuillez entrer un nom de musique !");
                }
            }
        </script>
    </body>
    </html>
    """)

# Commande Telegram pour ouvrir la Mini App avec le style Glass
@bot.message_handler(commands=['app', 'musique', 'start'])
def send_web_app(message):
    markup = InlineKeyboardMarkup()
    # ⚠️ IMPORTANT : Remplace "https://ton-app.onrender.com" par l'URL exacte de ton site web Render
    web_app_url = "https://yeo-bot-musique.onrender.com" 
    markup.add(InlineKeyboardButton("✨ Ouvrir le lecteur Glass", web_app=WebAppInfo(url=web_app_url)))
    
    bot.send_message(message.chat.id, "🎶 **Bienvenue sur ton interface musicale !**\n\nClique sur le bouton ci-dessous pour ouvrir ton application avec un design ultra-stylé :", reply_markup=markup, parse_mode="Markdown")

# Lancement du serveur Flask et du Bot en arrière-plan
if __name__ == "__main__":
    import threading
    # Lancement du bot dans un fil séparé pour ne pas bloquer Flask
    threading.Thread(target=lambda: bot.infinity_polling(none_stop=True)).start()
    # Lancement de Flask sur le port requis par Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
