# -*- coding: utf-8 -*-
"""منصة مسارات التصنيع.

    المنتج (items.route_code) ← المسار (routes) ← خطوات المسار (route_steps)
                                  ← بوابات الجودة (qc_templates.mfg_routes / requires)
                                  ← حركات المخزون (inventory.stock_tx)

لا يوجد في أي شاشة `if product == ...`: الشاشات تسأل هذه الوحدة عن مسار الأمر وخطواته وتقدّمه.
إضافة مسار جديد = صفوف في routes/route_steps + (اختياريًا) معالج تقدّم جديد بـ @step.
"""
import json, math

import db, inventory

FULL, SP, BANDAGE = 'FULL_GAUZE', 'SP_GAUZE', 'BANDAGE'
STERILE, NON_STERILE = 'sterile', 'non_sterile'

# حالة الدفعة النهائية بعد إنهاء الإنتاج (work_orders.final_status)
FS_PENDING, FS_APPROVED, FS_STORED, FS_REJECTED, FS_HOLD = 'PENDING_QC', 'APPROVED', 'STORED', 'REJECTED', 'HOLD'
FS_AR = {FS_PENDING: 'بانتظار موافقة الجودة', FS_APPROVED: 'معتمدة للتخزين', FS_STORED: 'مخزّنة',
         FS_REJECTED: 'مرفوضة', FS_HOLD: 'معلّقة'}
FS_EN = {FS_PENDING: 'Pending Quality', FS_APPROVED: 'Approved for Storage', FS_STORED: 'Stored',
         FS_REJECTED: 'Rejected', FS_HOLD: 'Hold'}
FS_CLS = {FS_PENDING: 'warn', FS_APPROVED: 'ok', FS_STORED: 'ok', FS_REJECTED: 'bad', FS_HOLD: 'warn'}


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
    return variant_of(dict(w or {}).get('route'))


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
    new = bool((w or {}).get('final_status'))
    if rc == BANDAGE:
        return 'BW_OUT' if new else 'CT_OUT'
    if rc == SP:
        return 'SPK_OUT' if new or variant_for(w) == NON_STERILE else 'PACKED'
    if is_v15(w):
        return 'STR_OUT' if variant_for(w) == STERILE else 'NSP_OUT'
    return 'PROD_OUT' if new else None


def is_v15(w):
    """أمر شاش بسير v15: المخرج مسحات ويُحسب المطلوب بالمسحات."""
    return (w or {}).get('req_swabs') is not None


def is_v14(w):
    """أمر يعمل بسير v14 (خطة قص/موافقة جودة) لا بالمراحل القديمة."""
    if (w or {}).get('final_status'):
        return True
    return bool(db.one('SELECT 1 FROM cutting_plans WHERE batch_no=?', (w['batch_no'],))) or \
        bool(db.one('SELECT 1 FROM folding_out WHERE batch_no=? AND tag_no IS NOT NULL', (w['batch_no'],)))


def approval_of(bn):
    """آخر قرار جودة نهائي مسجّل للتشغيلة."""
    return db.one('SELECT * FROM approvals WHERE batch_no=? ORDER BY id DESC LIMIT 1', (bn,))


def final_release(bn):
    """سجل الإفراج النهائي للتشغيلة: موافقة الجودة (v14) أو شهادة إفراج/سجل QA قديم."""
    w = db.one('SELECT final_status, approved_at FROM work_orders WHERE batch_no=?', (bn,)) or {}
    if w.get('final_status') in (FS_APPROVED, FS_STORED):
        a = db.one("SELECT id, ts FROM approvals WHERE batch_no=? AND decision='Approved' ORDER BY id DESC LIMIT 1", (bn,)) or {}
        return dict(ref=f"APR-{a.get('id', 0):06d}", d=(a.get('ts') or w.get('approved_at') or '')[:10],
                    decision='مفرج عنها', kind='approval')
    if w.get('final_status'):
        return None                       # بانتظار الجودة أو مرفوضة/معلّقة
    r = db.one("SELECT release_no ref, issue_date d, decision FROM releases WHERE batch_no=? AND decision='مفرج عنها'", (bn,))
    if r:
        return dict(r, kind='cert')
    q = db.one("""SELECT r.rec_no ref, r.rec_date d, r.decision FROM qc_records r
                  JOIN qc_templates t ON t.code=r.template_code
                  WHERE t.is_final=1 AND r.batch_no=? AND r.decision='مفرج' ORDER BY r.id DESC LIMIT 1""", (bn,))
    return dict(q, kind='qa', decision='مفرج عنها') if q else None


