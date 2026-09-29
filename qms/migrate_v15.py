# -*- coding: utf-8 -*-
"""ترحيل v15 — التتبع الكامل وربط كل مرحلة بمخرجات السابقة.

  • مشغلون (قائمة رئيسية) بدل النص الحر
  • uid ثابت لكل صنف (لا يتغير عند تغيير الكود) + أعمدة بطاقة المادة
  • العرض المرجعي للرولات مُستخرج من وصف/عرض المادة عند وضوحه (لا يغيّر الوصف)
  • ربط المنتج المعقم بكود SP من Master Data بمطابقة الوصف حرفيًا (بلا تحليل حروف الكود)
  • Packaging Configuration من بيانات Excel: ما لا يوجد فيه يُترك فارغًا ويظهر كتنبيه لمدير النظام
  • خطوات مسار الشاش حسب المعقم/غير المعقم
آمن على البيانات القائمة ويمكن تكراره.
"""
import os, re, sqlite3, sys

import db

HERE = os.path.dirname(os.path.abspath(__file__))

COLS = {
    'items': [('uid', 'TEXT'), ('name_ar', 'TEXT'), ('name_en', 'TEXT'), ('sp_item', 'TEXT'), ('barcode', 'TEXT'),
              ('lot_tracking', 'INTEGER DEFAULT 0'), ('roll_tracking', 'INTEGER DEFAULT 0'), ('supplier_name', 'TEXT'),
              ('min_stock', 'REAL'), ('location', 'TEXT'), ('notes', 'TEXT'), ('ref_width_cm', 'REAL'),
              ('waste_limit_pct', 'REAL')],
    'rolls': [('ref_width_cm', 'REAL'), ('actual_width_cm', 'REAL'), ('ref_length_m', 'REAL'),
              ('actual_length_m', 'REAL'), ('supplier_roll_no', 'TEXT')],
    'subrolls': [('barcode', 'TEXT'), ('run_no', 'TEXT'), ('sr_manual', 'INTEGER DEFAULT 0'), ('supplier_lot', 'TEXT'),
                 ('supplier_roll_no', 'TEXT')],
    'cutting_plans': [('operator', 'TEXT'), ('start_at', 'TEXT'), ('actual_width', 'REAL'), ('actual_length', 'REAL'),
                      ('ref_width', 'REAL'), ('ref_length', 'REAL'), ('sr_code', 'TEXT'), ('sr_manual', 'INTEGER DEFAULT 0')],
    'work_orders': [('req_swabs', 'REAL')],
    'folding_out': [('theoretical_qty', 'REAL'), ('waste_qty', 'REAL'), ('rejected_qty', 'REAL'), ('waste_pct', 'REAL')],
    'approvals': [('stage', 'TEXT')],
    'qc_templates': [('form_no', 'TEXT'), ('revision', 'TEXT'), ('effective_date', 'TEXT')],
    'qc_records': [('corrective', 'TEXT'), ('approved_by', 'TEXT')],
    'cycle_loads': [('rec_no', 'TEXT')],
    'cycles': [('start_at', 'TEXT'), ('end_at', 'TEXT'), ('aer_start_at', 'TEXT'), ('aer_end_at', 'TEXT'),
               ('aer_operator', 'TEXT'), ('verified_by', 'TEXT'), ('verified_at', 'TEXT')],
}

OPERATORS = [('يعقوب', 'production'), ('باسط', 'production'), ('سلفراز', 'production'), ('سيف', 'production'),
             ('اشتياق', 'production'), ('روبل', 'production'), ('ابراهيم', 'production'), ('اياز', 'production'),
             ('محمد السعيد', 'sterilization'), ('عبد الوهاب صابر', 'sterilization')]

UNITS = [('swab', 'مسحة', 0), ('envelope', 'مغلف', 6)]

