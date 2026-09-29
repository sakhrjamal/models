# -*- coding: utf-8 -*-
"""تقرير الأصل والفرع الموحّد (Batch Genealogy) لكل مسارات التصنيع.

من رقم تشغيلة نهائية: المنتج والأمر والمسار والخام ومورّده والعمليات والفحوص والمشغّلون والماكينات
والكميات والمرفوض والهالك والإفراج والمخزن والشحن — خط زمني وشجرة أصل.
ومن LOT خام / رول جامبو / LOT مورّد: كل التشغيلات والمنتجات النهائية المصنَّعة منه (تتبع أمامي).
"""
import json

import db, mfg, trace
from flask import render_template, request, url_for, redirect, abort, flash


def _supplier_of_grn(grn):
    return db.one("""SELECT rc.grn_no, rc.supplier_lot, rc.receipt_date, rc.item_code, rc.qty, rc.uom, rc.po_no, rc.coa_no,
                            rc.stock_status, rc.inspection_no, s.name supplier, s.country,
                            (SELECT decision FROM inspections i WHERE i.inspection_no=rc.inspection_no) qc_decision
                     FROM receipts rc LEFT JOIN suppliers s ON s.supplier_id=rc.supplier_id
                     WHERE rc.grn_no=?""", (grn,)) or {}


