# -*- coding: utf-8 -*-
"""ترحيل v13 — منصة مسارات التصنيع. آمن على البيانات القائمة ويمكن تكراره.

  • جداول المسارات وخطواتها والمخزون/WIP والسندات الفرعية والإشعارات (schema_v13.sql)
  • أعمدة جديدة: items.route_code/product_category، work_orders.route_code، receipts.packages/wh_location
  • بيانات مرجعية: المسارات الثلاثة وخطواتها، فئات الخام، بنود فحص الخام، ماكينات الأربطة،
    وأصناف رباط ضاغط مؤقتة بتكوين تعبئة (تُعدَّل من «بيانات المنتجات»)
  • الأوامر القائمة تأخذ مسار FULL_GAUZE — لا يتغير أي رقم تشغيلة أو سجل
"""
import os, sqlite3, sys

import db

HERE = os.path.dirname(os.path.abspath(__file__))

COLS = {
    'items':       [('route_code', 'TEXT'), ('product_category', 'TEXT')],
    'work_orders': [('route_code', 'TEXT'), ('route_override', 'INTEGER DEFAULT 0'), ('route_note', 'TEXT')],
    'receipts':    [('packages', 'INTEGER'), ('wh_location', 'TEXT'), ('created_by', 'TEXT')],
    'users':       [('lines', 'TEXT')],
}

ROUTES = [
    # code, ar, en, category, input_kind, letter, uom, fg sterile, fg non-sterile, sort
    ('FULL_GAUZE', 'تصنيع الشاش من الرول الخام', 'Full Gauze Manufacturing', 'Gauze', 'ROLL', None,
     'قطعة', 'بوكس', 'بوكس', 1),
    ('SP_GAUZE', 'الشاش نصف المصنع SP', 'Semi-Finished Gauze Packing', 'Gauze', 'SP', 'SP',
     'قطعة', 'بوكس', 'كرتون', 2),
    ('BANDAGE', 'الرباط الضاغط', 'Compression Bandage Production', 'Bandage', 'JUMBO', 'B',
     'رول', None, 'كرتون', 3),
]

# (route, variant, seq, step_code, name_ar, endpoint)
STEPS = [
    # المسار وحده يحدد المراحل — لا مراحل افتراضية لمجرد أن المنتج معقم أو غير معقم
    ('FULL_GAUZE', 'all', 1, 'SLIT', 'السليتر', 'slit_work'),
    ('FULL_GAUZE', 'all', 2, 'FOLD', 'الطي', 'fold_work'),
    ('FULL_GAUZE', 'all', 3, 'FINALQC', 'موافقة الجودة', 'order_view'),
    ('FULL_GAUZE', 'all', 4, 'WH', 'المخزن', 'warehouse'),
    ('SP_GAUZE', 'all', 1, 'ALLOC', 'تخصيص خام SP', 'sp_alloc'),
    ('SP_GAUZE', 'all', 2, 'SPPACK', 'التعبئة', 'sp_pack'),
    ('SP_GAUZE', 'all', 3, 'FINALQC', 'موافقة الجودة', 'order_view'),
    ('SP_GAUZE', 'all', 4, 'WH', 'المخزن', 'warehouse'),
    ('BANDAGE', 'all', 1, 'ALLOC', 'تخصيص الجامبو', 'bandage_alloc'),
    ('BANDAGE', 'all', 2, 'BMACH', 'ماكينة الأربطة', 'bandage_machine'),
    ('BANDAGE', 'all', 3, 'BWRAP', 'تغليف الأربطة', 'bandage_wrap'),
    ('BANDAGE', 'all', 4, 'FINALQC', 'موافقة الجودة', 'order_view'),
    ('BANDAGE', 'all', 5, 'WH', 'المخزن', 'warehouse'),
]

MAT_CLASSES = [
    ('RR', 'ROLL', 'رولات الشاش الخام'),
    ('PBT', 'JUMBO', 'جامبو رول الأربطة'),
    ('SP', 'SP', 'شاش نصف مصنع SP'),
]

RM_CHECKS = {
    'ROLL': ['Acidity / Alkalinity — الحموضة والقلوية', 'Dehydration', 'Impurities — الشوائب', 'Distillation',
             'Mesh — عدد الخيوط', 'Absorbency — الامتصاص', 'المظهر والنظافة', 'الأبعاد / الوزن', 'مراجعة COA'],
    'SP':   ['المظهر والنظافة', 'الأبعاد / المقاس', 'عدد الطبقات', 'خلو من الشوائب والشعر',
             'الخط الكاشف X-Ray (إن وُجد)', 'عدد القطع في العبوة', 'سلامة التغليف والحزم',
             'بيانات الملصق والـ LOT', 'مراجعة COA'],
    'JUMBO': ['العرض', 'الطول', 'الوزن', 'الشد / المطاطية', 'الحواف', 'المظهر والنظافة',
              'خلو من الشوائب والشعر', 'بيانات الملصق والـ LOT', 'مراجعة COA'],
}

