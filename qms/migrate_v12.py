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
                   CHECK(role IN ('viewer','operator','qc','qa','manager','admin')),
    active         INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    last_login     TEXT
)"""

DDL = [
    """CREATE TABLE IF NOT EXISTS qc_templates (
        code TEXT PRIMARY KEY, title TEXT NOT NULL,
        dept TEXT NOT NULL CHECK(dept IN ('QC','QA')),
        kind TEXT NOT NULL CHECK(kind IN ('release','daily','periodic')),
        area TEXT NOT NULL, freq_hours REAL, needs_batch INTEGER NOT NULL DEFAULT 0,
        fields_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1, note TEXT, updated_by TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime')))""",
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
    return bool(row) and "'manager'" not in (row[0] or '')


def rebuild_users(con):
    con.execute('ALTER TABLE users RENAME TO users_old')
    con.execute(USERS_DDL)
    con.execute("""INSERT INTO users(id,username,full_name,pw_hash,role,active,must_change_pw,created_at,last_login)
                   SELECT id,username,full_name,pw_hash,role,active,must_change_pw,created_at,last_login
                   FROM users_old""")
    con.execute('DROP TABLE users_old')


def seed_templates(con):
    import qc_seed
    n = 0
    for t in qc_seed.templates():
        cur = con.execute("""INSERT OR IGNORE INTO qc_templates
                (code,title,dept,kind,area,freq_hours,needs_batch,fields_json,note,updated_by)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (t['code'], t['title'], t['dept'], t['kind'], t['area'], t['freq_hours'],
                 t['needs_batch'], json.dumps(t['fields'], ensure_ascii=False), t['note'], 'system'))
        n += cur.rowcount
    return n


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
