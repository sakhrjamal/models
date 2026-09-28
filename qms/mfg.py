# -*- coding: utf-8 -*-
"""منصة مسارات التصنيع.

    المنتج (items.route_code) ← المسار (routes) ← خطوات المسار (route_steps)
                                  ← بوابات الجودة (qc_templates.mfg_routes / requires)
                                  ← حركات المخزون (inventory.stock_tx)

لا يوجد في أي شاشة `if product == ...`: الشاشات تسأل هذه الوحدة عن مسار الأمر وخطواته وتقدّمه.
إضافة مسار جديد = صفوف في routes/route_steps + (اختياريًا) معالج تقدّم جديد بـ @step.
"""
import json

import db, inventory

FULL, SP, BANDAGE = 'FULL_GAUZE', 'SP_GAUZE', 'BANDAGE'
STERILE, NON_STERILE = 'sterile', 'non_sterile'


# ------------------------------------------------------------------ المسارات
def routes(active_only=True):
    sql = 'SELECT * FROM routes' + (' WHERE active=1' if active_only else '') + ' ORDER BY sort_no, code'
    return db.q(sql)


def route(code):
    return db.one('SELECT * FROM routes WHERE code=?', (code,)) if code else None


def variant_of(sterile_text):
    """معقم/غير معقم (نص الصنف أو الأمر) ← sterile / non_sterile."""
    return STERILE if (sterile_text or '').strip() == 'معقم' else NON_STERILE


def variant_for(w):
    return variant_of((w or {}).get('route'))


def route_of_wo(w):
    return (w or {}).get('route_code') or FULL


def route_of_item(item):
    return (item or {}).get('route_code')


def steps_for(route_code, variant):
    return db.q("""SELECT * FROM route_steps WHERE route_code=? AND variant IN ('all',?) ORDER BY seq""",
                (route_code, variant))


def batch_letter(route_code, item):
    """حرف رقم التشغيلة: من المسار إن وُجد وإلا من ماكينة طي الصنف (الفلسفة القائمة)."""
    r = route(route_code)
    if r and r.get('batch_letter'):
        return r['batch_letter']
    if item and item.get('machine_code'):
        return (db.one('SELECT letter FROM machines WHERE machine_code=?', (item['machine_code'],)) or {}).get('letter')
    return None


def mat_class(prefix):
    r = db.one('SELECT class FROM mat_classes WHERE prefix=?', (prefix,))
    return r['class'] if r else None


def class_of_item(item_code):
    it = db.one('SELECT prefix FROM items WHERE item_code=?', (item_code,))
    return mat_class(it['prefix']) if it else None


def prefixes_of_class(cls):
    return [r['prefix'] for r in db.q('SELECT prefix FROM mat_classes WHERE class=?', (cls,))]


# ------------------------------------------------------------------ تكوين التعبئة
def pack_levels(item_code):
    return db.q('SELECT * FROM pack_config WHERE item_code=? ORDER BY level', (item_code,))


def pack_map(item_code):
    """{unit: {per_parent, allow_partial, level, unit_ar}}"""
    return {r['unit']: r for r in pack_levels(item_code)}


# مرحلة WIP الأخيرة لكل مسار — منها يُستلم المنتج التام في المخزن
def final_stage(w):
    rc = route_of_wo(w)
    if rc == BANDAGE:
        return 'CT_OUT'
    if rc == SP:
        return 'PACKED' if variant_for(w) == STERILE else 'SPK_OUT'
    return None                      # الشاش الكامل: WIP مشتق من الجداول القائمة (انظر lines.wip_rows)


# ------------------------------------------------------------------ المنتج التام / الإفراج
def final_release(bn):
    """سجل الإفراج النهائي للتشغيلة: شهادة إفراج (المعقم) أو سجل QA نهائي (is_final)."""
    r = db.one("SELECT release_no ref, issue_date d, decision FROM releases WHERE batch_no=? AND decision='مفرج عنها'", (bn,))
    if r:
        return dict(r, kind='cert')
    q = db.one("""SELECT r.rec_no ref, r.rec_date d, r.decision FROM qc_records r
                  JOIN qc_templates t ON t.code=r.template_code
                  WHERE t.is_final=1 AND r.batch_no=? AND r.decision='مفرج' ORDER BY r.id DESC LIMIT 1""", (bn,))
    return dict(q, kind='qa', decision='مفرج عنها') if q else None


