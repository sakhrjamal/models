# -*- coding: utf-8 -*-
"""نقطة تشغيل النظام — تعمل في وضع التطوير وفي وضع exe.

عند الإقلاع:
  1) تُنشئ قاعدة بيانات فارغة من schema.sql إن لم توجد
  2) تطبّق ترحيل v11 + v12 + الترحيلات المرجعية
  3) تُنشئ مستخدم admin افتراضيًا إن لزم
  4) تشغّل جدولة النسخ الاحتياطي التلقائي
  5) تسجّل الأخطاء في ملف داخل data/logs
"""
import os, sys, socket, threading, webbrowser, logging, datetime
from logging.handlers import RotatingFileHandler

if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
    sys.path.insert(0, sys._MEIPASS)

import db
import migrate_v11
import migrate_v12
import migrate_v13
import migrate_v14
import backup
import auth
from app import app


def setup_logging():
    d = os.path.join(os.path.dirname(db.DB_PATH), 'logs')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'qms-{datetime.date.today():%Y%m}.log')
    fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=6, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-7s  %(name)s  %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(logging.StreamHandler(sys.stdout))
    logging.getLogger('waitress').setLevel(logging.WARNING)


def ensure_db():
    if os.path.exists(db.DB_PATH):
        return True
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    schema = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))),
                          'schema.sql')
    if not os.path.exists(schema):
        return False
    con = db.get()
    con.executescript(open(schema, encoding='utf-8').read())
    con.commit(); con.close()
    print('  أُنشئت قاعدة بيانات فارغة. استورد فهرس الأصناف (seed.py) قبل الاستخدام.')
    return True


def free_port(p=5000):
    for i in range(p, p + 30):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', i)) != 0:
                return i
    return p


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def main():
    setup_logging()
    if not ensure_db():
        print('تعذّر تجهيز قاعدة البيانات.'); input('اضغط Enter…'); sys.exit(1)

    migrate_v11.run(verbose=True)
    migrate_v12.run(verbose=True)
    migrate_v13.run(verbose=True)
    migrate_v14.run(verbose=True)
    db.apply_migrations()
    creds = auth.ensure_admin()
    backup.schedule()

    port = free_port()
    bar = '=' * 62
    print(bar)
    print('   نظام إدارة الإنتاج والتتبع — مصنع الشاش والأربطة الطبية')
    print(bar)
    print(f'   على هذا الجهاز : http://127.0.0.1:{port}')
    print(f'   من أجهزة الصالة: http://{lan_ip()}:{port}')
    print(f'   قاعدة البيانات : {db.DB_PATH}')
    print(f'   النسخ الاحتياطي: {os.path.join(os.path.dirname(db.DB_PATH), "backups")}')
    if creds:
        print(bar)
        print(f'   أول تشغيل — سجّل الدخول بـ:  المستخدم: {creds[0]}   كلمة المرور: {creds[1]}')
        print('   سيُطلب منك تغيير كلمة المرور فورًا. أنشئ بقية المستخدمين من «إدارة النظام».')
    print(bar)
    print('   لإيقاف النظام: أغلق هذه النافذة')
    print(bar)

    threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=8, ident='QMS')
    except ImportError:
        app.run(host='0.0.0.0', port=port, threaded=True)


if __name__ == '__main__':
    main()
