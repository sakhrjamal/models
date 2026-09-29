# -*- coding: utf-8 -*-
"""طبقة الوصول لقاعدة البيانات — SQLite.

v11:
  • ‎tx()‎ / ‎run_many()‎ لتنفيذ عدة تعديلات في تعامل ذرّي واحد.
  • ‎alloc()‎ / ‎peek_number()‎ لتوليد أرقام سندات متسلسلة بلا تسابق
    عبر جدول ‎counters‎ و ‎BEGIN IMMEDIATE‎.
  • ‎log()‎ ينسب الحدث للمستخدم الحالي (من ‎flask.g‎) لا إلى «system».
"""
import sqlite3, os, sys, re, contextlib, logging, threading

log_ = logging.getLogger('qms')


def base_dir():
    """يعمل في وضع التطوير وفي وضع exe المحزّم"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# QMS_DB: مسار بديل لقاعدة البيانات (للاختبارات والتجارب) — الافتراضي data/qms.db بجانب البرنامج
DB_PATH = os.environ.get('QMS_DB') or os.path.join(base_dir(), 'data', 'qms.db')


_WAL_DONE = set()
_tl = threading.local()


class _Shared:
    """اتصال مشترك داخل reuse(): close() لا يغلقه، والباقي يُفوَّض للاتصال الحقيقي."""
    def __init__(self, con):
        self._c = con

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


def _open():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)      # timeout = busy_timeout
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    if DB_PATH not in _WAL_DONE:                     # وضع WAL محفوظ في الملف نفسه: يكفي ضبطه مرة لكل تشغيل
        con.execute('PRAGMA journal_mode=WAL')       # قراءة متزامنة أثناء الكتابة
        _WAL_DONE.add(DB_PATH)
    return con


def get():
    shared = getattr(_tl, 'con', None)
    return shared if shared is not None else _open()


@contextlib.contextmanager
def reuse():
    """داخل هذا السياق تشترك q/one في اتصال واحد — لقوائم تحسب حالة عشرات الأوامر (قراءة فقط)."""
    if getattr(_tl, 'con', None) is not None:
        yield
        return
    con = _open()
    _tl.con = _Shared(con)
    try:
        yield
    finally:
        _tl.con = None
        con.close()


def q(sql, args=()):
    con = get()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    con = get()
    try:
        cur = con.execute(sql, args); con.commit(); return cur.lastrowid
    finally:
        con.close()


# --------------------------------------------------------------- تعاملات ذرّية
@contextlib.contextmanager
def tx():
    """سياق تعامل واحد. كل ما بداخله يُثبَّت معًا أو يُلغى معًا.

        with db.tx() as con:
            con.execute(...); con.execute(...)
            no = db.alloc(con, scope, fmt)
    """
    con = get()
    try:
        con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def run_many(statements):
    """statements: قائمة (sql, args) — تُنفَّذ كلها في تعامل واحد."""
    last = None
    with tx() as con:
        for sql, args in statements:
            last = con.execute(sql, args).lastrowid
    return last


# --------------------------------------------------------------- أرقام السندات
def alloc(con, scope, fmt):
    """يحجز الرقم التالي للنطاق ‎scope‎ داخل تعامل قائم ‎con‎ (BEGIN IMMEDIATE).

    ‎fmt‎ نص فيه ‎{n}‎ أو ‎{n3}‎ (ثلاث خانات). لا تصادم بين محطتين لأن
    ‎BEGIN IMMEDIATE‎ يسلسل الكتّاب.
    """
    con.execute('INSERT OR IGNORE INTO counters(scope, n) VALUES(?, 0)', (scope,))
    con.execute('UPDATE counters SET n = n + 1 WHERE scope = ?', (scope,))
    n = con.execute('SELECT n FROM counters WHERE scope = ?', (scope,)).fetchone()[0]
    return fmt.format(n=n, n3=f'{n:03d}', n4=f'{n:04d}', n6=f'{n:06d}')


def use_number(con, manual, scope, fmt):
    """رقم سند: المكتوب يدويًا إن وُجد وإلا الرقم التالي من العدّاد.

    إذا جاء الرقم اليدوي بصيغة النطاق نفسها (مثل الرقم المقترح الذي يملأ الحقل
    مسبقًا) يُرفع العدّاد إليه، فلا يقترح النظام الرقم نفسه في السند التالي.
    """
    manual = (manual or '').strip()
    if not manual:
        return alloc(con, scope, fmt)
    prefix = fmt.split('{')[0]
    tail = manual[len(prefix):]
    if manual.startswith(prefix) and tail.isdigit():
        n = int(tail)
        con.execute('INSERT OR IGNORE INTO counters(scope, n) VALUES(?, 0)', (scope,))
        con.execute('UPDATE counters SET n = ? WHERE scope = ? AND n < ?', (n, scope, n))
    return manual


def peek_number(scope, fmt):
    """الرقم المتوقّع التالي دون حجزه — للعرض في النماذج فقط."""
    r = one('SELECT n FROM counters WHERE scope = ?', (scope,))
    n = (r['n'] if r else 0) + 1
    return fmt.format(n=n, n3=f'{n:03d}', n4=f'{n:04d}', n6=f'{n:06d}')


def seed_counter(scope, at_least):
    """يرفع عدّاد النطاق إلى قيمة لا تقل عن ‎at_least‎ (للترحيل من بيانات قديمة)."""
    with tx() as con:
        con.execute('INSERT OR IGNORE INTO counters(scope, n) VALUES(?, 0)', (scope,))
        con.execute('UPDATE counters SET n = ? WHERE scope = ? AND n < ?',
                    (at_least, scope, at_least))


# --------------------------------------------------------------- إعدادات وتدقيق
def setting(key, default=None):
    r = one('SELECT value FROM settings WHERE key=?', (key,))
    return r['value'] if r else default


def _actor():
    try:
        from flask import g, has_request_context
        if has_request_context():
            u = getattr(g, 'user', None)
            if u:
                return u['username']
    except Exception:
        pass
    return 'system'


def log(action, table, key, details='', actor=None):
    """يقيّد حدثًا في سجل التدقيق منسوبًا للمستخدم الحالي."""
    try:
        run('INSERT INTO audit_log(username, action, table_name, record_key, details) '
            'VALUES(?,?,?,?,?)', (actor or _actor(), action, table, key, details))
    except Exception as e:                       # لا نبتلع الخطأ بصمت
        log_.error('فشل الكتابة في سجل التدقيق: %s (%s/%s/%s)', e, action, table, key)


# --------------------------------------------------------------- ترحيلات مرجعية
def apply_migrations():
    """ترقيات بسيطة وآمنة للبيانات المرجعية عند فتح نسخة أحدث من النظام."""
    con = get()
    try:
        con.execute("INSERT OR REPLACE INTO settings(key,value,note) VALUES(?,?,?)",
                    ('slitter_edge_trim_each_cm', '1',
                     'تنظيف ثابت من كل طرف للرول قبل احتساب العرض التشغيلي للأسليتر بالسم'))
        con.execute("INSERT OR REPLACE INTO settings(key,value,note) VALUES(?,?,?)",
                    ('slitter_warning_limit_cm', '4',
                     'حد مرجعي لفرق خطة القص؛ تجاوزه يظهر تحذيرًا إضافيًا ولا يمنع الحفظ'))
        con.execute("UPDATE settings SET note=? WHERE key='slitter_max_trim_cm'",
                    ('إعداد قديم محفوظ للتوافق — لم يعد يمنع الحفظ',))

        con.execute("UPDATE slit_matrix SET std_width_cm=23 WHERE ABS(std_width_cm-22)<0.001")
        con.execute("UPDATE work_orders SET std_width_cm=23 WHERE std_width_cm IS NOT NULL AND ABS(std_width_cm-22)<0.001")
        con.execute("""UPDATE subrolls
                       SET width_cm=CASE WHEN ABS(COALESCE(width_cm,0)-22)<0.001 THEN 23 ELSE width_cm END,
                           std_width_cm=CASE WHEN ABS(COALESCE(std_width_cm,0)-22)<0.001 THEN 23 ELSE std_width_cm END,
                           area_cm2=CASE WHEN ABS(COALESCE(width_cm,0)-22)<0.001 AND length_m IS NOT NULL
                                         THEN ROUND(length_m*100*23,2) ELSE area_cm2 END,
                           width_check=CASE WHEN ABS(COALESCE(width_cm,0)-22)<0.001 OR ABS(COALESCE(std_width_cm,0)-22)<0.001
                                            THEN 'مطابق — تم تحديث العرض القياسي من 22 إلى 23 سم' ELSE width_check END
                       WHERE ABS(COALESCE(width_cm,0)-22)<0.001 OR ABS(COALESCE(std_width_cm,0)-22)<0.001""")
        con.execute("""UPDATE folding_in
                       SET width_cm=23,
                           area_used_cm2=CASE WHEN length_used_m IS NOT NULL THEN ROUND(length_used_m*100*23,2) ELSE area_used_cm2 END
                       WHERE ABS(COALESCE(width_cm,0)-22)<0.001""")

        for sr in con.execute("SELECT tag_no,xray,mesh,width_cm,sr_code FROM subrolls WHERE ABS(COALESCE(width_cm,0)-23)<0.001").fetchall():
            if sr['sr_code']:
                continue
            for it in con.execute("""SELECT item_code,width_cm FROM items
                                     WHERE prefix='SR' AND status='نشط'
                                       AND COALESCE(xray,'')=COALESCE(?, '')
                                       AND COALESCE(mesh,'')=COALESCE(?, '')
                                     ORDER BY item_code""", (sr['xray'], sr['mesh'])).fetchall():
                m = re.search(r'-?\d+(?:[.,]\d+)?', str(it['width_cm'] or ''))
                if m and abs(float(m.group(0).replace(',', '.'))-23) < 0.001:
                    con.execute("UPDATE subrolls SET sr_code=? WHERE tag_no=?", (it['item_code'], sr['tag_no']))
                    break

        for r in con.execute("SELECT item_code,width_cm,description FROM items WHERE prefix='SR'").fetchall():
            txt = str(r['width_cm'] or '').strip().lower().replace('cm', '').strip()
            try:
                is22 = abs(float(txt)-22) < 0.001
            except Exception:
                is22 = False
            if is22:
                desc = r['description']
                if desc:
                    desc = desc.replace('22 Cm', '23 Cm').replace('22 cm', '23 cm')
                con.execute("UPDATE items SET width_cm=?, description=? WHERE item_code=?",
                            ('23 Cm', desc, r['item_code']))
        con.commit()
    except sqlite3.OperationalError:
        con.rollback()
    finally:
        con.close()
