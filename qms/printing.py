# -*- coding: utf-8 -*-
"""طباعة السندات والتقارير والبطاقات على الورق الرسمي للشركة (Letterhead) — عربي RTL بترقيم صفحات.

الورق الرسمي: يُرفع مرة من «الإعدادات» (PNG/JPG/SVG بمقاس A4) ويُحفظ في data/letterhead.* ثم يُستعمل
كخلفية لكل صفحة مطبوعة. لا يُرسم بديل تقريبي؛ وإن لم يُرفع تظهر ترويسة نصية مؤقتة وتنبيه لمدير النظام.
الهوامش (أعلى/أسفل/جانبي) من الإعدادات لتطابق تصميم الورق الرسمي.
"""
import os, glob, datetime
from flask import render_template, request, abort, send_file, Response, url_for, g

import db, mfg, auth, barcode
from util import fmt_qty

LH_EXT = ('png', 'jpg', 'jpeg', 'svg', 'webp')


def letterhead_path():
    d = os.path.dirname(db.DB_PATH)
    for ext in LH_EXT:
        p = os.path.join(d, f'letterhead.{ext}')
        if os.path.exists(p):
            return p
    d2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'letterhead_default.png')
    if os.path.exists(d2):
        return d2
    d3 = os.path.join(getattr(__import__('sys'), '_MEIPASS', ''), 'static', 'letterhead_default.png')
    return d3 if os.path.exists(d3) else None


def margins():
    f = lambda k, d: db.setting(k, d)                                   # noqa: E731
    return dict(top=f('print_margin_top_mm', '46'), bottom=f('print_margin_bottom_mm', '32'), side=f('print_margin_side_mm', '15'))


def _kv(*pairs):
    return [(k, v) for k, v in pairs]


def _item(code):
    return db.one('SELECT * FROM items WHERE item_code=?', (code,)) or {}


def _spec(it):
    return f"{it.get('size') or '—'} · {it.get('ply') or '—'} طبقة · {it.get('mesh') or '—'} · {it.get('xray') or '—'}"


def _wo_head(w):
    it = _item(w['item_code'])
    return _kv(('أمر الإنتاج', w['wo_no']), ('رقم التشغيلة', w['batch_no']), ('المنتج', f"{w['item_code']} — {it.get('description') or ''}"),
               ('المواصفات', _spec(it)), ('المعقم/غير المعقم', it.get('sterile') or '—'))


def _table(title, headers, rows):
    return dict(title=title, headers=headers, rows=rows)


