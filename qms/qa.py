# -*- coding: utf-8 -*-
"""موافقة الجودة النهائية على الدفعة المنتجة (v14) — شاشة واحدة وقرار واحد.

  إنهاء الإنتاج ← الدفعة «بانتظار الجودة» ← اعتماد للتخزين / رفض / تعليق ← (الاعتماد) إشعار الإنتاج والمخزن ← المخزن

لا فحوصات مراحل ولا بوابات ولا QC/QA متشعّب هنا: السجل الكامل للقرار في جدول approvals، والتوقيع الإلكتروني
مسجَّل باسم المستخدم المصادَق (بلا إعادة كتابة كلمة المرور). فحص الخام الوارد يبقى في «استلام الخام».
"""
import datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, notify, constants
from util import s, fmt_qty

DECISIONS = {'approve': ('Approved', mfg.FS_APPROVED, 'اعتماد للتخزين'),
             'reject': ('Rejected', mfg.FS_REJECTED, 'رفض'),
             'hold': ('Hold', mfg.FS_HOLD, 'تعليق')}
DEC_AR = {'Approved': 'اعتماد للتخزين', 'Rejected': 'رفض', 'Hold': 'تعليق'}
DEC_CLS = {'Approved': 'ok', 'Rejected': 'bad', 'Hold': 'warn'}


def _batch_rows(where, args=()):
    rows = db.q(f"""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' AND {where}
                    ORDER BY COALESCE(finished_at,'') DESC, rowid DESC""", tuple(args))
    lines = auth.user_lines()
    return [w for w in rows if not lines or mfg.route_of_wo(w) in lines]


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/quality/pending', 'quality_pending')
    def quality_pending():
        rows = _batch_rows('final_status=?', (mfg.FS_PENDING,))
        raw = db.q("""SELECT r.grn_no, r.item_code, r.supplier_lot, r.qty, r.uom, r.receipt_date, i.description
                      FROM receipts r LEFT JOIN items i ON i.item_code=r.item_code
                      WHERE r.inspection_no IS NULL ORDER BY r.receipt_date""")
        return render_template('quality_pending.html', nav='qpending', rows=rows, raw=raw, fmt=fmt_qty)

    @route('/quality/review/<path:bn>', 'quality_review')
    def quality_review(bn):
        w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND COALESCE(order_type,'إنتاج')='إنتاج'", (bn,))
        if not w:
            abort(404)
        auth.need_line(mfg.route_of_wo(w))
        st = mfg.order_state(w)
        qty = float(w.get('produced_qty') if w.get('produced_qty') is not None else st['produced'])
        it = db.one('SELECT description, sterile, size, ply FROM items WHERE item_code=?', (w['item_code'],)) or {}
        hist = db.q('SELECT * FROM approvals WHERE batch_no=? ORDER BY id DESC', (bn,))
        decidable = w.get('final_status') in (mfg.FS_PENDING, mfg.FS_HOLD)
        return render_template('quality_review.html', nav='qpending', w=w, st=st, qty=qty, it=it, hist=hist,
                               equiv=mfg.pack_equiv(w['item_code'], qty, st['unit']), decidable=decidable,
                               fs_ar=mfg.FS_AR, fs_cls=mfg.FS_CLS, dec_ar=DEC_AR, dec_cls=DEC_CLS, fmt=fmt_qty)

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
        if key not in DECISIONS:
            flash('اختر القرار', 'bad')
            return redirect(back)
        decision, fs, ar = DECISIONS[key]
        note = s(f.get('note'))
        if key in ('reject', 'hold') and not note:
            flash('اكتب سبب الرفض/التعليق', 'bad')
            return redirect(back)
        if w.get('final_status') not in (mfg.FS_PENDING, mfg.FS_HOLD):
            flash('هذه الدفعة ليست بانتظار قرار الجودة', 'bad')
            return redirect(back)
        st = mfg.order_state(w)
        qty = float(w.get('produced_qty') if w.get('produced_qty') is not None else st['produced'])
        unit = st['unit']
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        with db.tx() as con:
            cur = con.execute('SELECT final_status FROM work_orders WHERE batch_no=?', (bn,)).fetchone()
            if cur['final_status'] not in (mfg.FS_PENDING, mfg.FS_HOLD):
                flash('تغيّرت حالة الدفعة — أعد فتحها', 'bad')
                return redirect(back)
            con.execute('INSERT INTO approvals(batch_no,decision,note,qty,unit,by_user) VALUES(?,?,?,?,?,?)',
                        (bn, decision, note, qty, unit, g.user['full_name']))
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
        return render_template('quality_held.html', nav='qheld', rows=rows, fs_ar=mfg.FS_AR, fs_cls=mfg.FS_CLS, fmt=fmt_qty)

    @route('/quality/log', 'quality_history')
    def quality_history():
        d = s(request.args.get('d'))
        where, args = '1=1', []
        if d:
            where, args = 'decision=?', [d]
        rows = db.q(f"""SELECT a.*, w.wo_no, w.item_code FROM approvals a LEFT JOIN work_orders w ON w.batch_no=a.batch_no
                        WHERE {where} ORDER BY a.id DESC LIMIT 300""", tuple(args))
        return render_template('quality_history.html', nav='qlog', rows=rows, d=d, dec_ar=DEC_AR, dec_cls=DEC_CLS, fmt=fmt_qty)
