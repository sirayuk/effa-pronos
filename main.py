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

# NOTE: If using RapidAPI, change API_BASE to "https://api-football-v1.p.rapidapi.com/v3"
# and HEADERS to {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

LEAGUES = {
    "ucl": {"id": 2, "name": "🏆 Champions League"},
    "afcon": {"id": 6, "name": "🌍 Africa Cup of Nations"},
    "worldcup": {"id": 1, "name": "🌎 World Cup"},
    "epl": {"id": 39, "name": "🏴 Premier League"},
    "laliga": {"id": 140, "name": "🇪🇸 La Liga"},
    "seriea": {"id": 135, "name": "🇮🇹 Serie A"},
    "ligue1": {"id": 61, "name": "🇫🇷 Ligue 1"},
}

gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n\n⚠️ <i>18+ | For entertainment only. Tips are analysis, not guarantees. Play responsibly.</i>"

# Structured output schema for Gemini predictions
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
        pass  # Silence HTTP request logs

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
        return f"✅ <b>FT</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status in ["1H", "2H", "HT", "ET", "BT", "P"]:
        return f"🔴 <b>LIVE {minute}'</b> | {league}\n{home} <b>{goals_h} - {goals_a}</b> {away}"
    elif status == "NS":
        return f"🕐 {date} UTC | {league}\n{home} vs {away}"
    else:
        return f"ℹ️ {status} | {league}\n{home} {goals_h} - {goals_a} {away}"

