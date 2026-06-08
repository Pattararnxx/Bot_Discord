import discord
from discord import app_commands
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
from urllib.parse import urlparse, parse_qs

load_dotenv()

# ========== CONSTANTS ==========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
CONFIG_FILE = "config.json"

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)

intents = discord.Intents.default()
intents.message_content = True

class PingPongBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = PingPongBot()

# ========== CONFIG ==========
DEFAULT_CONFIG = {
    "listen_channels": [],
    "individual": {
        "form_url": "",
        "sheet_id": "",
        "sheet_gid": 0,
        "categories": []
    },
    "team": {
        "form_url": "",
        "sheet_id": "",
        "sheet_gid": 0,
        "categories": []
    }
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)

def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def extract_sheet_id_gid(url: str) -> tuple[str, int]:
    """แยก sheet ID และ gid จาก URL"""
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    sheet_id = match.group(1) if match else ""
    gid_match = re.search(r'gid=(\d+)', url)
    gid = int(gid_match.group(1)) if gid_match else 0
    return sheet_id, gid

def extract_form_id(url: str) -> str:
    match = re.search(r'/forms/d/e/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'/forms/d/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else ""

async def fetch_form_options(form_url: str) -> list[str]:
    """ดึง dropdown options จาก Google Form"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
            resp = await hc.get(form_url)
            html = resp.text

        # Google Form เก็บ options ใน JSON ที่ฝังใน HTML
        match = re.search(r'var FB_PUBLIC_LOAD_DATA_ = (\[.+?\]);\s*</script>', html, re.DOTALL)
        if not match:
            return []

        data = json.loads(match.group(1))
        options = []

        def extract_opts(obj):
            if isinstance(obj, list):
                for item in obj:
                    extract_opts(item)
            # options อยู่ใน structure แบบ [option_text, '', 0]
            if isinstance(obj, list) and len(obj) >= 1 and isinstance(obj[0], str) and len(obj[0]) > 0:
                if isinstance(obj[-1], int) and len(obj) <= 4:
                    options.append(obj[0])

        extract_opts(data)
        # กรองเอาเฉพาะที่ดูเหมือน category (ไม่สั้นเกินไป ไม่ยาวเกินไป)
        filtered = [o for o in options if 3 < len(o) < 80]
        return list(dict.fromkeys(filtered))  # deduplicate
    except Exception as e:
        print(f"Form fetch error: {e}")
        return []


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

def _save_rows(sheet_id: str, gid: int, rows: list):
    ws = _get_worksheet(sheet_id, gid)
    for row in rows:
        ws.append_row(row)
    return len(rows)


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

async def analyze_message(text: str, image_data: bytes = None, individual_options: list = None, team_options: list = None) -> dict:
    ind_opts = ""
    if individual_options:
        opts_str = "\n".join(f"- {o}" for o in individual_options)
        ind_opts = f"\nสำหรับประเภทบุคคล ให้เลือกจากตัวเลือกนี้เท่านั้น (เลือกที่ตรงที่สุด):\n{opts_str}\n"

    team_opts = ""
    if team_options:
        opts_str = "\n".join(f"- {o}" for o in team_options)
        team_opts = f"\nสำหรับประเภททีม ให้เลือกจากตัวเลือกนี้เท่านั้น (เลือกที่ตรงที่สุด):\n{opts_str}\n"

    prompt = f"""คุณเป็น AI ช่วยแยกข้อมูลการสมัครแข่งขันปิงปอง ตอบกลับเป็น JSON เท่านั้น

{{
  "type": "individual" หรือ "team" หรือ "mixed" หรือ "unknown",
  "payment_status": "จ่ายแล้ว" หรือ "ยังไม่จ่าย",
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
  ],
  "notes": "หมายเหตุ"
}}

กฎ: ตัวเลขในวงเล็บ () = แรงค์, ถ้าไม่ระบุสถานะจ่าย = ยังไม่จ่าย, ถ้าไม่เกี่ยวการสมัคร ตอบ {{"type": "unknown"}}
{ind_opts}{team_opts}
ข้อความ: {text}"""

    try:
        raw = await call_gemini(prompt, image_data)
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Gemini analyze error: {e}")
        return {"type": "unknown"}

async def map_to_headers(entry: dict, headers: list, meta: dict) -> list:
    prompt = f"""กรอกข้อมูลลง Google Sheet

Headers (เรียงตาม column):
{json.dumps(headers, ensure_ascii=False)}

ข้อมูล:
{json.dumps(entry, ensure_ascii=False)}

เพิ่มเติม: timestamp={meta['timestamp']}, สถานะจ่าย={meta['payment_status']}, Discord={meta['discord_user']}