# خطوات المسار الشامل: تتفرع حسب المعقم/غير المعقم كما حدّدتها الإدارة
STEPS = [
    ('FULL_GAUZE', 'non_sterile', 1, 'SLIT', 'السليتر', 'slit_work'),
    ('FULL_GAUZE', 'non_sterile', 2, 'FOLD', 'الطي', 'fold_work'),
    ('FULL_GAUZE', 'non_sterile', 3, 'NSPACK', 'التعبئة', 'ns_pack'),
    ('FULL_GAUZE', 'non_sterile', 4, 'FINALQC', 'إفراج المنتج غير المعقم', 'order_view'),
    ('FULL_GAUZE', 'non_sterile', 5, 'WH', 'المخزن', 'warehouse'),
    ('FULL_GAUZE', 'sterile', 1, 'SLIT', 'السليتر', 'slit_work'),
    ('FULL_GAUZE', 'sterile', 2, 'FOLD', 'الطي', 'fold_work'),
    ('FULL_GAUZE', 'sterile', 3, 'SPREL', 'إفراج SP', 'order_view'),
    ('FULL_GAUZE', 'sterile', 4, 'STERPROC', 'الفرز والعد والتغليف', 'ster_order'),
    ('FULL_GAUZE', 'sterile', 5, 'PRESTER', 'إفراج قبل التعقيم', 'order_view'),
    ('FULL_GAUZE', 'sterile', 6, 'STERIL', 'التعقيم', 'ster_cycles'),
    ('FULL_GAUZE', 'sterile', 7, 'AERATION', 'التهوية', 'ster_cycles'),
    ('FULL_GAUZE', 'sterile', 8, 'FINALQC', 'الإفراج النهائي', 'order_view'),
    ('FULL_GAUZE', 'sterile', 9, 'WH', 'المخزن', 'warehouse'),
]
STEPS_VERSION = '15'


def _schema():
    for base in (getattr(sys, '_MEIPASS', None), HERE):
        if base and os.path.exists(os.path.join(base, 'schema_v15.sql')):
            with open(os.path.join(base, 'schema_v15.sql'), encoding='utf-8') as fh:
                return fh.read()
    raise FileNotFoundError('schema_v15.sql')