MACHINES = [
    ('BM-01', 'ماكينة تصنيع الأربطة', 'الأربطة', 'BM', None),
    ('BW-01', 'ماكينة تغليف الأربطة', 'الأربطة', 'BW', None),
]

# أصناف رباط ضاغط مؤقتة: الأكواد والتكوين أمثلة لتجربة المسار — تُعدَّل/تُحذف من «بيانات المنتجات»
BANDAGE_ITEMS = [(5, 'CB005'), (7.5, 'CB075'), (10, 'CB100'), (15, 'CB150')]


def _cols(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info({table})')}


def _schema_sql():
    for base in (getattr(sys, '_MEIPASS', None), HERE):
        if base and os.path.exists(os.path.join(base, 'schema_v13.sql')):
            return open(os.path.join(base, 'schema_v13.sql'), encoding='utf-8').read()
    raise FileNotFoundError('schema_v13.sql')


def seed_reference(con):
    con.executemany("""INSERT OR IGNORE INTO routes(code,name_ar,name_en,category,input_kind,batch_letter,base_uom,
                       fg_unit_sterile,fg_unit_non_sterile,sort_no) VALUES(?,?,?,?,?,?,?,?,?,?)""", ROUTES)
    con.executemany("""INSERT OR IGNORE INTO route_steps(route_code,variant,seq,step_code,name_ar,endpoint)
                       VALUES(?,?,?,?,?,?)""", STEPS)
    con.executemany('INSERT OR IGNORE INTO mat_classes(prefix,class,label_ar) VALUES(?,?,?)', MAT_CLASSES)
    for cls, labels in RM_CHECKS.items():
        con.executemany('INSERT OR IGNORE INTO rm_checks(class,seq,label_ar) VALUES(?,?,?)',
                        [(cls, i, l) for i, l in enumerate(labels, 1)])
    con.executemany('INSERT OR IGNORE INTO machines(machine_code,name,stage,letter,size_locked,active) VALUES(?,?,?,?,?,1)',
                    MACHINES)

    # الأصناف القائمة: شاش تام ← مسار التصنيع الكامل (يُعدَّل لكل صنف من بيانات المنتجات)
    con.execute("""UPDATE items SET route_code='FULL_GAUZE', product_category='Gauze'
                   WHERE prefix='GS' AND route_code IS NULL AND machine_code IS NOT NULL""")
    con.execute("UPDATE items SET product_category='Gauze Bandage' WHERE prefix='GB' AND product_category IS NULL")

    for w, code in BANDAGE_ITEMS:
        wt = f'{w:g}'
        con.execute("""INSERT OR IGNORE INTO items(item_code,prefix,category_ar,category_en,family,sterile,uom,
                       width_cm,description,status,route_code,product_category)
                       VALUES(?,?,?,?,?,?,?,?,?,'نشط','BANDAGE','Bandage')""",
                    (code, 'CB', 'منتج تام', 'Finished Good', 'Compression Bandage', 'غير معقم', 'رول',
                     f'{wt} cm', f'Compression Bandage {wt} cm — NON-STERILE (كود مؤقت: عدّله من بيانات المنتجات)'))
        con.execute("""INSERT OR IGNORE INTO pack_config(item_code,level,unit,unit_ar,per_parent,allow_partial)
                       VALUES(?,1,'box','بوكس',12,1)""", (code,))
        con.execute("""INSERT OR IGNORE INTO pack_config(item_code,level,unit,unit_ar,per_parent,allow_partial)
                       VALUES(?,2,'carton','كرتون',10,1)""", (code,))

    con.execute("UPDATE work_orders SET route_code='FULL_GAUZE' WHERE route_code IS NULL")


def run(verbose=False):
    if not os.path.exists(db.DB_PATH):
        return
    con = sqlite3.connect(db.DB_PATH, timeout=30)
    try:
        con.execute('PRAGMA foreign_keys=OFF')
        con.executescript(_schema_sql())
        con.execute('BEGIN IMMEDIATE')
        for table, cols in COLS.items():
            have = _cols(con, table)
            for col, typ in cols:
                if col not in have:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
        seed_reference(con)
        con.commit()
        if verbose:
            print('  اكتمل ترحيل v13 (مسارات التصنيع).')
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == '__main__':
    run(verbose=True)
