import os
import json
import html
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

LEAGUES = {
    "ucl": {"id": 2, "name": "🏆 Ligue des Champions", "season": 2026},
    "epl": {"id": 39, "name": "🏴 Premier League", "season": 2026},
    "laliga": {"id": 140, "name": "🇪🇸 La Liga", "season": 2026},
    "seriea": {"id": 135, "name": "🇮🇹 Serie A", "season": 2026},
    "ligue1": {"id": 61, "name": "🇫🇷 Ligue 1", "season": 2026},
    "bundesliga": {"id": 78, "name": "🇩🇪 Bundesliga", "season": 2026},
    "friendlies": {"id": 667, "name": "🌍 Matchs Amicaux Internationaux", "season": 2026},
    "wcq_africa": {"id": 36, "name": "🏆 Qualif. Coupe du Monde - Afrique", "season": 2026},
    "afcon": {"id": 6, "name": "🌍 Coupe d'Afrique des Nations", "season": 2025},
    "worldcup": {"id": 1, "name": "🌎 Coupe du Monde", "season": 2026},
}

gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n\n⚠️ <i>18+ | Pour divertissement uniquement. Les pronostics sont des analyses, pas des garanties. Jouez de manière responsable.</i>"

class MatchPrediction(BaseModel):
    tip: str
    confidence: str
    reason: str

# --- RENDER WEB SERVICE KEEP-ALIVE ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EFFA PRONOSS Bot is alive!")
    def log_message(self, format, *args):
        pass

def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# --- API & HELPER FUNCTIONS ---
def _sync_api_get(endpoint: str, params: dict = None) -> list:
    try:
        r = requests.get(f"{API_BASE}/{endpoint}", headers=HEADERS, params=params or {}, timeout=15)
        data = r.json()
        if data.get("errors"):
            logger.error(f"API-Football Error: {data['errors']}")
        return data.get("response", [])
    except Exception as e:
        logger.error(f"Request Error: {e}")
        return []

async def api_get(endpoint: str, params: dict = None) -> list:
    return await asyncio.to_thread(_sync_api_get, endpoint, params)

def format_match(f: dict) -> str:
    home = html.escape(f["teams"]["home"]["name"])
    away = html.escape(f["teams"]["away"]["name"])
    goals_h = f["goals"]["home"] if f["goals"]["home"] is not None else 0
    goals_a = f["goals"]["away"] if f["goals"]["away"] is not None else 0
    status = f["fixture"]["status"]["short"]
    minute = f["fixture"]["status"]["elapsed"]
    date = f["fixture"]["date"][:16].replace("T", " ")
    league = html.escape(f["league"]["name"])
    if status in ["FT", "AET", "PEN"]:
        return f"✅ <b>TERMINÉ</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status in ["1H", "2H", "HT", "ET", "BT", "P"]:
        return f"🔴 <b>EN DIRECT {minute}'</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status == "NS":
        return f"🕐 {date} UTC | {league}\n{home} vs {away}"
    else:
        return f"ℹ️ {status} | {league}\n{home} {goals_h} - {goals_a} {away}"

def _sync_gemini_prediction(home: str, away: str, league: str) -> dict:
    fallback = {
        "tip": "Plus de 1.5 Buts",
        "confidence": "Moyenne",
        "reason": "Les deux équipes montrent de bonnes statistiques offensives avant ce match."
    }
    if not gemini_client:
        return fallback
    try:
        prompt = (
            f"Analyse le match de football {home} vs {away} en {league}. "
            "Donne un prono réaliste (ex: Victoire Domicile, Victoire Extérieur, Match Nul, Plus de 1.5 Buts, Plus de 2.5 Buts, Les Deux Équipes Marquent Oui), "
            "un niveau de confiance (Élevée ou Moyenne), et une raison concise en FRANÇAIS en une phrase (max 15 mots). Ne garantis pas la victoire. Réponds en français."
        )
        res = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=MatchPrediction,
                temperature=0.4,
            ),
        )
        return json.loads(res.text)
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return fallback

async def get_gemini_prediction(home: str, away: str, league: str) -> dict:
    return await asyncio.to_thread(_sync_gemini_prediction, home, away, league)

