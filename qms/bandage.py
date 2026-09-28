# -*- coding: utf-8 -*-
"""مسار الرباط الضاغط (Compression / Elastic Bandage).

  جامبو رول (مستلم ← فحص جودة ← إفراج) ← تخصيص لأمر إنتاج ← ماكينة الأربطة ← رولات رباط (WIP)
  ← ماكينة التغليف ← رباط مغلَّف (WIP) ← تعبئة البوكسات ← تعبئة الكراتين ← فحص نهائي ← مخزن

كل مرحلة سند فرعي (proc_batch) يحمل أبوه: BM ← BW ← BX ← CT. الكمية التي تدخل المرحلة لا تتجاوز
ما خرج من المرحلة السابقة فعليًا، وكل انتقال قيد مخزون. مسار غير معقم دائمًا.
"""
import json, datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, notify
from util import s, num, doc_scope, num_unit
from inventory import InsufficientStock

STAGE_TITLE = {'BM': 'تشغيل ماكينة الأربطة', 'BW': 'تغليف الأربطة', 'BX': 'تعبئة البوكسات', 'CT': 'تعبئة الكراتين'}


def jumbo_prefixes():
    return mfg.prefixes_of_class('JUMBO')


def _rolls(where='', args=()):
    ph = ','.join('?' * len(jumbo_prefixes())) or "''"
    rows = db.q(f"""SELECT rl.*, i.description, i.width_cm iw, rc.supplier_lot slot
                    FROM rolls rl JOIN items i ON i.item_code=rl.item_code
                    LEFT JOIN receipts rc ON rc.grn_no=rl.grn_no
                    WHERE i.prefix IN ({ph}) {where} ORDER BY rl.roll_no""", tuple(jumbo_prefixes()) + tuple(args))
    for r in rows:
        r['remaining'] = inventory.balance(r['roll_no'], 'RM')
    return rows


def free_jumbo():
    """جامبو مفرج عنه وغير مخصص لأي أمر وله رصيد."""
    return [r for r in _rolls("AND rl.stock_status='مفرج' AND IFNULL(rl.batch_no,'')=''") if r['remaining'] > 1e-9]


def _bw(bn):
    return db.one("SELECT * FROM work_orders WHERE batch_no=? AND route_code=? AND COALESCE(order_type,'إنتاج')='إنتاج'",
                  (bn, mfg.BANDAGE)) if bn else None


def _procs(bn, stage):
    return db.q("SELECT * FROM proc_batches WHERE batch_no=? AND stage_code=? ORDER BY proc_no", (bn, stage))


def _next_proc(bn, stage):
    n = db.one("SELECT COUNT(*) n FROM proc_batches WHERE batch_no=? AND stage_code=?", (bn, stage))['n'] + 1
    return f'{bn}/{stage}{n:02d}'


def _avail(proc, stage_bucket):
    return inventory.balance(proc, stage_bucket)


def _machines(letter):
    return db.q("SELECT machine_code, name FROM machines WHERE letter=? AND active=1 ORDER BY machine_code", (letter,))


def _common(f, machine=True):
    """الحقول الإلزامية المشتركة لسندات الرباط. يعيد رسالة خطأ أو None.
    الماكينة والأوقات لمرحلتي التصنيع والتغليف فقط؛ البوكسات والكراتين: تاريخ ووردية ومشغّل."""
    need = ('work_date', 'shift', 'operator') + (('machine',) if machine else ())
    if not all(s(f.get(k)) for k in need):
        return 'التاريخ والوردية والمشغّل' + (' والماكينة' if machine else '') + ' بيانات إلزامية'
    if not machine:
        return None
    st, en = s(f.get('start_time')), s(f.get('end_time'))
    if not st or not en:
        return 'وقت البدء ووقت الانتهاء إلزاميان'
    if en <= st:
        return 'وقت الانتهاء يجب أن يكون بعد وقت البدء'
    return None


