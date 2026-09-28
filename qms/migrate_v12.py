# -*- coding: utf-8 -*-
"""ترحيل قاعدة بيانات v11 إلى v12 — آمن على البيانات.

  • جدول المستخدمين يُعاد بناؤه (نسخ كل الصفوف) ليقبل دورَي «مدير المصنع» و«مراقب الجودة»
  • جداول قوالب وسجلات الجودة
  • القوالب المبدئية الـ 12 (لا تُستبدل قوالب عُدّلت)
  • إعدادات جديدة

يُستدعى تلقائيًا من run.py. لا يحذف ولا يعدّل أي صف إنتاج قائم.
"""
import json, os, sqlite3, sys

import db

USERS_DDL = """CREATE TABLE users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    full_name      TEXT NOT NULL,
    pw_hash        TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer'
                   CHECK(role IN ('viewer','operator','store','qc','qa','manager','admin')),
    active         INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    last_login     TEXT,
    lines          TEXT
)"""

TPL_DDL = """CREATE TABLE qc_templates (
  code        TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  dept        TEXT NOT NULL CHECK(dept IN ('QC','QA')),
  kind        TEXT NOT NULL CHECK(kind IN ('release','inspection','daily','periodic')),
  area        TEXT NOT NULL,
  route       TEXT NOT NULL DEFAULT 'all' CHECK(route IN ('all','sterile','non_sterile')),
  freq_hours  REAL,
  needs_batch INTEGER NOT NULL DEFAULT 0,
  fields_json TEXT NOT NULL,
  requires    TEXT,
  mfg_routes  TEXT,
  is_final    INTEGER NOT NULL DEFAULT 0,
  active      INTEGER NOT NULL DEFAULT 1,
  version     INTEGER NOT NULL DEFAULT 1,
  note        TEXT,
  updated_by  TEXT,
  updated_at  TEXT DEFAULT (datetime('now','localtime'))
)"""

DDL = [
    """CREATE TABLE IF NOT EXISTS qc_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rec_no TEXT NOT NULL UNIQUE,
        template_code TEXT NOT NULL REFERENCES qc_templates(code),
        template_version INTEGER, spec_json TEXT,
        rec_date TEXT NOT NULL, rec_time TEXT, shift TEXT,
        batch_no TEXT, line_code TEXT, item_code TEXT,
        values_json TEXT NOT NULL, result TEXT, fail_count INTEGER DEFAULT 0,
        decision TEXT, inspector TEXT, notes TEXT, created_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')))""",
    "CREATE INDEX IF NOT EXISTS ix_qcr_tpl ON qc_records(template_code, rec_date)",
    "CREATE INDEX IF NOT EXISTS ix_qcr_batch ON qc_records(batch_no)",
    "CREATE INDEX IF NOT EXISTS ix_qcr_line ON qc_records(line_code, rec_date)",
]

SETTINGS = [
    # off = لا تنبيه | warn = تنبيه فقط | block = منع الطي/الأسليتر دون إفراج خط
    ('qc_gate_mode', 'warn',
     'ربط الإنتاج بإفراج الخط: off (بلا) / warn (تنبيه) / block (منع التسجيل قبل إفراج الجودة)'),
]


def _users_need_rebuild(con):
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    return bool(row) and "'store'" not in (row[0] or '')


def rebuild_users(con):
    con.execute('ALTER TABLE users RENAME TO users_old')
    con.execute(USERS_DDL)
    con.execute("""INSERT INTO users(id,username,full_name,pw_hash,role,active,must_change_pw,created_at,last_login)
                   SELECT id,username,full_name,pw_hash,role,active,must_change_pw,created_at,last_login
                   FROM users_old""")
    con.execute('DROP TABLE users_old')


def ensure_templates_table(con):
    """ينشئ جدول القوالب، أو يعيد بناءه (بنسخ الصفوف) إن كان بصيغة أقدم: بلا route/requires
    أو بقيد kind الأضيق."""
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='qc_templates'").fetchone()
    if not row:
        con.execute(TPL_DDL)
        return
    if "'inspection'" in row[0] and 'requires' in row[0]:
        have = {r[1] for r in con.execute('PRAGMA table_info(qc_templates)')}
        if 'mfg_routes' not in have:                       # v13: مسارات التصنيع + علم الإفراج النهائي
            con.execute('ALTER TABLE qc_templates ADD COLUMN mfg_routes TEXT')
        if 'is_final' not in have:
            con.execute('ALTER TABLE qc_templates ADD COLUMN is_final INTEGER NOT NULL DEFAULT 0')
        return
    con.execute('ALTER TABLE qc_templates RENAME TO qc_templates_old')
    con.execute(TPL_DDL)
    con.execute("""INSERT INTO qc_templates(code,title,dept,kind,area,freq_hours,needs_batch,fields_json,
                        active,version,note,updated_by,updated_at)
                   SELECT code,title,dept,kind,area,freq_hours,needs_batch,fields_json,
                          active,version,note,updated_by,updated_at FROM qc_templates_old""")
    con.execute('DROP TABLE qc_templates_old')


