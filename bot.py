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
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
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
 
# รองรับทั้ง local (ไฟล์ JSON) และ Render (environment variable)
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
if GOOGLE_CREDENTIALS_JSON:
    import io
    from google.oauth2.service_account import Credentials as SACredentials
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = SACredentials.from_service_account_info(creds_info, scopes=SCOPES)
else:
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
- "(จ่ายแล้ว)" ติดกับชื่อใคร = คนนั้นจ่ายแล้ว คนอื่นยังไม่จ่าย
- ถ้าไม่ระบุสถานะ = ยังไม่จ่าย
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
@client.tree.command(name="setup", description="ตั้งค่าระบบรับสมัคร — วางลิงก์ฟอร์มและชีทได้เลย")
@app_commands.rename(
    individual_form="ฟอร์ม-บุคคล",
    team_form="ฟอร์ม-ทีม",
    individual_sheet="ชีท-บุคคล",
    team_sheet="ชีท-ทีม",
    channel="channel"
)
@app_commands.describe(
    individual_form="วางลิงก์ Google Form สมัครบุคคล",
    team_form="วางลิงก์ Google Form สมัครทีม",
    individual_sheet="วางลิงก์ Google Sheet บุคคล (เปิด Sheet แท็บที่ถูกต้องแล้ว copy URL)",
    team_sheet="วางลิงก์ Google Sheet ทีม (เปิด Sheet แท็บที่ถูกต้องแล้ว copy URL)",
    channel="ชื่อ channel ที่บอทจะรับข้อมูล เช่น สมัครแข่ง"
)
async def setup(interaction: discord.Interaction, individual_form: str = None, team_form: str = None, individual_sheet: str = None, team_sheet: str = None, channel: str = None):
    await interaction.response.defer(thinking=True)
    config = load_config()
    changes = []
 
    if individual_form:
        config["individual"]["form_url"] = individual_form
        opts = await fetch_form_options(individual_form)
        config["individual"]["categories"] = opts
        if opts:
            changes.append(f"✅ **ฟอร์มบุคคล** — พบ {len(opts)} ประเภทการแข่งขัน")
        else:
            changes.append("⚠️ **ฟอร์มบุคคล** — บันทึกลิงก์แล้ว แต่ดึงประเภทไม่ได้ (ตรวจสอบสิทธิ์ฟอร์ม)")
 
    if team_form:
        config["team"]["form_url"] = team_form
        opts = await fetch_form_options(team_form)
        config["team"]["categories"] = opts
        if opts:
            changes.append(f"✅ **ฟอร์มทีม** — พบ {len(opts)} ประเภทการแข่งขัน")
        else:
            changes.append("⚠️ **ฟอร์มทีม** — บันทึกลิงก์แล้ว แต่ดึงประเภทไม่ได้ (ตรวจสอบสิทธิ์ฟอร์ม)")
 
    if individual_sheet:
        sid, gid = extract_sheet_id_gid(individual_sheet)
        config["individual"]["sheet_id"] = sid
        config["individual"]["sheet_gid"] = gid
        changes.append(f"✅ **ชีทบุคคล** — เชื่อมต่อแล้ว")
 
    if team_sheet:
        sid, gid = extract_sheet_id_gid(team_sheet)
        config["team"]["sheet_id"] = sid
        config["team"]["sheet_gid"] = gid
        changes.append(f"✅ **ชีทลีม** — เชื่อมต่อแล้ว")
 
    if channel:
        channels = [c.strip() for c in channel.split(",")]
        config["listen_channels"] = channels
        ch_list = ", ".join(f"#{c}" for c in channels)
        changes.append(f"✅ **Channel** — บอทจะรับข้อมูลใน {ch_list}")
 
    if changes:
        save_config(config)
        msg = "## ⚙️ ตั้งค่าสำเร็จ\n" + "\n".join(changes)
        msg += "\n\n> ใช้ `/ดูการตั้งค่า` เพื่อตรวจสอบการตั้งค่าทั้งหมด"
        await interaction.followup.send(msg)
    else:
        msg = (
            "## ❓ วิธีใช้ `/setup`\n"
            "ใส่อย่างน้อย 1 อย่างต่อไปนี้\n\n"
            "**`individual_form`** — ลิงก์ฟอร์มสมัครบุคคล\n"
            "**`team_form`** — ลิงก์ฟอร์มสมัครทีม\n"
            "**`individual_sheet`** — ลิงก์ชีทบุคคล\n"
            "**`team_sheet`** — ลิงก์ชีทลีม\n"
            "**`channel`** — ชื่อ channel เช่น `สมัครแข่ง`"
        )
        await interaction.followup.send(msg)
 
