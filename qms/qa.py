# -*- coding: utf-8 -*-
"""الإفراجات الخمسة فقط (كل ما عداها سجلات متابعة لا توقف الإنتاج):

  1) خام/جامبو رول   — فحص الاستلام (receipts)          قبل الاستخدام في الإنتاج
  2) منتج غير معقم   — إفراج نهائي بعد التعبئة            قبل مخزن المنتج التام
  3) SP نصف مصنع     — بعد الطي والكراتين الوسيطة         قبل الفرز والعد
  4) قبل التعقيم     — بعد الفرز والتغليف والبوكسات       قبل دورة التعقيم
  5) نهائي معقم      — بعد التعقيم والتهوية وفعالية الدورة  قبل التخزين

القرار سجل واحد في approvals (المرحلة + المستخدم + الوقت) ويُوقَّع باسم المستخدم المصادَق.
"""
import datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, notify, constants
from util import s, fmt_qty

DECISIONS = {'approve': ('Approved', mfg.FS_APPROVED, 'اعتماد للتخزين'),
             'reject': ('Rejected', mfg.FS_REJECTED, 'رفض'),
             'hold': ('Hold', mfg.FS_HOLD, 'تعليق')}
DEC_AR = {'Approved': 'اعتماد/إفراج', 'Rejected': 'رفض', 'Hold': 'تعليق'}
DEC_CLS = {'Approved': 'ok', 'Rejected': 'bad', 'Hold': 'warn'}
STAGE_AR = {'RAW': 'إفراج الخام', 'NS_FG': 'إفراج المنتج غير المعقم', 'SP': 'إفراج SP', 'PRE_STER': 'إفراج قبل التعقيم',
            'FINAL_STER': 'الإفراج النهائي للمعقم'}


def _batch_rows(where, args=()):
    rows = db.q(f"""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' AND {where}
                    ORDER BY COALESCE(finished_at,'') DESC, rowid DESC""", tuple(args))
    lines = auth.user_lines()
    return [w for w in rows if not lines or mfg.route_of_wo(w) in lines]