def released_qty(bn):
    """(كمية مفرجة, وحدة, مرجع الإفراج) — بوحدة الإنتاج للأوامر الجديدة وبوحدة المخزن للسجلات القديمة."""
    fr = final_release(bn)
    if not fr:
        return 0.0, None, None
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    if fr['kind'] == 'approval':
        return float(w.get('approved_qty') or 0), (w.get('uom') if is_v15(w) else production_unit(w)), fr['ref']
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


def sr_executed(bn):
    """عدد السب رول الناتج عن خطط قص نُفّذت لهذا الأمر."""
    return int(_sum("""SELECT COUNT(*) FROM subrolls s JOIN cutting_plans p ON p.plan_no=s.plan_no
                       WHERE p.batch_no=? AND p.status='منفّذ'""", (bn,)))


@step('SLIT')
def _slit(w, bn):
    need, ex = _n(w.get('sr_needed')), sr_executed(bn)
    if need:
        return _frac(ex, need)
    return 1.0 if (ex or db.one('SELECT 1 FROM folding_in WHERE batch_no=?', (bn,))) else 0.0


@step('FOLD')
def _fold(w, bn):
    return _frac(_sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,)), _req_swabs(w))


def _req_swabs(w):
    return _n(w.get('req_swabs')) if w.get('req_swabs') is not None else _req(w)


@step('NSPACK')
def _nspack(w, bn):
    return _frac(_sum('SELECT SUM(swabs_in) FROM ns_packing WHERE batch_no=?', (bn,)), _req_swabs(w))


@step('SPREL')
def _sprel(w, bn):
    tot = _sum('SELECT SUM(qty) FROM intermediates WHERE batch_no=?', (bn,))
    ok = _sum("SELECT SUM(qty) FROM intermediates WHERE batch_no=? AND status IN ('Released for Sorting','Consumed')", (bn,))
    return _frac(ok, tot) if tot else 0.0


@step('STERPROC')
def _sterproc(w, bn):
    return _frac(_sum("""SELECT SUM(received_qty) FROM ster_records WHERE batch_no=? AND status NOT IN ('Sorting','Packaging','Boxing')""",
                      (bn,)), _req_swabs(w))


def _rec_frac(bn, statuses):
    tot = _sum('SELECT COUNT(*) FROM ster_records WHERE batch_no=?', (bn,))
    n = _sum(f"SELECT COUNT(*) FROM ster_records WHERE batch_no=? AND status IN ({','.join('?' * len(statuses))})",
             (bn, *statuses))
    return _frac(n, tot) if tot else 0.0


ST_AFTER_PRE = ('Released for Sterilization', 'In Sterilization', 'Sterilization Completed', 'Transferred to Aeration',
                'Aeration Completed', 'Pending Final QC Release')
ST_AFTER_STER = ('Sterilization Completed', 'Transferred to Aeration', 'Aeration Completed', 'Pending Final QC Release')
ST_AFTER_AER = ('Aeration Completed', 'Pending Final QC Release')


@step('PRESTER')
def _prester(w, bn):
    return _rec_frac(bn, ST_AFTER_PRE)


@step('STERIL')
def _steril(w, bn):
    return _rec_frac(bn, ST_AFTER_STER)


@step('AERATION')
def _aeration(w, bn):
    return _rec_frac(bn, ST_AFTER_AER)


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
        good = _proc_qty('BW', bn)
        rej = _sum("SELECT SUM(qty_reject) FROM proc_batches WHERE batch_no=? AND stage_code IN ('BM','BW','BX')", (bn,))
        scrap = _proc_qty('BM', bn, 'qty_scrap')
        unit = 'رول'
    elif rc == SP:
        good = _sum("SELECT SUM(json_extract(extra_json,'$.pieces')) FROM proc_batches WHERE batch_no=? AND stage_code='SPK'", (bn,))
        rej = _proc_qty('SPK', bn, 'qty_reject')
        scrap = _proc_qty('SPK', bn, 'qty_scrap')
        unit = 'قطعة'
    elif is_v15(w):
        good = _sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,))
        rej = _sum('SELECT SUM(rejected_qty) FROM folding_out WHERE batch_no=?', (bn,))
        scrap = _sum('SELECT SUM(waste_qty) FROM folding_out WHERE batch_no=?', (bn,))
        unit = 'مسحة'
    else:
        good = _sum('SELECT SUM(env_good*per_envelope) FROM packaging WHERE batch_no=?', (bn,)) or \
            _sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,))
        rej = _sum('SELECT SUM(env_scrap*per_envelope) FROM packaging WHERE batch_no=?', (bn,))
        scrap = _sum('SELECT SUM(scrap) FROM folding_out WHERE batch_no=?', (bn,)) + \
            _sum('SELECT SUM(scrap) FROM sorting WHERE batch_no=?', (bn,))
        unit = 'قطعة'
    return dict(good=good, reject=rej, scrap=scrap, unit=unit)


