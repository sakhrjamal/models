# -*- coding: utf-8 -*-
"""ترحيل v14 — التبسيط: المنتج يقود النظام.

  • بيانات المنتج: عرض السب رول، الإنتاجية المعيارية لكل سب رول، الخام (BOM)، الوحدة (باكت)
  • خطط القص، حالة الجامبو، حالة السب رول، قرارات الجودة النهائية، الأولوية
  • خطوات المسارات المبسّطة (الصنف/المسار يحدد المراحل، لا مراحل افتراضية للمعقم)
  • إعدادات: عرض الجامبو الافتراضي، إيقاف بوابات الجودة الافتراضية

آمن على البيانات القائمة ويمكن تكراره.
"""
import os, re, sqlite3, sys

import db

HERE = os.path.dirname(os.path.abspath(__file__))

COLS = {
    'items':       [('sub_roll_width_cm', 'REAL'), ('yield_per_sr', 'REAL'), ('raw_item', 'TEXT'), ('box_code', 'TEXT')],
    'work_orders': [('priority', 'TEXT'), ('final_status', 'TEXT'), ('finished_at', 'TEXT'), ('finished_by', 'TEXT'),
                    ('approved_qty', 'REAL'), ('approved_by', 'TEXT'), ('approved_at', 'TEXT'), ('stored_at', 'TEXT'),
                    ('qc_note', 'TEXT'), ('raw_item', 'TEXT'), ('produced_qty', 'REAL')],
    'rolls':       [('use_status', 'TEXT'), ('plan_no', 'TEXT')],
    'subrolls':    [('used_doc', 'TEXT'), ('plan_no', 'TEXT')],
    # سند الطي الجديد = صف في folding_out (المخرج) + صف في folding_in (استهلاك السب رول) بالرقم نفسه
    'folding_out': [('tag_no', 'TEXT'), ('operator', 'TEXT'), ('unit', 'TEXT'), ('start_time', 'TEXT'),
                    ('end_time', 'TEXT'), ('created_by', 'TEXT'), ('created_at', 'TEXT'),
                    ('updated_by', 'TEXT'), ('updated_at', 'TEXT')],
}

# فهارس فريدة: السب رول يُستخدم مرة واحدة حتى لو فُتحت صفحتان معًا (قاعدة البيانات نفسها تمنع التكرار)
UNIQUE = [
    ('ux_fout_tag', 'folding_out', 'tag_no'),
    ('ux_fin_tag', 'folding_in', 'tag_no'),
    ('ux_rolls_plan', 'cutting_plans', 'roll_no'),
]

UNITS = [('piece', 'قطعة', 1), ('pack', 'باكت', 2), ('roll', 'رول', 3), ('box', 'بوكس', 4), ('carton', 'كرتون', 5)]

STEPS_VERSION = '14'


def _schema():
    for base in (getattr(sys, '_MEIPASS', None), HERE):
        if base and os.path.exists(os.path.join(base, 'schema_v14.sql')):
            return open(os.path.join(base, 'schema_v14.sql'), encoding='utf-8').read()
    raise FileNotFoundError('schema_v14.sql')


def _cols(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info({table})')}


def _num(v):
    m = re.search(r'-?\d+(?:[.,]\d+)?', str(v or ''))
    return float(m.group(0).replace(',', '.')) if m else None


def rebuild_steps(con):
    """مرة واحدة: خطوات المسارات كما في migrate_v13.STEPS (المبسّطة)."""
    import migrate_v13
    row = con.execute("SELECT value FROM settings WHERE key='route_steps_version'").fetchone()
    if row and row[0] == STEPS_VERSION:
        return
    con.execute('DELETE FROM route_steps')
    con.executemany("""INSERT INTO route_steps(route_code,variant,seq,step_code,name_ar,endpoint)
                       VALUES(?,?,?,?,?,?)""", migrate_v13.STEPS)
    con.execute("INSERT OR REPLACE INTO settings(key,value,note) VALUES('route_steps_version',?,?)",
                (STEPS_VERSION, 'إصدار خطوات المسارات — لا تعدّله'))


def seed(con):
    con.executemany('INSERT OR IGNORE INTO units(code,name_ar,sort_no) VALUES(?,?,?)', UNITS)

    # عرض السب رول من مصفوفة القص المعتمدة (ماكينة × طبقات) — بيانات المصنع لا تخمين من الكود
    for it in con.execute("""SELECT item_code, machine_code, ply FROM items
                             WHERE sub_roll_width_cm IS NULL AND machine_code IS NOT NULL AND ply IS NOT NULL
                               AND prefix IN ('GS','GB')""").fetchall():
        m = con.execute('SELECT std_width_cm FROM slit_matrix WHERE key=?', (f'{it[1]}-{it[2]}',)).fetchone()
        if m:
            con.execute('UPDATE items SET sub_roll_width_cm=? WHERE item_code=?', (m[0], it[0]))

    # مثال GS310M كما عرّفته الإدارة: إنتاجية 5000 باكت/سب رول، الوحدة باكت، خام 120 سم، تكوين تعبئة تجريبي
    g = con.execute("SELECT 1 FROM items WHERE item_code='GS310M'").fetchone()
    if g:
        con.execute("""UPDATE items SET yield_per_sr=COALESCE(yield_per_sr,5000), raw_item=COALESCE(raw_item,'RR005'),
                       uom=CASE WHEN uom='قطعة' THEN 'باكت' ELSE uom END WHERE item_code='GS310M'""")
        if not con.execute("SELECT 1 FROM pack_config WHERE item_code='GS310M'").fetchone():
            for lv, unit, ar, per in ((1, 'pack', 'باكت', 100), (2, 'box', 'بوكس', 20), (3, 'carton', 'كرتون', 10)):
                con.execute("""INSERT INTO pack_config(item_code,level,unit,unit_ar,per_parent,allow_partial)
                               VALUES('GS310M',?,?,?,?,1)""", (lv, unit, ar, per))

    # الإعدادات: عرض الجامبو الافتراضي 120 (إن بقي الافتراضي القديم 90)، وإيقاف بوابات الجودة الافتراضية
    con.execute("UPDATE settings SET value='120' WHERE key='default_jumbo_width_cm' AND value='90'")
    con.execute("UPDATE settings SET value='off' WHERE key='qc_gate_mode' AND value='warn'")
    con.execute("INSERT OR IGNORE INTO settings(key,value,note) VALUES('fg_location','مخزن المنتج التام','موقع استلام المنتج التام الافتراضي')")

    # الرولات القديمة المرتبطة بتشغيلة سبق قصّها
    con.execute("UPDATE rolls SET use_status='مستهلك' WHERE use_status IS NULL AND IFNULL(batch_no,'')<>''")


def run(verbose=False):
    if not os.path.exists(db.DB_PATH):
        return
    con = sqlite3.connect(db.DB_PATH, timeout=30)
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
        for name, table, col in UNIQUE:
            try:
                where = '' if table == 'cutting_plans' else f' WHERE {col} IS NOT NULL'
                con.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({col}){where}')
            except sqlite3.IntegrityError:            # بيانات قديمة مكررة: تبقى الحماية في المنطق
                pass
        con.commit()
        if verbose:
            print('  اكتمل ترحيل v14 (التبسيط).')
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == '__main__':
    run(verbose=True)