def _sync_gemini_prediction(home: str, away: str, league: str) -> dict:
    fallback = {
        "tip": "Over 1.5 Goals",
        "confidence": "Medium",
        "reason": "Both sides show solid attacking metrics heading into this fixture."
    }
    if not gemini_client:
        return fallback
    try:
        prompt = (
            f"Analyze the football match {home} vs {away} in {league}. "
            "Provide a realistic betting market tip (e.g., Home Win, Away Win, Draw, Over 1.5 Goals, Over 2.5 Goals, BTTS Yes), "
            "a confidence level (High or Medium), and a concise one-sentence reason (max 15 words). Do not guarantee wins."
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

    draw.text((W // 2, 75), "EFFA PRONOSS - DAILY 3 TIPS", font=font_big, fill=(10, 45, 32), anchor="mm")
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

    draw.text((W // 2, H - 40), "18+ | Entertainment Only | Play Responsibly", font=font_small, fill=(255, 255, 255), anchor="mm")
    bio = BytesIO()
    bio.name = "tips.jpg"
    img.save(bio, "JPEG", quality=90)
    bio.seek(0)
    return bio

# --- KEYBOARDS ---
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 LIVE SCORES NOW", callback_data="live")],
        [InlineKeyboardButton("🎯 TODAY'S 3 FREE TIPS", callback_data="tips")],
        [InlineKeyboardButton("📅 UPCOMING MATCHES", callback_data="upcoming_menu")],
        [InlineKeyboardButton("🔍 SEARCH A MATCH", callback_data="search_help")],
        [InlineKeyboardButton("📊 HEAD-TO-HEAD / STATS", callback_data="stats_help")],
        [InlineKeyboardButton("🏆 YESTERDAY RESULTS", callback_data="results")],
    ])

def upcoming_keyboard():
    buttons = [[InlineKeyboardButton(v["name"], callback_data=f"upcoming_{key}")] for key, v in LEAGUES.items()]
    buttons.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚽ Welcome to <b>EFFA PRONOSS</b> ⚽\n\n"
        "🔴 Live Scores\n"
        "🎯 3 Free Daily AI Tips\n"
        "📅 Upcoming - UCL, AFCON, World Cup & Top Leagues\n"
        "🔍 Search Any Match: Just type <b>Arsenal vs Chelsea</b>\n\n"
        "Choose below 👇" + DISCLAIMER
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    now_utc = datetime.now(timezone.utc)

    if data == "menu":
        await q.edit_message_text("Choose an option 👇", reply_markup=main_keyboard())
        return

    if data == "live":
        await q.edit_message_text("⏳ Fetching live scores...")
        fixtures = await api_get("fixtures", {"live": "all"})
        if not fixtures:
            await q.edit_message_text("No live matches right now. Check Upcoming 📅", reply_markup=main_keyboard())
            return
        txt = "🔴 <b>LIVE NOW</b>\n\n" + "\n\n".join([format_match(f) for f in fixtures[:15]])
        await q.edit_message_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
        return

    if data == "results":
        await q.edit_message_text("⏳ Fetching yesterday's results...")
        yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
        fixtures = await api_get("fixtures", {"date": yesterday})
        top_ids = {v["id"] for v in LEAGUES.values()}
        filtered = [f for f in fixtures if f["league"]["id"] in top_ids] or fixtures
        if not filtered:
            await q.edit_message_text(f"No results found for {yesterday}.", reply_markup=main_keyboard())
            return
        txt = f"🏆 <b>Results - {yesterday}</b>\n\n" + "\n\n".join([format_match(f) for f in filtered[:15]])
        await q.edit_message_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
        return

    if data == "tips":
        await q.edit_message_text("⏳ Analyzing today's best 3 matches with AI...")
        today = now_utc.strftime("%Y-%m-%d")
        fixtures = await api_get("fixtures", {"date": today})
        top_ids = {v["id"] for v in LEAGUES.values()}
        upcoming_today = [f for f in fixtures if f["fixture"]["status"]["short"] == "NS"]
        picked = [f for f in upcoming_today if f["league"]["id"] in top_ids][:3]
        if len(picked) < 3:
            picked = (picked + [f for f in upcoming_today if f not in picked])[:3]
        if not picked:
            await q.edit_message_text("No upcoming matches left today, check back tomorrow!", reply_markup=main_keyboard())
            return

        matches_for_image = []
        txt = "🎯 <b>TODAY'S 3 FREE TIPS - EFFA PRONOSS</b>\n\n"

        for f in picked:
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            league = f["league"]["name"]
            pred = await get_gemini_prediction(home, away, league)

            txt += (
                f"🏆 <b>{html.escape(league)}</b>\n"
                f"{html.escape(home)} vs {html.escape(away)}\n"
                f"✅ <b>Tip:</b> {html.escape(pred['tip'])} ({html.escape(pred['confidence'])})\n"
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
            await q.message.reply_text(txt[:4000], reply_markup=main_keyboard(), parse_mode="HTML")
        return

    if data == "upcoming_menu":
        await q.edit_message_text("📅 Choose a League for Upcoming Matches (Next 3 Days):", reply_markup=upcoming_keyboard())
        return

    if data.startswith("upcoming_"):
        key = data.replace("upcoming_", "")
        league = LEAGUES.get(key)
        if not league:
            return
        await q.edit_message_text(f"⏳ Loading {league['name']}...")
        txt = f"📅 <b>Upcoming - {league['name']}</b>\n\n"
        count = 0

        for i in range(3):
            if count >= 6:
                break
            d = (now_utc + timedelta(days=i)).strftime("%Y-%m-%d")
            fixtures = await api_get("fixtures", {"date": d, "league": league["id"], "season": 2026})
            for f in fixtures:
                if count >= 6:
                    break
                home = f["teams"]["home"]["name"]
                away = f["teams"]["away"]["name"]
                pred = await get_gemini_prediction(home, away, league["name"])
                txt += (
                    f"🕐 {d} {f['fixture']['date'][11:16]} UTC\n"
                    f"<b>{html.escape(home)} vs {html.escape(away)}</b>\n"
                    f"🎯 {html.escape(pred['tip'])} — <i>{html.escape(pred['reason'])}</i>\n\n"
                )
                count += 1

        if count == 0:
            txt += "No scheduled matches in the next 3 days for this league."
        txt += DISCLAIMER
        await q.edit_message_text(txt[:4000], reply_markup=upcoming_keyboard(), parse_mode="HTML")
        return

    if data == "search_help":
        await q.edit_message_text(
            "🔍 Just type in the chat:\n\n<code>Arsenal vs Chelsea</code>\n\n"
            "I will fetch the score/kickoff time and AI prediction.",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    if data == "stats_help":
        await q.edit_message_text(
            "📊 To get Head-to-Head history, type:\n\n<code>Stats Arsenal vs Chelsea</code>",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if " vs " not in text.lower():
        await update.message.reply_text("Type a match like: <b>Arsenal vs Chelsea</b>", reply_markup=main_keyboard(), parse_mode="HTML")
        return

    is_stats = text.lower().startswith("stats")
    clean = text.lower().replace("stats", "").strip()
    parts = clean.split(" vs ")
    if len(parts) != 2:
        await update.message.reply_text("Use format: <code>Team1 vs Team2</code>", reply_markup=main_keyboard(), parse_mode="HTML")
        return

    home_q, away_q = parts[0].strip(), parts[1].strip()
    status_msg = await update.message.reply_text(f"⏳ Searching {html.escape(home_q.title())} vs {html.escape(away_q.title())}...")

    found = None
    now_utc = datetime.now(timezone.utc)
    for offset in [0, 1, 2, -1]:
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
        await status_msg.edit_text(
            "❌ Match not found in the 4-day window. Try full team names (e.g., <i>Manchester City vs Liverpool</i>).",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    home = found["teams"]["home"]["name"]
    away = found["teams"]["away"]["name"]
    league = found["league"]["name"]
    score_txt = format_match(found)

    # Fetch Head-to-Head stats if user asked for "Stats"
    h2h_txt = ""
    if is_stats:
        h_id = found["teams"]["home"]["id"]
        a_id = found["teams"]["away"]["id"]
        h2h_matches = await api_get("fixtures/headtohead", {"h2h": f"{h_id}-{a_id}", "last": 5})
        if h2h_matches:
            h2h_lines = [
                f"• {m['fixture']['date'][:10]}: {html.escape(m['teams']['home']['name'])} {m['goals']['home']}-{m['goals']['away']} {html.escape(m['teams']['away']['name'])}"
                for m in h2h_matches
            ]
            h2h_txt = "\n\n📊 <b>Last 5 Head-to-Head:</b>\n" + "\n".join(h2h_lines)

    pred = await get_gemini_prediction(home, away, league)
    reply = (
        f"{score_txt}"
        f"{h2h_txt}\n\n"
        f"🎯 <b>AI Analysis:</b>\n"
        f"✅ <b>Tip:</b> {html.escape(pred['tip'])} ({html.escape(pred['confidence'])})\n"
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
    print("EFFA PRONOSS Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