@client.tree.command(name="ดูการตั้งค่า", description="ดูการตั้งค่าระบบรับสมัครทั้งหมด")
async def show_config(interaction: discord.Interaction):
    config = load_config()
    ind = config["individual"]
    team = config["team"]
    channels = config.get("listen_channels", [])
 
    def status(val): return "✅" if val else "❌"
    def ch_fmt(val): return ", ".join(f"#{c}" for c in val) if val else "ยังไม่ได้ตั้ง"
 
    lines = ["## ⚙️ การตั้งค่าระบบรับสมัคร\n"]
 
    # Channel
    lines.append(f"📢 **รับข้อมูลใน:** {ch_fmt(channels)}")
 
    # บุคคล
    lines.append(f"\n👤 **ฟอร์มบุคคล**")
    lines.append(f"{status(ind['form_url'])} ฟอร์ม: {'เชื่อมแล้ว' if ind['form_url'] else 'ยังไม่ได้ตั้ง'}")
    lines.append(f"{status(ind['sheet_id'])} ชีท: {'เชื่อมแล้ว' if ind['sheet_id'] else 'ยังไม่ได้ตั้ง'}")
    if ind["categories"]:
        lines.append(f"📋 ประเภทที่รู้จัก {len(ind['categories'])} รายการ:")
        lines.append("\n".join(f"  • {c}" for c in ind["categories"]))
    else:
        lines.append("📋 ยังไม่มีประเภทการแข่งขัน (ใช้ `/setup individual_form` เพื่อดึงข้อมูล)")
 
    # ทีม
    lines.append(f"\n🏓 **ฟอร์มทีม**")
    lines.append(f"{status(team['form_url'])} ฟอร์ม: {'เชื่อมแล้ว' if team['form_url'] else 'ยังไม่ได้ตั้ง'}")
    lines.append(f"{status(team['sheet_id'])} ชีท: {'เชื่อมแล้ว' if team['sheet_id'] else 'ยังไม่ได้ตั้ง'}")
    if team["categories"]:
        lines.append(f"📋 ประเภทที่รู้จัก {len(team['categories'])} รายการ:")
        lines.append("\n".join(f"  • {c}" for c in team["categories"]))
    else:
        lines.append("📋 ยังไม่มีประเภทการแข่งขัน")
 
    # คำแนะนำถ้ายังไม่ครบ
    missing = []
    if not ind["form_url"]: missing.append("`individual_form`")
    if not ind["sheet_id"]: missing.append("`individual_sheet`")
    if not team["form_url"]: missing.append("`team_form`")
    if not team["sheet_id"]: missing.append("`team_sheet`")
    if not channels: missing.append("`channel`")
 
    if missing:
        lines.append(f"\n> ⚠️ ยังขาด: {', '.join(missing)} — ใช้ `/setup` เพื่อตั้งค่า")
    else:
        lines.append("\n> 🟢 ตั้งค่าครบแล้ว พร้อมรับสมัครได้เลย!")
 
    await interaction.response.send_message("\n".join(lines))
 
@client.tree.command(name="อัปเดตฟอร์ม", description="ดึงประเภทการแข่งขันจากฟอร์มใหม่ — ใช้เมื่อเพิ่มประเภทใหม่ในฟอร์ม")
async def reload_form(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    config = load_config()
    results = []
    if config["individual"]["form_url"]:
        opts = await fetch_form_options(config["individual"]["form_url"])
        config["individual"]["categories"] = opts
        results.append(f"✅ ฟอร์มบุคคล — พบ **{len(opts)}** ประเภทการแข่งขัน")
    else:
        results.append("❌ ยังไม่ได้ตั้งค่าฟอร์มบุคคล — ใช้ `/setup individual_form` ก่อน")
    if config["team"]["form_url"]:
        opts = await fetch_form_options(config["team"]["form_url"])
        config["team"]["categories"] = opts
        results.append(f"✅ ฟอร์มทีม — พบ **{len(opts)}** ประเภทการแข่งขัน")
    else:
        results.append("❌ ยังไม่ได้ตั้งค่าฟอร์มทีม — ใช้ `/setup team_form` ก่อน")
    save_config(config)
    await interaction.followup.send("## 🔄 อัปเดตฟอร์มแล้ว\n" + "\n".join(results))
 
@client.tree.command(name="รีเซ็ต", description="ล้างการตั้งค่าทั้งหมดและเริ่มใหม่")
async def reset_config(interaction: discord.Interaction):
    save_config(dict(DEFAULT_CONFIG))
    await interaction.response.send_message(
        "## ♻️ ล้างการตั้งค่าแล้ว\n"
        "การตั้งค่าทั้งหมดถูกลบเรียบร้อย\n\n"
        "> ใช้ `/setup` เพื่อตั้งค่าใหม่ได้เลย"
    )
 
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
        return
 
    individual_count, team_count = await save_entries(result, config)
 
    # สร้าง reply จาก summary
    summary = result.get("summary", {})
    lines = ["## ✅ บันทึกข้อมูลแล้ว"]
 
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
 
# Web server เล็กๆ ให้ UptimeRobot ping เพื่อไม่ให้ Render sleep
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass  # ปิด log
 
port = int(os.getenv("PORT", 10000))
server = HTTPServer(("0.0.0.0", port), HealthHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"🌐 Web server running on port {port}")
 
client.run(DISCORD_TOKEN)
 
