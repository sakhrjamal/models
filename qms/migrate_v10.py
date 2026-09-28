# -*- coding: utf-8 -*-
"""
ترحيل قاعدة البيانات إلى بنية v10:
  - فصل أمر التقطيع (أسليتر) عن أمر الإنتاج (طي)
  - مخزون السب رول بحالات
  - أرقام سندات متسلسلة
لا يُهدم أي جدول. يضيف أعمدة فقط. آمن على البيانات الحالية.
"""
import sqlite3, os, sys

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'qms.db')

ADD = {
 'work_orders': [
   ('order_type', "TEXT DEFAULT 'إنتاج'"),   # تقطيع / إنتاج
   ('sr_spec_key', 'TEXT'),                   # نوع السب رول المطلوب (FD-10-12)
   ('sr_needed', 'INTEGER'),                  # عدد السب رول المحسوب
   ('pieces_per_sr', 'REAL'),                 # متوسط إنتاج السب رول الواحد
   ('jumbo_needed', 'REAL'),                  # عدد الجامبو التقريبي
 ],
 'subrolls': [
   ('stock_status', "TEXT DEFAULT 'متاح'"),   # متاح / محجوز / مستهلك
   ('consumed_by_batch', 'TEXT'),             # تشغيلة الطي التي سحبته
   ('consumed_date', 'TEXT'),
   ('slit_batch', 'TEXT'),                    # تشغيلة الأسليتر (مصدره)
 ],
 'folding_in': [
   ('sr_source_batch', 'TEXT'),               # تشغيلة الأسليتر التي جاء منها السب رول
 ],
 'slitting': [
   ('doc_seq', 'TEXT'),                       # رقم السند المتسلسل /01
 ],
 'folding_out': [
   ('doc_seq', 'TEXT'),
 ],
}

def cols(con, t):
    return {r[1] for r in con.execute(f'PRAGMA table_info({t})')}

def main():
    if not os.path.exists(DB):
        print('لا توجد قاعدة بيانات — شغّل seed.py أولًا'); sys.exit(1)
    con = sqlite3.connect(DB)
    # نسخة احتياطية تلقائية قبل الترحيل
    import shutil, datetime
    bak = DB + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    shutil.copy(DB, bak)
    print('نسخة احتياطية:', os.path.basename(bak))

    added = 0
    for t, fields in ADD.items():
        have = cols(con, t)
        for name, decl in fields:
            if name not in have:
                con.execute(f'ALTER TABLE {t} ADD COLUMN {name} {decl}')
                print(f'  + {t}.{name}')
                added += 1

    # جدول توليد أرقام السندات المتسلسلة
    con.execute("""CREATE TABLE IF NOT EXISTS doc_seq(
        batch_no TEXT PRIMARY KEY, last_seq INTEGER DEFAULT 0)""")

    # ترحيل البيانات القائمة: السب رول الموجودة تصير "متاح" أو "مستهلك"
    con.execute("""UPDATE subrolls SET slit_batch = batch_no
                   WHERE slit_batch IS NULL AND batch_no IS NOT NULL""")
    con.execute("""UPDATE subrolls SET stock_status='مستهلك',
                     consumed_by_batch = batch_no
                   WHERE tag_no IN (SELECT tag_no FROM folding_in WHERE tag_no IS NOT NULL)
                     AND stock_status='متاح'""")
    # أوامر التشغيل القائمة: من مرحلة SL تصير "تقطيع"، غيرها "إنتاج"
    con.execute("""UPDATE work_orders SET order_type='تقطيع'
                   WHERE stage_code='SL' AND order_type='إنتاج'""")

    con.commit()
    print(f'\nاكتمل الترحيل — {added} عمود مضاف.')
    for t in ('subrolls','work_orders'):
        n = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {t}: {n} صف سليم')
    con.close()

if __name__ == '__main__':
    main()
