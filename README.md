# TIRD Newsletter Collector

ดาวน์โหลด Newsletter จาก TIRD ด้วย `curl.exe` และดูแลรายการผ่าน Terminal UI (Textual + Rich) สำหรับ **บัญชีที่ได้รับสิทธิ์เข้าถึงเท่านั้น**

**HTTP Engine:** `curl.exe` ของ Windows — คงการส่ง Credentials ผ่าน `stdin` ตาม Collector v4.0 ซึ่งผ่านการทดสอบ `GetService` และ `GetServiceById` แล้ว ไม่ได้เปลี่ยนไปใช้ `requests` แทน

## ติดตั้งครั้งแรก (Windows)

1. ติดตั้ง Python 3.10+ และตรวจว่า Windows มี `curl.exe` (`where curl.exe`)
2. เปิด PowerShell ในโฟลเดอร์โปรเจกต์ แล้วติดตั้ง Dependencies:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. คัดลอก `.env.example` เป็น `.env`

   ```powershell
   Copy-Item .env.example .env
   ```

4. ขณะเข้าสู่ระบบ TIRD ด้วยบัญชีของคุณ เปิด DevTools → Network → `/Content/GetService` → Headers แล้วใส่ **Authorization**, **Cookie** และ **Referer** จาก Request ที่ได้รับอนุญาตลงใน `.env` ห้ามเติม `Bearer` เองหาก Browser ไม่ได้ส่งคำนั้น

5. ดับเบิลคลิก **`START_TIRD.bat`** หรือใช้:

   ```powershell
   python tird_collector.py
   ```

## ใช้งาน Terminal UI

- `REFRESH`: ตรวจ Session และโหลด Catalog ใหม่ (แสดงความผิดพลาดบนหน้าจอโดยไม่ปิดแอป)
- ช่อง **Search** และ **Year**: กรองชื่อเอกสารและปี (`2026` หรือ `2569`)
- กด **Enter** บนแถวเอกสารเพื่อเลือกหรือยกเลิกเลือก
- `DOWNLOAD SELECTED`: ดาวน์โหลดเฉพาะแถวที่เลือกภายในผลการกรอง
- `DOWNLOAD ALL`: ดาวน์โหลดทุกแถวที่แสดงอยู่ (เคารพตัวกรอง)
- `STOP`: หยุดหลังคำขอปัจจุบันเสร็จ ไม่ยกเลิกคำขอที่กำลังส่ง
- `OPEN FOLDER`: เปิดโฟลเดอร์ผลลัพธ์

โปรแกรมข้าม PDF ที่มีอยู่และบันทึกสำเร็จแล้วโดยอัตโนมัติ ยกเว้นเริ่มด้วย `--force` ข้อมูลจัดเก็บตาม `TIRD_Newsletter/<ปี ค.ศ.>/<เดือน>/` พร้อม `manifest.csv` และ SHA-256

## โหมดคำสั่งเดิม (ยังใช้ได้)

```powershell
python tird_collector.py --test-only    # ตรวจ GetService และ GetServiceById เท่านั้น
python tird_collector.py --list         # แสดงรายการ
python tird_collector.py --headless     # ดาวน์โหลดทั้งหมดโดยไม่เปิด UI
python tird_collector.py --limit 1      # ดาวน์โหลดเฉพาะ 1 รายการ
python tird_collector.py --year 2026   # กรองปี
python tird_collector.py --ui          # บังคับเปิด UI
```

> **หมายเหตุ:** การทดสอบ `--test-only` ที่ผ่านแล้ว **ยังไม่ได้ยืนยันการดาวน์โหลด PDF**; ทดสอบ `--limit 1` ก่อนดาวน์โหลดทั้งหมดจริง

## ความปลอดภัย

- `TIRD_COOKIE` และ `TIRD_AUTHORIZATION` เป็นความลับ เก็บเฉพาะใน `.env` ซึ่ง Git ไม่ติดตาม
- ถ้า Session หมดอายุ ให้ Login ใหม่ และอัปเดต `.env` แล้วกด `REFRESH`
- ไม่มีระบบข้าม Login, ตรวจสิทธิ์ หรือมาตรการป้องกันการเข้าถึงของเว็บไซต์
- อย่าแนบ Logs, Screenshot หรือ Configuration ที่มี Credentials จริงลง Repository
