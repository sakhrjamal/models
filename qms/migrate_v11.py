# -*- coding: utf-8 -*-
"""ترحيل قاعدة بيانات v10 إلى v11 — آمن على البيانات، يضيف فقط.

  • جداول: users, signatures, counters
  • إعدادات النسخ الاحتياطي
  • مستخدم admin افتراضي إن لم يوجد مستخدمون
  • مِلء العدّادات من أعلى رقم سند موجود (حتى لا يتكرر الترقيم)

يُستدعى تلقائيًا من run.py قبل تشغيل النظام. يمكن تشغيله يدويًا أيضًا.
"""
import os, re, sqlite3, sys, datetime, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('QMS_DB') or os.path.join(HERE, 'data', 'qms.db')

DDL = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('viewer','operator','qa','admin')),
        active INTEGER NOT NULL DEFAULT 1, must_change_pw INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')), last_login TEXT)""",
    """CREATE TABLE IF NOT EXISTS signatures (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')),
        username TEXT, full_name TEXT, role TEXT, meaning TEXT,
        table_name TEXT, record_key TEXT, signed_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_sig_rec ON signatures(table_name, record_key)",
    "CREATE TABLE IF NOT EXISTS counters (scope TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS ix_audit_rec ON audit_log(table_name, record_key)",
    "CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts)",
]

SETTINGS = [
    ('backup_interval_h', '6',  'الفاصل بين النسخ الاحتياطية التلقائية بالساعة'),
    ('backup_keep',       '60', 'عدد النسخ الاحتياطية المحفوظة (تُحذف الأقدم)'),
    ('session_hours',     '12', 'مدة بقاء جلسة تسجيل الدخول بالساعات'),
]

# (جدول، عمود، بادئة الرقم) — النطاق = «البادئة-YYMMDD»
DOC_SOURCES = [
    ('receipts',          'grn_no',          'GRN'),
    ('inspections',       'inspection_no',   'QC-RM'),
    ('sorting',           'doc_no',          'SORT'),
    ('packaging',         'doc_no',          'PKG'),
    ('post_ster_receipts','doc_no',          'PSR'),
    ('post_ster_receipts','quarantine_card', 'QRT'),
    ('releases',          'release_no',      'REL'),
]
_TRAIL = re.compile(r'^(?P<stem>.+)-(?P<seq>\d+)$')


def _table_exists(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def backfill_counters(con):
    for table, col, prefix in DOC_SOURCES:
        if not _table_exists(con, table):
            continue
        stems = {}
        for (val,) in con.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL"):
            m = _TRAIL.match(str(val).strip())
            if not m:
                continue
            stem, seq = m.group('stem'), int(m.group('seq'))
            if not stem.startswith(prefix):
                continue
            stems[stem] = max(stems.get(stem, 0), seq)
        for stem, mx in stems.items():
            con.execute("INSERT OR IGNORE INTO counters(scope,n) VALUES(?,0)", (stem,))
            con.execute("UPDATE counters SET n=? WHERE scope=? AND n<?", (mx, stem, mx))

    # دورات التعقيم: MON-YYDD-EO-NNN
    if _table_exists(con, 'cycles'):
        stems = {}
        for (val,) in con.execute("SELECT cycle_no FROM cycles WHERE cycle_no IS NOT NULL"):
            m = _TRAIL.match(str(val).strip())
            if m and '-EO-' in str(val):
                stems[m.group('stem')] = max(stems.get(m.group('stem'), 0), int(m.group('seq')))
        for stem, mx in stems.items():
            con.execute("INSERT OR IGNORE INTO counters(scope,n) VALUES(?,0)", (stem,))
            con.execute("UPDATE counters SET n=? WHERE scope=? AND n<?", (mx, stem, mx))


def run(verbose=False):
    if not os.path.exists(DB):
        return
    con = sqlite3.connect(DB)
    try:
        for stmt in DDL:
            con.execute(stmt)
        for k, v, note in SETTINGS:
            con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES(?,?,?)", (k, v, note))
        backfill_counters(con)
        con.commit()

        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            try:
                from werkzeug.security import generate_password_hash
                h = generate_password_hash('admin', method='pbkdf2:sha256')
            except Exception:
                h = None
            if h:
                con.execute("""INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw)
                               VALUES('admin','مدير النظام',?,'admin',1,1)""", (h,))
                con.commit()
                if verbose:
                    print('  + أُنشئ مستخدم admin (كلمة المرور: admin — يجب تغييرها عند أول دخول)')
        if verbose:
            print('  اكتمل ترحيل v11.')
    finally:
        con.close()


def main():
    if not os.path.exists(DB):
        print('لا توجد قاعدة بيانات — شغّل seed.py أولًا'); sys.exit(1)
    bak = DB + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    shutil.copy(DB, bak)
    print('نسخة احتياطية:', os.path.basename(bak))
    run(verbose=True)


if __name__ == '__main__':
    main()
