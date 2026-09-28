# -*- coding: utf-8 -*-
"""عدم المطابقة (NCR) والإجراء التصحيحي.

  • يُفتح تلقائيًا عند أي سجل جودة غير مطابق، ويدويًا من الإنتاج أو الجودة.
  • مسار الحالة: مفتوح ← قيد المعالجة ← مغلق. الإغلاق لضمان الجودة بتوقيع إلكتروني
    ولا يتم قبل تسجيل السبب الجذري والإجراء وتصرّف المنتج.
  • أي NCR مفتوحة على تشغيلة تمنع إفراجها (سجل QA أو شهادة الإفراج النهائي).
"""
import datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth
from util import s, num, doc_scope

ST_OPEN, ST_WORK, ST_CLOSED = 'مفتوح', 'قيد المعالجة', 'مغلق'
SEVERITY = ('حرجة', 'رئيسية', 'ثانوية')
DISPOSITION = ('إعادة تشغيل / إعادة فحص', 'فرز وفصل المطابق', 'قبول بتنازل موثّق', 'إتلاف', 'إرجاع للمورّد')
STAGES = ('استلام الخام', 'الأسليتر', 'الطي', 'الفرز', 'التغليف', 'التعقيم', 'المنتج النهائي', 'أخرى')


def open_for_batch(bn):
    """عدم مطابقة غير مغلقة على تشغيلة (تمنع الإفراج)."""
    if not bn:
        return []
    return db.q("SELECT dev_no, description FROM deviations WHERE batch_no=? AND status<>?", (bn, ST_CLOSED))


def create(con, batch_no, stage, description, source_rec=None, severity='ثانوية', actor=None):
    """يفتح NCR داخل تعامل قائم ويعيد رقمها."""
    scope, fmt = doc_scope('NCR', None)
    no = db.alloc(con, scope, fmt)
    con.execute("""INSERT INTO deviations(dev_no,ddate,batch_no,stage,description,status,source_rec,severity,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (no, datetime.date.today().isoformat(), batch_no, stage, description, ST_OPEN,
                 source_rec, severity, actor or (g.user['username'] if getattr(g, 'user', None) else 'system')))
    return no


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/ncr', 'ncr_list')
    def ncr_list():
        a = request.args
        where, args = ['1=1'], []
        st = s(a.get('status')) or 'open'
        if st == 'open':
            where.append('status<>?'); args.append(ST_CLOSED)
        elif st != 'all':
            where.append('status=?'); args.append(st)
        if s(a.get('batch')):
            where.append('batch_no LIKE ?'); args.append(f"%{a['batch'].strip()}%")
        rows = db.q(f"SELECT * FROM deviations WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 300", tuple(args))
        return render_template('ncr_list.html', nav='ncr', rows=rows, st=st, a=a)

    @route('/ncr/new', 'ncr_new', methods=['GET', 'POST'])
    def ncr_new():
        if not (auth.can('enter') or auth.can('qc_record')):
            abort(403)
        if request.method == 'POST':
            f = request.form
            desc, stage, bn = s(f.get('description')), s(f.get('stage')), s(f.get('batch_no'))
            if not desc or stage not in STAGES:
                flash('المرحلة ووصف عدم المطابقة إلزاميان', 'bad')
                return redirect(url_for('ncr_new', b=bn or ''))
            if bn and not db.one('SELECT 1 FROM work_orders WHERE batch_no=?', (bn,)):
                flash('رقم التشغيلة غير موجود', 'bad')
                return redirect(url_for('ncr_new'))
            sev = f.get('severity') if f.get('severity') in SEVERITY else 'ثانوية'
            with db.tx() as con:
                no = create(con, bn, stage, desc, None, sev)
                if num(f.get('qty_affected')) is not None:
                    con.execute('UPDATE deviations SET qty_affected=? WHERE dev_no=?', (num(f.get('qty_affected')), no))
            db.log('create', 'deviations', no, f'{bn or "-"}; {stage}')
            flash(f'فُتحت عدم المطابقة {no}', 'ok')
            return redirect(url_for('ncr_view', dev_no=no))
        return render_template('ncr_new.html', nav='ncr', stages=STAGES, severity=SEVERITY,
                               batches=db.q("SELECT batch_no,item_code FROM work_orders ORDER BY rowid DESC LIMIT 80"),
                               b=request.args.get('b') or '')

    @route('/ncr/<path:dev_no>', 'ncr_view', methods=['GET', 'POST'])
    def ncr_view(dev_no):
        d = db.one('SELECT * FROM deviations WHERE dev_no=?', (dev_no,))
        if not d:
            abort(404)
        if request.method == 'POST':
            f, act = request.form, request.form.get('act')
            back = url_for('ncr_view', dev_no=dev_no)
            if d['status'] == ST_CLOSED:
                flash('عدم المطابقة مغلقة — لا تُعدَّل', 'bad')
                return redirect(back)
            auth.need('qc_record')
            fields = dict(root_cause=s(f.get('root_cause')), action=s(f.get('action')),
                          disposition=s(f.get('disposition')), owner=s(f.get('owner')),
                          severity=f.get('severity') if f.get('severity') in SEVERITY else d['severity'],
                          qty_affected=num(f.get('qty_affected')))
            if fields['disposition'] and fields['disposition'] not in DISPOSITION:
                flash('تصرّف المنتج غير صحيح', 'bad')
                return redirect(back)
            status = ST_WORK
            if act == 'close':
                auth.need('qc_sign')
                miss = [n for k, n in (('root_cause', 'السبب الجذري'), ('action', 'الإجراء التصحيحي'),
                                       ('disposition', 'تصرّف المنتج')) if not fields[k]]
                if miss:
                    flash('لا يُغلق قبل تسجيل: ' + '، '.join(miss), 'bad')
                    return redirect(back)
                if not auth.check_esign(f.get('esign_pw')):
                    flash('التوقيع الإلكتروني غير صحيح — أعد إدخال كلمة مرورك للإغلاق', 'bad')
                    return redirect(back)
                status = ST_CLOSED
            with db.tx() as con:
                con.execute("""UPDATE deviations SET root_cause=?, action=?, disposition=?, owner=?, severity=?,
                               qty_affected=?, status=?, close_date=?, closed_by=? WHERE dev_no=?""",
                            (fields['root_cause'], fields['action'], fields['disposition'], fields['owner'],
                             fields['severity'], fields['qty_affected'], status,
                             datetime.date.today().isoformat() if status == ST_CLOSED else None,
                             g.user['username'] if status == ST_CLOSED else None, dev_no))
                if status == ST_CLOSED:
                    con.execute(*auth.signature_row('إغلاق عدم مطابقة', 'deviations', dev_no))
            db.log('close' if status == ST_CLOSED else 'update', 'deviations', dev_no, status)
            flash(f'{dev_no}: {status}', 'ok')
            return redirect(back)
        rec = db.one('SELECT id, rec_no FROM qc_records WHERE rec_no=?', (d['source_rec'],)) if d['source_rec'] else None
        return render_template('ncr_view.html', nav='ncr', d=d, rec=rec, severity=SEVERITY,
                               dispositions=DISPOSITION, sigs=auth.signatures_for('deviations', dev_no))