def released_qty(bn):
    """(كمية مفرجة, وحدة, مرجع الإفراج) — بوحدة المخزن للمسار."""
    fr = final_release(bn)
    if not fr:
        return 0.0, None, None
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    rt = route(route_of_wo(w)) or {}
    var = variant_for(w)
    unit = rt.get('fg_unit_sterile' if var == STERILE else 'fg_unit_non_sterile') or 'بوكس'
    rc = route_of_wo(w)
    if fr['kind'] == 'cert':
        r = db.one('SELECT qty_boxes FROM releases WHERE batch_no=?', (bn,))
        return float(r['qty_boxes'] or 0), unit, fr['ref']
    if rc == BANDAGE:
        n = db.one("""SELECT IFNULL(SUM(qty_out),0) n FROM proc_batches WHERE batch_no=? AND stage_code='CT'""", (bn,))['n']
        return float(n), unit, fr['ref']
    if rc == SP:
        n = db.one("""SELECT IFNULL(SUM(qty_out),0) n FROM proc_batches WHERE batch_no=? AND stage_code='SPK'
                      AND unit='كرتون'""", (bn,))['n']
        return float(n), unit, fr['ref']
    p = db.one('SELECT SUM(boxes) b, SUM(env_good) e FROM packaging WHERE batch_no=?', (bn,))
    return float((p and (p['b'] or p['e'])) or 0), unit, fr['ref']


def fg_received(bn):
    return float(db.one('SELECT IFNULL(SUM(qty),0) n FROM fg_receipts WHERE batch_no=?', (bn,))['n'])


# ------------------------------------------------------------------ معالجات التقدّم
HANDLERS = {}


def step(code):
    def deco(fn):
        HANDLERS[code] = fn
        return fn
    return deco


def _n(v):
    return float(v or 0)


def _frac(x, req):
    return min(x / req, 1.0) if req else (1.0 if x else 0.0)


def _req(w):
    return _n(w.get('qty_required'))


def _sum(sql, args):
    r = db.one(sql, args)
    return _n(list(r.values())[0]) if r else 0.0


@step('RM')
def _rm(w, bn):
    return 1.0 if db.one('SELECT 1 FROM folding_in WHERE batch_no=?', (bn,)) else 0.0


@step('SLIT')
def _slit(w, bn):
    return 1.0 if db.one('SELECT 1 FROM folding_in WHERE batch_no=?', (bn,)) else 0.0


@step('FOLD')
def _fold(w, bn):
    return _frac(_sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,)), _req(w))


@step('SORT')
def _sort(w, bn):
    return _frac(_sum('SELECT SUM(pieces_used) FROM sorting WHERE batch_no=?', (bn,)), _req(w))


@step('PACK')
def _pack(w, bn):
    return _frac(_sum('SELECT SUM(env_good*per_envelope) FROM packaging WHERE batch_no=?', (bn,)), _req(w))


@step('STER')
def _ster(w, bn):
    if db.one('SELECT 1 FROM post_ster_receipts WHERE batch_no=?', (bn,)):
        return 1.0
    return 0.5 if db.one('SELECT 1 FROM cycle_loads WHERE batch_no=?', (bn,)) else 0.0


@step('FINALQC')
def _finalqc(w, bn):
    return 1.0 if final_release(bn) else 0.0


@step('WH')
def _wh(w, bn):
    rel, _, _ = released_qty(bn)
    return _frac(fg_received(bn), rel) if rel else 0.0


@step('ALLOC')
def _alloc(w, bn):
    if route_of_wo(w) == BANDAGE:
        return 1.0 if db.one('SELECT 1 FROM allocations WHERE batch_no=? AND voided=0', (bn,)) else 0.0
    return _frac(_sum('SELECT SUM(qty) FROM allocations WHERE batch_no=? AND voided=0', (bn,)), _req(w))


def _proc_qty(stage, bn, col='qty_out'):
    return _sum(f"SELECT SUM({col}) FROM proc_batches WHERE batch_no=? AND stage_code='{stage}'", (bn,))


@step('SPPACK')
def _sppack(w, bn):
    # المُعالَج = سليم + مرفوض + هالك (qty_in): الخطوة تكتمل حين تُعبَّأ/تُصنَّف كل الكمية المصروفة
    return _frac(_sum("SELECT SUM(qty_in) FROM proc_batches WHERE batch_no=? AND stage_code='SPK'", (bn,)), _req(w))