def raw_sources(bn):
    """مصادر الخام لتشغيلة: قائمة {kind, ref, grn, supplier, supplier_lot, item, qc, qty}."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
    rc = mfg.route_of_wo(w)
    out = []
    if rc == mfg.SP:
        for a in db.q("SELECT * FROM allocations WHERE batch_no=? AND voided=0 ORDER BY id", (bn,)):
            g_ = _supplier_of_grn(a['source_ref'])
            out.append(dict(kind='LOT SP', ref=a['source_ref'], grn=g_.get('grn_no'), supplier=g_.get('supplier'),
                            supplier_lot=g_.get('supplier_lot'), item=g_.get('item_code'), qc=g_.get('qc_decision'),
                            qty=f'{a["qty"]:g} {a["unit"]}', doc=a['doc_no'], date=a['alloc_date'],
                            operator=a['operator']))
    elif rc == mfg.BANDAGE:
        for a in db.q("SELECT * FROM allocations WHERE batch_no=? AND voided=0 ORDER BY id", (bn,)):
            rl = db.one('SELECT grn_no, width_cm, length_m, weight_kg FROM rolls WHERE roll_no=?', (a['source_ref'],)) or {}
            g_ = _supplier_of_grn(rl.get('grn_no'))
            used = db.one("SELECT IFNULL(SUM(qty_in),0) n FROM proc_batches WHERE batch_no=? AND source_ref=? AND stage_code='BM'",
                          (bn, a['source_ref']))['n']
            out.append(dict(kind='جامبو رول', ref=a['source_ref'], grn=g_.get('grn_no'), supplier=g_.get('supplier'),
                            supplier_lot=g_.get('supplier_lot'), item=g_.get('item_code'), qc=g_.get('qc_decision'),
                            qty=f'المستخدم {used:g} م من {rl.get("length_m") or "?"} م', doc=a['doc_no'],
                            date=a['alloc_date'], operator=a['operator']))
    else:
        d = trace.batch_chain(bn)
        for r in d.get('supply', []):
            out.append(dict(kind='رول خام', ref=r['roll_no'], grn=r.get('grn_no'), supplier=r.get('supplier'),
                            supplier_lot=r.get('supplier_lot'), item=None, qc=r.get('qc_decision'), qty='', doc=None,
                            date=r.get('receipt_date'), operator=None))
    return out


def events(bn):
    """الخط الزمني: كل سندات التشغيلة مرتبة بالتاريخ. كل حدث: dict(date, stage, title, meta, kind, link)."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
    ev = []

    def add(date, stage, title, meta='', kind='', link=None):
        ev.append(dict(date=date or '', stage=stage, title=title, meta=meta, kind=kind, link=link))

    add(w.get('issue_date'), 'أمر الإنتاج', f'{w["wo_no"]} — {w.get("item_code") or ""}',
        f'المسار: {(mfg.route(mfg.route_of_wo(w)) or {}).get("name_ar")} · المطلوب {w.get("qty_required") or "—"} {w.get("uom") or ""}'
        f' · أصدره {w.get("issued_by") or "—"}')
    for r in raw_sources(bn):
        add(r.get('date'), 'الخام', f'{r["kind"]} {r["ref"]}', f'استلام {r["grn"]} · المورّد {r["supplier"] or "—"} · LOT المورّد {r["supplier_lot"] or "—"} · {r["qty"]}')
    for r in db.q("""SELECT * FROM slitting WHERE batch_no=? OR batch_no IN (SELECT DISTINCT COALESCE(slit_batch,batch_no)
                     FROM subrolls WHERE tag_no IN (SELECT tag_no FROM folding_in WHERE batch_no=?)) ORDER BY id""", (bn, bn)):
        subs = db.q('SELECT tag_no FROM subrolls WHERE plan_no=? ORDER BY tag_no', (r['doc_no'],)) if r.get('doc_no') else []
        add(r['sdate'], 'السليتر', f'{r["doc_no"] or r["batch_no"]} — الجامبو {r["roll_no"]}',
            f'{r["length_m"] or "—"}م × {r["width_cm"]}سم' + (f' · سب رول: {subs[0]["tag_no"]} … {subs[-1]["tag_no"]} ({len(subs)})' if subs else ''))
    for r in db.q('SELECT * FROM folding_out WHERE batch_no=? ORDER BY id', (bn,)):
        add(r['fdate'], 'الطي', f'سند {r["doc_no"]} — {r["qty_good"]:g} {r.get("unit") or "قطعة"}',
            f'ماكينة {r["machine_code"]} · السب رول {r.get("tag_no") or "—"} · المشغّل {r.get("operator") or "—"}')
    if w.get('finished_at'):
        add(w['finished_at'][:10], 'إنهاء الإنتاج', f'{w.get("produced_qty") or 0:g} {w.get("uom") or ""}', f'أنهاها {w.get("finished_by") or "—"}')
    for a in db.q('SELECT * FROM approvals WHERE batch_no=? ORDER BY id', (bn,)):
        add((a['ts'] or '')[:10], 'قرار الجودة', {'Approved': 'اعتماد للتخزين', 'Rejected': 'رفض', 'Hold': 'تعليق'}.get(a['decision'], a['decision']),
            f'{a["by_user"] or "—"} · {a["qty"]:g} {a["unit"] or ""}' + (f' · {a["note"]}' if a['note'] else ''),
            'qc' if a['decision'] == 'Approved' else 'bad')
    for r in db.q('SELECT * FROM proc_batches WHERE batch_no=? ORDER BY work_date, proc_no', (bn,)):
        names = {'BM': 'ماكينة الأربطة', 'BW': 'تغليف الأربطة', 'BX': 'تعبئة البوكسات', 'CT': 'تعبئة الكراتين', 'SPK': 'تعبئة SP'}
        add(r['work_date'], names.get(r['stage_code'], r['stage_code']), f'{r["proc_no"]} — {r["qty_out"]:g} {r["unit"]}',
            f'الأب: {r["parent_proc"] or r["batch_no"]} · المشغّل {r["operator"] or "—"} · ماكينة {r["machine"] or "—"} · '
            f'مرفوض {r["qty_reject"] or 0:g} · هالك {r["qty_scrap"] or 0:g}', link=None)
    for r in db.q('SELECT * FROM sorting WHERE batch_no=? ORDER BY sdate, doc_no', (bn,)):
        add(r['sdate'], 'الفرز', f'سند {r["doc_no"]} — {r["groups"]:g} مجموعة', f'المشغّل {r["operator"] or "—"} · تالف {r["scrap"] or 0:g}')
    for r in db.q('SELECT * FROM packaging WHERE batch_no=? ORDER BY pdate, doc_no', (bn,)):
        add(r['pdate'], 'التغليف', f'سند {r["doc_no"]} — {r["env_good"] or 0:g} مغلف / {r["boxes"] or 0:g} بوكس',
            f'المشغّل {r["operator"] or "—"} · حالة {r["doc_status"]}')
    for c in trace.batch_chain(bn).get('cycles', []):
        add(c.get('cycle_date'), 'التعقيم', f'دورة {c["cycle_no"]}', f'CI/BI: {c.get("bi_result") or "—"} · الحالة {c.get("status")}', 'qc')
    for r in db.q("""SELECT q.*, t.title FROM qc_records q JOIN qc_templates t ON t.code=q.template_code
                     WHERE q.batch_no=? ORDER BY q.rec_date, q.id""", (bn,)):
        add(r['rec_date'], 'الجودة', f'{r["rec_no"]} — {r["title"]}',
            f'{r["result"]}{" · " + r["decision"] if r["decision"] else ""} · {r["inspector"] or "—"} · خط {r["line_code"] or "—"}',
            'bad' if r['result'] == 'غير مطابق' else 'qc', url_for('quality_record', rid=r['id']))
    rel = db.one('SELECT * FROM releases WHERE batch_no=?', (bn,))
    if rel:
        add(rel['issue_date'], 'الإفراج النهائي', f'شهادة {rel["release_no"]} — {rel["decision"]}', f'{rel.get("qa_officer") or ""}', 'qc')
    for n in db.q('SELECT * FROM deviations WHERE batch_no=? ORDER BY id', (bn,)):
        add(n['ddate'], 'عدم مطابقة', f'{n["dev_no"]} — {n["status"]}', (n['description'] or '')[:100], 'bad', url_for('ncr_view', dev_no=n['dev_no']))
    for f in db.q('SELECT * FROM fg_receipts WHERE batch_no=? ORDER BY id', (bn,)):
        add(f['rdate'], 'المخزن', f'استلام {f["doc_no"]} — {f["qty"]:g} {f["unit"]}', f'الموقع {f["location"] or "—"} · {f["received_by"] or "—"}')
    for sh in db.q('SELECT * FROM shipments WHERE batch_no=? ORDER BY id', (bn,)):
        add(sh['sdate'], 'الشحن', f'{sh["doc_no"]} — {sh["customer"]}', f'{sh["qty"]:g} {sh.get("unit") or ""}' + (' — مُبطَل' if sh.get('voided') else ''))
    ev.sort(key=lambda e: e['date'])
    return ev