def seed_templates(con):
    """يضيف القوالب الناقصة، ويحدّث فقط ما لم يعدّله أحد (updated_by='system' والإصدار 1)،
    فلا يُكتب فوق تعديلات الجودة."""
    import qc_seed
    n = 0
    for t in qc_seed.templates():
        row = (t['code'], t['title'], t['dept'], t['kind'], t['area'], t['route'], t['freq_hours'],
               t['needs_batch'], json.dumps(t['fields'], ensure_ascii=False),
               json.dumps(t['requires']), t['note'], json.dumps(t['mfg_routes']), t['is_final'])
        cur = con.execute("""INSERT OR IGNORE INTO qc_templates
                (code,title,dept,kind,area,route,freq_hours,needs_batch,fields_json,requires,note,mfg_routes,
                 is_final,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'system')""", row)
        if cur.rowcount:
            n += 1
        else:
            cur = con.execute("""UPDATE qc_templates SET title=?,dept=?,kind=?,area=?,route=?,freq_hours=?,
                    needs_batch=?,fields_json=?,requires=?,note=?,mfg_routes=?,is_final=?
                    WHERE code=? AND updated_by='system' AND version=1""", row[1:] + (row[0],))
            n += cur.rowcount
            # قالب عدّلته الجودة: نضبط فقط حقلَي المسار والإفراج النهائي إن لم يُضبطا بعد
            con.execute("UPDATE qc_templates SET mfg_routes=?, is_final=? WHERE code=? AND mfg_routes IS NULL",
                        (row[11], row[12], row[0]))
    return n


NCR_COLS = [('source_rec', 'TEXT'), ('severity', 'TEXT'), ('disposition', 'TEXT'),
            ('qty_affected', 'REAL'), ('created_by', 'TEXT'), ('closed_by', 'TEXT')]


EXTRA_COLS = {
    'shipments': [('unit', 'TEXT'), ('shipper', 'TEXT'), ('created_by', 'TEXT'), ('voided', 'INTEGER DEFAULT 0'),
                  ('void_reason', 'TEXT'), ('void_by', 'TEXT')],
    'bom': [('lot', 'TEXT'), ('created_by', 'TEXT'), ('voided', 'INTEGER DEFAULT 0'), ('void_reason', 'TEXT')],
}


def ensure_extra(con):
    """أعمدة الشحن وصرف المواد على الجداول القائمة."""
    for table, cols in EXTRA_COLS.items():
        have = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        if not have:
            continue
        for col, typ in cols:
            if col not in have:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')


def ensure_ncr(con):
    """يضيف أعمدة عدم المطابقة الجديدة إلى جدول deviations القائم."""
    have = {r[1] for r in con.execute('PRAGMA table_info(deviations)')}
    if not have:
        return
    for col, typ in NCR_COLS:
        if col not in have:
            con.execute(f'ALTER TABLE deviations ADD COLUMN {col} {typ}')
    con.execute('CREATE INDEX IF NOT EXISTS ix_dev_batch ON deviations(batch_no)')


def run(verbose=False):
    if not os.path.exists(db.DB_PATH):
        return
    con = sqlite3.connect(db.DB_PATH, timeout=30)
    try:
        con.execute('PRAGMA foreign_keys=OFF')
        con.execute('BEGIN IMMEDIATE')
        if _users_need_rebuild(con):
            rebuild_users(con)
            if verbose:
                print('  + أُعيد بناء جدول المستخدمين (أدوار جديدة).')
        ensure_templates_table(con)
        ensure_ncr(con)
        ensure_extra(con)
        for stmt in DDL:
            con.execute(stmt)
        for k, v, note in SETTINGS:
            con.execute('INSERT OR IGNORE INTO settings(key,value,note) VALUES(?,?,?)', (k, v, note))
        added = seed_templates(con)
        con.commit()
        if verbose:
            print(f'  اكتمل ترحيل v12 (قوالب جودة جديدة: {added}).')
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == '__main__':
    run(verbose=True)
