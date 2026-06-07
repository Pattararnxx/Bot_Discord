import discord
import gspread
import asyncio
import base64
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
import json
import re
import httpx
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID_INDIVIDUAL = os.getenv("SHEET_ID_INDIVIDUAL")
SHEET_ID_TEAM = os.getenv("SHEET_ID_TEAM")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
SHEET_GID_INDIVIDUAL = int(os.getenv("SHEET_GID_INDIVIDUAL", "0"))
SHEET_GID_TEAM = int(os.getenv("SHEET_GID_TEAM", "0"))

LISTEN_CHANNELS = ["สมัครแข่ง", "register", "สมัคร"]

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ========== SHEET HELPERS ==========
def _get_worksheet(sheet_id: str, gid: int):
    sh = gc.open_by_key(sheet_id)
    for ws in sh.worksheets():
        if ws.id == gid:
            return ws
    return sh.get_worksheet(0)


def _get_headers(sheet_id: str, gid: int) -> tuple:
    ws = _get_worksheet(sheet_id, gid)
    return ws, ws.row_values(1)


# ========== GEMINI ==========
async def call_gemini(prompt: str, image_data: bytes = None) -> str:
    parts = [{"text": prompt}]
    if image_data:
        b64 = base64.b64encode(image_data).decode()
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    payload = {"contents": [{"parts": parts}]}
    async with httpx.AsyncClient(timeout=30) as hc:
        resp = await hc.post(GEMINI_URL, json=payload)
        resp.raise_for_status()
        result = resp.json()
    return result["candidates"][0]["content"]["parts"][0]["text"].strip()


async def analyze_message(text: str, image_data: bytes = None) -> dict:
    """รอบแรก: แยกข้อมูลดิบออกมาก่อน"""
    prompt = f"""
คุณเป็น AI ช่วยแยกข้อมูลการสมัครแข่งขันปิงปอง

ตอบกลับเป็น JSON เท่านั้น ห้ามมีข้อความอื่น

{{
  "type": "individual" หรือ "team" หรือ "mixed" หรือ "unknown",
  "payment_status": "จ่ายแล้ว" หรือ "ยังไม่จ่าย",
  "discord_source": "{text[:50]}",
  "individual_entries": [
    {{
      "ชื่อ": "ชื่อ-นามสกุล",
      "ประเภท": "รุ่นที่สมัคร",
      "แรงค์": "ตัวเลข หรือ - ถ้าไม่มี",
      "สังกัด": "สังกัด หรือ -",
      "เบอร์": "เบอร์โทร หรือ -"
    }}
  ],
  "team_entries": [
    {{
      "ชื่อทีม": "ชื่อทีม",
      "ประเภท": "ประเภททีม",
      "เบอร์": "เบอร์ติดต่อ หรือ -",
      "ผู้เล่น": [
        {{"ชื่อ": "ชื่อผู้เล่น", "แรงค์": "แรงค์ หรือ -", "อายุ": "อายุ หรือ -"}}
      ]
    }}
  ]
}}

กฎ:
- ตัวเลขในวงเล็บ () หลังชื่อ = แรงค์
- ถ้าไม่ระบุสถานะจ่าย = ยังไม่จ่าย
- ถ้าไม่เกี่ยวกับการสมัคร ตอบ {{"type": "unknown"}}

ข้อความ: {text}
"""
    try:
        raw = await call_gemini(prompt, image_data)
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Gemini analyze error: {e}")
        return {"type": "unknown"}


async def map_to_headers(entry: dict, headers: list, meta: dict) -> list:
    """รอบสอง: ให้ Gemini จับคู่ข้อมูลกับ header จริงของ Sheet"""
    prompt = f"""
คุณต้องกรอกข้อมูลลง Google Sheet

Headers ของ Sheet (เรียงตามลำดับ column):
{json.dumps(headers, ensure_ascii=False)}

ข้อมูลที่มี:
{json.dumps(entry, ensure_ascii=False)}

ข้อมูลเพิ่มเติม:
- ประทับเวลา: {meta.get('timestamp')}
- สถานะจ่ายเงิน: {meta.get('payment_status')}
- Discord User: {meta.get('discord_user')}

ให้ตอบกลับเป็น JSON array ที่มีค่าตรงกับแต่ละ column ตามลำดับ
ถ้า column ไหนไม่มีข้อมูลให้ใส่ "-"
column ที่เกี่ยวกับเวลา/timestamp ให้ใส่ค่าประทับเวลาที่ให้มา
column ที่เกี่ยวกับการชำระเงิน/QR/สแกน ให้ใส่สถานะจ่ายเงิน
ตอบแค่ JSON array เท่านั้น เช่น ["ค่า1", "ค่า2", "ค่า3"]
"""
    try:
        raw = await call_gemini(prompt)
        raw = re.sub(r"```json|```", "", raw).strip()
        row = json.loads(raw)
        if isinstance(row, list):
            # ปรับให้ตรงจำนวน column
            while len(row) < len(headers):
                row.append("-")
            return row[:len(headers)]
    except Exception as e:
        print(f"Gemini map error: {e}")
    return ["-"] * len(headers)