# ---------------------------------------------------------------- السندات
def doc_data(kind, key):
    if kind == 'order':
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (key,))
        if not w:
            return None
        it = _item(w['item_code'])
        st = mfg.order_state(w)
        card = mfg.product_card(it, w['qty_required'])
        req = card.get('req') or {}
        meta = _wo_head(w) + _kv(('الكمية المطلوبة', f"{fmt_qty(w['qty_required'])} {w.get('uom') or ''}"),
                                 ('بالمسحات', fmt_qty(w.get('req_swabs')) if w.get('req_swabs') else '—'),
                                 ('ماكينة الطي', card.get('machine_name') or '—'), ('عرض السب رول', f"{fmt_qty(w.get('std_width_cm'))} سم"),
                                 ('المرحلة', st['stage_ar']), ('تاريخ الإصدار', w['issue_date']), ('أصدره', w.get('issued_by') or '—'))
        secs = []
        if req:
            secs.append(_table('احتياج الخامة', ['البند', 'القيمة'], [
                ['جامبو رول', fmt_qty(req.get('jumbo_needed'))], ['سب رول', fmt_qty(req.get('sr_needed'))],
                ['سب رول لكل جامبو', fmt_qty(req.get('per_jumbo'))], ['العرض المفيد (سم)', fmt_qty(req.get('usable_width'))],
                ['كمية نظرية لكل سب رول (مسحة)', fmt_qty(req.get('yield_per_sr'))]]))
        secs.append(_table('تكوين التعبئة', ['البند'], [[x['text']] for x in card.get('pack_config', [])]))
        return dict(title='أمر إنتاج', no=w['wo_no'], meta=meta, sections=secs, sign=['مدير المصنع', 'مسؤول الإنتاج'])
    if kind == 'slitter':
        p = db.one('SELECT * FROM cutting_plans WHERE plan_no=?', (key,))
        if not p:
            return None
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (p['batch_no'],))
        rl = db.one('SELECT r.*, i.description FROM rolls r LEFT JOIN items i ON i.item_code=r.item_code WHERE r.roll_no=?', (p['roll_no'],)) or {}
        rc = db.one('SELECT s.name supplier, r.supplier_lot FROM receipts r LEFT JOIN suppliers s ON s.supplier_id=r.supplier_id WHERE r.grn_no=?',
                    (rl.get('grn_no'),)) or {}
        meta = _wo_head(w) + _kv(('رقم تشغيل السليتر', p['plan_no']), ('الجامبو (الرقم الداخلي/الباركود)', p['roll_no']),
                                 ('رقم رول المورد', rl.get('supplier_roll_no') or '—'), ('لوط المورد', rc.get('supplier_lot') or '—'),
                                 ('المورد', rc.get('supplier') or '—'), ('كود المادة', f"{rl.get('item_code')} — {rl.get('description') or ''}"),
                                 ('العرض المرجعي / الفعلي (سم)', f"{fmt_qty(p.get('ref_width'))} / {fmt_qty(p.get('actual_width'))}"),
                                 ('الطول المرجعي / الفعلي (م)', f"{fmt_qty(p.get('ref_length'))} / {fmt_qty(p.get('actual_length'))}"),
                                 ('كود Sub Roll (Master)', p.get('sr_code') or '—'), ('المشغل', p.get('operator') or '—'),
                                 ('التاريخ/الوقت', p.get('executed_at') or p.get('created_at')), ('الحالة', p['status']))
        subs = db.q('SELECT * FROM subrolls WHERE plan_no=? ORDER BY tag_no', (key,))
        return dict(title='سند تشغيل السليتر', no=p['plan_no'], meta=meta, sections=[_table('السب رول الناتج', ['الباركود / الرقم', 'كود Sub Roll', 'العرض (سم)', 'الطول (م)', 'الحالة'],
                    [[s_['tag_no'], s_['sr_code'] or '—', fmt_qty(s_['width_cm']), fmt_qty(s_['length_m']), s_['stock_status']] for s_ in subs])],
                    sign=['المشغل', 'مراقب الجودة'])
    if kind == 'folding':
        fo = db.one('SELECT * FROM folding_out WHERE doc_no=?', (key,))
        if not fo:
            return None
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (fo['batch_no'],))
        fi = db.one('SELECT * FROM folding_in WHERE doc_no=?', (key,)) or {}
        sr = db.one('SELECT * FROM subrolls WHERE tag_no=?', (fo['tag_no'],)) or {}
        meta = _wo_head(w) + _kv(('رقم سند الطي', fo['doc_no']), ('Sub Roll', f"{fo['tag_no']} ({sr.get('sr_code') or '—'})"),
                                 ('رن السليتر', sr.get('run_no') or '—'), ('الجامبو المصدر', sr.get('roll_no') or '—'),
                                 ('لوط المورد', sr.get('supplier_lot') or '—'), ('الماكينة', fo.get('machine_code')),
                                 ('المشغل', fo.get('operator') or '—'), ('التاريخ/الوقت', f"{fo['fdate']} {fo.get('end_time') or ''}"))
        theo = fo.get('theoretical_qty')
        rows = [['الكمية النظرية (مسحة)', fmt_qty(theo)], ['السليم الفعلي (مسحة)', fmt_qty(fo['qty_good'])],
                ['الهدر (مسحة)', fmt_qty(fo.get('waste_qty'))], ['نسبة الهدر %', fmt_qty(fo.get('waste_pct'))],
                ['Yield %', fmt_qty(round(fo['qty_good'] / theo * 100, 2)) if theo else '—'], ['مرفوض/هالك فعلي مسجل', fmt_qty(fo.get('rejected_qty'))]]
        return dict(title='سند ماكينة الطي', no=fo['doc_no'], meta=meta, sections=[_table('النتائج', ['البند', 'القيمة'], rows)],
                    sign=['المشغل', 'مراقب الجودة'])
    if kind == 'ns_pack':
        r = db.one('SELECT * FROM ns_packing WHERE doc_no=?', (key,))
        if not r:
            return None
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],))
        src = db.q('SELECT s.*, f.tag_no FROM ns_pack_src s JOIN folding_out f ON f.doc_no=s.fold_doc WHERE s.doc_no=?', (key,))
        meta = _wo_head(w) + _kv(('رقم سند التعبئة', r['doc_no']), ('المشغل', r.get('operator') or '—'), ('التاريخ', r['created_at']))
        return dict(title='سند التعبئة (غير معقم)', no=r['doc_no'], meta=meta, sections=[
            _table('التعبئة', ['البند', 'القيمة'], [['مسحات مدخلة', fmt_qty(r['swabs_in'])], ['مسحات/باكت', fmt_qty(r['swabs_per_pack'])],
                   ['باكتات متوقعة', fmt_qty(r['packs_expected'])], ['باكتات فعلية', fmt_qty(r['packs_actual'])],
                   ['باكت/كرتون', fmt_qty(r['packs_per_carton'])], ['كراتين متوقعة', fmt_qty(r['cartons_expected'])],
                   ['كراتين فعلية', fmt_qty(r['cartons_actual'])]]),
            _table('مصدر المسحات (سندات الطي)', ['سند الطي', 'Sub Roll', 'المسحات'], [[x['fold_doc'], x['tag_no'], fmt_qty(x['swabs'])] for x in src])],
            sign=['مسؤول التعبئة', 'مراقب الجودة'])
    if kind == 'ster_record':
        r = db.one('SELECT * FROM ster_records WHERE rec_no=?', (key,))
        if not r:
            return None
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],))
        ins = db.q('SELECT i.*, m.fold_doc, m.tag_no FROM ster_inputs i JOIN intermediates m ON m.barcode=i.barcode WHERE i.rec_no=?', (key,))
        meta = _wo_head(w) + _kv(('سجل الفرز والعد والتغليف', r['rec_no']), ('كود SP', r.get('sp_code') or '—'), ('الحالة', r['status']),
                                 ('دورة التعقيم', r.get('cycle_no') or '—'))
        rows = [['الفرز والعد', f"مستلم {fmt_qty(r['received_qty'])} · مقبول {fmt_qty(r['sort_accepted'])} · مرفوض {fmt_qty(r['sort_rejected'])}", r.get('sort_operator') or '—', r.get('sort_at') or '—'],
                ['التغليف', f"{fmt_qty(r['ppe'])} مسحة/مغلف · متوقع {fmt_qty(r['env_expected'])} · فعلي {fmt_qty(r['env_actual'])} · مرفوض {fmt_qty(r['env_rejected'])}", r.get('pack_operator') or '—', r.get('pack_at') or '—'],
                ['البوكسات', f"{fmt_qty(r['env_per_box'])} مغلف/بوكس · متوقع {fmt_qty(r['boxes_expected'])} · فعلي {fmt_qty(r['boxes_actual'])} · فرق {fmt_qty(r['box_diff'])}", r.get('box_operator') or '—', r.get('box_at') or '—']]
        return dict(title='سجل معالجة المنتج المعقم', no=r['rec_no'], meta=meta, sections=[
            _table('المدخلات (كراتين وسيطة)', ['الباركود', 'سند الطي', 'Sub Roll', 'المسحات'], [[i['barcode'], i['fold_doc'], i['tag_no'], fmt_qty(i['qty'])] for i in ins]),
            _table('المراحل', ['المرحلة', 'التفاصيل', 'المشغل', 'الوقت'], rows)], sign=['مسؤول التغليف', 'مراقب الجودة'])
    if kind == 'cycle':
        c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (key,))
        if not c:
            return None
        lines = db.q('SELECT l.*, i.description FROM cycle_loads l LEFT JOIN items i ON i.item_code=l.item_code WHERE l.cycle_no=?', (key,))
        meta = _kv(('رقم دورة التعقيم', c['cycle_no']), ('الغرفة/الجهاز', c.get('machine_code')), ('المسؤول', c.get('operator') or '—'),
                   ('البداية', c.get('start_at') or '—'), ('النهاية', c.get('end_at') or '—'), ('الحالة', c.get('status')),
                   ('بدء التهوية', c.get('aer_start_at') or '—'), ('نهاية التهوية', c.get('aer_end_at') or '—'),
                   ('CI خارجي/داخلي', f"{c.get('ci_external') or '—'} / {c.get('ci_internal') or '—'}"), ('المؤشر البيولوجي', c.get('bi_result') or '—'),
                   ('رقم تقرير الجهاز', c.get('machine_report_no') or '—'), ('لوط الغاز', c.get('gas_lot') or '—'))
        return dict(title='سند دورة التعقيم', no=c['cycle_no'], meta=meta, sections=[
            _table('اللوطات داخل الدورة', ['المنتج', 'لوط الإنتاج', 'سجل المعالجة', 'بوكسات'],
                   [[f"{l['item_code']}", l['batch_no'], l['rec_no'] or '—', fmt_qty(l['boxes_in'])] for l in lines])], sign=['المسؤول', 'مراقب الجودة', 'ضمان الجودة'])
    if kind == 'release':
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (key,))
        if not w:
            return None
        ap = db.q('SELECT * FROM approvals WHERE batch_no=? ORDER BY id', (key,))
        import qa
        return dict(title='سجل قرارات الإفراج', no=key, meta=_wo_head(w) + _kv(('الحالة النهائية', mfg.FS_AR.get(w.get('final_status'), '—')),
                    ('الكمية المعتمدة', f"{fmt_qty(w.get('approved_qty'))} {w.get('uom') or ''}")), sections=[_table('القرارات', ['المرحلة', 'القرار', 'الكمية', 'بواسطة', 'الوقت', 'ملاحظة'],
                    [[qa.STAGE_AR.get(a.get('stage')) or 'إفراج', qa.DEC_AR.get(a['decision'], a['decision']), f"{fmt_qty(a['qty'])} {a['unit'] or ''}", a['by_user'], a['ts'], a['note'] or '—'] for a in ap])],
                    sign=['مسؤول الجودة', 'مدير الجودة'])
    if kind == 'inventory':
        rows = db.q('SELECT * FROM stock_tx WHERE ref_doc=? ORDER BY id', (key,))
        if not rows:
            return None
        return dict(title='حركة مخزون', no=key, meta=_kv(('المستند', key)), sections=[_table('القيود', ['الوقت', 'المالك', 'المرحلة', 'الكمية', 'الوحدة', 'ملاحظة'],
                    [[r['ts'], r['owner'], (__import__('inventory').STAGES.get(r['stage']) or (r['stage'],))[0], fmt_qty(r['qty']), r['unit'], r['note'] or '—'] for r in rows])],
                    sign=['أمين المخزن'])
    if kind == 'monitor':
        import json
        r = db.one('SELECT r.*, t.title, t.form_no, t.revision, t.effective_date FROM qc_records r JOIN qc_templates t ON t.code=r.template_code WHERE r.id=?', (key,))
        if not r:
            return None
        spec = json.loads(r['spec_json'] or '[]')
        vals = json.loads(r['values_json'] or '{}')
        return dict(title=r['title'], no=r['rec_no'],
                    meta=_kv(('رقم النموذج', r['form_no']), ('الإصدار', r['revision']), ('تاريخ السريان', r['effective_date']),
                             ('تاريخ الفحص', f"{r['rec_date']} {r['rec_time'] or ''}"), ('القائم بالفحص', r['inspector']),
                             ('التشغيلة', r['batch_no'] or '—'), ('الخط / المرحلة', r['line_code'] or '—'), ('النتيجة', r['result'])),
                    sections=[_table('بنود الفحص (Checklist)', ['البند', 'القيمة', 'التقييم'],
                              [[f['label'], vals.get(f['id'], '—'), 'غير مطابق' if any(x['id'] == f['id'] for x in (vals.get('__fails') or [])) else ('مطابق' if vals.get(f['id']) is not None else '—')] for f in spec]),
                              _table('الملاحظات والإجراء التصحيحي', ['الملاحظات', 'الإجراء التصحيحي'], [[r['notes'] or '—', r['corrective'] or '—']])],
                    sign=['القائم بالفحص', r['approved_by'] or 'المعتمِد'])
    return None