# ------------------------------------------------------------------ v15: المنتج يقود كل شيء (الوحدة الأساسية: المسحة)
SWAB = 'مسحة'


def _setting_f(key, default):
    try:
        return float(db.setting(key, str(default)))
    except (TypeError, ValueError):
        return float(default)


def norm_unit(u):
    """توحيد اسم الوحدة: باك/باكت/pack ← pack."""
    u = (u or '').strip()
    return {'باك': 'pack', 'باكت': 'pack', 'pack': 'pack', 'بوكس': 'box', 'box': 'box', 'كرتون': 'carton',
            'carton': 'carton', 'مسحة': 'swab', 'swab': 'swab', 'قطعة': 'swab', 'مغلف': 'envelope'}.get(u, u)


def production_unit(w):
    """وحدة مخرج الإنتاج: المسحة لأوامر الشاش الكامل (v15)؛ وللخطوط الأخرى وحدة المسار."""
    w = w or {}
    if route_of_wo(w) == FULL and is_v15(w):
        return SWAB
    if w.get('uom'):
        return w['uom']
    it = db.one('SELECT uom FROM items WHERE item_code=?', (w.get('item_code'),)) or {}
    return it.get('uom') or (route(route_of_wo(w)) or {}).get('base_uom') or 'قطعة'


def pack_spec(item_code):
    return db.one('SELECT * FROM pack_spec WHERE item_code=?', (item_code,)) or {}


def swabs_per_unit(item):
    """(عدد المسحات في الوحدة التجارية, اسم الوحدة): غير معقم = باكت (100)، معقم = بوكس (100) من Packaging Configuration."""
    ps = pack_spec(item['item_code'])
    if variant_of(item.get('sterile')) == STERILE:
        return ps.get('swabs_per_box'), 'بوكس'
    return ps.get('swabs_per_pack'), 'باكت'


def item_sr_width(item):
    """(عرض السب رول سم, المصدر) — من بيانات المنتج وإلا مصفوفة القص (ماكينة × طبقات)."""
    if item.get('sub_roll_width_cm'):
        w = float(item['sub_roll_width_cm'])
        snap = _snap_to_master(item, w)
        if snap is not None and snap != w:
            return snap, f'Master Data (اعتُمد الأصغر: {snap:g} بدل {w:g})'
        return w, 'بيانات المنتج'
    if item.get('machine_code') and item.get('ply'):
        m = db.one('SELECT std_width_cm FROM slit_matrix WHERE key=?', (f"{item['machine_code']}-{item['ply']}",))
        if m:
            w = float(m['std_width_cm'])
            snap = _snap_to_master(item, w)
            if snap is not None and snap != w:
                return snap, f'Master Data (اعتُمد الأصغر: {snap:g} بدل {w:g} من مصفوفة القص)'
            return w, 'مصفوفة القص'
    return None, None


SNAP_TOL_CM = 2.0


def _sr_master_width(r):
    w = r.get('ref_width_cm')
    if w is None:
        import migrate_v15
        w = migrate_v15.width_from_text(r.get('width_cm'), r.get('description'))
    return float(w) if w is not None else None


def _snap_to_master(item, w):
    """عند اختلاف مصفوفة القص عن عرض Sub Roll في Excel: يُعتمد الرقم الأصغر الأقرب (≤ العرض وضمن السماحية) من Master Data."""
    best = None
    for r in db.q("SELECT * FROM items WHERE prefix='SR' AND status='نشط'"):
        if (r.get('mesh') or None) != (item.get('mesh') or None) or (r.get('xray') or None) != (item.get('xray') or None):
            continue
        rw = _sr_master_width(r)
        if rw is None:
            continue
        if abs(rw - w) < 0.01:
            return w
        if rw < w and w - rw <= SNAP_TOL_CM and (best is None or rw > best):
            best = rw
    return best