ตอบเป็น JSON array ตามลำดับ column เท่านั้น เช่น ["ค่า1","ค่า2"]
- column timestamp/ประทับเวลา ใส่ค่า timestamp
- column ชำระ/QR/สแกน ใส่สถานะจ่ายเงิน
- ไม่มีข้อมูลใส่ -"""
    try:
        raw = await call_gemini(prompt)
        raw = re.sub(r"```json|```", "", raw).strip()
        row = json.loads(raw)
        if isinstance(row, list):
            while len(row) < len(headers):
                row.append("-")
            return row[:len(headers)]
    except Exception as e:
        print(f"Gemini map error: {e}")
    return ["-"] * len(headers)


# ========== SAVE ==========
async def save_entries(data: dict, discord_user: str, config: dict) -> tuple[int, int]:
    meta = {
        "timestamp": datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
        "payment_status": data.get("payment_status", "ยังไม่จ่าย"),
        "discord_user": discord_user,
    }
    individual_count = 0
    team_count = 0

    if data.get("individual_entries") and config["individual"]["sheet_id"]:
        ws, headers = await asyncio.to_thread(
            _get_headers, config["individual"]["sheet_id"], config["individual"]["sheet_gid"]
        )
        rows = []
        for entry in data["individual_entries"]:
            row = await map_to_headers(entry, headers, meta)
            rows.append(row)
        if rows:
            await asyncio.to_thread(_save_rows, config["individual"]["sheet_id"], config["individual"]["sheet_gid"], rows)
            individual_count = len(rows)

    if data.get("team_entries") and config["team"]["sheet_id"]:
        ws, headers = await asyncio.to_thread(
            _get_headers, config["team"]["sheet_id"], config["team"]["sheet_gid"]
        )
        rows = []
        for team in data["team_entries"]:
            row = await map_to_headers(team, headers, meta)
            rows.append(row)
        if rows:
            await asyncio.to_thread(_save_rows, config["team"]["sheet_id"], config["team"]["sheet_gid"], rows)
            team_count = len(rows)

    return individual_count, team_count


# ========== SLASH COMMANDS ==========
@client.tree.command(name="setup", description="ตั้งค่า Bot สำหรับการแข่งขัน")
@app_commands.describe(
    individual_form="URL ของ Google Form สมัครบุคคล",
    team_form="URL ของ Google Form สมัครทีม",
    individual_sheet="URL ของ Google Sheet บุคคล (รวม gid)",
    team_sheet="URL ของ Google Sheet ทีม (รวม gid)",
    channel="ชื่อ channel ที่ Bot จะฟัง (คั่นด้วย , ถ้าหลาย channel)"
)
async def setup(
    interaction: discord.Interaction,
    individual_form: str = None,
    team_form: str = None,
    individual_sheet: str = None,
    team_sheet: str = None,
    channel: str = None
):
    await interaction.response.defer(thinking=True)
    config = load_config()
    changes = []

    if individual_form:
        config["individual"]["form_url"] = individual_form
        opts = await fetch_form_options(individual_form)
        config["individual"]["categories"] = opts
        changes.append(f"✅ Form บุคคล: ดึงได้ **{len(opts)}** ตัวเลือก")

    if team_form:
        config["team"]["form_url"] = team_form
        opts = await fetch_form_options(team_form)
        config["team"]["categories"] = opts
        changes.append(f"✅ Form ทีม: ดึงได้ **{len(opts)}** ตัวเลือก")

    if individual_sheet:
        sid, gid = extract_sheet_id_gid(individual_sheet)
        config["individual"]["sheet_id"] = sid
        config["individual"]["sheet_gid"] = gid
        changes.append(f"✅ Sheet บุคคล: `{sid}` (gid={gid})")

    if team_sheet:
        sid, gid = extract_sheet_id_gid(team_sheet)
        config["team"]["sheet_id"] = sid
        config["team"]["sheet_gid"] = gid
        changes.append(f"✅ Sheet ทีม: `{sid}` (gid={gid})")

    if channel:
        channels = [c.strip() for c in channel.split(",")]
        config["listen_channels"] = channels
        changes.append(f"✅ ฟัง channel: {', '.join(channels)}")

    if changes:
        save_config(config)
        await interaction.followup.send("**⚙️ บันทึก config แล้ว**\n" + "\n".join(changes))
    else:
        await interaction.followup.send("❌ ไม่ได้ระบุอะไรเลย ใช้ `/setup [option] [ค่า]`")


@client.tree.command(name="config", description="ดู config ปัจจุบัน")
async def show_config(interaction: discord.Interaction):
    config = load_config()
    ind = config["individual"]
    team = config["team"]
    channels = config.get("listen_channels", [])

    lines = ["**⚙️ Config ปัจจุบัน**\n"]
    lines.append(f"📢 ฟัง channel: {', '.join(channels) if channels else '(ยังไม่ตั้ง)'}")
    lines.append(f"\n👤 **บุคคล**")
    lines.append(f"• Sheet: `{ind['sheet_id'] or '(ยังไม่ตั้ง)'}` gid={ind['sheet_gid']}")
    lines.append(f"• Form: {ind['form_url'] or '(ยังไม่ตั้ง)'}")
    lines.append(f"• ตัวเลือกประเภท: {len(ind['categories'])} รายการ")
    if ind["categories"]:
        lines.append("  " + "\n  ".join(f"- {c}" for c in ind["categories"][:5]))
        if len(ind["categories"]) > 5:
            lines.append(f"  ... และอีก {len(ind['categories'])-5} รายการ")

    lines.append(f"\n🏓 **ทีม**")
    lines.append(f"• Sheet: `{team['sheet_id'] or '(ยังไม่ตั้ง)'}` gid={team['sheet_gid']}")
    lines.append(f"• Form: {team['form_url'] or '(ยังไม่ตั้ง)'}")
    lines.append(f"• ตัวเลือกประเภท: {len(team['categories'])} รายการ")
    if team["categories"]:
        lines.append("  " + "\n  ".join(f"- {c}" for c in team["categories"][:5]))

    await interaction.response.send_message("\n".join(lines))


@client.tree.command(name="reload_form", description="ดึง options จาก Form ใหม่อีกครั้ง")
async def reload_form(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    config = load_config()
    results = []

    if config["individual"]["form_url"]:
        opts = await fetch_form_options(config["individual"]["form_url"])
        config["individual"]["categories"] = opts
        results.append(f"✅ Form บุคคล: ดึงได้ **{len(opts)}** ตัวเลือก")
    else:
        results.append("❌ ยังไม่ได้ตั้ง Form บุคคล")

    if config["team"]["form_url"]:
        opts = await fetch_form_options(config["team"]["form_url"])
        config["team"]["categories"] = opts
        results.append(f"✅ Form ทีม: ดึงได้ **{len(opts)}** ตัวเลือก")
    else:
        results.append("❌ ยังไม่ได้ตั้ง Form ทีม")

    save_config(config)
    await interaction.followup.send("\n".join(results))


@client.tree.command(name="reset_config", description="รีเซ็ต config ทั้งหมด")
async def reset_config(interaction: discord.Interaction):
    save_config(dict(DEFAULT_CONFIG))
    await interaction.response.send_message("♻️ รีเซ็ต config เรียบร้อยแล้ว")


# ========== ON MESSAGE ==========
@client.event
async def on_ready():
    print(f"✅ Bot พร้อมใช้งาน: {client.user}")
    config = load_config()
    channels = config.get("listen_channels", [])
    print(f"🎯 ฟัง channel: {channels if channels else '(ยังไม่ตั้ง — ใช้ /setup channel)'}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    config = load_config()
    listen_channels = config.get("listen_channels", [])

    if not listen_channels or message.channel.name not in listen_channels:
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
        data = await analyze_message(
            text or "(ดูจากรูปภาพ)",
            image_data,
            config["individual"].get("categories"),
            config["team"].get("categories")
        )

    if data.get("type") == "unknown":
        return

    individual_count, team_count = await save_entries(data, str(message.author), config)

    # สร้าง reply
    lines = ["✅ **บันทึกข้อมูลแล้ว**"]
    status_emoji = "💰" if data.get("payment_status") == "จ่ายแล้ว" else "⏳"
    lines.append(f"{status_emoji} สถานะ: {data.get('payment_status', 'ยังไม่จ่าย')}")

    if data.get("individual_entries"):
        lines.append(f"\n👤 **รายชื่อบุคคล ({individual_count} คน)**")
        for e in data["individual_entries"]:
            lines.append(f"• {e.get('ชื่อ','')} — {e.get('ประเภท','')} (แรงค์: {e.get('แรงค์','-')})")

    if data.get("team_entries"):
        lines.append(f"\n🏓 **ทีม ({team_count} ทีม)**")
        for t in data["team_entries"]:
            lines.append(f"• {t.get('ชื่อทีม','')} — {t.get('ประเภท','')}")
            for p in t.get("ผู้เล่น", []):
                if p.get("ชื่อ", "-") != "-":
                    lines.append(f"  └ {p['ชื่อ']} (แรงค์: {p.get('แรงค์','-')})")

    if data.get("notes"):
        lines.append(f"\n📝 {data['notes']}")

    await message.reply("\n".join(lines))


client.run(DISCORD_TOKEN)