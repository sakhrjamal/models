# -*- coding: utf-8 -*-
"""نسخ احتياطي تلقائي ومتسق لقاعدة البيانات.

يستخدم ‎VACUUM INTO‎ فينتج ملفًا واحدًا سليمًا يتضمن ما في WAL، بخلاف نسخ
‎qms.db‎ يدويًا أثناء التشغيل الذي قد يُنتج نسخة ناقصة.
"""
import os, glob, shutil, sqlite3, threading, datetime, logging
import db

log = logging.getLogger('qms')


def backup_dir():
    d = os.path.join(os.path.dirname(db.DB_PATH), 'backups')
    os.makedirs(d, exist_ok=True)
    return d


def make_backup(tag='auto'):
    if not os.path.exists(db.DB_PATH):
        return None
    d = backup_dir()
    name = f"qms-{datetime.datetime.now():%Y%m%d-%H%M%S}-{tag}.db"
    dest = os.path.join(d, name)
    n = 1
    while os.path.exists(dest):                        # نسختان في الثانية نفسها: لا تصادم أسماء
        n += 1
        dest = os.path.join(d, name[:-3] + f'-{n}.db')
    name = os.path.basename(dest)
    con = db.get()
    try:
        con.execute('VACUUM INTO ?', (dest,))
    finally:
        con.close()
    _prune(d)
    log.info('نسخة احتياطية: %s', name)
    ok, msg = verify(dest)
    _set('backup_verify_last', f"{'سليمة' if ok else 'تالفة'} — {name} — {datetime.datetime.now():%Y-%m-%d %H:%M}" + ('' if ok else f' — {msg}'))
    if not ok:
        log.error('النسخة الاحتياطية فشل فحصها: %s', msg)
    copy_extra(dest)
    return dest


def _set(key, value):
    try:
        db.run("INSERT INTO settings(key,value,note) VALUES(?,?,'') ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    except Exception:                                   # noqa: BLE001 — لا يُفشل النسخ بسبب تسجيل الحالة
        pass


def verify(path):
    """فحص سلامة ملف النسخة (PRAGMA integrity_check) — يعيد (سليم؟، رسالة)."""
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            r = con.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            con.close()
        return r == 'ok', r
    except Exception as e:                              # noqa: BLE001
        return False, str(e)


def extra_dir():
    return (db.setting('backup_extra_dir', '') or '').strip()


def check_extra(path):
    """اختبار مجلد النسخ الثاني: يُنشأ إن لزم ثم تُكتب وتُحذف ملف تجربة. يعيد (نجح؟، رسالة)."""
    try:
        os.makedirs(path, exist_ok=True)
        t = os.path.join(path, '.qms_write_test')
        with open(t, 'w') as fh:
            fh.write('ok')
        os.remove(t)
        return True, 'المجلد صالح للكتابة'
    except Exception as e:                              # noqa: BLE001
        return False, str(e)


def copy_extra(dest):
    """نسخ الملف إلى المكان الثاني (قرص خارجي/مجلد شبكة) إن كان مضبوطًا، وتسجيل النتيجة."""
    d = extra_dir()
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        shutil.copy2(dest, os.path.join(d, os.path.basename(dest)))
        _prune(d)
        _set('backup_extra_last', f"نجحت — {os.path.basename(dest)} — {datetime.datetime.now():%Y-%m-%d %H:%M}")
        return True
    except Exception as e:                              # noqa: BLE001
        log.error('فشل النسخ إلى المكان الثاني %s: %s', d, e)
        _set('backup_extra_last', f"فشلت — {datetime.datetime.now():%Y-%m-%d %H:%M} — {e}")
        return False


def _prune(d):
    try:
        keep = int(db.setting('backup_keep', '60') or 60)
    except (TypeError, ValueError):
        keep = 60
    files = sorted(glob.glob(os.path.join(d, 'qms-*.db')), key=os.path.getmtime, reverse=True)
    for old in files[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass


def last_backups(limit=15):
    d = backup_dir()
    files = sorted(glob.glob(os.path.join(d, 'qms-*.db')), key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:limit]:
        st = os.stat(f)
        out.append(dict(name=os.path.basename(f),
                        size_kb=round(st.st_size / 1024),
                        when=datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')))
    return out


def schedule():
    """نسخة بعد دقيقة من الإقلاع ثم كل ‎backup_interval_h‎ ساعة (افتراضي 6)."""
    try:
        interval = float(db.setting('backup_interval_h', '6') or 6)
    except (TypeError, ValueError):
        interval = 6.0
    interval = max(interval, 1.0)

    def _run():
        try:
            make_backup('auto')
        except Exception as e:
            log.error('فشل النسخ الاحتياطي التلقائي: %s', e)
        t = threading.Timer(interval * 3600, _run)
        t.daemon = True
        t.start()

    first = threading.Timer(60, _run)
    first.daemon = True
    first.start()