def ref_length_of(item):
    """الطول المرجعي للرول من Master Data (خامة المنتج) وإلا الإعداد الافتراضي."""
    raw = db.one('SELECT length_m FROM items WHERE item_code=?', (item.get('raw_item'),)) if item.get('raw_item') else None
    try:
        v = float(str(raw['length_m']).lower().replace('m', '')) if raw and raw['length_m'] not in (None, '') else None
    except ValueError:
        v = None
    return v or _setting_f('default_sr_length_m', 2000)


def theoretical_swabs(machine, std_width, length_m, actual_width=None):
    """الكمية النظرية من سب رول: مساحته الفعلية ÷ استهلاك المسحة القياسي (عرض السب رول القياسي × طول القطعة للماكينة)."""
    import forms
    cl = forms.cut_length(machine)
    if not (cl and std_width and length_m):
        return None
    w = float(actual_width or std_width)
    return int((w * float(length_m) * 100.0) / (float(std_width) * cl) + 1e-9)


def item_yield(item, length_m=None):
    """(إنتاجية السب رول بالمسحات, المصدر): تجاوز يدوي في Master Data وإلا نظري من الماكينة والطول."""
    if item.get('yield_per_sr'):
        return float(item['yield_per_sr']), 'بيانات المنتج'
    srw, _ = item_sr_width(item)
    y = theoretical_swabs(item.get('machine_code'), srw, length_m or ref_length_of(item))
    return (float(y), 'محسوب من الماكينة وطول الرول') if y else (None, None)


def match_subrolls(item):
    """أكواد Sub Roll المطابقة لمواصفات المنتج من Master Data (العرض + Plain/X-Ray + Mesh). بلا تخمين ولا توليد."""
    srw, _ = item_sr_width(item)
    if not srw:
        return []
    out = []
    for r in db.q("SELECT * FROM items WHERE prefix='SR' AND status='نشط' ORDER BY item_code"):
        w = r.get('ref_width_cm')
        if w is None:
            import migrate_v15
            w = migrate_v15.width_from_text(r.get('width_cm'), r.get('description'))
        if w is not None and abs(float(w) - srw) < 0.01 and (r.get('mesh') or None) == (item.get('mesh') or None) \
                and (r.get('xray') or None) == (item.get('xray') or None):
            out.append(r)
    return out


