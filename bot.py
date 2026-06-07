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
 
# ========== CONFIG ==========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID_INDIVIDUAL = os.getenv("SHEET_ID_INDIVIDUAL")
SHEET_ID_TEAM = os.getenv("SHEET_ID_TEAM")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
 
# ชื่อ channel ที่ bot จะฟัง (ใส่ชื่อ channel จริงในเซิร์ฟเวอร์ Discord)
LISTEN_CHANNELS = ["สมัครแข่ง", "register", "สมัคร"]
 
# ========== SETUP ==========
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
 
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
 
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
 
# ========== GEMINI PROMPT ==========
SYSTEM_PROMPT = """
คุณเป็น AI ช่วยแยกข้อมูลการสมัครแข่งขันปิงปอง จากข้อความที่ส่งมา
 
ให้แยกข้อมูลและตอบกลับเป็น JSON เท่านั้น ห้ามมีข้อความอื่น
 
รูปแบบที่ต้องการ:
{
  "type": "individual" หรือ "team" หรือ "mixed" หรือ "unknown",
  "payment_status": "จ่ายแล้ว" หรือ "ยังไม่จ่าย",
  "affiliation": "สังกัดหรือชื่อทีม (ถ้ามี)",
  "individual_entries": [
    {
      "name": "ชื่อ-นามสกุล",
      "categories": ["รุ่นที่สมัคร เช่น เดี่ยวทั่วไปชาย, เดี่ยว40ปีหญิง, จำกัดมือชาย"],
      "rank": "แรงค์ (ตัวเลขในวงเล็บ หรือ ไม่มีแรงค์)",
      "phone": "เบอร์โทร (ถ้ามี)"
    }
  ],
  "team_entries": [
    {
      "team_name": "ชื่อทีม",
      "category": "ประเภททีม เช่น ทีมทั่วไปชาย, ทีมรวมอายุ100ปี",
      "players": [
        {"name": "ชื่อผู้เล่น", "rank": "แรงค์", "age": "อายุ (ถ้ามี)"}
      ]
    }
  ],
  "notes": "หมายเหตุพิเศษ เช่น แก้ rank, ยังไม่จ่าย"
}
 
กฎ:
- ตัวเลขในวงเล็บ () = แรงค์
- ยังไม่จ่าย, ค้างจ่าย = payment_status: ยังไม่จ่าย
- จ่ายแล้ว = payment_status: จ่ายแล้ว
- ถ้าไม่ระบุสถานะ = ยังไม่จ่าย
- รุ่นอายุ: 7ปี, 9ปี, 11ปี, 13ปี, 15ปี, 40ปี, 50ปี, 60ปี+, ทั่วไป, จำกัดมือ
- ถ้าข้อความไม่เกี่ยวกับการสมัคร ให้ตอบ {"type": "unknown"}
"""
 
# ========== ASYNC GEMINI (ไม่ block event loop) ==========
async def analyze_with_gemini(text: str, image_data: bytes = None) -> dict:
    try:
        parts = [{"text": SYSTEM_PROMPT + "\n\nข้อความ: " + text}]
 
        if image_data:
            b64 = base64.b64encode(image_data).decode()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
 
        payload = {"contents": [{"parts": parts}]}
 
        async with httpx.AsyncClient(timeout=30) as hc:
            resp = await hc.post(GEMINI_URL, json=payload)
            resp.raise_for_status()
            result = resp.json()
 
        raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Gemini error: {e}")
        return {"type": "unknown"}
 
 
# ========== GOOGLE SHEETS (รัน in thread เพื่อไม่ block) ==========
def _get_or_create_sheet(spreadsheet_id: str, sheet_name: str, headers: list):
    try:
        sh = gc.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=len(headers))
            ws.append_row(headers)
        return ws
    except Exception as e:
        import traceback
        print(f"Sheet error: {e}")
        traceback.print_exc()
        return None
 
 
