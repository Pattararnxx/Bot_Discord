import discord
from discord import app_commands
import gspread
import asyncio
import base64
from google.oauth2.service_account import Credentials
import google.auth.transport.requests
from datetime import datetime
import os
import json
import re
import httpx
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
CONFIG_FILE = "config.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/forms.body.readonly"
]
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

DEFAULT_CONFIG = {
    "listen_channels": [],
    "individual": {"form_url": "", "sheet_id": "", "sheet_gid": 0, "categories": []},
    "team": {"form_url": "", "sheet_id": "", "sheet_gid": 0, "categories": []}
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
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    sheet_id = match.group(1) if match else ""
    gid_match = re.search(r'gid=(\d+)', url)
    gid = int(gid_match.group(1)) if gid_match else 0
    return sheet_id, gid

async def fetch_form_options(form_url: str) -> list[str]:
    try:
        match = re.search(r'/forms/d/(?:e/)?([a-zA-Z0-9_-]+)', form_url)
        if not match:
            return []
        form_id = match.group(1)
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        api_url = f"https://forms.googleapis.com/v1/forms/{form_id}"
        headers = {"Authorization": f"Bearer {creds.token}"}
        async with httpx.AsyncClient(timeout=15) as hc:
            resp = await hc.get(api_url, headers=headers)
        print(f"Forms API [{form_id}] status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Forms API error: {resp.text[:300]}")
            return []
        data = resp.json()
        options = []
        for item in data.get("items", []):
            question = item.get("questionItem", {}).get("question", {})
            for opt in question.get("choiceQuestion", {}).get("options", []):
                val = opt.get("value", "").strip()
                if val:
                    options.append(val)
        print(f"ดึงได้ {len(options)} ตัวเลือก: {options[:3]}")
        return options
    except Exception as e:
        print(f"Form fetch error: {e}")
        return []

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

# ========== AI PROVIDERS ==========
# เรียงลำดับ: Groq ก่อน (เร็ว+ฟรี 30RPM) แล้วค่อย Gemini
AI_PROVIDERS = []

if GROQ_API_KEY:
    AI_PROVIDERS += [
        {"type": "groq", "model": "llama-3.3-70b-versatile"},
        {"type": "groq", "model": "llama3-8b-8192"},
    ]

AI_PROVIDERS += [
    {"type": "gemini", "model": "gemini-2.5-flash-lite"},
    {"type": "gemini", "model": "gemini-2.5-flash"},
]

async def call_ai(prompt: str, image_data: bytes = None) -> str:
    """เรียก AI โดยลอง provider ตามลำดับ ถ้า rate limit ก็สลับไปตัวถัดไป"""
    for provider in AI_PROVIDERS:
        try:
            if provider["type"] == "groq":
                # Groq ไม่รองรับรูปภาพ ข้ามถ้ามีรูป
                if image_data:
                    continue
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": provider["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1
                }
                async with httpx.AsyncClient(timeout=30) as hc:
                    resp = await hc.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    print(f"Groq {provider['model']} rate limited, trying next...")
                    continue
                resp.raise_for_status()
                result = resp.json()
                text = result["choices"][0]["message"]["content"].strip()
                print(f"✓ ใช้ Groq {provider['model']}")
                return text

            elif provider["type"] == "gemini":
                parts = [{"text": prompt}]
                if image_data:
                    b64 = base64.b64encode(image_data).decode()
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
                payload = {"contents": [{"parts": parts}]}
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{provider['model']}:generateContent?key={GEMINI_API_KEY}"
                async with httpx.AsyncClient(timeout=30) as hc:
                    resp = await hc.post(url, json=payload)
                if resp.status_code == 429:
                    print(f"Gemini {provider['model']} rate limited, trying next...")
                    await asyncio.sleep(1)
                    continue
                resp.raise_for_status()
                result = resp.json()
                text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                print(f"✓ ใช้ Gemini {provider['model']}")
                return text

        except Exception as e:
            print(f"{provider['type']} {provider['model']} error: {e}, trying next...")
            continue

    raise Exception("ทุก AI provider rate limited หมดแล้ว")