def requirements(item, qty, jumbo_width=None, jumbo_length=None):
    """احتياج الخامة — تلقائي من بيانات المنتج (الكمية بالوحدة التجارية ← مسحات ← سب رول ← جامبو):

        المسحات المطلوبة  = الكمية × مسحات الوحدة التجارية        (باكت أو بوكس = 100)
        نظري/سب رول      = طول السب رول × عرضه ÷ استهلاك المسحة    (5,000 لسب رول 2000 م على ماكينة 10)
        العرض المفيد     = عرض الجامبو − 2 × تنظيف الطرف           (120 − 1 − 1 = 118)
        سب رول/جامبو     = ⌊العرض المفيد ÷ عرض السب رول⌋
        سب رول مطلوب     = ⌈المسحات ÷ النظري⌉ ،  جامبو مطلوب = ⌈سب رول ÷ سب رول/جامبو⌉
    """
    miss = []
    srw, srw_src = item_sr_width(item)
    spu, cunit = swabs_per_unit(item)
    length = float(jumbo_length) if jumbo_length else ref_length_of(item)
    yld, y_src = item_yield(item, length)
    if not srw:
        miss.append('عرض السب رول')
    if not yld:
        miss.append('الكمية النظرية للسب رول (ماكينة الطي/الطول)')
    if not spu:
        miss.append(f'عدد المسحات في {cunit} (Packaging Configuration)')
    jw = float(jumbo_width) if jumbo_width else _setting_f('default_jumbo_width_cm', 120)
    edge = _setting_f('slitter_edge_trim_each_cm', 1)
    usable = round(max(jw - 2 * edge, 0), 2)
    per = int(usable // srw + 1e-9) if srw else 0
    if srw and per < 1:
        miss.append('عرض الجامبو لا يتسع لسب رول واحد')
    qty = float(qty or 0)
    swabs = qty * float(spu) if spu else None
    sr_need = math.ceil(swabs / yld - 1e-9) if (yld and swabs) else None
    jumbo_need = math.ceil(sr_need / per) if (sr_need and per) else None
    m = match_subrolls(item)
    return dict(ok=not miss, missing=miss, sr_width=srw, sr_width_src=srw_src, yield_per_sr=yld, yield_src=y_src,
                jumbo_width=jw, edge_each=edge, usable_width=usable, per_jumbo=per, sr_needed=sr_need,
                jumbo_needed=jumbo_need, raw_item=item.get('raw_item'), swabs_needed=swabs, swabs_per_unit=spu,
                commercial_unit=cunit, sr_matches=[r['item_code'] for r in m],
                trim_waste=round(usable - per * srw, 2) if srw else None, unit=SWAB)


def spec_lines(item):
    """Packaging Configuration كنص مقروء للصنف (تحذير عند غياب قيمة)."""
    ps = pack_spec(item['item_code'])
    f = lambda v: (f'{v:g}' if v is not None else '—')                    # noqa: E731
    if variant_of(item.get('sterile')) == STERILE:
        ppe, spb = ps.get('swabs_per_envelope'), ps.get('swabs_per_box')
        epb = (spb / ppe) if (ppe and spb) else None
        return [f'{f(ppe)} مسحة = 1 مغلف', f'{f(spb)} مسحة = 1 بوكس ({f(epb)} مغلف)',
                f'{f(ps.get("boxes_per_carton"))} بوكس = 1 كرتون']
    return [f'{f(ps.get("swabs_per_pack"))} مسحة = 1 باكت', f'{f(ps.get("packs_per_carton"))} باكت = 1 كرتون']


def equiv_from_swabs(item, swabs):
    """المكافئ التلقائي لعدد مسحات في وحدات التعبئة (يتخطى ما لم يُعرَّف)."""
    ps = pack_spec(item['item_code'])
    out = []
    sw = float(swabs or 0)
    if variant_of(item.get('sterile')) == STERILE:
        if ps.get('swabs_per_envelope'):
            out.append(dict(unit='مغلف', qty=round(sw / ps['swabs_per_envelope'], 2)))
        if ps.get('swabs_per_box'):
            boxes = sw / ps['swabs_per_box']
            out.append(dict(unit='بوكس', qty=round(boxes, 2)))
            if ps.get('boxes_per_carton'):
                out.append(dict(unit='كرتون', qty=round(boxes / ps['boxes_per_carton'], 2)))
    elif ps.get('swabs_per_pack'):
        packs = sw / ps['swabs_per_pack']
        out.append(dict(unit='باكت', qty=round(packs, 2)))
        if ps.get('packs_per_carton'):
            out.append(dict(unit='كرتون', qty=round(packs / ps['packs_per_carton'], 2)))
    return out


def pack_equiv(item_code, qty, unit):
    """المكافئ لخطوط SP والرباط من pack_config."""
    levels = pack_levels(item_code)
    start = 0
    for lv in levels:
        if norm_unit(lv['unit_ar']) == norm_unit(unit):
            start = lv['level']
    out, cur = [], float(qty or 0)
    for lv in levels:
        if lv['level'] > start:
            cur = cur / lv['per_parent']
            out.append(dict(unit=lv['unit_ar'], qty=round(cur, 2)))
    return out


def _pack_text(levels, rc):
    lower = 'رول' if rc == BANDAGE else 'قطعة'
    out = []
    for x in levels:
        per = x['per_parent']
        per = int(per) if float(per) == int(per) else per
        out.append(dict(unit=x['unit_ar'], per=x['per_parent'], text=f"{per:,} {lower} = 1 {x['unit_ar']}"))
        lower = x['unit_ar']
    return out


def product_card(item, qty=None, jumbo_width=None):
    """كل ما يعرفه النظام عن المنتج تلقائيًا — تعرضه شاشات الأوامر فلا يدخله أحد."""
    rc = item.get('route_code')
    r = route(rc) or {}
    var = variant_of(item.get('sterile'))
    steps = [s['name_ar'] for s in steps_for(rc, var)] if rc else []
    pm = pack_levels(item['item_code'])
    ps = pack_spec(item['item_code'])
    spu, cunit = swabs_per_unit(item) if rc == FULL else (None, item.get('uom'))
    unit = cunit if rc == FULL else (item.get('uom') or r.get('base_uom') or 'قطعة')
    card = dict(item_code=item['item_code'], description=item.get('description'), size=item.get('size'),
                ply=item.get('ply'), xray=item.get('xray'), mesh=item.get('mesh'), sterile=item.get('sterile'),
                route_code=rc, route_name=r.get('name_ar'), route_en=r.get('name_en'), steps=steps, unit=unit,
                machine=item.get('machine_code'),
                machine_name=(db.one('SELECT name FROM machines WHERE machine_code=?', (item.get('machine_code'),)) or {}).get('name'),
                pack_code=ps.get('pack_code') or item.get('pack_code'), box_code=ps.get('box_code') or item.get('box_code'),
                env_code=ps.get('env_code'), carton_code=ps.get('carton_code') or item.get('master_box'),
                pack_config=([dict(unit='', per=0, text=s_) for s_ in spec_lines(item)] if rc == FULL
                             else _pack_text(pm, rc)),
                raw_item=item.get('raw_item'), variant=var, sp_item=item.get('sp_item'))
    if rc == FULL:
        card['req'] = requirements(item, qty, jumbo_width)
        card['swab_unit'] = SWAB
        if qty and spu:
            card['equiv'] = equiv_from_swabs(item, float(qty) * spu)
    elif qty:
        card['equiv'] = pack_equiv(item['item_code'], qty, unit)
    return card


def produced(bn, w=None):
    """(الكمية المنتجة الفعلية بمخرج الإنتاج, وحدته) — مسحات للشاش الكامل."""
    w = w or db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    return output(bn)['good'], production_unit(w)


def fg_qty(w):
    """(كمية المنتج النهائي بالوحدة التجارية, وحدتها): باكت لغير المعقم، بوكس للمعقم — من سندات التعبئة/التغليف الفعلية."""
    bn = w['batch_no']
    if variant_for(w) == STERILE:
        return _sum('SELECT SUM(boxes_actual) FROM ster_records WHERE batch_no=?', (bn,)), 'بوكس'
    return _sum('SELECT SUM(packs_actual) FROM ns_packing WHERE batch_no=?', (bn,)), 'باكت'


def subroll_fits(w, sr):
    """(ok, سبب) — هل يصلح السب رول لأمر إنتاج؟ سب رول الأمر نفسه دائمًا؛ وغيره بشرط تطابق الماكينة والطبقات
    والعرض والكاشف والميش. يُستخدم في القائمة وفي التحقق عند الحفظ/التعديل."""
    import forms
    if sr.get('batch_no') == w['batch_no']:
        return True, ''
    sw = _n(w.get('std_width_cm'))
    if sw and sr.get('width_cm') and abs(_n(sr['width_cm']) - sw) > 0.01:
        return False, (f"عرض السب رول {sr['tag_no']} ({_n(sr['width_cm']):g} سم) لا يطابق عرض المنتج ({sw:g} سم)")
    return forms.sr_matches(w, sr)


def avail_subrolls(w):
    """السب رول المتاح للطي في أمر: سب رول الأمر نفسه أولًا ثم المتبقي المطابق لمواصفاته من أوامر أخرى."""
    bn = w['batch_no']
    rows = db.q("""SELECT * FROM subrolls WHERE stock_status='متاح'
                   AND (batch_no=? OR (dest_machine=? AND IFNULL(ply,0)=?))
                   ORDER BY (IFNULL(batch_no,'')<>?), tag_no""", (bn, w.get('fold_machine'), w.get('ply') or 0, bn))
    return [r for r in rows if subroll_fits(w, r)[0]]


# مراحل الأمر كما يراها المستخدم (المسار والمعقم/غير المعقم يحددان الخطوات)
STAGE_UI = {
    'new':             ('جديد', 'New', ''),
    'slitting':        ('في السليتر', 'In Slitting', 'info'),
    'ready_fold':      ('جاهز للطي', 'Ready for Folding', 'info'),
    'production':      ('قيد الطي', 'In Folding', 'warn'),
    'packing':         ('بانتظار التعبئة', 'Pending Packing', 'warn'),
    'ready_finish':    ('جاهز للإنهاء', 'Ready to Finish', 'info'),
    'sp_release':      ('بانتظار إفراج SP', 'Pending SP Release', 'warn'),
    'processing':      ('فرز / عد / تغليف', 'Sorting & Packaging', 'info'),
    'pre_ster_qc':     ('بانتظار إفراج قبل التعقيم', 'Pending Pre-Sterilization Release', 'warn'),
    'ready_ster':      ('جاهز للتعقيم', 'Ready for Sterilization', 'info'),
    'sterilizing':     ('في التعقيم', 'In Sterilization', 'info'),
    'aeration':        ('في التهوية', 'In Aeration', 'info'),
    'pending_quality': ('بانتظار الإفراج النهائي', 'Pending Final QC Release', 'warn'),
    'approved':        ('معتمد للتخزين', 'Approved for Storage', 'ok'),
    'stored':          ('مكتمل — مخزّن', 'Stored / Completed', 'ok'),
    'rejected':        ('مرفوض', 'Rejected', 'bad'),
    'hold':            ('معلّق', 'Hold', 'warn'),
    'closed':          ('مغلق', 'Closed', ''),
}
STER_RANK = ['sp_release', 'processing', 'pre_ster_qc', 'ready_ster', 'sterilizing', 'aeration', 'pending_quality']
REC_STAGE = {'Sorting': 'processing', 'Packaging': 'processing', 'Boxing': 'processing',
             'Ready for Pre-Sterilization QC': 'pre_ster_qc', 'Pre-Sterilization Hold': 'pre_ster_qc',
             'Released for Sterilization': 'ready_ster', 'In Sterilization': 'sterilizing',
             'Sterilization Completed': 'aeration', 'Transferred to Aeration': 'aeration',
             'Aeration Completed': 'pending_quality', 'Pending Final QC Release': 'pending_quality'}


def order_state(w):
    """حالة أمر الإنتاج وإجراؤه التالي — مصدر واحد للوحة والقوائم وزر «استكمال العمل»."""
    if isinstance(w, str):
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (w,))
    bn, rc = w['batch_no'], route_of_wo(w)
    v15 = rc == FULL and is_v15(w)
    req = _req_swabs(w) if v15 else _n(w.get('qty_required'))
    prod, unit = produced(bn, w)
    fs = w.get('final_status')
    eps = 1e-6
    st = dict(batch_no=bn, route=rc, required=req, produced=prod, remaining=max(req - prod, 0), unit=unit,
              final_status=fs, plans=0, sr_executed=0, sr_available=0, docs=0, next=None, can_finish=False,
              fg_qty=0.0, fg_unit=w.get('uom'), v15=v15, sterile=variant_for(w) == STERILE, packed_swabs=0.0,
              unpacked=0.0)
    docs = int(_sum('SELECT COUNT(*) FROM folding_out WHERE batch_no=?', (bn,)))
    W = lambda label, ep, kind='work': dict(label=label, endpoint=ep, kind=kind)          # noqa: E731
    if rc == FULL:
        plans = db.q('SELECT status FROM cutting_plans WHERE batch_no=?', (bn,))
        ex = sr_executed(bn)
        avail = len(avail_subrolls(w)) if not fs else 0
        need = int(_n(w.get('sr_needed')))
        st.update(plans=len(plans), sr_executed=ex, sr_available=avail, docs=docs, sr_needed=need)
        fold_more = st['remaining'] > eps
        if v15:
            st['fg_qty'], st['fg_unit'] = fg_qty(w)
        if not docs:
            key = 'new' if not plans else ('ready_fold' if ex >= max(need, 1) else 'slitting')
            nxt = W('ابدأ خطة القص', 'slit_work') if key == 'new' else (
                W('استكمال القص', 'slit_work') if key == 'slitting' else W('ابدأ الطي', 'fold_work'))
            can_finish = False
        elif not v15:
            key = 'production'
            nxt = W('إنهاء الإنتاج وإرساله للجودة', 'order_view', 'finish') if not fold_more else (
                W('تسجيل سند طي', 'fold_work') if avail else W('استكمال القص', 'slit_work'))
            can_finish = docs > 0
        elif not st['sterile']:
            packed = _sum('SELECT SUM(swabs_in) FROM ns_packing WHERE batch_no=?', (bn,))
            folded = prod
            st.update(packed_swabs=packed, unpacked=max(folded - packed, 0))
            more_sr = fold_more and (avail or ex < need)
            if more_sr:
                key = 'production'
                nxt = W('تسجيل سند طي', 'fold_work') if avail else W('استكمال القص', 'slit_work')
            elif st['unpacked'] > eps:
                key, nxt = 'packing', W('تعبئة (باكت / كرتون)', 'ns_pack')
            else:
                key, nxt = 'ready_finish', W('إنهاء الإنتاج وإرساله للجودة', 'order_view', 'finish')
            can_finish = st['fg_qty'] > eps
        else:
            recs = db.q('SELECT status FROM ster_records WHERE batch_no=?', (bn,))
            inter = db.q('SELECT status FROM intermediates WHERE batch_no=? AND rec_no IS NULL', (bn,))
            inter_all = {r['status'] for r in inter}
            stages = {REC_STAGE.get(r['status'], 'processing') for r in recs}
            if 'Pending SP Release' in inter_all:
                stages.add('sp_release')
            if 'Released for Sorting' in inter_all:
                stages.add('processing')
            more_sr = fold_more and (avail or ex < need)
            if more_sr:
                key = 'production'
                nxt = W('تسجيل سند طي', 'fold_work') if avail else W('استكمال القص', 'slit_work')
            elif stages:
                key = min(stages, key=lambda s: STER_RANK.index(s))
                nxt = {'sp_release': dict(label='بانتظار إفراج SP من الجودة', endpoint=None, kind='wait'),
                       'processing': W('الفرز والعد والتغليف', 'ster_order'),
                       'pre_ster_qc': dict(label='بانتظار الإفراج قبل التعقيم', endpoint=None, kind='wait'),
                       'ready_ster': W('إنشاء دورة تعقيم', 'ster_cycle_new'),
                       'sterilizing': W('متابعة دورة التعقيم', 'ster_cycles'),
                       'aeration': W('متابعة التهوية', 'ster_cycles'),
                       'pending_quality': dict(label='بانتظار الإفراج النهائي', endpoint=None, kind='wait')}[key]
            else:
                key, nxt = 'production', W('تسجيل سند طي', 'fold_work')
            can_finish = False
    else:
        p = progress(bn)
        ops = [x for x in p['steps'] if x['code'] not in ('FINALQC', 'WH')]
        first_open = next((x for x in ops if x['frac'] < 1), None)
        started = prod > 0 or any(x['frac'] > 0 for x in ops)
        key = 'production' if started else 'new'
        nxt = W(first_open['name'], first_open['endpoint']) if first_open else W('إنهاء الإنتاج وإرساله للجودة', 'order_view', 'finish')
        can_finish = prod > eps
        st['docs'] = docs
    if fs == FS_PENDING:
        key, nxt = 'pending_quality', dict(label='بانتظار الإفراج النهائي', endpoint=None, kind='wait')
    elif fs == FS_APPROVED:
        key, nxt = 'approved', dict(label='بانتظار التخزين', endpoint='warehouse', kind='store')
    elif fs == FS_STORED:
        key, nxt = 'stored', dict(label='مكتمل', endpoint=None, kind='done')
    elif fs == FS_REJECTED:
        key, nxt = 'rejected', dict(label='مرفوض من الجودة', endpoint=None, kind='wait')
    elif fs == FS_HOLD:
        key, nxt = 'hold', dict(label='معلّق لدى الجودة', endpoint=None, kind='wait')
    elif w.get('status') == 'مغلقة':
        key, nxt = 'closed', dict(label='الأمر مغلق', endpoint=None, kind='done')
    ar, en, cls = STAGE_UI[key]
    if v15 and st['sterile'] and not fs:
        can_finish = False                                   # المعقم: الإفراج النهائي يُطلب آليًا بعد التهوية
    st.update(stage=key, stage_ar=ar, stage_en=en, cls=cls, next=nxt, can_finish=can_finish and not fs)
    return st


def alloc_proc_no(con, bn, stage):
    """يحجز رقم سند مرحلة داخل التعامل نفسه من عدّاد لا يتراجع — لا يُعاد استعمال رقم سند محذوف."""
    import re
    top = 0
    for r in con.execute("SELECT proc_no FROM proc_batches WHERE batch_no=? AND stage_code=?", (bn, stage)).fetchall():
        m = re.search(r'(\d+)$', r['proc_no'] or '')
        if m:
            top = max(top, int(m.group(1)))
    scope = f'PROC:{bn}:{stage}'
    con.execute('INSERT OR IGNORE INTO counters(scope,n) VALUES(?,0)', (scope,))
    con.execute('UPDATE counters SET n=? WHERE scope=? AND n<?', (top, scope, top))
    return db.alloc(con, scope, f'{bn}/{stage}' + '{n:02d}')