# --- IMAGE GENERATOR ---
def _sync_create_tips_image(matches: list) -> BytesIO:
    W, H = 1080, 1450
    img = Image.new("RGB", (W, H), (10, 45, 32))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 150], fill=(212, 175, 55))
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 52)
        font_med = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 28)
    except Exception:
        font_big = font_med = font_small = ImageFont.load_default()
    draw.text((W // 2, 75), "EFFA PRONOSS - 3 PRONOS DU JOUR", font=font_big, fill=(10, 45, 32), anchor="mm")
    y = 180
    for m in matches:
        draw.rounded_rectangle([40, y, W - 40, y + 360], radius=25, fill=(255, 255, 255))
        draw.text((70, y + 25), m["league"][:35], font=font_small, fill=(100, 100, 100))
        draw.text((70, y + 75), m["home"][:24], font=font_med, fill=(0, 0, 0))
        draw.text((70, y + 125), "vs", font=font_small, fill=(100, 100, 100))
        draw.text((70, y + 165), m["away"][:24], font=font_med, fill=(0, 0, 0))
        draw.text((70, y + 240), f"🕐 {m['time']} UTC", font=font_small, fill=(0, 0, 0))
        draw.rounded_rectangle([540, y + 220, W - 70, y + 305], radius=15, fill=(34, 139, 34))
        draw.text((775, y + 262), f"✅ {m['tip']}", font=font_med, fill=(255, 255, 255), anchor="mm")
        for idx, logo_url in enumerate([m.get("home_logo"), m.get("away_logo")]):
            if logo_url:
                try:
                    resp = requests.get(logo_url, timeout=5)
                    logo = Image.open(BytesIO(resp.content)).convert("RGBA").resize((110, 110))
                    img.paste(logo, (770 if idx == 0 else 900, y + 40), logo)
                except Exception:
                    pass
        y += 390
    draw.text((W // 2, H - 40), "18+ | Divertissement Uniquement | Jouez Responsablement", font=font_small, fill=(255, 255, 255), anchor="mm")
    bio = BytesIO()
    bio.name = "tips.jpg"
    img.save(bio, "JPEG", quality=90)
    bio.seek(0)
    return bio

# --- KEYBOARDS ---
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 SCORES EN DIRECT", callback_data="live")],
        [InlineKeyboardButton("🎯 3 PRONOS GRATUITS DU JOUR", callback_data="tips")],
        [InlineKeyboardButton("📅 MATCHS À VENIR", callback_data="upcoming_menu")],
        [InlineKeyboardButton("🔍 RECHERCHER UN MATCH", callback_data="search_help")],
        [InlineKeyboardButton("📊 FACE-À-FACE / STATS", callback_data="stats_help")],
        [InlineKeyboardButton("🏆 RÉSULTATS D'HIER", callback_data="results")],
    ])

def upcoming_keyboard():
    buttons = [[InlineKeyboardButton(v["name"], callback_data=f"upcoming_{key}")] for key, v in LEAGUES.items()]
    buttons.append([InlineKeyboardButton("⬅️ Retour au Menu", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚽ Bienvenue sur <b>EFFA PRONOSS</b> ⚽\n\n"
        "🔴 Scores en Direct\n"
        "🎯 3 Pronos Gratuits par Jour avec IA\n"
        "📅 Matchs à Venir - Ligue des Champions, CAN, Coupe du Monde & Grands Championnats\n"
        "🔍 Rechercher un Match : Tape juste <b>Arsenal vs Chelsea</b>\n\n"
        "Choisis ci-dessous 👇" + DISCLAIMER
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    now_utc = datetime.now(timezone.utc)
    if data == "menu":
        await q.edit_message_text("Choisis une option 👇", reply_markup=main_keyboard())
        return
    if data == "live":
        await q.edit_message_text("⏳ Récupération des scores en direct...")
        fixtures = await api_get("fixtures", {"live": "all"})
        if not fixtures:
            await q.edit_message_text("Aucun match en direct pour le moment. Vérifie les Matchs à Venir 📅", reply_markup=main_keyboard())
            return
        txt = "🔴 <b>EN DIRECT MAINTENANT</b>\n\n" + "\n\n".join([format_match(f) for f in fixtures[:15]])
        await q.edit_message_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
        return
    if data == "results":
        await q.edit_message_text("⏳ Récupération des résultats d'hier...")
        yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
        fixtures = await api_get("fixtures", {"date": yesterday})
        top_ids = {v["id"] for v in LEAGUES.values()}
        filtered = [f for f in fixtures if f["league"]["id"] in top_ids] or fixtures
        if not filtered:
            await q.edit_message_text(f"Aucun résultat trouvé pour {yesterday}.", reply_markup=main_keyboard())
            return
        txt = f"🏆 <b>Résultats - {yesterday}</b>\n\n" + "\n\n".join([format_match(f) for f in filtered[:15]])
        await q.edit_message_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
        return
    if data == "tips":
        await q.edit_message_text("⏳ Analyse des 3 meilleurs matchs du jour avec l'IA...")
        today = now_utc.strftime("%Y-%m-%d")
        fixtures = await api_get("fixtures", {"date": today})
        top_ids = {v["id"] for v in LEAGUES.values()}
        upcoming_today = [f for f in fixtures if f["fixture"]["status"]["short"] == "NS"]
        picked = [f for f in upcoming_today if f["league"]["id"] in top_ids][:3]
        if len(picked) < 3:
            picked = (picked + [f for f in upcoming_today if f not in picked])[:3]
        if not picked:
            await q.edit_message_text("Aucun match à venir aujourd'hui, reviens demain !", reply_markup=main_keyboard())
            return
        matches_for_image = []
        txt = "🎯 <b>3 PRONOS GRATUITS DU JOUR - EFFA PRONOSS</b>\n\n"
        preds = await asyncio.gather(*[get_gemini_prediction(f["teams"]["home"]["name"], f["teams"]["away"]["name"], f["league"]["name"]) for f in picked])
        for f, pred in zip(picked, preds):
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            league = f["league"]["name"]
            txt += (
                f"🏆 <b>{html.escape(league)}</b>\n"
                f"{html.escape(home)} vs {html.escape(away)}\n"
                f"✅ <b>Prono :</b> {html.escape(pred['tip'])} ({html.escape(pred['confidence'])})\n"
                f"💡 <i>{html.escape(pred['reason'])}</i>\n\n"
            )
            matches_for_image.append({
                "home": home, "away": away, "league": league,
                "time": f["fixture"]["date"][11:16], "tip": pred["tip"],
                "home_logo": f["teams"]["home"]["logo"], "away_logo": f["teams"]["away"]["logo"],
            })
        txt += DISCLAIMER
        try:
            img = await asyncio.to_thread(_sync_create_tips_image, matches_for_image)
            await q.message.reply_photo(photo=img, caption=txt[:1024], reply_markup=main_keyboard(), parse_mode="HTML")
        except Exception as e:
            logger.error(f"Image error: {e}")
            try:
                await q.message.reply_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
            except Exception:
                await q.message.reply_text("Erreur image, voici les pronos en texte.", reply_markup=main_keyboard())
        return
    if data == "upcoming_menu":
        await q.edit_message_text("📅 Choisis un championnat pour les matchs à venir :", reply_markup=upcoming_keyboard())
        return
    if data.startswith("upcoming_"):
        key = data.replace("upcoming_", "")
        league = LEAGUES.get(key)
        if not league:
            return
        await q.edit_message_text(f"⏳ Chargement {league['name']}...")
        # FIX MATCHS AMICAUX: Friendlies + Tournois = search by date, Clubs = next=6
        fixtures = []
        if key in ["friendlies", "wcq_africa", "afcon", "worldcup"]:
            for offset in range(0, 8):
                d = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
                day_fixtures = await api_get("fixtures", {"date": d, "league": league["id"], "season": league["season"]})
                fixtures.extend(day_fixtures)
                if len(fixtures) >= 6:
                    break
            if not fixtures:
                for offset in range(0, 8):
                    d = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
                    day_fixtures = await api_get("fixtures", {"date": d, "league": league["id"], "season": 2025})
                    fixtures.extend(day_fixtures)
                    if len(fixtures) >= 6:
                        break
            fixtures = fixtures[:6]
        else:
            fixtures = await api_get("fixtures", {"league": league["id"], "season": league["season"], "next": 6})
        txt = f"📅 <b>À Venir - {league['name']}</b>\n\n"
        if not fixtures:
            if key in ["worldcup", "afcon"]:
                txt += "🏆 Ce tournoi est terminé. Prochaine édition bientôt.\nRegardez les Matchs Amicaux Internationaux à la place."
            elif key == "friendlies":
                txt += "🌍 Aucun match amical dans les 8 prochains jours.\nEssaie les Qualif. Coupe du Monde - Afrique 🏆"
            else:
                txt += "⏸️ Aucun match à venir - Le championnat est en pause Trêve Internationale.\nRegardez les Matchs Amicaux Internationaux 🌍"
        else:
            preds = await asyncio.gather(*[get_gemini_prediction(f["teams"]["home"]["name"], f["teams"]["away"]["name"], league["name"]) for f in fixtures[:6]])
            for f, pred in zip(fixtures[:6], preds):
                home = f["teams"]["home"]["name"]
                away = f["teams"]["away"]["name"]
                d = f["fixture"]["date"][:10]
                t = f["fixture"]["date"][11:16]
                txt += (
                    f"🕐 {d} {t} UTC\n"
                    f"<b>{html.escape(home)} vs {html.escape(away)}</b>\n"
                    f"🎯 {html.escape(pred['tip'])} — <i>{html.escape(pred['reason'])}</i>\n\n"
                )
        txt += DISCLAIMER
        try:
            await q.edit_message_text(txt[:4000], reply_markup=upcoming_keyboard(), parse_mode="HTML")
        except Exception as e:
            logger.error(f"Edit failed, sending new msg: {e}")
            await q.message.reply_text(txt[:4000], reply_markup=upcoming_keyboard(), parse_mode="HTML")
        return
    if data == "search_help":
        await q.edit_message_text("🔍 Tape juste dans le chat :\n\n<code>Arsenal vs Chelsea</code>\n\nJe vais chercher le score / l'heure du coup d'envoi et le prono IA.", reply_markup=main_keyboard(), parse_mode="HTML")
        return
    if data == "stats_help":
        await q.edit_message_text("📊 Pour avoir l'historique Face-à-Face, tape :\n\n<code>Stats Arsenal vs Chelsea</code>", reply_markup=main_keyboard(), parse_mode="HTML")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if " vs " not in text.lower():
        await update.message.reply_text("Tape un match comme : <b>Arsenal vs Chelsea</b>", reply_markup=main_keyboard(), parse_mode="HTML")
        return
    is_stats = text.lower().startswith("stats")
    clean = text.lower().replace("stats", "").strip()
    parts = clean.split(" vs ")
    if len(parts) != 2:
        await update.message.reply_text("Utilise le format : <code>Equipe1 vs Equipe2</code>", reply_markup=main_keyboard(), parse_mode="HTML")
        return
    home_q, away_q = parts[0].strip(), parts[1].strip()
    status_msg = await update.message.reply_text(f"⏳ Recherche {html.escape(home_q.title())} vs {html.escape(away_q.title())}...")
    found = None
    now_utc = datetime.now(timezone.utc)
    for offset in [0, 1, 2, 3, 4, 5, 6, 7, -1]:
        d = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
        fixtures = await api_get("fixtures", {"date": d})
        for f in fixtures:
            h = f["teams"]["home"]["name"].lower()
            a = f["teams"]["away"]["name"].lower()
            if (home_q in h or h in home_q) and (away_q in a or a in away_q):
                found = f
                break
        if found:
            break
    if not found:
        await status_msg.edit_text("❌ Match introuvable dans les 8 prochains jours. Essaie les noms complets (ex : <i>Manchester City vs Liverpool</i>).", reply_markup=main_keyboard(), parse_mode="HTML")
        return
    home = found["teams"]["home"]["name"]
    away = found["teams"]["away"]["name"]
    league = found["league"]["name"]
    score_txt = format_match(found)
    h2h_txt = ""
    if is_stats:
        h_id = found["teams"]["home"]["id"]
        a_id = found["teams"]["away"]["id"]
        h2h_matches = await api_get("fixtures/headtohead", {"h2h": f"{h_id}-{a_id}", "last": 5})
        if h2h_matches:
            h2h_lines = [f"• {m['fixture']['date'][:10]}: {html.escape(m['teams']['home']['name'])} {m['goals']['home']}-{m['goals']['away']} {html.escape(m['teams']['away']['name'])}" for m in h2h_matches]
            h2h_txt = "\n\n📊 <b>Les 5 Derniers Face-à-Face :</b>\n" + "\n".join(h2h_lines)
    pred = await get_gemini_prediction(home, away, league)
    reply = (
        f"{score_txt}"
        f"{h2h_txt}\n\n"
        f"🎯 <b>Analyse IA :</b>\n"
        f"✅ <b>Prono :</b> {html.escape(pred['tip'])} ({html.escape(pred['confidence'])})\n"
        f"💡 <i>{html.escape(pred['reason'])}</i>"
        f"{DISCLAIMER}"
    )
    await status_msg.edit_text(reply[:4000], reply_markup=main_keyboard(), parse_mode="HTML")

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN environment variable is missing!")
    keep_alive()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("EFFA PRONOSS Bot is running...", flush=True)
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"❌ BOT CRASHED: {e}", flush=True)
        traceback.print_exc()
        import time
        print("Waiting 60s to show error...", flush=True)
        time.sleep(60)