# ========== 1 CALL ANALYZE + MAP ==========
async def process_message(
    text: str,
    image_data: bytes,
    ind_headers: list,
    team_headers: list,
    ind_options: list,
    team_options: list,
    timestamp: str,
    discord_user: str
) -> dict:
    """รวม analyze + map เป็น 1 call เดียว"""

    ind_opts = ""
    if ind_options:
        ind_opts = "\nตัวเลือกประเภทบุคคล (เลือกที่ตรงที่สุด):\n" + "\n".join(f"- {o}" for o in ind_options)

    team_opts = ""
    if team_options:
        team_opts = "\nตัวเลือกประเภททีม (เลือกที่ตรงที่สุด):\n" + "\n".join(f"- {o}" for o in team_options)

    prompt = f"""คุณเป็น AI ช่วยแยกข้อมูลการสมัครแข่งขันปิงปองและกรอกลง Google Sheet

ตอบกลับเป็น JSON เท่านั้น ห้ามมีข้อความอื่น

Headers ของ Sheet บุคคล: {json.dumps(ind_headers, ensure_ascii=False)}
Headers ของ Sheet ทีม: {json.dumps(team_headers, ensure_ascii=False)}

timestamp = {timestamp}
discord_user = {discord_user}
{ind_opts}
{team_opts}

รูปแบบคำตอบ:
{{
  "type": "individual" หรือ "team" หรือ "mixed" หรือ "unknown",
  "individual_rows": [
    ["ค่า col1", "ค่า col2", ...]
  ],
  "team_rows": [
    ["ค่า col1", "ค่า col2", ...]
  ],
  "summary": {{
    "individual_entries": [
      {{"ชื่อ": "...", "ประเภท": "...", "แรงค์": "...", "payment_status": "จ่ายแล้ว หรือ ยังไม่จ่าย"}}
    ],
    "team_entries": [
      {{"ชื่อทีม": "...", "ประเภท": "...", "payment_status": "จ่ายแล้ว หรือ ยังไม่จ่าย", "ผู้เล่น": []}}
    ]
  }}
}}

กฎ:
- กรอกข้อมูลลง array ให้ตรงตาม Headers ที่ให้ไว้ทุก column
- column timestamp/ประทับเวลา ใส่ค่า timestamp ที่ให้มา
- column ชำระ/QR/สแกน ใส่สถานะจ่ายเงิน (จ่ายแล้ว หรือ ยังไม่จ่าย)
- column ที่ไม่มีข้อมูลใส่ -
- ตัวเลขในวงเล็บ () หลังชื่อ = แรงค์
- "(จ่ายแล้ว)" ติดกับชื่อใคร = การสมัครรายการนั้นของคนนั้นจ่ายแล้ว รายการอื่นของคนเดียวกันยังไม่จ่าย
- ถ้าไม่ระบุสถานะ = ยังไม่จ่าย
- ห้ามสร้างข้อมูลขึ้นมาเอง เช่น เบอร์โทร อายุ แรงค์ ถ้าไม่มีในข้อความให้ใส่ - เท่านั้น
- ห้ามเดาหรือประมาณค่าใดๆ ทั้งสิ้น
- ถ้าไม่เกี่ยวการสมัคร ตอบ {{"type": "unknown", "individual_rows": [], "team_rows": [], "summary": {{}}}}


ข้อความ: {text}"""

    try:
        raw = await call_ai(prompt, image_data)
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"AI process error: {e}")
        return {"type": "unknown", "individual_rows": [], "team_rows": [], "summary": {}}


# ========== SAVE ==========
async def save_entries(result: dict, config: dict) -> tuple[int, int]:
    individual_count = 0
    team_count = 0

    ind_rows = result.get("individual_rows", [])
    if ind_rows and config["individual"]["sheet_id"]:
        await asyncio.to_thread(
            _save_rows, config["individual"]["sheet_id"], config["individual"]["sheet_gid"], ind_rows
        )
        individual_count = len(ind_rows)

    team_rows = result.get("team_rows", [])
    if team_rows and config["team"]["sheet_id"]:
        await asyncio.to_thread(
            _save_rows, config["team"]["sheet_id"], config["team"]["sheet_gid"], team_rows
        )
        team_count = len(team_rows)

    return individual_count, team_count


