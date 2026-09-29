# -*- coding: utf-8 -*-
"""ترحيل v16: الصيانة (وقائية وطارئة) — آمن وقابل للتكرار (إضافات فقط + إعادة بناء جدول المستخدمين لقبول دور «فني صيانة»).

المصدر: نموذج «جدول الصيانة الوقائية» (رقم النموذج XXXQP-12.F02): الآلات السبع وأرقامها والمدة 30 يومًا.
بنود الفحص الافتراضية **مقترحة** (لم يصل نموذج فحص رسمي) ويعدّلها مدير النظام من «الصيانة ← المعدات والبنود».
"""
import os, sqlite3, sys

import db

DDL = """
CREATE TABLE IF NOT EXISTS maint_equipment (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sr         INTEGER,                         -- الرقم التسلسلي في نموذج الجدول
  no         TEXT NOT NULL UNIQUE,            -- رقم الآلة (Equipment Number)
  name_en    TEXT, name_ar TEXT NOT NULL,
  location   TEXT,
  machine_code TEXT,                          -- ربط اختياري بماكينة الإنتاج (لتحذير الإنتاج عند العطل)
  freq_days  INTEGER NOT NULL DEFAULT 30,
  next_due   TEXT, last_done TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  notes      TEXT
);
CREATE TABLE IF NOT EXISTS maint_checklist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  equip_id INTEGER NOT NULL REFERENCES maint_equipment(id),
  seq INTEGER NOT NULL, item_ar TEXT NOT NULL, item_en TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  proposed INTEGER NOT NULL DEFAULT 1         -- 1 = بند مقترح لم يُعتمد من نموذج رسمي
);
CREATE TABLE IF NOT EXISTS maint_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL UNIQUE,              -- PM-2026-000001 / EM-2026-000001
  kind TEXT NOT NULL CHECK(kind IN ('PM','EM')),
  equip_id INTEGER NOT NULL REFERENCES maint_equipment(id),
  status TEXT NOT NULL,                       -- Planned / Reported / In Progress / Completed / Verified / Cancelled
  planned_date TEXT,
  reported_by TEXT, reported_at TEXT,
  problem TEXT, severity TEXT, production_stopped INTEGER DEFAULT 0,
  assigned_to TEXT, start_at TEXT, end_at TEXT, downtime_h REAL,
  cause TEXT, action_taken TEXT, parts_used TEXT,
  result TEXT, notes TEXT,
  done_by TEXT, verified_by TEXT, verified_at TEXT,
  parent_order TEXT,
  created_by TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_mo_equip ON maint_orders(equip_id, kind, status);
CREATE INDEX IF NOT EXISTS ix_mo_date ON maint_orders(planned_date);
CREATE TABLE IF NOT EXISTS maint_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES maint_orders(id),
  seq INTEGER, item_ar TEXT NOT NULL, result TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_mr_order ON maint_results(order_id);
"""

# من نموذج جدول الصيانة الوقائية: (الرقم التسلسلي، رقم الآلة، English، العربية، ماكينة الإنتاج المرتبطة)
EQUIPMENT = [
    (1, '1', 'Machine (Sliter)', 'الآلة السيلتر', 'SL-01'),
    (2, '5', 'Machine 10', 'الآلة 10', 'FD-10'),
    (3, '4', 'Machine 7.5', 'الآلة 7.5', 'FD-75'),
    (4, '3', 'Machine 5', 'الآلة 5', 'FD-05'),
    (5, '6', 'Packing M/C', 'آلة التغليف', 'PK-01'),
    (6, '2', 'PPT M/C', 'آلة الرباط', None),
    (7, '7', 'Sterilization', 'التعقيم', 'EO-01'),
]

