import os
import json
import html
import time
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
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")

API_BASE = "https://v3.football.api-sports.io"

# League IDs in API-Football v3
LEAGUES = {
    "ucl": {"id": 2, "name": "🏆 Ligue des Champions"},
    "epl": {"id": 39, "name": "🏴 Premier League"},
    "laliga": {"id": 140, "name": "🇪🇸 La Liga"},
    "seriea": {"id": 135, "name": "🇮🇹 Serie A"},
    "ligue1": {"id": 61, "name": "🇫🇷 Ligue 1"},
    "bundesliga": {"id": 78, "name": "🇩🇪 Bundesliga"},
    "friendlies": {"id": 10, "name": "🌍 Matchs Amicaux Internationaux"},
    "wcq_africa": {"id": 29, "name": "🏆 Qualif. Coupe du Monde - Afrique"},
    "afcon": {"id": 6, "name": "🌍 Coupe d'Afrique des Nations"},
    "worldcup": {"id": 1, "name": "🌎 Coupe du Monde"},
}

gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "\n\n⚠️ <i>18+ | Pour divertissement uniquement. "
    "Les pronostics sont des analyses, pas des garanties. Jouez de manière responsable.</i>"
)

# Simple in-memory cache to protect API-Football daily quota
_API_CACHE: dict[str, tuple[float, list]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


class MatchPrediction(BaseModel):
    tip: str
    confidence: str
    reason: str


# --- RENDER / KOYEB WEB SERVICE KEEP-ALIVE ---
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
def _sync_api_get(endpoint: str, params: dict = None) -> tuple[list, str | None]:
    if not API_FOOTBALL_KEY:
        return [], "Clé API_FOOTBALL_KEY manquante dans les variables d'environnement."

    params = params or {}
    cache_key = f"{endpoint}:{json.dumps(params, sort_keys=True)}"
    now = time.time()

    # Don't cache live scores for more than 30 seconds
    ttl = 30 if params.get("live") else CACHE_TTL_SECONDS
    if cache_key in _API_CACHE:
        ts, cached_data = _API_CACHE[cache_key]
        if now - ts < ttl:
            return cached_data, None

    try:
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        r = requests.get(f"{API_BASE}/{endpoint}", headers=headers, params=params, timeout=15)
        data = r.json()

        errors = data.get("errors")
        if errors:
            err_msg = str(errors)
            logger.error(f"API-Football Error ({endpoint} {params}): {err_msg}")
            return [], err_msg

        resp = data.get("response", [])
        _API_CACHE[cache_key] = (now, resp)
        return resp, None
    except Exception as e:
        logger.error(f"Request Error ({endpoint}): {e}")
        return [], str(e)


async def api_get(endpoint: str, params: dict = None) -> tuple[list, str | None]:
    return await asyncio.to_thread(_sync_api_get, endpoint, params)


async def safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Safely edits a Telegram callback message without crashing on identical text or long HTML."""
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        logger.error(f"Telegram BadRequest on edit: {e}")
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def format_match(f: dict) -> str:
    home = html.escape(f["teams"]["home"]["name"])
    away = html.escape(f["teams"]["away"]["name"])
    goals_h = f["goals"]["home"] if f["goals"]["home"] is not None else 0
    goals_a = f["goals"]["away"] if f["goals"]["away"] is not None else 0
    status = f["fixture"]["status"]["short"]
    minute = f["fixture"]["status"]["elapsed"]
    date_str = f["fixture"]["date"][:16].replace("T", " ")
    league = html.escape(f["league"]["name"])

    if status in ["FT", "AET", "PEN"]:
        return f"✅ <b>TERMINÉ</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status in ["1H", "2H", "HT", "ET", "BT", "P"]:
        return f"🔴 <b>EN DIRECT {minute}'</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status == "NS":
        return f"🕐 {date_str} UTC | {league}\n{home} vs {away}"
    else:
        return f"ℹ️ {status} | {league}\n{home} {goals_h} - {goals_a} {away}"


async def get_gemini_prediction(home: str, away: str, league: str) -> dict:
    fallback = {
        "tip": "Plus de 1.5 Buts",
        "confidence": "Moyenne",
        "reason": "Les deux équipes montrent un bon potentiel offensif avant ce match.",
    }
    if not gemini_client:
        return fallback

    try:
        prompt = (
            f"Analyse le match de football {home} vs {away} en {league}. "
            "Donne un prono réaliste (ex: Victoire Domicile, Victoire Extérieur, Match Nul, "
            "Plus de 1.5 Buts, Plus de 2.5 Buts, Les Deux Équipes Marquent Oui), "
            "un niveau de confiance (Élevée ou Moyenne), et une raison concise en FRANÇAIS en une phrase (max 15 mots). "
            "Ne garantis jamais la victoire."
        )
        res = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=MatchPrediction,
                temperature=0.4,
            ),
        )
        if res.parsed:
            return res.parsed.model_dump()
        return json.loads(res.text)
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return fallback


# --- IMAGE GENERATOR ---
def _sync_create_tips_image(matches: list) -> BytesIO:
    W, H = 1080, 1450
    img = Image.new("RGB", (W, H), (10, 45, 32))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 150], fill=(212, 175, 55))

    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        font_med = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 26)
    except Exception:
        font_big = font_med = font_small = ImageFont.load_default()

    draw.text((W // 2, 75), "EFFA PRONOSS - 3 PRONOS DU JOUR", font=font_big, fill=(10, 45, 32), anchor="mm")

    y = 180
    for m in matches:
        draw.rounded_rectangle([40, y, W - 40, y + 360], radius=25, fill=(255, 255, 255))
        draw.text((70, y + 25), m["league"][:36], font=font_small, fill=(100, 100, 100))
        # Truncate team names cleanly so they don't collide with logos at x=770
        draw.text((70, y + 75), m["home"][:20], font=font_med, fill=(0, 0, 0))
        draw.text((70, y + 125), "vs", font=font_small, fill=(100, 100, 100))
        draw.text((70, y + 165), m["away"][:20], font=font_med, fill=(0, 0, 0))
        draw.text((70, y + 245), f"🕐 {m['time']} UTC", font=font_small, fill=(0, 0, 0))

        draw.rounded_rectangle([520, y + 220, W - 70, y + 305], radius=15, fill=(34, 139, 34))
        draw.text((765, y + 262), f"✅ {m['tip'][:24]}", font=font_med, fill=(255, 255, 255), anchor="mm")

        for idx, logo_url in enumerate([m.get("home_logo"), m.get("away_logo")]):
            if logo_url:
                try:
                    resp = requests.get(logo_url, timeout=5)
                    logo = Image.open(BytesIO(resp.content)).convert("RGBA").resize((110, 110))
                    img.paste(logo, (770 if idx == 0 else 900, y + 40), logo)
                except Exception:
                    pass
        y += 390

    draw.text(
        (W // 2, H - 40),
        "18+ | Divertissement Uniquement | Jouez Responsablement",
        font=font_small,
        fill=(255, 255, 255),
        anchor="mm",
    )

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
    if not update.message:
        return
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
        await safe_edit(q, "Choisis une option 👇", reply_markup=main_keyboard())
        return

    if data == "live":
        await safe_edit(q, "⏳ Récupération des scores en direct...")
        fixtures, err = await api_get("fixtures", {"live": "all"})
        if err:
            await safe_edit(q, f"⚠️ Erreur API Football :\n<code>{html.escape(err)}</code>", reply_markup=main_keyboard())
            return
        if not fixtures:
            await safe_edit(q, "Aucun match en direct pour le moment. Vérifie les Matchs à Venir 📅", reply_markup=main_keyboard())
            return

        blocks = [format_match(f) for f in fixtures[:12]]
        txt = "🔴 <b>EN DIRECT MAINTENANT</b>\n\n" + "\n\n".join(blocks)
        await safe_edit(q, txt, reply_markup=main_keyboard())
        return

    if data == "results":
        await safe_edit(q, "⏳ Récupération des résultats d'hier...")
        yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
        fixtures, err = await api_get("fixtures", {"date": yesterday})
        if err:
            await safe_edit(q, f"⚠️ Erreur API Football :\n<code>{html.escape(err)}</code>", reply_markup=main_keyboard())
            return

        top_ids = {v["id"] for v in LEAGUES.values()}
        filtered = [f for f in fixtures if f["league"]["id"] in top_ids] or fixtures
        if not filtered:
            await safe_edit(q, f"Aucun résultat trouvé pour {yesterday}.", reply_markup=main_keyboard())
            return

        blocks = [format_match(f) for f in filtered[:12]]
        txt = f"🏆 <b>Résultats - {yesterday}</b>\n\n" + "\n\n".join(blocks)
        await safe_edit(q, txt, reply_markup=main_keyboard())
        return

    if data == "tips":
        await safe_edit(q, "⏳ Analyse des 3 meilleurs matchs du jour avec l'IA...")
        today = now_utc.strftime("%Y-%m-%d")
        fixtures, err = await api_get("fixtures", {"date": today})
        if err:
            await safe_edit(q, f"⚠️ Erreur API Football :\n<code>{html.escape(err)}</code>", reply_markup=main_keyboard())
            return

        top_ids = {v["id"] for v in LEAGUES.values()}
        upcoming_today = [f for f in fixtures if f["fixture"]["status"]["short"] == "NS"]
        picked = [f for f in upcoming_today if f["league"]["id"] in top_ids][:3]
        if len(picked) < 3:
            picked = (picked + [f for f in upcoming_today if f not in picked])[:3]

        if not picked:
            await safe_edit(q, "Aucun match à venir aujourd'hui, reviens demain !", reply_markup=main_keyboard())
            return

        matches_for_image = []
        txt = "🎯 <b>3 PRONOS GRATUITS DU JOUR - EFFA PRONOSS</b>\n\n"
        preds = await asyncio.gather(
            *[get_gemini_prediction(f["teams"]["home"]["name"], f["teams"]["away"]["name"], f["league"]["name"]) for f in picked]
        )

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
                "home": home,
                "away": away,
                "league": league,
                "time": f["fixture"]["date"][11:16],
                "tip": pred["tip"],
                "home_logo": f["teams"]["home"]["logo"],
                "away_logo": f["teams"]["away"]["logo"],
            })
        txt += DISCLAIMER

        try:
            img = await asyncio.to_thread(_sync_create_tips_image, matches_for_image)
            await q.message.reply_photo(photo=img, caption=txt[:1024], reply_markup=main_keyboard(), parse_mode="HTML")
        except Exception as e:
            logger.error(f"Image error: {e}")
            await q.message.reply_text(txt, reply_markup=main_keyboard(), parse_mode="HTML")
        return

    if data == "upcoming_menu":
        await safe_edit(q, "📅 Choisis un championnat pour les matchs à venir :", reply_markup=upcoming_keyboard())
        return

    if data.startswith("upcoming_"):
        key = data.replace("upcoming_", "")
        league = LEAGUES.get(key)
        if not league:
            return

        await safe_edit(q, f"⏳ Chargement {league['name']}...")

        # Fetch by date across the next 10 days (works on ALL API-Football plans without 'next' or season errors)
        league_fixtures = []
        last_err = None
        for offset in range(0, 10):
            d = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
            day_fixtures, err = await api_get("fixtures", {"date": d})
            if err:
                last_err = err
                break
            matched = [
                f for f in day_fixtures
                if f["league"]["id"] == league["id"] and f["fixture"]["status"]["short"] == "NS"
            ]
            league_fixtures.extend(matched)
            if len(league_fixtures) >= 5:
                break

        if last_err and not league_fixtures:
            await safe_edit(q, f"⚠️ Erreur API Football :\n<code>{html.escape(last_err)}</code>", reply_markup=upcoming_keyboard())
            return

        league_fixtures = league_fixtures[:5]
        txt = f"📅 <b>À Venir - {league['name']}</b>\n\n"

        if not league_fixtures:
            txt += (
                "⏸️ Aucun match prévu dans les 10 prochains jours pour cette compétition.\n"
                "Essaie un autre championnat ou regarde les Matchs Amicaux 🌍"
            )
        else:
            preds = await asyncio.gather(
                *[get_gemini_prediction(f["teams"]["home"]["name"], f["teams"]["away"]["name"], league["name"]) for f in league_fixtures]
            )
            for f, pred in zip(league_fixtures, preds):
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
        await safe_edit(q, txt, reply_markup=upcoming_keyboard())
        return

    if data == "search_help":
        await safe_edit(
            q,
            "🔍 Tape juste dans le chat :\n\n<code>Arsenal vs Chelsea</code>\n\n"
            "Je vais chercher le score / l'heure du coup d'envoi et le prono IA.",
            reply_markup=main_keyboard(),
        )
        return

    if data == "stats_help":
        await safe_edit(
            q,
            "📊 Pour avoir l'historique Face-à-Face, tape :\n\n<code>Stats Arsenal vs Chelsea</code>",
            reply_markup=main_keyboard(),
        )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if " vs " not in text.lower():
        await update.message.reply_text(
            "Tape un match comme : <b>Arsenal vs Chelsea</b>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    is_stats = text.lower().startswith("stats")
    clean = text.lower()
    if is_stats:
        clean = clean[5:].strip()

    parts = clean.split(" vs ")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        await update.message.reply_text(
            "Utilise le format : <code>Equipe1 vs Equipe2</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    home_q, away_q = parts[0].strip(), parts[1].strip()
    status_msg = await update.message.reply_text(
        f"⏳ Recherche <b>{html.escape(home_q.title())} vs {html.escape(away_q.title())}</b>...",
        parse_mode="HTML",
    )

    found = None
    last_err = None
    now_utc = datetime.now(timezone.utc)

    # Check today, tomorrow, yesterday first (most common queries), then remaining days
    for offset in [0, 1, -1, 2, 3, 4, 5, 6, 7]:
        d = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
        fixtures, err = await api_get("fixtures", {"date": d})
        if err:
            last_err = err
            break
        for f in fixtures:
            h = f["teams"]["home"]["name"].lower()
            a = f["teams"]["away"]["name"].lower()
            if (home_q in h or h in home_q) and (away_q in a or a in away_q):
                found = f
                break
        if found:
            break

    if last_err and not found:
        await status_msg.edit_text(
            f"⚠️ Erreur API Football :\n<code>{html.escape(last_err)}</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    if not found:
        await status_msg.edit_text(
            "❌ Match introuvable (hier à +7 jours). Essaie les noms officiels (ex : <i>Manchester City vs Liverpool</i>).",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    home = found["teams"]["home"]["name"]
    away = found["teams"]["away"]["name"]
    league = found["league"]["name"]
    score_txt = format_match(found)
    h2h_txt = ""

    if is_stats:
        h_id = found["teams"]["home"]["id"]
        a_id = found["teams"]["away"]["id"]
        h2h_matches, _ = await api_get("fixtures/headtohead", {"h2h": f"{h_id}-{a_id}", "last": 5})
        if h2h_matches:
            h2h_lines = [
                f"• {m['fixture']['date'][:10]}: {html.escape(m['teams']['home']['name'])} "
                f"{m['goals']['home']}-{m['goals']['away']} {html.escape(m['teams']['away']['name'])}"
                for m in h2h_matches
            ]
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
    await status_msg.edit_text(reply, reply_markup=main_keyboard(), parse_mode="HTML")


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
        time.sleep(60)