def _cols(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info({table})')}


def width_from_text(*vals):
    """العرض بالسم من نص مثل «120 Cm» أو وصف «...-120 Cm-2000». يعيد None إن لم يكن واضحًا."""
    for v in vals:
        if v in (None, ''):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*c\s*m\b', str(v), re.I)
        if m:
            return float(m.group(1).replace(',', '.'))
        if re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*', str(v)):
            return float(str(v).replace(',', '.'))
    return None


def rebuild_steps(con):
    row = con.execute("SELECT value FROM settings WHERE key='route_steps_version'").fetchone()
    if row and row[0] == STEPS_VERSION:
        return
    con.execute("DELETE FROM route_steps WHERE route_code='FULL_GAUZE'")
    con.executemany("INSERT INTO route_steps(route_code,variant,seq,step_code,name_ar,endpoint) VALUES(?,?,?,?,?,?)", STEPS)
    con.execute("INSERT OR REPLACE INTO settings(key,value,note) VALUES('route_steps_version',?,?)",
                (STEPS_VERSION, 'إصدار خطوات المسارات — لا تعدّله'))


def seed(con):
    con.executemany('INSERT OR IGNORE INTO units(code,name_ar,sort_no) VALUES(?,?,?)', UNITS)
    con.executemany('INSERT OR IGNORE INTO operators(name,dept) VALUES(?,?)', OPERATORS)
    con.execute("UPDATE items SET uid=lower(hex(randomblob(16))) WHERE uid IS NULL")
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_items_uid ON items(uid)')

    # العرض المرجعي للرولات والسب رول من عرض/وصف المادة (يبقى الوصف كما هو)
    for it in con.execute("SELECT item_code, width_cm, description FROM items WHERE prefix IN ('RR','SR') AND ref_width_cm IS NULL").fetchall():
        w = width_from_text(it['width_cm'], it['description'])
        if w:
            con.execute('UPDATE items SET ref_width_cm=? WHERE item_code=?', (w, it['item_code']))
    # الطول المرجعي كما في الملف (الرول 2000 م) محفوظ في length_m أصلًا

    # الوحدة التجارية: غير معقم = باكت، معقم = بوكس (حسب تعريف الإدارة)
    con.execute("UPDATE items SET uom='باكت' WHERE prefix='GS' AND sterile='غير معقم' AND IFNULL(uom,'') IN ('','قطعة','باك')")
    con.execute("UPDATE items SET uom='بوكس' WHERE prefix='GS' AND sterile='معقم' AND IFNULL(uom,'') IN ('','قطعة')")

    # كود SP الداخلي: مطابقة حرفية للوصف في Master Data (SP لا يُخترع)
    sp = {}
    for r in con.execute("SELECT item_code, description FROM items WHERE prefix='SP'").fetchall():
        sp.setdefault((r['description'] or '').strip().upper(), []).append(r['item_code'])
    for g in con.execute("SELECT item_code, description FROM items WHERE prefix='GS' AND sp_item IS NULL").fetchall():
        m = sp.get((g['description'] or '').strip().upper(), [])
        if len(m) == 1:
            con.execute('UPDATE items SET sp_item=? WHERE item_code=?', (m[0], g['item_code']))

    # بيانات النموذج الرسمي: رقم النموذج/الإصدار/تاريخ السريان (يعدّلها مدير النظام)
    con.execute("UPDATE qc_templates SET form_no=code WHERE form_no IS NULL")
    con.execute("UPDATE qc_templates SET revision='00' WHERE revision IS NULL")
    con.execute("UPDATE qc_templates SET effective_date=date('now','localtime') WHERE effective_date IS NULL")
    # مثال إنتاجية GS310M الذي زرعته v14 يُلغى: الكمية النظرية تُحسب الآن من الأبعاد الفعلية
    con.execute("UPDATE items SET yield_per_sr=NULL WHERE item_code='GS310M' AND yield_per_sr=5000")

    # Packaging Configuration من Excel: الأكواد فقط؛ الأعداد غير الموجودة في الملف تبقى NULL (تنبيه لمدير النظام)
    for g in con.execute("SELECT item_code, sterile, pack_code, master_box FROM items WHERE prefix='GS'").fetchall():
        if con.execute('SELECT 1 FROM pack_spec WHERE item_code=?', (g['item_code'],)).fetchone():
            continue
        first = lambda v: (v or '').split(',')[0].strip() or None          # noqa: E731
        if g['sterile'] == 'معقم':
            con.execute("""INSERT INTO pack_spec(item_code,swabs_per_box,box_code,carton_code) VALUES(?,?,?,?)""",
                        (g['item_code'], 100, first(g['pack_code']), first(g['master_box'])))
        else:
            con.execute("""INSERT INTO pack_spec(item_code,swabs_per_pack,pack_code,carton_code) VALUES(?,?,?,?)""",
                        (g['item_code'], 100, first(g['pack_code']), first(g['master_box'])))
    # قيم التعبئة التي كنت قد زرعتها كمثال لـ GS310M (100/20/10) ليست من Excel: تُحذف
    ex = con.execute("SELECT per_parent FROM pack_config WHERE item_code='GS310M' ORDER BY level").fetchall()
    if [r[0] for r in ex] == [100.0, 20.0, 10.0]:
        con.execute("DELETE FROM pack_config WHERE item_code='GS310M'")

    # قرارات الإدارة: حد الهدر 3%؛ البوكس في الأربطة = 12 رباطًا؛ الكراتين يدوية
    con.execute("UPDATE settings SET value='0.03', note='حد الهدر المسموح في الطي (3%)' WHERE key='scrap_limit' AND value='0.05'")
    con.execute("""UPDATE pack_config SET per_parent=12 WHERE unit='box' AND per_parent=10
                   AND item_code IN (SELECT item_code FROM items WHERE route_code='BANDAGE')""")
    con.execute("UPDATE settings SET value='46' WHERE key='print_margin_top_mm' AND value='45'")
    con.execute("UPDATE settings SET value='32' WHERE key='print_margin_bottom_mm' AND value='25'")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('print_margin_top_mm','46','هامش أعلى الصفحة المطبوعة (يتسع لرأس الورق الرسمي)')")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('print_margin_bottom_mm','32','هامش أسفل الصفحة المطبوعة (تذييل الورق الرسمي)')")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('print_margin_side_mm','15','هامش جانبي للصفحة المطبوعة')")


def run(verbose=False):
    if not os.path.exists(db.DB_PATH):
        return
    con = sqlite3.connect(db.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=OFF')
        con.executescript(_schema())
        con.execute('BEGIN IMMEDIATE')
        for table, cols in COLS.items():
            have = _cols(con, table)
            for col, typ in cols:
                if col not in have:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
        rebuild_steps(con)
        seed(con)
        con.commit()
        if verbose:
            print('  اكتمل ترحيل v15 (التتبع الكامل).')
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == '__main__':
    run(verbose=True)
