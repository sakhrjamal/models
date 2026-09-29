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
    new = bool((w or {}).get('final_status'))
    if rc == BANDAGE:
        return 'BW_OUT' if new else 'CT_OUT'
    if rc == SP:
        return 'SPK_OUT' if new or variant_for(w) == NON_STERILE else 'PACKED'
    return 'PROD_OUT' if new else None   # الشاش الكامل: قيد PROD_OUT لكل سند طي (v14)


# ------------------------------------------------------------------ المنتج التام / الإفراج
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
        return float(w.get('approved_qty') or 0), production_unit(w), fr['ref']
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
    else:
        good = _sum('SELECT SUM(env_good*per_envelope) FROM packaging WHERE batch_no=?', (bn,)) or \
            _sum('SELECT SUM(qty_good) FROM folding_out WHERE batch_no=?', (bn,))
        rej = _sum('SELECT SUM(env_scrap*per_envelope) FROM packaging WHERE batch_no=?', (bn,))
        scrap = _sum('SELECT SUM(scrap) FROM folding_out WHERE batch_no=?', (bn,)) + \
            _sum('SELECT SUM(scrap) FROM sorting WHERE batch_no=?', (bn,))
        unit = 'قطعة'
    return dict(good=good, reject=rej, scrap=scrap, unit=unit)


# ------------------------------------------------------------------ v14: المنتج يقود كل شيء
def _setting_f(key, default):
    try:
        return float(db.setting(key, str(default)))
    except (TypeError, ValueError):
        return float(default)


def norm_unit(u):
    """توحيد اسم الوحدة: باك/باكت/pack ← pack."""
    u = (u or '').strip()
    return {'باك': 'pack', 'باكت': 'pack', 'pack': 'pack', 'بوكس': 'box', 'box': 'box', 'كرتون': 'carton',
            'carton': 'carton'}.get(u, u)


def production_unit(w):
    """وحدة إنتاج الأمر: من بيانات المنتج (باكت لـ GS310M) وإلا وحدة المسار الأساسية."""
    w = w or {}
    if w.get('uom'):
        return w['uom']
    it = db.one('SELECT uom FROM items WHERE item_code=?', (w.get('item_code'),)) or {}
    return it.get('uom') or (route(route_of_wo(w)) or {}).get('base_uom') or 'قطعة'


def item_sr_width(item):
    """(عرض السب رول سم, المصدر) — من بيانات المنتج وإلا مصفوفة القص (ماكينة × طبقات)."""
    if item.get('sub_roll_width_cm'):
        return float(item['sub_roll_width_cm']), 'بيانات المنتج'
    if item.get('machine_code') and item.get('ply'):
        m = db.one('SELECT std_width_cm FROM slit_matrix WHERE key=?', (f"{item['machine_code']}-{item['ply']}",))
        if m:
            return float(m['std_width_cm']), 'مصفوفة القص'
    return None, None


def item_yield(item):
    """(إنتاجية السب رول بوحدة إنتاج الصنف, المصدر) — القيمة المعيارية أو حساب هندسي من الماكينة."""
    if item.get('yield_per_sr'):
        return float(item['yield_per_sr']), 'بيانات المنتج'
    if item.get('machine_code') and item.get('ply'):
        import forms
        y = forms.sr_yield(f"{item['machine_code']}-{item['ply']}", _setting_f('default_sr_length_m', 2000))
        if y:
            pm = pack_map(item['item_code'])
            if norm_unit(item.get('uom')) == 'pack' and 'pack' in pm:
                y = y / pm['pack']['per_parent']
            return float(y), 'محسوب من الماكينة'
    return None, None