def _insert_proc(con, proc, bn, stage, parent, source, item, f, qty_in, qty_out, rej, scrap, unit, extra, scrap_unit='قطعة'):
    con.execute("""INSERT INTO proc_batches(proc_no,batch_no,route_code,stage_code,parent_proc,source_ref,item_code,machine,
                operator,shift,work_date,start_time,end_time,qty_in,qty_out,qty_reject,qty_scrap,scrap_unit,unit,extra_json,
                notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proc, bn, mfg.BANDAGE, stage, parent, source, item, s(f.get('machine')), s(f.get('operator')),
                 s(f.get('shift')), s(f.get('work_date')), s(f.get('start_time')), s(f.get('end_time')), qty_in, qty_out,
                 rej, scrap, scrap_unit, unit, json.dumps(extra, ensure_ascii=False), s(f.get('notes')), g.user['username']))
    if parent:
        con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                    (proc, parent, 'PROC', qty_out, unit, proc))
    con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))


def _maybe_ready_for_qc(w, bn):
    """عند اكتمال الكمية المطلوبة في الكراتين ينتظر المنتج الفحص النهائي (بعد تثبيت السند)."""
    o = mfg.output(bn)
    if o['good'] >= float(w.get('qty_required') or 0) - 1e-6 and not mfg.final_release(bn):
      with db.tx() as con:
        notify.push(con, 'ready_qc', f'اكتمل إنتاج التشغيلة {bn} من الرباط الضاغط وأصبح جاهزًا للفحص النهائي',
                    f'{w["item_code"]} — {o["good"]:g} رول', url_for('quality_home'), bn, mfg.BANDAGE, ('qc_record',))


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    def gate(area, bn, line):
        from app import qc_gate                      # ربط الإنتاج بإفراج الجودة (تنبيه/منع حسب الإعداد)
        return qc_gate(area, bn, line)

    @route('/bandage', 'bandage_home')
    def bandage_home():
        rows = db.q("""SELECT * FROM work_orders WHERE route_code=? AND COALESCE(order_type,'إنتاج')='إنتاج'
                       AND status IN ('صادر','قيد التنفيذ') ORDER BY batch_start_date DESC, batch_no DESC""", (mfg.BANDAGE,))
        for r in rows:
            r['prog'] = mfg.progress(r['batch_no'])
        return render_template('bandage_home.html', nav='bandage', rows=rows, jumbo=free_jumbo())

    # ------------------------------------------------------------ تخصيص الجامبو
    @route('/bandage/<path:bn>/alloc', 'bandage_alloc', methods=['GET', 'POST'])
    def bandage_alloc(bn):
        w = _bw(bn)
        if not w:
            flash('اختر أمر إنتاج على مسار الرباط', 'bad'); return redirect(url_for('bandage_home'))
        auth.need_line(mfg.BANDAGE)
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        pw = num_unit(it.get('width_cm'))
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            back = url_for('bandage_alloc', bn=bn)
            roll_no = s(f.get('roll_no'))
            rl = next((r for r in _rolls("AND rl.roll_no=?", (roll_no,))), None) if roll_no else None
            if not (s(f.get('alloc_date')) and s(f.get('operator'))):
                flash('التاريخ واسم المسؤول إلزاميان', 'bad'); return redirect(back)
            if not rl:
                flash('اختر جامبو رول من القائمة', 'bad'); return redirect(back)
            if rl['stock_status'] != 'مفرج':
                flash(f'لا يجوز استخدام جامبو رول غير مفرج عنه (الحالة: {rl["stock_status"]})', 'bad'); return redirect(back)
            if rl.get('batch_no') and rl['batch_no'] != bn:
                flash(f'الجامبو مخصص مسبقًا للتشغيلة {rl["batch_no"]}', 'bad'); return redirect(back)
            if rl['remaining'] <= 1e-9:
                flash('لا رصيد متبقٍ في هذا الجامبو', 'bad'); return redirect(back)
            if pw and rl.get('width_cm') and float(rl['width_cm']) < pw:
                flash(f'عرض الجامبو {rl["width_cm"]:g} سم أقل من عرض المنتج {pw:g} سم', 'bad'); return redirect(back)
            ascope, afmt = doc_scope('ALC', f.get('alloc_date'))
            with db.tx() as con:
                doc = db.alloc(con, ascope, afmt)
                con.execute("""INSERT INTO allocations(doc_no,batch_no,route_code,source_type,source_ref,qty,unit,alloc_date,
                            operator,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (doc, bn, mfg.BANDAGE, 'JUMBO', roll_no, rl['remaining'], 'م', s(f.get('alloc_date')),
                             s(f.get('operator')), g.user['username']))
                con.execute("UPDATE rolls SET batch_no=?, issue_date=? WHERE roll_no=?", (bn, s(f.get('alloc_date')), roll_no))
                con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                            (bn, roll_no, 'JUMBO', rl['remaining'], 'م', doc))
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
                notify.push(con, 'order', f'أصبح أمر إنتاج الرباط {bn} جاهزًا للتشغيل — خُصّص الجامبو {roll_no}',
                            f'{w["item_code"]} · {rl["remaining"]:g} م', url_for('bandage_machine', bn=bn), bn,
                            mfg.BANDAGE, ('enter',))
            db.log('create', 'allocations', doc, f'{bn}; {roll_no}')
            flash(f'خُصّص الجامبو {roll_no} للتشغيلة {bn} — السند {doc}', 'ok')
            return redirect(back)
        return render_template('bandage_alloc.html', nav='bandage', bn=bn, b=w, product_width=pw,
                               free=free_jumbo(), mine=[r for r in _rolls("AND rl.batch_no=?", (bn,))],
                               progress=mfg.progress(bn))

    # ------------------------------------------------------------ المراحل
    for name, st in (('bandage_machine', 'machine'), ('bandage_wrap', 'wrap'), ('bandage_box', 'box'),
                     ('bandage_carton', 'carton')):
        app.add_url_rule(f'/bandage/<path:bn>/{st}', endpoint=name,
                         view_func=(lambda st_: (lambda bn: _stage(bn, st_)))(st), methods=['GET', 'POST'])

    def _stage(bn, stage):
        code = {'machine': 'BM', 'wrap': 'BW', 'box': 'BX', 'carton': 'CT'}.get(stage)
        w = _bw(bn)
        if not code or not w:
            flash('أمر إنتاج رباط غير موجود', 'bad'); return redirect(url_for('bandage_home'))
        auth.need_line(mfg.BANDAGE)
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        pm = mfg.pack_map(w['item_code'])
        f = request.form
        back = url_for(f'bandage_{stage}', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            err = _common(f, code in ('BM', 'BW'))
            if err:
                flash(err, 'bad'); return redirect(back)
            try:
                res = {'BM': _do_machine, 'BW': _do_wrap, 'BX': _do_box, 'CT': _do_carton}[code](w, it, pm, f)
            except InsufficientStock as e:
                flash(str(e), 'bad'); return redirect(back)
            if isinstance(res, str):
                flash(res, 'bad'); return redirect(back)
            flash(res['msg'], 'ok')
            return redirect(back)
        rows = _procs(bn, code)
        for r in rows:
            r['x'] = json.loads(r['extra_json'] or '{}')
        srcs = []
        if code == 'BM':
            srcs = [r for r in _rolls("AND rl.batch_no=?", (bn,)) if r['remaining'] > 1e-9]
        else:
            prev, bucket = {'BW': ('BM', 'BM_OUT'), 'BX': ('BW', 'BW_OUT'), 'CT': ('BX', 'BX_OUT')}[code]
            for r in _procs(bn, prev):
                r['available'] = _avail(r['proc_no'], bucket)
                if r['available'] > 1e-9:
                    r['x'] = json.loads(r['extra_json'] or '{}')
                    srcs.append(r)
        return render_template('bandage_stage.html', nav='bandage', bn=bn, b=w, code=code, title=STAGE_TITLE[code],
                               rows=rows, srcs=srcs, pm=pm, machines=_machines('BM' if code == 'BM' else 'BW'),
                               product=it, progress=mfg.progress(bn), stage=stage,
                               defaults=dict(roll_len=num_unit(it.get('length_m'))))

    def _do_machine(w, it, pm, f):
        bn = w['batch_no']
        roll_no = s(f.get('roll_no'))
        rl = next((r for r in _rolls("AND rl.roll_no=? AND rl.batch_no=?", (roll_no, bn))), None)
        inp, out, rej = num(f.get('input_len')), num(f.get('output_rolls')), num(f.get('reject_rolls'), 0) or 0
        rlen = num(f.get('roll_len'))
        scrap = num(f.get('scrap'), 0) or 0
        if not rl:
            return 'اختر جامبو رول مخصصًا لهذه التشغيلة (خصّصه أولًا)'
        if rl['stock_status'] != 'مفرج':
            return 'الجامبو غير مفرج عنه'
        if not inp or inp <= 0 or not out or out <= 0 or out != int(out) or rej < 0 or rej != int(rej) or scrap < 0:
            return 'المدخل والناتج (رولات صحيحة) يجب أن يكونا أكبر من صفر، والمرفوض والهالك غير سالبين'
        if inp - rl['remaining'] > 1e-6:
            return f'طول المدخل {inp:g} م أكبر من المتاح في الجامبو {rl["remaining"]:g} م'
        tol = 1 + float(db.setting('bandage_yield_tolerance_pct', '2') or 2) / 100
        if rlen and (out + rej) * rlen > inp * tol:
            return f'الناتج ({int(out + rej)} رول × {rlen:g} م) أكبر من طول المدخل {inp:g} م — راجع الأرقام'
        if gate('bandage', bn, s(f.get('machine'))):
            return 'المنع مفعّل: سجّل إفراج بدء تشغيل الماكينة من الجودة أولًا'
        proc = _next_proc(bn, 'BM')
        with db.tx() as con:
            _insert_proc(con, proc, bn, 'BM', None, roll_no, w['item_code'], f, inp, out, rej, scrap, 'رول',
                         dict(roll_len=rlen, input_weight=num(f.get('input_weight')), width=rl.get('width_cm')),
                         s(f.get('scrap_unit')) or 'كجم')
            inventory.move(con, roll_no, 'RM', proc, 'USED', inp, 'م', bn, mfg.BANDAGE, 'proc', proc)
            inventory.post(con, proc, 'BM_OUT', out, 'رول', bn, mfg.BANDAGE, 'proc', proc, note=f'من {roll_no}')
            if rej:
                inventory.post(con, proc, 'REJECT', rej, 'رول', bn, mfg.BANDAGE, 'reject', proc)
            if scrap:
                inventory.post(con, proc, 'SCRAP', scrap, s(f.get('scrap_unit')) or 'كجم', bn, mfg.BANDAGE, 'scrap', proc)
            con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                        (proc, roll_no, 'JUMBO', inp, 'م', proc))
            notify.push(con, 'stage', f'اكتمل تشغيل ماكينة الأربطة ({proc}) ويوجد {int(out)} رول جاهز للتغليف',
                        f'{w["item_code"]} · الجامبو {roll_no}', url_for('bandage_wrap', bn=bn), bn, mfg.BANDAGE, ('enter',))
        db.log('create', 'proc_batches', proc, f'in={inp:g}m; out={int(out)}; rej={int(rej)}')
        return dict(msg=f'سُجّل {proc}: {int(out)} رول سليم — بانتظار التغليف')

    def _split(f, avail):
        inp, good, rej = num(f.get('input_qty')), num(f.get('good')), num(f.get('reject'), 0) or 0
        if not inp or inp <= 0 or good is None or good < 0 or rej < 0:
            return None, 'كمية المدخل والسليم يجب أن تكون أرقامًا صحيحة وغير سالبة'
        if inp - avail > 1e-6:
            return None, f'كمية المدخل {inp:g} أكبر من المتاح من المرحلة السابقة {avail:g}'
        if abs(good + rej - inp) > 1e-6:
            return None, f'السليم + المرفوض ({good + rej:g}) يجب أن يساوي كمية المدخل ({inp:g})'
        return (inp, good, rej), None

    def _do_wrap(w, it, pm, f):
        bn, src = w['batch_no'], s(f.get('src'))
        p = db.one("SELECT * FROM proc_batches WHERE proc_no=? AND batch_no=? AND stage_code='BM'", (src, bn)) if src else None
        if not p:
            return 'اختر دفعة ناتجة فعليًا من ماكينة الأربطة'
        vals, err = _split(f, _avail(src, 'BM_OUT'))
        if err:
            return err
        inp, good, rej = vals
        proc = _next_proc(bn, 'BW')
        with db.tx() as con:
            _insert_proc(con, proc, bn, 'BW', src, src, w['item_code'], f, inp, good, rej, 0, 'رول',
                         dict(material=s(f.get('material')), material_lot=s(f.get('material_lot'))))
            if good:
                inventory.move(con, src, 'BM_OUT', proc, 'BW_OUT', good, 'رول', bn, mfg.BANDAGE, 'proc', proc)
            if rej:
                inventory.move(con, src, 'BM_OUT', proc, 'REJECT', rej, 'رول', bn, mfg.BANDAGE, 'reject', proc)
            notify.push(con, 'stage', f'اكتمل تغليف {int(good)} رول ({proc}) وأصبحت جاهزة للتعبئة في البوكسات',
                        w['item_code'], url_for('bandage_box', bn=bn), bn, mfg.BANDAGE, ('enter',))
        db.log('create', 'proc_batches', proc, f'in={inp:g}; wrapped={good:g}; rej={rej:g}')
        return dict(msg=f'سُجّل {proc}: {int(good)} رول مغلَّف')

    def _do_box(w, it, pm, f):
        bn, src = w['batch_no'], s(f.get('src'))
        p = db.one("SELECT * FROM proc_batches WHERE proc_no=? AND batch_no=? AND stage_code='BW'", (src, bn)) if src else None
        if not p:
            return 'اختر دفعة ناتجة فعليًا من تغليف الأربطة'
        if 'box' not in pm:
            return 'تكوين التعبئة (رول ← بوكس) غير معرَّف لهذا الصنف — عرّفه من «بيانات المنتجات»'
        vals, err = _split(f, _avail(src, 'BW_OUT'))
        if err:
            return err
        inp, good, rej = vals
        per = pm['box']['per_parent']
        full, rest = divmod(good, per)
        if rest and not pm['box']['allow_partial']:
            return f'{good:g} رول لا تكمل بوكسات كاملة (لكل بوكس {per:g}) والبوكس الجزئي غير مسموح'
        boxes = int(full + (1 if rest else 0))
        if boxes <= 0:
            return 'لا توجد كمية سليمة للتعبئة'
        proc = _next_proc(bn, 'BX')
        with db.tx() as con:
            _insert_proc(con, proc, bn, 'BX', src, src, w['item_code'], f, inp, boxes, rej, 0, 'بوكس',
                         dict(rolls=good, rolls_per_box=per, full_boxes=int(full), partial_rolls=int(rest)))
            mid = inventory.move(con, src, 'BW_OUT', proc, 'BX_OUT', good, 'رول', bn, mfg.BANDAGE, 'proc', proc,
                                 note=f'{boxes} بوكس')
            # الكمية الداخلة للمرحلة التالية تُقاس بالبوكس: نعدّل القيد الوارد إلى وحدة البوكس
            con.execute("UPDATE stock_tx SET qty=?, unit='بوكس' WHERE move_id=? AND stage='BX_OUT'", (boxes, mid))
            if rej:
                inventory.move(con, src, 'BW_OUT', proc, 'REJECT', rej, 'رول', bn, mfg.BANDAGE, 'reject', proc)
            notify.push(con, 'stage', f'اكتملت تعبئة البوكسات ({proc}) وأصبحت جاهزة لتعبئة الكراتين',
                        f'{boxes} بوكس', url_for('bandage_carton', bn=bn), bn, mfg.BANDAGE, ('enter',))
        db.log('create', 'proc_batches', proc, f'rolls={good:g}; boxes={boxes}')
        return dict(msg=f'سُجّل {proc}: {boxes} بوكس' + (f' (آخرها جزئي {int(rest)} رول)' if rest else ''))

    def _do_carton(w, it, pm, f):
        bn, src = w['batch_no'], s(f.get('src'))
        p = db.one("SELECT * FROM proc_batches WHERE proc_no=? AND batch_no=? AND stage_code='BX'", (src, bn)) if src else None
        if not p:
            return 'اختر دفعة بوكسات ناتجة فعليًا من مرحلة البوكسات'
        if 'carton' not in pm:
            return 'تكوين التعبئة (بوكس ← كرتون) غير معرَّف لهذا الصنف — عرّفه من «بيانات المنتجات»'
        boxes_in = num(f.get('boxes_in'))
        avail = _avail(src, 'BX_OUT')
        if not boxes_in or boxes_in <= 0 or boxes_in != int(boxes_in):
            return 'عدد البوكسات يجب أن يكون عددًا صحيحًا أكبر من صفر'
        if boxes_in - avail > 1e-6:
            return f'عدد البوكسات {boxes_in:g} أكبر من المتاح {avail:g}'
        per = pm['carton']['per_parent']
        full, rest = divmod(boxes_in, per)
        if rest and not pm['carton']['allow_partial']:
            return f'{boxes_in:g} بوكس لا تكمل كراتين كاملة (لكل كرتون {per:g}) والكرتون الجزئي غير مسموح'
        cartons = int(full + (1 if rest else 0))
        sx = json.loads(p['extra_json'] or '{}')
        rolls = round((sx.get('rolls') or 0) * boxes_in / (p['qty_out'] or 1))
        proc = _next_proc(bn, 'CT')
        with db.tx() as con:
            _insert_proc(con, proc, bn, 'CT', src, src, w['item_code'], f, boxes_in, cartons, 0, 0, 'كرتون',
                         dict(rolls=rolls, boxes=int(boxes_in), full_cartons=int(full), partial_boxes=int(rest)))
            mid = inventory.move(con, src, 'BX_OUT', proc, 'CT_OUT', boxes_in, 'بوكس', bn, mfg.BANDAGE, 'proc', proc)
            con.execute("UPDATE stock_tx SET qty=?, unit='كرتون' WHERE move_id=? AND stage='CT_OUT'", (cartons, mid))
        _maybe_ready_for_qc(w, bn)
        db.log('create', 'proc_batches', proc, f'boxes={boxes_in:g}; cartons={cartons}')
        return dict(msg=f'سُجّل {proc}: {cartons} كرتون ({rolls} رول) — المنتج النهائي بانتظار الفحص النهائي')
