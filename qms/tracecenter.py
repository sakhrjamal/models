# -*- coding: utf-8 -*-
"""مركز التتبع v15: بحث بأي معرّف (باركود، LOT مورّد، جامبو، سب رول، تشغيل قص، سند طي، أمر، تشغيلة،
كرتون SP، سجل تعقيم، دورة تعقيم…) ثم رسم السلسلة كاملة في الاتجاهين:
  خلفيًا: المنتج النهائي ← سند الطي ← السب رول ← الجامبو ← LOT المورّد
  أماميًا: LOT المورّد / الجامبو / السب رول ← كل الطي والتعبئة والدورات والمنتجات النهائية."""
import db
from util import fmt_qty
from flask import render_template, request, redirect, url_for, abort, jsonify


def _batches(sql, args=()):
    return sorted({r['b'] for r in db.q(sql, args) if r['b']})


def resolve(q):
    """كل ما يطابق النص كمعرّف: قائمة {kind, ref, label, batches}."""
    q = (q or '').strip()
    if not q:
        return []
    like = q
    out = []

    def add(kind, ref, label, batches):
        out.append(dict(kind=kind, ref=ref, label=label, batches=batches))

    for r in db.q("""SELECT roll_no, batch_no, supplier_lot, supplier_roll_no FROM rolls
                     WHERE roll_no=? OR supplier_lot=? OR supplier_roll_no=?""", (like, like, like)):
        bs = _batches("SELECT DISTINCT batch_no b FROM cutting_plans WHERE roll_no=? UNION SELECT batch_no FROM allocations WHERE source_ref=? AND voided=0",
                      (r['roll_no'], r['roll_no']))
        add('جامبو رول', r['roll_no'], f"جامبو {r['roll_no']} — LOT المورّد {r['supplier_lot'] or '—'}", bs)
    for r in db.q("SELECT DISTINCT grn_no, supplier_lot FROM receipts WHERE grn_no=? OR supplier_lot=? OR po_no=?", (like, like, like)):
        bs = _batches("""SELECT DISTINCT cp.batch_no b FROM cutting_plans cp JOIN rolls r ON r.roll_no=cp.roll_no
                         WHERE r.grn_no=? UNION SELECT batch_no FROM allocations WHERE source_ref=? AND voided=0
                         UNION SELECT a.batch_no FROM allocations a JOIN rolls r2 ON r2.roll_no=a.source_ref WHERE r2.grn_no=? AND a.voided=0""",
                      (r['grn_no'], r['grn_no'], r['grn_no']))
        add('استلام / LOT مورّد', r['grn_no'], f"استلام {r['grn_no']} — LOT {r['supplier_lot']}", bs)
    for r in db.q("SELECT tag_no, barcode, run_no, roll_no FROM subrolls WHERE tag_no=? OR barcode=?", (like, like)):
        bs = _batches("SELECT batch_no b FROM subrolls WHERE tag_no=? UNION SELECT batch_no FROM folding_in WHERE tag_no=?",
                      (r['tag_no'], r['tag_no']))
        add('سب رول', r['tag_no'], f"سب رول {r['tag_no']} من الجامبو {r['roll_no']}", bs)
    for r in db.q("SELECT DISTINCT run_no, plan_no FROM subrolls WHERE run_no=? OR plan_no=?", (like, like)):
        bs = _batches("SELECT DISTINCT batch_no b FROM subrolls WHERE run_no=? OR plan_no=?", (like, like))
        add('تشغيل قص', r['run_no'] or r['plan_no'], f"سند القص {r['run_no'] or r['plan_no']}", bs)
    for r in db.q("SELECT DISTINCT batch_no b, doc_no FROM folding_out WHERE doc_no=?", (like,)):
        add('سند طي', r['doc_no'], f"سند الطي {r['doc_no']}", [r['b']])
    for r in db.q("SELECT wo_no, batch_no FROM work_orders WHERE wo_no=? OR batch_no=?", (like, like)):
        add('أمر / تشغيلة', r['batch_no'], f"أمر {r['wo_no']} — تشغيلة {r['batch_no']}", [r['batch_no']])
    for r in db.q("SELECT doc_no, batch_no FROM ns_packing WHERE doc_no=?", (like,)):
        add('سند تعبئة', r['doc_no'], f"تعبئة {r['doc_no']}", [r['batch_no']])
    for r in db.q("SELECT barcode, batch_no FROM intermediates WHERE barcode=? OR fold_doc=?", (like, like)):
        add('كرتون SP وسيط', r['barcode'], f"كرتون وسيط {r['barcode']}", [r['batch_no']])
    for r in db.q("SELECT rec_no, batch_no FROM ster_records WHERE rec_no=?", (like,)):
        add('سجل فرز/تغليف', r['rec_no'], f"سجل {r['rec_no']}", [r['batch_no']])
    for r in db.q("SELECT cycle_no FROM cycles WHERE cycle_no=?", (like,)):
        bs = _batches("SELECT DISTINCT batch_no b FROM cycle_loads WHERE cycle_no=?", (r['cycle_no'],))
        add('دورة تعقيم', r['cycle_no'], f"دورة {r['cycle_no']}", bs)
    return out