def _final_stage_key(w):
    return 'FINAL_STER' if mfg.variant_for(w) == mfg.STERILE and mfg.is_v15(w) else 'NS_FG'


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    def _dec(key, note):
        if key not in DECISIONS:
            raise ValueError('اختر القرار')
        if key in ('reject', 'hold') and not note:
            raise ValueError('اكتب سبب الرفض/التعليق')
        return DECISIONS[key]

    # ---------------------------------------------------------------- قائمة الإفراجات
    @route('/quality/pending', 'quality_pending')
    def quality_pending():
        rows = _batch_rows('final_status=?', (mfg.FS_PENDING,))
        raw = db.q("""SELECT r.grn_no, r.item_code, r.supplier_lot, r.qty, r.uom, r.receipt_date, i.description
                      FROM receipts r LEFT JOIN items i ON i.item_code=r.item_code
                      WHERE r.inspection_no IS NULL ORDER BY r.receipt_date""")
        sp = db.q("""SELECT m.batch_no, w.wo_no, m.item_code, m.sp_code, COUNT(*) n, SUM(m.qty) qty
                     FROM intermediates m JOIN work_orders w ON w.batch_no=m.batch_no
                     WHERE m.status='Pending SP Release' GROUP BY m.batch_no ORDER BY m.batch_no""")
        pre = db.q("""SELECT r.*, w.wo_no FROM ster_records r JOIN work_orders w ON w.batch_no=r.batch_no
                      WHERE r.status='Ready for Pre-Sterilization QC' ORDER BY r.rec_no""")
        for w in rows:
            w['stage_key'] = _final_stage_key(w)
        return render_template('quality_pending.html', nav='qpending', rows=rows, raw=raw, sp=sp, pre=pre, fmt=fmt_qty,
                               stage_ar=STAGE_AR)

    # ---------------------------------------------------------------- إفراج SP (3)
    @route('/quality/sp/<path:bn>', 'quality_sp_review')
    def quality_sp_review(bn):
        w = db.one("SELECT * FROM work_orders WHERE batch_no=?", (bn,))
        if not w:
            abort(404)
        auth.need_line(mfg.route_of_wo(w))
        cartons = db.q("SELECT * FROM intermediates WHERE batch_no=? ORDER BY barcode", (bn,))
        it = db.one('SELECT description, sp_item, size, ply, mesh, xray FROM items WHERE item_code=?', (w['item_code'],)) or {}
        hist = db.q("SELECT * FROM approvals WHERE batch_no=? AND stage='SP' ORDER BY id DESC", (bn,))
        pend = [c for c in cartons if c['status'] in ('Pending SP Release', 'Hold')]
        import proc
        return render_template('quality_sp.html', nav='qpending', w=w, it=it, cartons=cartons, pend=pend, hist=hist, fmt=fmt_qty,
                               inter_ar=proc.INTER_AR, dec_ar=DEC_AR, dec_cls=DEC_CLS)

    @route('/quality/sp/<path:bn>/decide', 'quality_sp_decide', methods=['POST'])
    def quality_sp_decide(bn):
        auth.need('qc_sign')
        w = db.one("SELECT * FROM work_orders WHERE batch_no=?", (bn,))
        if not w:
            abort(404)
        auth.need_line(mfg.route_of_wo(w))
        back = url_for('quality_sp_review', bn=bn)
        f = request.form
        note = s(f.get('note'))
        try:
            decision, _, ar = _dec(s(f.get('decision')), note)
        except ValueError as e:
            flash(str(e), 'bad')
            return redirect(back)
        new = {'Approved': 'Released for Sorting', 'Rejected': 'Rejected', 'Hold': 'Hold'}[decision]
        with db.tx() as con:
            rows = con.execute("SELECT barcode, qty FROM intermediates WHERE batch_no=? AND rec_no IS NULL "
                               "AND status IN ('Pending SP Release','Hold')", (bn,)).fetchall()
            if not rows:
                flash('لا توجد كراتين وسيطة بانتظار القرار', 'bad')
                return redirect(back)
            qty = float(sum(r['qty'] for r in rows))
            con.execute("UPDATE intermediates SET status=? WHERE batch_no=? AND rec_no IS NULL AND status IN ('Pending SP Release','Hold')",
                        (new, bn))
            con.execute('INSERT INTO approvals(batch_no,decision,note,qty,unit,by_user,stage) VALUES(?,?,?,?,?,?,?)',
                        (bn, decision, note, qty, 'مسحة', g.user['full_name'], 'SP'))
            con.execute(*auth.signature_row(f'إفراج SP: { {"Approved": "إفراج"}.get(decision, ar) }', 'intermediates', bn))
            if decision == 'Approved':
                notify.push(con, 'sp_released', f'أُفرج عن SP للدفعة {bn} ({fmt_qty(qty)} مسحة، {len(rows)} كرتون) وهي جاهزة للفرز والعد',
                            w['item_code'], url_for('ster_order', bn=bn), bn, mfg.FULL, ('enter',))
        db.log('qc_decision', 'intermediates', bn, f'SP {decision}; {len(rows)} cartons; {note or ""}')
        flash(f'{ {"Approved": "أُفرج للفرز والعد"}.get(decision, ar) } — {len(rows)} كرتون وسيط ({fmt_qty(qty)} مسحة)', 'ok' if decision == 'Approved' else 'warn')
        return redirect(url_for('quality_pending'))

    # ---------------------------------------------------------------- إفراج قبل التعقيم (4)
    @route('/quality/pre/<rec_no>', 'quality_pre_review')
    def quality_pre_review(rec_no):
        r = db.one('SELECT * FROM ster_records WHERE rec_no=?', (rec_no,))
        if not r:
            abort(404)
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],))
        auth.need_line(mfg.route_of_wo(w))
        it = db.one('SELECT * FROM items WHERE item_code=?', (r['item_code'],)) or {}
        ins = db.q('SELECT * FROM ster_inputs WHERE rec_no=?', (rec_no,))
        hist = db.q("SELECT * FROM approvals WHERE batch_no=? AND stage='PRE_STER' ORDER BY id DESC", (r['batch_no'],))
        import proc
        return render_template('quality_pre.html', nav='qpending', r=r, w=w, it=it, ins=ins, hist=hist, fmt=fmt_qty,
                               rec_ar=proc.REC_AR, dec_ar=DEC_AR, dec_cls=DEC_CLS,
                               decidable=r['status'] in ('Ready for Pre-Sterilization QC', 'Pre-Sterilization Hold'))

    @route('/quality/pre/<rec_no>/decide', 'quality_pre_decide', methods=['POST'])
    def quality_pre_decide(rec_no):
        auth.need('qc_sign')
        r = db.one('SELECT * FROM ster_records WHERE rec_no=?', (rec_no,))
        if not r:
            abort(404)
        w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (r['batch_no'],))
        auth.need_line(mfg.route_of_wo(w))
        back = url_for('quality_pre_review', rec_no=rec_no)
        note = s(request.form.get('note'))
        try:
            decision, _, ar = _dec(s(request.form.get('decision')), note)
        except ValueError as e:
            flash(str(e), 'bad')
            return redirect(back)
        if r['status'] not in ('Ready for Pre-Sterilization QC', 'Pre-Sterilization Hold'):
            flash('هذا السجل ليس بانتظار الإفراج قبل التعقيم', 'bad')
            return redirect(back)
        new = {'Approved': 'Released for Sterilization', 'Rejected': 'Rejected Pre-Sterilization', 'Hold': 'Pre-Sterilization Hold'}[decision]
        with db.tx() as con:
            con.execute('UPDATE ster_records SET status=?, pre_release_by=?, pre_release_at=? WHERE rec_no=?',
                        (new, g.user['full_name'], datetime.datetime.now().isoformat(sep=' ', timespec='minutes'), rec_no))
            con.execute('INSERT INTO approvals(batch_no,decision,note,qty,unit,by_user,stage) VALUES(?,?,?,?,?,?,?)',
                        (r['batch_no'], decision, (f'{rec_no}: ' + (note or '')).strip(), r['boxes_actual'], 'بوكس',
                         g.user['full_name'], 'PRE_STER'))
            con.execute(*auth.signature_row(f'إفراج قبل التعقيم: {ar}', 'ster_records', rec_no))
            if decision == 'Approved':
                notify.push(con, 'pre_ster_released', f'أُفرج عن السجل {rec_no} ({fmt_qty(r["boxes_actual"])} بوكس) وهو جاهز لدورة التعقيم',
                            w['item_code'], url_for('ster_cycle_new'), r['batch_no'], mfg.FULL, ('enter',))
        db.log('qc_decision', 'ster_records', rec_no, f'PRE_STER {decision}; {note or ""}')
        import proc
        proc.ster_sync(r['batch_no'])
        flash(f'{ {"Approved": "أُفرج للتعقيم"}.get(decision, ar) } — السجل {rec_no}', 'ok' if decision == 'Approved' else 'warn')
        return redirect(url_for('quality_pending'))

    # ---------------------------------------------------------------- الإفراج النهائي (2 و5)
    def _cycles_of(bn):
        cs = db.q("""SELECT DISTINCT c.* FROM cycles c JOIN cycle_loads l ON l.cycle_no=c.cycle_no WHERE l.batch_no=?
                     ORDER BY c.cycle_no""", (bn,))
        for c in cs:
            c['recs'] = [x['rec_no'] for x in db.q('SELECT rec_no FROM cycle_loads WHERE cycle_no=? AND batch_no=?', (c['cycle_no'], bn))]
            c['ok'] = constants.is_conform(c.get('ci_external')) and constants.is_conform(c.get('ci_internal')) \
                and constants.canon(c.get('bi_result'), constants.BI_RESULTS) == constants.BI_NEG
            c['bad'] = constants.is_nonconform(c.get('ci_external')) or constants.is_nonconform(c.get('ci_internal')) \
                or constants.canon(c.get('bi_result'), constants.BI_RESULTS) == constants.BI_POS
        return cs

    @route('/quality/review/<path:bn>', 'quality_review')
    def quality_review(bn):
        w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND COALESCE(order_type,'إنتاج')='إنتاج'", (bn,))
        if not w:
            abort(404)
        auth.need_line(mfg.route_of_wo(w))
        st = mfg.order_state(w)
        qty = float(w.get('produced_qty') if w.get('produced_qty') is not None else (st['fg_qty'] if st['v15'] else st['produced']))
        unit = st['fg_unit'] if st['v15'] else st['unit']
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        hist = db.q('SELECT * FROM approvals WHERE batch_no=? ORDER BY id DESC', (bn,))
        decidable = w.get('final_status') in (mfg.FS_PENDING, mfg.FS_HOLD)
        cycles = _cycles_of(bn) if st['sterile'] and st['v15'] else []
        blocked = None
        if cycles:
            if any(c['bad'] for c in cycles):
                blocked = 'إحدى دورات التعقيم نتيجتها غير مطابقة — لا إفراج نهائي'
            elif not all(c['ok'] for c in cycles):
                blocked = 'سجّل تحقق فعالية كل دورة (CI خارجي/داخلي + المؤشر البيولوجي) قبل الإفراج النهائي'
        equiv = mfg.equiv_from_swabs(it, st['produced']) if st['v15'] else mfg.pack_equiv(w['item_code'], qty, unit)
        recs = db.q('SELECT * FROM ster_records WHERE batch_no=? ORDER BY rec_no', (bn,)) if st['sterile'] else []
        packs = db.q('SELECT * FROM ns_packing WHERE batch_no=? ORDER BY doc_no', (bn,)) if (st['v15'] and not st['sterile']) else []
        return render_template('quality_review.html', nav='qpending', w=w, st=st, qty=qty, unit=unit, it=it, hist=hist, equiv=equiv,
                               decidable=decidable, fs_ar=mfg.FS_AR, fs_cls=mfg.FS_CLS, dec_ar=DEC_AR, dec_cls=DEC_CLS, fmt=fmt_qty,
                               cycles=cycles, blocked=blocked, recs=recs, packs=packs, stage_ar=STAGE_AR, stage_key=_final_stage_key(w),
                               ci_opts=constants.CI_RESULTS, bi_opts=(constants.BI_NEG, constants.BI_POS, constants.BI_PENDING))

    @route('/quality/cycle/<path:cycle_no>/verify', 'quality_cycle_verify', methods=['POST'])
    def quality_cycle_verify(cycle_no):
        """تحقق الجودة من فعالية دورة التعقيم (CI/BI) — شرط الإفراج النهائي للمعقم."""
        auth.need('qc_sign')
        c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
        if not c:
            abort(404)
        f = request.form
        ci1, ci2 = constants.canon(s(f.get('ci_external')), constants.CI_RESULTS), constants.canon(s(f.get('ci_internal')), constants.CI_RESULTS)
        bi = constants.canon(s(f.get('bi_result')), constants.BI_RESULTS)
        bn = s(f.get('batch_no'))
        with db.tx() as con:
            con.execute("""UPDATE cycles SET ci_external=?, ci_internal=?, bi_result=?, verified_by=?, verified_at=? WHERE cycle_no=?""",
                        (ci1, ci2, bi, g.user['full_name'], datetime.datetime.now().isoformat(sep=' ', timespec='minutes'), cycle_no))
        db.log('update', 'cycles', cycle_no, f'verify CI {ci1}/{ci2} BI {bi}')
        flash(f'سُجّل تحقق فعالية الدورة {cycle_no}', 'ok')
        return redirect(url_for('quality_review', bn=bn) if bn else url_for('quality_pending'))

    @route('/quality/decide/<path:bn>', 'quality_decide', methods=['POST'])
    def quality_decide(bn):
        auth.need('qc_sign')
        w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND COALESCE(order_type,'إنتاج')='إنتاج'", (bn,))
        if not w:
            abort(404)
        auth.need_line(mfg.route_of_wo(w))
        back = url_for('quality_review', bn=bn)
        f = request.form
        key = s(f.get('decision'))
        note = s(f.get('note'))
        try:
            decision, fs, ar = _dec(key, note)
        except ValueError as e:
            flash(str(e), 'bad')
            return redirect(back)
        if w.get('final_status') not in (mfg.FS_PENDING, mfg.FS_HOLD):
            flash('هذه الدفعة ليست بانتظار قرار الجودة', 'bad')
            return redirect(back)
        st = mfg.order_state(w)
        if key == 'approve' and st['sterile'] and st['v15']:
            for c in _cycles_of(bn):
                if not c['ok']:
                    flash('لا إفراج نهائي قبل تسجيل تحقق فعالية كل دورة تعقيم ومطابقتها', 'bad')
                    return redirect(back)
        qty = float(w.get('produced_qty') if w.get('produced_qty') is not None else (st['fg_qty'] if st['v15'] else st['produced']))
        unit = st['fg_unit'] if st['v15'] else st['unit']
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        with db.tx() as con:
            cur = con.execute('SELECT final_status FROM work_orders WHERE batch_no=?', (bn,)).fetchone()
            if cur['final_status'] not in (mfg.FS_PENDING, mfg.FS_HOLD):
                flash('تغيّرت حالة الدفعة — أعد فتحها', 'bad')
                return redirect(back)
            con.execute('INSERT INTO approvals(batch_no,decision,note,qty,unit,by_user,stage) VALUES(?,?,?,?,?,?,?)',
                        (bn, decision, note, qty, unit, g.user['full_name'], _final_stage_key(w)))
            con.execute("""UPDATE work_orders SET final_status=?, qc_note=?, approved_by=?, approved_at=?,
                           approved_qty=CASE WHEN ?='Approved' THEN ? ELSE NULL END WHERE batch_no=?""",
                        (fs, note, g.user['full_name'], now, decision, qty, bn))
            con.execute(*auth.signature_row('اعتماد الدفعة للتخزين' if key == 'approve' else f'قرار الجودة: {ar}', 'work_orders', bn))
            if key == 'approve':
                notify.push(con, 'approved',
                            f'تم اعتماد الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، الكمية المعتمدة {fmt_qty(qty)} {unit}، وهي جاهزة للتخزين.',
                            f'{w["item_code"]}', url_for('warehouse'), bn, mfg.route_of_wo(w), ('enter', 'warehouse'))
            else:
                notify.push(con, 'qc_' + ('reject' if key == 'reject' else 'hold'),
                            f'{ar} الدفعة {bn} لأمر الإنتاج {w["wo_no"]} — السبب: {note}', f'{w["item_code"]}',
                            url_for('order_view', bn=bn), bn, mfg.route_of_wo(w), ('enter', 'wo_issue'))
        db.log('qc_decision', 'work_orders', bn, f'{decision}; {fmt_qty(qty)} {unit}; {note or ""}')
        flash({'approve': f'اعتُمدت الدفعة {bn} للتخزين ({fmt_qty(qty)} {unit}) وأُبلغ الإنتاج والمخزن',
               'reject': f'رُفضت الدفعة {bn} وأُبلغ الإنتاج', 'hold': f'عُلّقت الدفعة {bn} وأُبلغ الإنتاج'}[key],
              'ok' if key == 'approve' else 'warn')
        return redirect(url_for('quality_pending'))

    @route('/quality/held', 'quality_held')
    def quality_held():
        rows = _batch_rows('final_status IN (?,?)', (mfg.FS_REJECTED, mfg.FS_HOLD))
        for w in rows:
            w['last'] = db.one('SELECT * FROM approvals WHERE batch_no=? ORDER BY id DESC LIMIT 1', (w['batch_no'],))
        sp_hold = db.q("SELECT batch_no, COUNT(*) n, SUM(qty) qty, status FROM intermediates WHERE status IN ('Hold','Rejected') GROUP BY batch_no, status")
        pre_hold = db.q("SELECT * FROM ster_records WHERE status IN ('Pre-Sterilization Hold','Rejected Pre-Sterilization')")
        return render_template('quality_held.html', nav='qheld', rows=rows, fs_ar=mfg.FS_AR, fs_cls=mfg.FS_CLS, fmt=fmt_qty,
                               sp_hold=sp_hold, pre_hold=pre_hold)

    @route('/quality/log', 'quality_history')
    def quality_history():
        d = s(request.args.get('d'))
        st_ = s(request.args.get('stage'))
        where, args = ['1=1'], []
        if d:
            where.append('a.decision=?'); args.append(d)
        if st_:
            where.append('a.stage=?'); args.append(st_)
        rows = db.q(f"""SELECT a.*, w.wo_no, w.item_code FROM approvals a LEFT JOIN work_orders w ON w.batch_no=a.batch_no
                        WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT 300""", tuple(args))
        return render_template('quality_history.html', nav='qlog', rows=rows, d=d, dec_ar=DEC_AR, dec_cls=DEC_CLS, fmt=fmt_qty,
                               stage_ar=STAGE_AR, st_=st_)
