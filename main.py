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
    "friendlies": {"id": 667, "name": "🌍 Matchs Amicaux Internationaux", "season": 2026},
    "wcq_africa": {"id": 36, "name": "🏆 Qualif. Coupe du Monde - Afrique", "season": 2026},
    "afcon": {"id": 6, "name": "🌍 Coupe d'Afrique des Nations", "season": 2025},
    "worldcup": {"id": 1, "name": "🌎 Coupe du Monde", "season": 2026},
}

gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n\n⚠️ <i>18+ | Pour divertissement uniquement. Les pronostics sont des analyses, pas des garanties. Jouez de manière responsable.</i>"

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
    text