# ---------------------------------------------------------------- البطاقات
def _label(title, code, lines, kind=''):
    return dict(title=title, code=code, lines=lines, svg=barcode.svg(code), kind=kind)


def label_data(kind, key):
    out = []
    if kind == 'jumbo':
        r = db.one('SELECT r.*, i.description, rc.supplier_lot slot FROM rolls r LEFT JOIN items i ON i.item_code=r.item_code '
                   'LEFT JOIN receipts rc ON rc.grn_no=r.grn_no WHERE r.roll_no=?', (key,))
        if r:
            out.append(_label('جامبو رول', r['roll_no'], _kv(('كود المادة', r['item_code']), ('لوط المورد', r.get('slot') or '—'),
                        ('رقم رول المورد', r.get('supplier_roll_no') or '—'), ('العرض المرجعي', f"{fmt_qty(r.get('ref_width_cm') or r.get('width_cm'))} سم"),
                        ('الطول المرجعي', f"{fmt_qty(r.get('ref_length_m') or r.get('length_m'))} م"), ('الحالة', r.get('use_status') or 'متاح'))))
    elif kind == 'subroll':
        subs = db.q('SELECT * FROM subrolls WHERE plan_no=? OR tag_no=? OR batch_no=? ORDER BY tag_no', (key, key, key))
        for s_ in subs:
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (s_['batch_no'],)) or {}
            it = _item(w.get('item_code'))
            out.append(_label('بطاقة Sub Roll', s_['tag_no'], _kv(
                ('كود Sub Roll', s_.get('sr_code') or '—'), ('رقم تشغيل السليتر', s_.get('run_no') or s_.get('plan_no') or '—'),
                ('أمر الإنتاج', w.get('wo_no') or '—'), ('المنتج المقصود', w.get('item_code') or '—'),
                ('العرض', f"{fmt_qty(s_.get('width_cm'))} سم"), ('الطول', f"{fmt_qty(s_.get('length_m'))} م"),
                ('المواصفات', f"{it.get('mesh') or '—'} · {it.get('xray') or '—'}"), ('الجامبو المصدر', s_.get('roll_no') or '—'),
                ('لوط المورد', s_.get('supplier_lot') or '—'), ('تاريخ القص', s_.get('sdate') or '—'), ('المشغل', s_.get('operator') or '—'),
                ('الحالة', 'جاهز للطي (Available for Folding)' if s_.get('stock_status') in ('متاح', 'مخطط') else s_.get('stock_status'))), 'subroll'))
    elif kind == 'intermediate':
        rows = db.q('SELECT * FROM intermediates WHERE barcode=? OR fold_doc=? OR batch_no=? ORDER BY barcode', (key, key, key))
        for m in rows:
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (m['batch_no'],)) or {}
            it = _item(w.get('item_code'))
            out.append(_label('بطاقة المنتج الوسيط SP', m['barcode'], _kv(
                ('كود SP', m.get('sp_code') or '—'), ('مصدر SP', 'إنتاج داخلي' if m['sp_source'] == 'INTERNAL_PRODUCTION' else 'استلام مورد'),
                ('أمر الإنتاج', w.get('wo_no') or '—'), ('لوط الإنتاج', m['batch_no']), ('المقاس', it.get('size') or '—'),
                ('الطبقات', it.get('ply') or '—'), ('Mesh', it.get('mesh') or '—'), ('Plain/X-Ray', it.get('xray') or '—'),
                ('الكمية', f"{fmt_qty(m['qty'])} مسحة"), ('سند الطي', m.get('fold_doc') or '—'), ('Sub Roll', m.get('tag_no') or '—'),
                ('تاريخ الإنتاج', m.get('prod_date') or '—'), ('المشغل', m.get('operator') or '—')), 'inter'))
    elif kind in ('envelope', 'box', 'carton'):
        r = db.one('SELECT * FROM ster_records WHERE rec_no=?', (key,))
        if r:
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],)) or {}
            it = _item(w.get('item_code'))
            ps = mfg.pack_spec(w.get('item_code'))
            title, code, per = {'envelope': ('بطاقة المغلف المعقم', ps.get('env_code'), f"{fmt_qty(r.get('ppe'))} مسحة"),
                                'box': ('بطاقة البوكس المعقم', ps.get('box_code') or it.get('pack_code'), f"{fmt_qty(ps.get('swabs_per_box'))} مسحة"),
                                'carton': ('بطاقة الكرتون الرئيسي', ps.get('carton_code') or it.get('master_box'), f"{fmt_qty(ps.get('boxes_per_carton'))} بوكس")}[kind]
            n = int(request.args.get('n', 1) or 1)
            for i in range(max(1, min(n, 200))):
                out.append(_label(title, f"{r['rec_no']}", _kv(
                    ('المنتج', f"{w.get('item_code')} — {it.get('description') or ''}"), ('كود العبوة', code or '—'), ('المحتوى', per),
                    ('لوط الإنتاج', r['batch_no']), ('رقم دورة التعقيم', r.get('cycle_no') or 'سيُسجَّل بعد إنشاء الدورة'),
                    ('المقاس/الطبقات', f"{it.get('size') or '—'} / {it.get('ply') or '—'}"), ('Mesh · نوع', f"{it.get('mesh') or '—'} · {it.get('xray') or '—'}"),
                    ('تاريخ التغليف', r.get('pack_at') or '—')), kind))
    elif kind in ('pack', 'ns_carton'):
        r = db.one('SELECT * FROM ns_packing WHERE doc_no=?', (key,))
        if r:
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],)) or {}
            it = _item(w.get('item_code'))
            ps = mfg.pack_spec(w.get('item_code'))
            code = ps.get('pack_code') if kind == 'pack' else ps.get('carton_code')
            out.append(_label('بطاقة الباكت' if kind == 'pack' else 'بطاقة الكرتون', r['doc_no'], _kv(
                ('المنتج', f"{w.get('item_code')} — {it.get('description') or ''}"), ('كود العبوة', code or '—'),
                ('لوط الإنتاج', r['batch_no']), ('المحتوى', f"{fmt_qty(r['swabs_per_pack'])} مسحة/باكت" if kind == 'pack' else f"{fmt_qty(r['packs_per_carton'])} باكت"),
                ('تاريخ التعبئة', r['created_at'])), kind))
    elif kind == 'fg':
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (key,))
        if w:
            it = _item(w['item_code'])
            out.append(_label('بطاقة المنتج النهائي', w['batch_no'], _kv(('المنتج', f"{w['item_code']} — {it.get('description') or ''}"),
                        ('لوط الإنتاج', w['batch_no']), ('الكمية', f"{fmt_qty(w.get('approved_qty'))} {w.get('uom') or ''}"),
                        ('حالة الجودة', mfg.FS_AR.get(w.get('final_status'), '—')))))
    return out