@step('BMACH')
def _bm(w, bn):
    return _frac(_proc_qty('BM', bn), _req(w))


@step('BWRAP')
def _bw(w, bn):
    return _frac(_proc_qty('BW', bn), _req(w))


@step('BBOX')
def _bbox(w, bn):
    return _frac(_sum("""SELECT SUM(json_extract(extra_json,'$.rolls')) FROM proc_batches
                         WHERE batch_no=? AND stage_code='BX'""", (bn,)), _req(w))


@step('BCARTON')
def _bct(w, bn):
    return _frac(_sum("""SELECT SUM(json_extract(extra_json,'$.rolls')) FROM proc_batches
                         WHERE batch_no=? AND stage_code='CT'""", (bn,)), _req(w))


def progress(bn):
    """تقدّم أمر إنتاج حسب خطوات مساره: قائمة الخطوات بنسبة كل خطوة، والنسبة الكلية، والمرحلة الحالية."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
    if not w:
        return None
    if (w.get('order_type') or 'إنتاج') == 'تقطيع':
        n = db.one('SELECT COUNT(*) n FROM subrolls WHERE batch_no=? OR slit_batch=?', (bn, bn))['n']
        req = w.get('sr_needed') or w.get('qty_required') or 0
        f = _frac(n, req) if req else (1.0 if n else 0.0)
        return dict(route=None, variant=None, steps=[dict(code='SLIT', name='القص وإنشاء السب رول', endpoint='slit',
                                                          frac=f, state='done' if f >= 1 else ('active' if f else 'pending'))],
                    pct=round(f * 100), current='القص وإنشاء السب رول' if f < 1 else 'مكتمل', done=f >= 1)
    rc, var = route_of_wo(w), variant_for(w)
    steps, current = [], None
    for s in steps_for(rc, var):
        h = HANDLERS.get(s['step_code'])
        f = round(h(w, bn), 4) if h else 0.0
        steps.append(dict(code=s['step_code'], name=s['name_ar'], endpoint=s['endpoint'], frac=f))
    first_open = next((i for i, s in enumerate(steps) if s['frac'] < 1), None)
    for i, s in enumerate(steps):
        s['state'] = 'done' if s['frac'] >= 1 else ('active' if i == first_open or s['frac'] > 0 else 'pending')
    pct = round(sum(s['frac'] for s in steps) / len(steps) * 100) if steps else 0
    if first_open is not None:
        current = steps[first_open]['name']
    return dict(route=rc, variant=var, steps=steps, pct=pct, current=current or 'مكتمل', done=first_open is None)


# ------------------------------------------------------------------ المخرجات (سليم / مرفوض / هالك)
def output(bn):
    """مجموع الناتج السليم والمرفوض والهالك لتشغيلة بوحدة المسار الأساسية (قطعة أو رول)."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    rc = route_of_wo(w)
    good = rej = scrap = 0.0
    if rc == BANDAGE:
        good = _sum("SELECT SUM(json_extract(extra_json,'$.rolls')) FROM proc_batches WHERE batch_no=? AND stage_code='CT'", (bn,))
        rej = _sum("SELECT SUM(qty_reject) FROM proc_batches WHERE batch_no=? AND stage_code IN ('BM','BW','BX')", (bn,))
        scrap = _proc_qty('BM', bn, 'qty_scrap')
        unit = 'رول'
    elif rc == SP and variant_for(w) == NON_STERILE:
        good = _sum("SELECT SUM(json_extract(extra_json,'$.pieces')) FROM proc_batches WHERE batch_no=? AND stage_code='SPK'", (bn,))
        rej = _proc_qty('SPK', bn, 'qty_reject')
        scrap = _proc_qty('SPK', bn, 'qty_scrap')
        unit = 'قطعة'
    else:
        good = _sum('SELECT SUM(env_good*per_envelope) FROM packaging WHERE batch_no=?', (bn,)) or \
            _sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,))
        rej = _sum('SELECT SUM(env_scrap*per_envelope) FROM packaging WHERE batch_no=?', (bn,))
        scrap = _sum('SELECT SUM(scrap) FROM folding_out WHERE batch_no=?', (bn,)) + \
            _sum('SELECT SUM(scrap) FROM sorting WHERE batch_no=?', (bn,))
        unit = 'قطعة'
    return dict(good=good, reject=rej, scrap=scrap, unit=unit)