def _save_individual(data: dict, discord_user: str):
    headers = ["Timestamp", "ชื่อ-นามสกุล", "ประเภท/รุ่น", "แรงค์", "เบอร์โทร",
               "สังกัด", "สถานะจ่ายเงิน", "Discord User", "หมายเหตุ"]
    ws = _get_or_create_sheet(SHEET_ID_INDIVIDUAL, "Discord Registrations", headers)
    if not ws:
        return 0
    count = 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for entry in data.get("individual_entries", []):
        categories = ", ".join(entry.get("categories", []))
        row = [
            timestamp,
            entry.get("name", ""),
            categories,
            entry.get("rank", "ไม่มีแรงค์"),
            entry.get("phone", ""),
            data.get("affiliation", ""),
            data.get("payment_status", "ยังไม่จ่าย"),
            discord_user,
            data.get("notes", "")
        ]
        ws.append_row(row)
        count += 1
    return count
 
 
def _save_team(data: dict, discord_user: str):
    headers = ["Timestamp", "ชื่อทีม", "ประเภททีม", "ผู้เล่น1", "แรงค์1",
               "ผู้เล่น2", "แรงค์2", "ผู้เล่น3", "แรงค์3",
               "สังกัด", "สถานะจ่ายเงิน", "Discord User", "หมายเหตุ"]
    ws = _get_or_create_sheet(SHEET_ID_TEAM, "Discord Team Registrations", headers)
    if not ws:
        return 0
    count = 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for team in data.get("team_entries", []):
        players = team.get("players", [])
        p = [players[i] if i < len(players) else {} for i in range(3)]
        row = [
            timestamp,
            team.get("team_name", ""),
            team.get("category", ""),
            p[0].get("name", ""), p[0].get("rank", ""),
            p[1].get("name", ""), p[1].get("rank", ""),
            p[2].get("name", ""), p[2].get("rank", ""),
            data.get("affiliation", ""),
            data.get("payment_status", "ยังไม่จ่าย"),
            discord_user,
            data.get("notes", "")
        ]
        ws.append_row(row)
        count += 1
    return count
 
 
def build_reply(data: dict, individual_count: int, team_count: int) -> str:
    if data.get("type") == "unknown":
        return None
 
    lines = ["✅ **บันทึกข้อมูลแล้ว**"]
 
    if data.get("affiliation"):
        lines.append(f"🏢 สังกัด: {data['affiliation']}")
 
    status_emoji = "💰" if data.get("payment_status") == "จ่ายแล้ว" else "⏳"
    lines.append(f"{status_emoji} สถานะ: {data.get('payment_status', 'ยังไม่จ่าย')}")
 
    if data.get("individual_entries"):
        lines.append(f"\n👤 **รายชื่อบุคคล ({individual_count} คน)**")
        for e in data["individual_entries"]:
            cats = ", ".join(e.get("categories", []))
            rank = e.get("rank", "ไม่มีแรงค์")
            lines.append(f"• {e.get('name','')} — {cats} (แรงค์: {rank})")
 
    if data.get("team_entries"):
        lines.append(f"\n🏓 **ทีม ({team_count} ทีม)**")
        for t in data["team_entries"]:
            lines.append(f"• {t.get('team_name','')} — {t.get('category','')}")
            for p in t.get("players", []):
                lines.append(f"  └ {p.get('name','')} (แรงค์: {p.get('rank','?')})")
 
    if data.get("notes"):
        lines.append(f"\n📝 หมายเหตุ: {data['notes']}")
 
    return "\n".join(lines)
 
 
# ========== DISCORD EVENTS ==========
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
 
    # ดึงรูปภาพถ้ามี
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
 
    # วิเคราะห์ด้วย Gemini
    async with message.channel.typing():
        data = await analyze_with_gemini(text or "(ดูจากรูปภาพ)", image_data)
 
    if data.get("type") == "unknown":
        return
 
    # บันทึกลง Sheet (รันใน thread แยก ไม่ block Discord)
    individual_count = 0
    team_count = 0
    discord_user = str(message.author)
 
    if data.get("individual_entries"):
        individual_count = await asyncio.to_thread(_save_individual, data, discord_user)
    if data.get("team_entries"):
        team_count = await asyncio.to_thread(_save_team, data, discord_user)
 
    # ตอบกลับ Discord
    reply = build_reply(data, individual_count, team_count)
    if reply:
        await message.reply(reply)
 
 
client.run(DISCORD_TOKEN)
 