def chain(bn):
    """السلسلة الخلفية الكاملة لتشغيلة."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
    if not w:
        return None
    d = dict(w=w)
    d['jumbos'] = db.q("""SELECT r.roll_no, r.supplier_lot, r.supplier_roll_no, r.grn_no, r.width_cm, r.length_m,
                                 r.stock_status, cp.plan_no, cp.status plan_status, cp.executed_by, cp.executed_at,
                                 rc.po_no, rc.receipt_date, s.name supplier
                          FROM cutting_plans cp JOIN rolls r ON r.roll_no=cp.roll_no
                          LEFT JOIN receipts rc ON rc.grn_no=r.grn_no LEFT JOIN suppliers s ON s.supplier_id=rc.supplier_id
                          WHERE cp.batch_no=? ORDER BY cp.plan_no""", (bn,))
    d['subrolls'] = db.q("""SELECT tag_no, barcode, run_no, roll_no, sr_code, width_cm, length_m, stock_status, consumed_by_batch
                            FROM subrolls WHERE batch_no=? ORDER BY tag_no""", (bn,))
    d['folds'] = db.q("""SELECT o.doc_no, o.fdate, o.machine_code, o.qty_good, o.theoretical_qty, o.waste_qty, o.waste_pct,
                                i.tag_no, i.operator, i.roll_no
                         FROM folding_out o LEFT JOIN folding_in i ON i.doc_no=o.doc_no
                         WHERE o.batch_no=? ORDER BY o.doc_no""", (bn,))
    d['inters'] = db.q('SELECT * FROM intermediates WHERE batch_no=? ORDER BY barcode', (bn,))
    d['packs'] = db.q('SELECT * FROM ns_packing WHERE batch_no=? ORDER BY doc_no', (bn,))
    d['recs'] = db.q('SELECT * FROM ster_records WHERE batch_no=? ORDER BY rec_no', (bn,))
    d['cycles'] = db.q("""SELECT c.cycle_no, c.status, c.start_at, c.end_at, c.operator, l.rec_no, l.boxes_in
                          FROM cycle_loads l JOIN cycles c ON c.cycle_no=l.cycle_no WHERE l.batch_no=? ORDER BY c.cycle_no""", (bn,))
    d['allocs'] = db.q("""SELECT a.doc_no, a.source_ref, a.qty, a.unit, a.alloc_date, a.operator, rc.supplier_lot, rc.grn_no
                          FROM allocations a LEFT JOIN receipts rc ON rc.grn_no=a.source_ref OR rc.grn_no=(SELECT grn_no FROM rolls WHERE roll_no=a.source_ref)
                          WHERE a.batch_no=? AND a.voided=0 ORDER BY a.id""", (bn,))
    d['procs'] = db.q('SELECT proc_no, stage_code, qty_in, qty_out, qty_reject, operator, parent_proc FROM proc_batches WHERE batch_no=? ORDER BY proc_no', (bn,))
    d['approvals'] = db.q('SELECT * FROM approvals WHERE batch_no=? ORDER BY id', (bn,))
    return d


def forward(kind, ref):
    """التتبع الأمامي من جامبو/سب رول/LOT: كل التشغيلات والمنتجات النهائية."""
    rolls = []
    if kind == 'جامبو رول':
        rolls = [ref]
    elif kind == 'استلام / LOT مورّد':
        rolls = [r['roll_no'] for r in db.q('SELECT roll_no FROM rolls WHERE grn_no=?', (ref,))]
    subs = []
    if kind == 'سب رول':
        subs = [ref]
    elif rolls:
        subs = [r['tag_no'] for r in db.q('SELECT tag_no FROM subrolls WHERE roll_no IN (%s)' % ','.join('?' * len(rolls)), rolls)] if rolls else []
    if not subs:
        return []
    ph = ','.join('?' * len(subs))
    rows = db.q(f"""SELECT DISTINCT i.batch_no, i.doc_no, i.tag_no, w.item_code, w.final_status, w.wo_no
                    FROM folding_in i JOIN work_orders w ON w.batch_no=i.batch_no WHERE i.tag_no IN ({ph}) ORDER BY i.batch_no, i.doc_no""", subs)
    return rows


def register(app):
    @app.route('/trace/center', endpoint='trace_center')
    def trace_center():
        q = (request.args.get('q') or '').strip()
        hits = resolve(q) if q else []
        sel = request.args.get('b')
        if not sel and len(hits) == 1 and len(hits[0]['batches']) == 1:
            sel = hits[0]['batches'][0]
        d = chain(sel) if sel else None
        fwd = []
        if q:
            for h in hits:
                if h['kind'] in ('جامبو رول', 'استلام / LOT مورّد', 'سب رول'):
                    fwd += [dict(x, src=h['label']) for x in forward(h['kind'], h['ref'])]
        return render_template('trace_center.html', nav='trace', q=q, hits=hits, sel=sel, d=d, fwd=fwd, fmt=fmt_qty)

    @app.route('/api/trace', endpoint='trace_api')
    def trace_api():
        q = request.args.get('q', '')
        return jsonify(resolve(q))

    @app.route('/scan', endpoint='scan')
    def scan():
        """باركود ممسوح: يوجّه للصفحة المناسبة (سب رول ← الطي، كرتون SP ← الفرز…)."""
        code = (request.args.get('code') or '').strip()
        if not code:
            return redirect(url_for('trace_center'))
        hits = resolve(code)
        if hits and hits[0]['batches']:
            return redirect(url_for('trace_center', q=code))
        return redirect(url_for('trace_center', q=code))
