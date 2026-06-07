# 🏓 PingPong Bot — คู่มือติดตั้ง

## โครงสร้างไฟล์
```
pingpong_bot/
├── bot.py                 ← โค้ดหลัก
├── .env                   ← ใส่ Keys 
├── service_account.json   ← ไฟล์ JSON จาก Google Cloud
└── requirements.txt       ← library ที่ต้องติดตั้ง
```

---

## ขั้นตอนติดตั้ง

### 1. ติดตั้ง library
เปิด Terminal / Command Prompt แล้วรัน:
```
pip install -r requirements.txt
```

### 2. สร้างไฟล์ .env
- เปิดด้วย Notepad แล้วใส่ค่าต่างๆ

### 3. วางไฟล์ service_account.json
- ไฟล์ JSON ที่ดาวน์โหลดจาก Google Cloud
- วางไว้ในโฟลเดอร์เดียวกับ bot.py
- เปลี่ยนชื่อเป็น service_account.json

### 4. หา Sheet ID
จาก URL ของ Google Sheet:
```
https://docs.google.com/spreadsheets/d/[SHEET_ID]/edit
```
เอาส่วนที่อยู่หลัง /d/ และก่อน /edit

### 5. ตั้งชื่อ Channel
เปิด bot.py หาบรรทัด:
```python
LISTEN_CHANNELS = ["สมัครแข่ง", "registration", "สมัคร"]
```
เปลี่ยนเป็นชื่อ channel จริงในเซิร์ฟเวอร์

### 6. เพิ่ม Bot เข้า Server
ใช้ลิงก์ OAuth2 ที่ได้จาก Discord Developer Portal

### 7. รัน Bot
```
python bot.py
```
ถ้าเห็น "✅ Bot พร้อมใช้งาน" แสดงว่าสำเร็จ

---

## วิธีใช้งาน
ส่งข้อความใน channel ที่กำหนด Bot จะ:
1. อ่านและวิเคราะห์ข้อมูล
2. บันทึกลง Google Sheet
3. ตอบกลับสรุปข้อมูลที่บันทึก

รองรับทั้งข้อความธรรมดา และรูปภาพ/สลิปที่มีข้อมูลการสมัคร

---

## หากบอทไม่ตอบ
- ตรวจสอบว่าเปิด Message Content Intent แล้ว
- ตรวจสอบชื่อ channel ตรงกับ LISTEN_CHANNELS ใน bot.py
- ดู error ใน Terminal