def requirements(item, qty, jumbo_width=None):
    """احتياج الخامة: تلقائي 100% من بيانات المنتج والإعدادات — لا يُدخله المستخدم.

        العرض المفيد     = عرض الجامبو − تنظيف الطرف الأيسر − تنظيف الطرف الأيمن   (120 − 1 − 1 = 118)
        سب رول لكل جامبو = ⌊العرض المفيد ÷ عرض السب رول⌋                           (⌊118 ÷ 23⌋ = 5)
        سب رول مطلوب    = ⌈الكمية ÷ إنتاجية السب رول⌉                              (⌈25000 ÷ 5000⌉ = 5)
        جامبو مطلوب     = ⌈السب رول المطلوب ÷ سب رول لكل جامبو⌉                     (⌈5 ÷ 5⌉ = 1)
    """
    miss = []
    srw, srw_src = item_sr_width(item)
    yld, y_src = item_yield(item)
    if not srw:
        miss.append('عرض السب رول')
    if not yld:
        miss.append('إنتاجية السب رول')
    jw = float(jumbo_width) if jumbo_width else _setting_f('default_jumbo_width_cm', 120)
    edge = _setting_f('slitter_edge_trim_each_cm', 1)
    usable = round(max(jw - 2 * edge, 0), 2)
    per = int(usable // srw + 1e-9) if srw else 0
    if srw and per < 1:
        miss.append('عرض الجامبو لا يتسع لسب رول واحد')
    qty = float(qty or 0)
    sr_need = math.ceil(qty / yld - 1e-9) if (yld and qty > 0) else None
    jumbo_need = math.ceil(sr_need / per) if (sr_need and per) else None
    return dict(ok=not miss, missing=miss, sr_width=srw, sr_width_src=srw_src, yield_per_sr=yld, yield_src=y_src,
                jumbo_width=jw, edge_each=edge, usable_width=usable, per_jumbo=per, sr_needed=sr_need,
                jumbo_needed=jumbo_need, raw_item=item.get('raw_item'), trim_waste=round(usable - per * srw, 2) if srw else None,
                unit=item.get('uom') or 'قطعة')


def pack_equiv(item_code, qty, unit):
    """المكافئ التلقائي للكمية في وحدات التعبئة الأعلى (بوكس / كرتون) من تكوين تعبئة الصنف."""
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
    """تكوين التعبئة كنص مقروء: «100 قطعة = 1 باكت · 20 باكت = 1 بوكس · 10 بوكس = 1 كرتون»."""
    lower = 'رول' if rc == BANDAGE else 'قطعة'
    out = []
    for x in levels:
        per = x['per_parent']
        per = int(per) if float(per) == int(per) else per
        out.append(dict(unit=x['unit_ar'], per=x['per_parent'], text=f"{per:,} {lower} = 1 {x['unit_ar']}"))
        lower = x['unit_ar']
    return out


def product_card(item, qty=None, jumbo_width=None):
    """كل ما يعرفه النظام عن المنتج تلقائيًا — تعرضه شاشة أمر الإنتاج فلا يدخله أحد."""
    rc = item.get('route_code')
    r = route(rc) or {}
    var = variant_of(item.get('sterile'))
    steps = [s['name_ar'] for s in steps_for(rc, var)] if rc else []
    pm = pack_levels(item['item_code'])
    unit = item.get('uom') or r.get('base_uom') or 'قطعة'
    card = dict(item_code=item['item_code'], description=item.get('description'), size=item.get('size'),
                ply=item.get('ply'), xray=item.get('xray'), mesh=item.get('mesh'), sterile=item.get('sterile'),
                route_code=rc, route_name=r.get('name_ar'), route_en=r.get('name_en'), steps=steps, unit=unit,
                machine=item.get('machine_code'),
                machine_name=(db.one('SELECT name FROM machines WHERE machine_code=?', (item.get('machine_code'),)) or {}).get('name'),
                pack_code=item.get('pack_code'), box_code=item.get('box_code'), carton_code=item.get('master_box'),
                pack_config=_pack_text(pm, rc),
                raw_item=item.get('raw_item'), variant=var)
    if rc == FULL:
        card['req'] = requirements(item, qty, jumbo_width)
    if qty:
        card['equiv'] = pack_equiv(item['item_code'], qty, unit)
    return card


def produced(bn, w=None):
    """(الكمية المنتجة الفعلية, وحدة الإنتاج) — تُحسب من السندات دائمًا ولا تُخزَّن يدويًا."""
    w = w or db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    return output(bn)['good'], production_unit(w)


def subroll_fits(w, sr):
    """(ok, سبب) — هل يصلح السب رول لأمر إنتاج؟ سب رول الأمر نفسه دائمًا؛ وغيره بشرط تطابق الماكينة والطبقات
    والعرض (عرض السب رول = عرض المنتج) والكاشف والميش. يُستخدم في القائمة وفي التحقق عند الحفظ/التعديل."""
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


# مراحل الأمر كما يراها المستخدم (لا مراحل إضافية للمعقم/غير المعقم — المسار وحده يحدد الخطوات)
STAGE_UI = {
    'new':             ('جديد', 'New', ''),
    'slitting':        ('في السليتر', 'In Slitting', 'info'),
    'ready_fold':      ('جاهز للطي', 'Ready for Folding', 'info'),
    'production':      ('قيد الإنتاج', 'In Production', 'warn'),
    'pending_quality': ('بانتظار الجودة', 'Pending Quality', 'warn'),
    'approved':        ('معتمد للتخزين', 'Approved for Storage', 'ok'),
    'stored':          ('مكتمل — مخزّن', 'Stored / Completed', 'ok'),
    'rejected':        ('مرفوض', 'Rejected', 'bad'),
    'hold':            ('معلّق', 'Hold', 'warn'),
    'closed':          ('مغلق', 'Closed', ''),
}


def order_state(w):
    """حالة أمر الإنتاج وإجراؤه التالي — مصدر واحد للوحة والقوائم وزر «استكمال العمل»."""
    if isinstance(w, str):
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (w,))
    bn, rc = w['batch_no'], route_of_wo(w)
    req = _n(w.get('qty_required'))
    prod, unit = produced(bn, w)
    fs = w.get('final_status')
    eps = 1e-6
    st = dict(batch_no=bn, route=rc, required=req, produced=prod, remaining=max(req - prod, 0), unit=unit,
              final_status=fs, plans=0, sr_executed=0, sr_available=0, docs=0, next=None, can_finish=False)
    docs = int(_sum('SELECT COUNT(*) FROM folding_out WHERE batch_no=?', (bn,)))
    if rc == FULL:
        plans = db.q('SELECT status FROM cutting_plans WHERE batch_no=?', (bn,))
        ex = sr_executed(bn)
        avail = len(avail_subrolls(w)) if not fs else 0
        need = int(_n(w.get('sr_needed')))
        st.update(plans=len(plans), sr_executed=ex, sr_available=avail, docs=docs, sr_needed=need)
        if docs:
            key = 'production'
        elif not plans:
            key = 'new'
        elif ex >= max(need, 1):
            key = 'ready_fold'
        else:
            key = 'slitting'
        if key == 'new':
            nxt = dict(label='ابدأ خطة القص', endpoint='slit_work', kind='work')
        elif key == 'slitting':
            nxt = dict(label='استكمال القص', endpoint='slit_work', kind='work')
        elif key == 'ready_fold':
            nxt = dict(label='ابدأ الطي', endpoint='fold_work', kind='work')
        elif st['remaining'] <= eps:
            nxt = dict(label='إنهاء الإنتاج وإرساله للجودة', endpoint='order_view', kind='finish')
        elif avail:
            nxt = dict(label='تسجيل سند طي', endpoint='fold_work', kind='work')
        elif ex < need:
            nxt = dict(label='استكمال القص', endpoint='slit_work', kind='work')
        else:
            nxt = dict(label='إنهاء الإنتاج بالكمية الفعلية', endpoint='order_view', kind='finish')
        can_finish = docs > 0
    else:
        p = progress(bn)
        ops = [x for x in p['steps'] if x['code'] not in ('FINALQC', 'WH')]
        first_open = next((x for x in ops if x['frac'] < 1), None)
        started = prod > 0 or any(x['frac'] > 0 for x in ops)
        key = 'production' if started else 'new'
        if first_open:
            nxt = dict(label=first_open['name'], endpoint=first_open['endpoint'], kind='work')
        else:
            nxt = dict(label='إنهاء الإنتاج وإرساله للجودة', endpoint='order_view', kind='finish')
        can_finish = prod > eps
        st['docs'] = docs
    if fs == FS_PENDING:
        key, nxt = 'pending_quality', dict(label='بانتظار موافقة الجودة', endpoint=None, kind='wait')
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