# ========== SAVE TO SHEET ==========
def _save_rows(sheet_id: str, gid: int, rows: list[list]):
    ws = _get_worksheet(sheet_id, gid)
    for row in rows:
        ws.append_row(row)
    return len(rows)


async def save_individual(data: dict, discord_user: str) -> int:
    ws, headers = await asyncio.to_thread(_get_headers, SHEET_ID_INDIVIDUAL, SHEET_GID_INDIVIDUAL)
    meta = {
        "timestamp": datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
        "payment_status": data.get("payment_status", "ยังไม่จ่าย"),
        "discord_user": discord_user,
    }
    rows = []
    for entry in data.get("individual_entries", []):
        row = await map_to_headers(entry, headers, meta)
        rows.append(row)
    if rows:
        await asyncio.to_thread(_save_rows, SHEET_ID_INDIVIDUAL, SHEET_GID_INDIVIDUAL, rows)
    return len(rows)


async def save_team(data: dict, discord_user: str) -> int:
    ws, headers = await asyncio.to_thread(_get_headers, SHEET_ID_TEAM, SHEET_GID_TEAM)
    meta = {
        "timestamp": datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
        "payment_status": data.get("payment_status", "ยังไม่จ่าย"),
        "discord_user": discord_user,
    }
    rows = []
    for team in data.get("team_entries", []):
        row = await map_to_headers(team, headers, meta)
        rows.append(row)
    if rows:
        await asyncio.to_thread(_save_rows, SHEET_ID_TEAM, SHEET_GID_TEAM, rows)
    return len(rows)


# ========== REPLY ==========
def build_reply(data: dict, individual_count: int, team_count: int) -> str:
    if data.get("type") == "unknown":
        return None
    lines = ["✅ **บันทึกข้อมูลแล้ว**"]
    status_emoji = "💰" if data.get("payment_status") == "จ่ายแล้ว" else "⏳"
    lines.append(f"{status_emoji} สถานะ: {data.get('payment_status', 'ยังไม่จ่าย')}")
    if data.get("individual_entries"):
        lines.append(f"\n👤 **รายชื่อบุคคล ({individual_count} คน)**")
        for e in data["individual_entries"]:
            lines.append(f"• {e.get('ชื่อ', '')} — {e.get('ประเภท', '')} (แรงค์: {e.get('แรงค์', '-')})")
    if data.get("team_entries"):
        lines.append(f"\n🏓 **ทีม ({team_count} ทีม)**")
        for t in data["team_entries"]:
            lines.append(f"• {t.get('ชื่อทีม', '')} — {t.get('ประเภท', '')}")
            for p in t.get("ผู้เล่น", []):
                if p.get("ชื่อ", "-") != "-":
                    lines.append(f"  └ {p['ชื่อ']} (แรงค์: {p.get('แรงค์', '-')})")
    if data.get("notes"):
        lines.append(f"\n📝 {data['notes']}")
    return "\n".join(lines)


# ========== DISCORD ==========
@client.event
async def on_ready():
    print(f"✅ Bot พร้อมใช้งาน: {client.user}")
    print(f"🎯 ฟัง channel: {LISTEN_CHANNELS}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.name not in LISTEN_CHANNELS:
        return

    text = message.content.strip()
    image_data = None

    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                try:
                    async with httpx.AsyncClient() as hc:
                        resp = await hc.get(att.url)
                        image_data = resp.content
                except Exception as e:
                    print(f"Image download error: {e}")
                break

    if not text and not image_data:
        return

    async with message.channel.typing():
        data = await analyze_message(text or "(ดูจากรูปภาพ)", image_data)

    if data.get("type") == "unknown":
        return

    individual_count = 0
    team_count = 0
    discord_user = str(message.author)

    if data.get("individual_entries"):
        individual_count = await save_individual(data, discord_user)
    if data.get("team_entries"):
        team_count = await save_team(data, discord_user)

    reply = build_reply(data, individual_count, team_count)
    if reply:
        await message.reply(reply)


client.run(DISCORD_TOKEN)