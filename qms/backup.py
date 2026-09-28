# -*- coding: utf-8 -*-
"""نسخ احتياطي تلقائي ومتسق لقاعدة البيانات.

يستخدم ‎VACUUM INTO‎ فينتج ملفًا واحدًا سليمًا يتضمن ما في WAL، بخلاف نسخ
‎qms.db‎ يدويًا أثناء التشغيل الذي قد يُنتج نسخة ناقصة.
"""
import os, glob, threading, datetime, logging
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
    con = db.get()
    try:
        con.execute('VACUUM INTO ?', (dest,))
    finally:
        con.close()
    _prune(d)
    log.info('نسخة احتياطية: %s', name)
    return dest


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