# ========== SLASH COMMANDS ==========
@client.tree.command(name="setup", description="ตั้งค่า Bot สำหรับการแข่งขัน")
@app_commands.describe(
    individual_form="URL ของ Google Form สมัครบุคคล (ลิงก์ edit)",
    team_form="URL ของ Google Form สมัครทีม (ลิงก์ edit)",
    individual_sheet="URL ของ Google Sheet บุคคล (รวม gid)",
    team_sheet="URL ของ Google Sheet ทีม (รวม gid)",
    channel="ชื่อ channel ที่ Bot จะฟัง (คั่นด้วย , ถ้าหลาย channel)"
)
async def setup(interaction: discord.Interaction, individual_form: str = None, team_form: str = None, individual_sheet: str = None, team_sheet: str = None, channel: str = None):
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
        await interaction.followup.send("❌ ไม่ได้ระบุอะไรเลย")

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
    lines.append(f"• ตัวเลือกประเภท: {len(ind['categories'])} รายการ")
    if ind["categories"]:
        lines.append("\n".join(f"  - {c}" for c in ind["categories"]))
    lines.append(f"\n🏓 **ทีม**")
    lines.append(f"• Sheet: `{team['sheet_id'] or '(ยังไม่ตั้ง)'}` gid={team['sheet_gid']}")
    lines.append(f"• ตัวเลือกประเภท: {len(team['categories'])} รายการ")
    if team["categories"]:
        lines.append("\n".join(f"  - {c}" for c in team["categories"]))
    # แสดง AI providers ที่ใช้งานได้
    providers_str = " → ".join(f"{p['type']}:{p['model'].split('-')[0]}" for p in AI_PROVIDERS)
    lines.append(f"\n🤖 AI fallback: {providers_str}")
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

@client.event
async def on_ready():
    print(f"✅ Bot พร้อมใช้งาน: {client.user}")
    config = load_config()
    channels = config.get("listen_channels", [])
    print(f"🎯 ฟัง channel: {channels if channels else '(ยังไม่ตั้ง)'}")
    providers = " → ".join(f"{p['type']}:{p['model']}" for p in AI_PROVIDERS)
    print(f"🤖 AI providers: {providers}")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    config = load_config()
    if not config.get("listen_channels") or message.channel.name not in config["listen_channels"]:
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

    # ดึง headers จาก Sheet ทั้งสอง
    ind_headers, team_headers = [], []
    if config["individual"]["sheet_id"]:
        _, ind_headers = await asyncio.to_thread(
            _get_headers, config["individual"]["sheet_id"], config["individual"]["sheet_gid"]
        )
    if config["team"]["sheet_id"]:
        _, team_headers = await asyncio.to_thread(
            _get_headers, config["team"]["sheet_id"], config["team"]["sheet_gid"]
        )

    timestamp = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
    discord_user = str(message.author)

    async with message.channel.typing():
        placeholder = await message.reply("⏳ กำลังประมวลผล...")

        result = await process_message(
            text or "(ดูจากรูปภาพ)",
            image_data,
            ind_headers,
            team_headers,
            config["individual"].get("categories", []),
            config["team"].get("categories", []),
            timestamp,
            discord_user
        )

    if result.get("type") == "unknown":
        await placeholder.delete()
        return

    individual_count, team_count = await save_entries(result, config)

    # สร้าง reply จาก summary
    summary = result.get("summary", {})
    lines = ["✅ **บันทึกข้อมูลแล้ว**"]

    ind_entries = summary.get("individual_entries", [])
    if ind_entries:
        lines.append(f"\n👤 **รายชื่อบุคคล ({individual_count} คน)**")
        for e in ind_entries:
            ps = e.get("payment_status", "ยังไม่จ่าย")
            emoji = "💰" if ps == "จ่ายแล้ว" else "⏳"
            lines.append(f"{emoji} {e.get('ชื่อ','')} — {e.get('ประเภท','')} (แรงค์: {e.get('แรงค์','-')})")

    team_entries = summary.get("team_entries", [])
    if team_entries:
        lines.append(f"\n🏓 **ทีม ({team_count} ทีม)**")
        for t in team_entries:
            ps = t.get("payment_status", "ยังไม่จ่าย")
            emoji = "💰" if ps == "จ่ายแล้ว" else "⏳"
            lines.append(f"{emoji} {t.get('ชื่อทีม','')} — {t.get('ประเภท','')}")
            for p in t.get("ผู้เล่น", []):
                if isinstance(p, dict):
                    if p.get("ชื่อ", "-") != "-":
                        lines.append(f"  └ {p['ชื่อ']} (แรงค์: {p.get('แรงค์','-')})")
                elif isinstance(p, str) and p and p != "-":
                    lines.append(f"  └ {p}")

    await message.reply("\n".join(lines))

client.run(DISCORD_TOKEN)