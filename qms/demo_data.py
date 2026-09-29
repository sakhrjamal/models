# -*- coding: utf-8 -*-
"""بيانات تجريبية (v15): سيناريو GS310M حتى منتصف الإنتاج، ليكمله المستخدم من الواجهة.

يمر بنفس شاشات النظام (لا إدراج مباشر في الجداول)، فتكون كل الأرقام والقيود والتتبع حقيقية:
  استلام جامبو رول RR005 ← فحص وإفراج ← أمر إنتاج GS310M (500 باكت) ← قص الجامبو (5 سب رول)
  ← 3 سندات طي (4,850 مسحة سليمة لكل سب رول).
المتبقي للتجربة: طي بقية السب رول، ثم تعبئة (100 مسحة = باكت) ← إنهاء الإنتاج ← إفراج الجودة ← المخزن.

لحذفها: احذف data/qms.db وشغّل seed.py من جديد.
"""
import re, secrets, datetime

import db, auth


def load():
    from app import app
    pw = secrets.token_hex(8)
    db.run("INSERT OR REPLACE INTO users(username,full_name,pw_hash,role,active,must_change_pw) VALUES(?,?,?,?,1,0)",
           ('demo_seed', 'تجهيز البيانات التجريبية', auth.hash_pw(pw), 'admin'))
    c = app.test_client()
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get('/login').get_data(True)).group(1)

    def post(url, data):
        d = dict(data); d['_csrf'] = tok
        return c.post(url, data=d, follow_redirects=True)

    try:
        post('/login', {'username': 'demo_seed', 'password': pw})
        today = datetime.date.today().isoformat()
        r = post('/receipts/new', {'supplier_name': 'شركة النسيج المتحدة', 'item_code': 'RR005',
                                   'supplier_lot': 'DEMO-J1', 'roll_count': '1'})
        grn = re.search(r'GRN-\d{6}-\d{3}', r.get_data(True)).group(0)
        post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': pw, 'insp_date': today})
        db.run("UPDATE pack_spec SET packs_per_carton=COALESCE(packs_per_carton,20) WHERE item_code='GS310M'")   # بيانات تجريبية فقط
        post('/orders/new', {'item_code': 'GS310M', 'qty_required': '500'})
        w = db.one("SELECT batch_no FROM work_orders WHERE item_code='GS310M' ORDER BY rowid DESC LIMIT 1")
        bn = w['batch_no']
        roll = db.one('SELECT roll_no FROM rolls WHERE grn_no=?', (grn,))['roll_no']
        opn = db.one("SELECT name FROM operators WHERE dept='production' ORDER BY id")['name']
        post(f'/slitter/{bn}', {'roll_no': roll, 'mode': 'run', 'actual_width': '120', 'actual_length': '2000', 'operator': opn})
        for s in db.q('SELECT tag_no FROM subrolls WHERE batch_no=? ORDER BY tag_no LIMIT 3', (bn,)):
            post(f'/folding/{bn}', {'tag_no': s['tag_no'], 'qty_good': '4850', 'operator': opn})
        print(f'  أُنشئ أمر الإنتاج {bn} (GS310M — 500 باكت = 50,000 مسحة) وقُصّ الجامبو {roll} (5 سب رول) وسُجّلت 3 سندات طي.')
    finally:
        db.run("UPDATE users SET active=0 WHERE username='demo_seed'")


if __name__ == '__main__':
    load()