def register(app):
    @app.route('/letterhead', endpoint='letterhead')
    def letterhead():
        p = letterhead_path()
        if not p:
            abort(404)
        return send_file(p)

    @app.route('/print/<kind>/<path:key>', endpoint='print_doc')
    def print_doc(kind, key):
        d = doc_data(kind, key)
        if not d:
            abort(404)
        db.log('print', 'documents', f'{kind}/{key}', d['title'])
        return render_template('print_doc.html', d=d, lh=bool(letterhead_path()), m=margins(), printed=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                               user=g.user['full_name'], factory=db.setting('factory_name', ''), back=request.referrer)

    @app.route('/label/<kind>/<path:key>', endpoint='print_label')
    def print_label(kind, key):
        labels = label_data(kind, key)
        if not labels:
            abort(404)
        db.log('print', 'labels', f'{kind}/{key}', f'{len(labels)} labels')
        return render_template('label_doc.html', labels=labels, kind=kind, key=key, back=request.referrer)

    @app.route('/tags', endpoint='tags')
    def tags():
        """بطاقات Sub Roll لأمر (توافق مع الروابط القديمة)."""
        bn = (request.args.get('b') or '').strip()
        g.crumb_args = {'bn': bn}
        labels = label_data('subroll', bn) if bn else []
        return render_template('label_doc.html', labels=labels, kind='subroll', key=bn, back=request.referrer)