def tree(bn):
    """شجرة الأصل من المنتج النهائي إلى المورّد: قائمة متداخلة [{label, sub, children}]."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
    rc = mfg.route_of_wo(w)
    srcs = raw_sources(bn)

    def src_nodes():
        return [dict(label=f'{r["kind"]} {r["ref"]}', sub=f'استلام {r["grn"]} · LOT المورّد {r["supplier_lot"] or "—"}',
                     children=[dict(label=f'المورّد: {r["supplier"] or "—"}', sub='', children=[])]) for r in srcs]

    def chain(stages):
        """stages مرتبة من الأخير إلى الأقدم؛ كل عنصر (label, sub)."""
        node = None
        leaves = src_nodes()
        for label, sub in reversed(stages):
            node = dict(label=label, sub=sub, children=leaves if node is None else [node])
            leaves = None
            if node['children'] is None:
                node['children'] = []
        return node

    procs = {st: db.q("SELECT * FROM proc_batches WHERE batch_no=? AND stage_code=? ORDER BY proc_no", (bn, st))
             for st in ('BM', 'BW', 'BX', 'CT', 'SPK')}

    def names(rows):
        return '، '.join(r['proc_no'] for r in rows) or 'لم تُسجَّل'
    fg = db.q('SELECT doc_no FROM fg_receipts WHERE batch_no=?', (bn,))
    top = dict(label=f'المنتج النهائي — التشغيلة {bn}', sub=f'{w.get("item_code")} · {(mfg.route(rc) or {}).get("name_ar")}',
               children=[])
    if fg:
        top['children'].append(dict(label='مخزن المنتج التام', sub='، '.join(x['doc_no'] for x in fg), children=[]))
    if rc == mfg.BANDAGE:
        stages = [('تعبئة الكراتين', names(procs['CT'])), ('تعبئة البوكسات', names(procs['BX'])),
                  ('تغليف الأربطة', names(procs['BW'])), ('ماكينة الأربطة', names(procs['BM']))]
    elif rc == mfg.SP:
        if mfg.variant_for(w) == mfg.STERILE:
            stages = [('التعقيم والتهوية', '، '.join(c['cycle_no'] for c in trace.batch_chain(bn).get('cycles', [])) or '—'),
                      ('التغليف الأولي', '، '.join(r['doc_no'] for r in db.q('SELECT doc_no FROM packaging WHERE batch_no=?', (bn,))) or '—'),
                      ('الفرز', '، '.join(r['doc_no'] for r in db.q('SELECT doc_no FROM sorting WHERE batch_no=?', (bn,))) or '—'),
                      ('تخصيص خام SP', '، '.join(r['doc_no'] for r in db.q('SELECT doc_no FROM allocations WHERE batch_no=? AND voided=0', (bn,))) or '—')]
        else:
            stages = [('تعبئة SP (باك/بوكس/كرتون)', names(procs['SPK'])),
                      ('تخصيص خام SP', '، '.join(r['doc_no'] for r in db.q('SELECT doc_no FROM allocations WHERE batch_no=? AND voided=0', (bn,))) or '—')]
    else:
        d = trace.batch_chain(bn)
        stages = []
        if mfg.is_v14(w):
            ap = db.one("SELECT id FROM approvals WHERE batch_no=? AND decision='Approved' ORDER BY id DESC LIMIT 1", (bn,))
            if ap:
                stages.append(('موافقة الجودة', f'APR-{ap["id"]:06d}'))
            stages += [('الطي', '، '.join(sorted({r['doc_no'] for r in d.get('fold_out', [])})) or '—'),
                       ('السليتر — خطط القص', '، '.join(r['plan_no'] for r in db.q('SELECT plan_no FROM cutting_plans WHERE batch_no=?', (bn,))) or '—')]
            top['children'].append(chain(stages))
            return top
        if mfg.variant_for(w) == mfg.STERILE:
            stages += [('التعقيم والتهوية', '، '.join(c['cycle_no'] for c in d.get('cycles', [])) or '—')]
        stages += [('التغليف', '، '.join(r['doc_no'] for r in d.get('packaging', [])) or '—')]
        if d.get('sorting'):
            stages += [('الفرز', '، '.join(r['doc_no'] for r in d['sorting']))]
        stages += [('الطي', '، '.join(sorted({r['doc_no'] for r in d.get('fold_out', [])})) or '—'),
                   ('الأسليتر — السب رول', '، '.join(r['batch_no'] for r in d.get('slit_orders', [])) or '—')]
    top['children'].append(chain(stages))
    return top


def forward(ref):
    """كل التشغيلات والمنتجات النهائية المصنَّعة من خام (رقم استلام / LOT مورّد / رول جامبو / سند)."""
    ref = (ref or '').strip()
    if not ref:
        return []
    grns = {r['grn_no'] for r in db.q('SELECT grn_no FROM receipts WHERE grn_no=? OR supplier_lot=?', (ref, ref))}
    rolls = {r['roll_no'] for r in db.q('SELECT roll_no FROM rolls WHERE roll_no=? OR grn_no IN (%s)' %
                                       (','.join('?' * len(grns)) or "''"), (ref,) + tuple(grns))}
    batches = {}
    for a in db.q('SELECT batch_no, source_ref FROM allocations WHERE voided=0'):
        if a['source_ref'] in grns or a['source_ref'] in rolls or a['source_ref'] == ref:
            batches[a['batch_no']] = a['source_ref']
    for r in db.q("""SELECT DISTINCT fi.batch_no, sr.roll_no FROM folding_in fi JOIN subrolls sr ON sr.tag_no=fi.tag_no"""):
        if r['roll_no'] in rolls or r['roll_no'] == ref:
            batches[r['batch_no']] = r['roll_no']
    out = []
    for bn, src in sorted(batches.items()):
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) or {}
        fr = mfg.final_release(bn)
        out.append(dict(batch_no=bn, item_code=w.get('item_code'), route=(mfg.route(mfg.route_of_wo(w)) or {}).get('name_ar'),
                        source=src, release=fr['ref'] if fr else None, decision=fr['decision'] if fr else None,
                        fg=mfg.fg_received(bn),
                        customers='، '.join(sorted({x['customer'] for x in db.q(
                            "SELECT customer FROM shipments WHERE batch_no=? AND IFNULL(voided,0)=0", (bn,))})) or '—'))
    return out


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/genealogy', 'genealogy')
    def genealogy():
        b = (request.args.get('b') or '').strip()
        ref = (request.args.get('ref') or '').strip()
        data = None
        if b:
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (b,))
            if w:
                rc = mfg.route_of_wo(w)
                data = dict(w=w, route=mfg.route(rc), progress=mfg.progress(b), out=mfg.output(b), events=events(b),
                            tree=tree(b), sources=raw_sources(b), release=mfg.final_release(b),
                            fg=mfg.fg_received(b), variant=mfg.variant_for(w))
        return render_template('genealogy.html', nav='trace', b=b, ref=ref, d=data, fwd=forward(ref) if ref else [],
                               print_mode=bool(request.args.get('print')))