COMMON = [
    ('تنظيف الآلة من الغبار والألياف والمخلفات', 'Clean machine from dust, fibres and residues'),
    ('فحص التزييت والتشحيم للأجزاء المتحركة', 'Check lubrication of moving parts'),
    ('فحص الأحزمة والسلاسل والبكرات', 'Check belts, chains and rollers'),
    ('فحص أزرار الإيقاف الطارئ والأغطية الواقية', 'Check emergency stops and safety guards'),
    ('فحص التوصيلات والأسلاك الكهربائية', 'Check electrical connections and wiring'),
    ('فحص الحساسات ومفاتيح الحد', 'Check sensors and limit switches'),
    ('فحص التسريب (هواء / زيت)', 'Check for air / oil leaks'),
    ('تجربة تشغيل بعد الصيانة والتأكد من عدم وجود أصوات أو اهتزاز غير طبيعي', 'Trial run after maintenance; no abnormal noise/vibration'),
]
EXTRA = {
    'SL-01': [('فحص حالة سكاكين القص وشحذها', 'Check / sharpen slitting knives'), ('فحص التوتر ومحاذاة الرول', 'Check tension and roll alignment')],
    'FD': [('فحص سكين القطع وحالته', 'Check cutting knife'), ('فحص وحدة الطي ومعايرة طول القطع', 'Check folding unit; calibrate cut length'), ('فحص العداد', 'Check counter')],
    'PK-01': [('فحص أذرع اللحام الحراري ودرجة الحرارة', 'Check heat-sealing jaws and temperature'), ('فحص رول فيلم التغليف والشد', 'Check packing film reel / tension'), ('فحص الطابعة (LOT والتاريخ)', 'Check printer (LOT / date)')],
    None: [('فحص أسطوانات الضغط وحاملات الرول', 'Check pressure rollers and roll holders')],
    'EO-01': [('فحص الأختام والأبواب', 'Check door seals'), ('فحص الضغط والتفريغ', 'Check pressure and vacuum'), ('فحص خطوط الغاز والصمامات', 'Check gas lines and valves'),
              ('معايرة حساسات الحرارة والرطوبة', 'Calibrate temperature / humidity sensors'), ('فحص أجهزة السلامة والتهوية', 'Check safety and ventilation systems')],
}


def _users_need_rebuild(con):
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    return bool(row) and "'maint'" not in (row[0] or '')


def rebuild_users(con):
    """يقبل دور «maint» (فني صيانة) في قيد الأدوار. يحافظ على كل الأعمدة والصفوف."""
    cols = [r[1] for r in con.execute('PRAGMA table_info(users)')]
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
    new_sql = sql.replace("'admin'))", "'admin','maint'))", 1) if "'admin'))" in sql else sql.replace("'admin')", "'admin','maint')", 1)
    if new_sql == sql:
        return
    con.execute('ALTER TABLE users RENAME TO users_old')
    con.execute(new_sql)
    lst = ','.join(cols)
    con.execute(f'INSERT INTO users({lst}) SELECT {lst} FROM users_old')
    con.execute('DROP TABLE users_old')


def seed(con):
    if con.execute('SELECT COUNT(*) FROM maint_equipment').fetchone()[0] == 0:
        for sr, no, en, ar, mc in EQUIPMENT:
            if mc and not con.execute('SELECT 1 FROM machines WHERE machine_code=?', (mc,)).fetchone():
                mc = None
            eid = con.execute('INSERT INTO maint_equipment(sr,no,name_en,name_ar,machine_code,freq_days) VALUES(?,?,?,?,?,30)',
                              (sr, no, en, ar, mc)).lastrowid
            key = 'FD' if (mc or '').startswith('FD') else mc
            items = COMMON + EXTRA.get(key, [])
            for i, (ar_, en_) in enumerate(items, 1):
                con.execute('INSERT INTO maint_checklist(equip_id,seq,item_ar,item_en,proposed) VALUES(?,?,?,?,1)', (eid, i, ar_, en_))
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('maint_form_pm','XXXQP-12.F02','رقم نموذج جدول الصيانة الوقائية')")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('maint_form_em','','رقم نموذج بلاغ/أمر الصيانة الطارئة (لم يصل بعد)')")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('maint_form_wo','','رقم نموذج أمر الصيانة الوقائية/سجل الفحص (لم يصل بعد)')")


def run(verbose=False):
    if not os.path.exists(db.DB_PATH):
        return
    con = sqlite3.connect(db.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=OFF')
        con.executescript(DDL)
        if _users_need_rebuild(con):
            rebuild_users(con)
        seed(con)
        con.commit()
    finally:
        con.close()
    if verbose:
        print('  اكتمل ترحيل v16 (الصيانة).')


if __name__ == '__main__':
    run(True)
