# -*- coding: utf-8 -*-
"""مسار الرباط الضاغط (v14 مبسّط): جامبو رول ← ماكينة الأربطة ← تغليف ← (إنهاء الإنتاج) ← موافقة الجودة ← مخزن.

  • الأمر = المنتج + الكمية. طول الرول وعدد الرولات في البوكس/الكرتون من بيانات المنتج (لا تُكتب هنا).
  • المشغّل والتاريخ والماكينة تلقائية؛ يُدخل المستخدم الكميات الفعلية فقط، والمقترح جاهز مسبقًا.
  • البوكس والكرتون وحدات تعبئة تُحسب تلقائيًا من الناتج — ليست شاشات ولا مراحل.
  • كل حركة كمية قيد في دفتر المخزون؛ ولا تقبل أي مرحلة أكثر مما خرج من سابقتها.
"""
import json, datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory
from util import s, num, num_unit, fmt_qty, doc_scope
from inventory import InsufficientStock

STAGE_TITLE = {'BM': 'ماكينة الأربطة', 'BW': 'تغليف الأربطة'}


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


def _machines(letter):
    return db.q("SELECT machine_code, name FROM machines WHERE letter=? AND active=1 ORDER BY machine_code", (letter,))


def _insert_proc(con, proc, bn, stage, parent, source, item, machine, qty_in, qty_out, rej, scrap, unit, extra,
                 notes=None, scrap_unit='قطعة'):
    now = datetime.datetime.now()
    con.execute("""INSERT INTO proc_batches(proc_no,batch_no,route_code,stage_code,parent_proc,source_ref,item_code,machine,
                operator,work_date,start_time,end_time,qty_in,qty_out,qty_reject,qty_scrap,scrap_unit,unit,extra_json,
                notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proc, bn, mfg.BANDAGE, stage, parent, source, item, machine, g.user['full_name'], now.date().isoformat(),
                 None, now.strftime('%H:%M'), qty_in, qty_out, rej, scrap, scrap_unit, unit,
                 json.dumps(extra, ensure_ascii=False), notes, g.user['username']))
    if parent:
        con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                    (proc, parent, 'PROC', qty_out, unit, proc))
    con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/bandage', 'bandage_home')
    def bandage_home():
        import prod
        rows = prod.visible_orders(200, "AND route_code='BANDAGE' AND final_status IS NULL AND status IN ('صادر','قيد التنفيذ')")
        return render_template('line_orders.html', nav='bandage', title='خط الرباط الضاغط', rows=rows,
                               sub='جامبو رول ← ماكينة الأربطة ← تغليف — ثم إنهاء الإنتاج وإرساله للجودة',
                               extra=f'جامبو مفرج عنه غير مخصص: {len(free_jumbo())}', fmt=fmt_qty, gen='bandage')

    # ------------------------------------------------------------ تخصيص الجامبو (نقرة واحدة)
    @route('/bandage/<path:bn>/alloc', 'bandage_alloc', methods=['GET', 'POST'])
    def bandage_alloc(bn):
        w = _bw(bn)
        if not w:
            flash('اختر أمر إنتاج على مسار الرباط', 'bad'); return redirect(url_for('bandage_home'))
        auth.need_line(mfg.BANDAGE)
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        pw = num_unit(it.get('width_cm'))
        back = url_for('bandage_alloc', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            roll_no = s(request.form.get('roll_no'))
            rl = next((r for r in _rolls("AND rl.roll_no=?", (roll_no,))), None) if roll_no else None
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
            today = datetime.date.today().isoformat()
            ascope, afmt = doc_scope('ALC', today)
            with db.tx() as con:
                doc = db.alloc(con, ascope, afmt)
                con.execute("""INSERT INTO allocations(doc_no,batch_no,route_code,source_type,source_ref,qty,unit,alloc_date,
                            operator,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (doc, bn, mfg.BANDAGE, 'JUMBO', roll_no, rl['remaining'], 'م', today,
                             g.user['full_name'], g.user['username']))
                con.execute("UPDATE rolls SET batch_no=?, issue_date=?, use_status='مخصص' WHERE roll_no=?", (bn, today, roll_no))
                con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                            (bn, roll_no, 'JUMBO', rl['remaining'], 'م', doc))
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            db.log('create', 'allocations', doc, f'{bn}; {roll_no}')
            flash(f'خُصّص الجامبو {roll_no} للتشغيلة {bn}', 'ok')
            import prod
            prod.next_bar(('تشغيل ماكينة الأربطة', url_for('bandage_machine', bn=bn)),
                          ('أمر الإنتاج', url_for('order_view', bn=bn), False))
            return redirect(back)
        adoc = {a['source_ref']: a['doc_no'] for a in db.q("SELECT doc_no, source_ref FROM allocations WHERE batch_no=? AND voided=0", (bn,))}
        return render_template('bandage_alloc.html', nav='bandage', bn=bn, b=w, product_width=pw, fmt=fmt_qty, adoc=adoc,
                               free=free_jumbo(), mine=[r for r in _rolls("AND rl.batch_no=?", (bn,))])

    # ------------------------------------------------------------ المراحل (ماكينة الأربطة، التغليف)
    for name, st in (('bandage_machine', 'machine'), ('bandage_wrap', 'wrap')):
        app.add_url_rule(f'/bandage/<path:bn>/{st}', endpoint=name,
                         view_func=(lambda st_: (lambda bn: _stage(bn, st_)))(st), methods=['GET', 'POST'])

    def _stage(bn, stage):
        code = {'machine': 'BM', 'wrap': 'BW'}.get(stage)
        w = _bw(bn)
        if not code or not w:
            flash('أمر إنتاج رباط غير موجود', 'bad'); return redirect(url_for('bandage_home'))
        auth.need_line(mfg.BANDAGE)
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for(f'bandage_{stage}', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر', 'bad'); return redirect(back)
            try:
                res = (_do_machine if code == 'BM' else _do_wrap)(w, it, request.form)
            except InsufficientStock as e:
                flash(str(e), 'bad'); return redirect(back)
            if isinstance(res, str):
                flash(res, 'bad'); return redirect(back)
            flash(res['msg'], 'ok')
            import prod
            st = mfg.order_state(bn)
            n = st['next']
            btns = []
            if n and n['kind'] in ('work', 'finish') and n.get('endpoint'):
                btns.append((n['label'], url_for(n['endpoint'], bn=bn) + ('#finish' if n['kind'] == 'finish' else '')))
            btns.append(('أمر الإنتاج', url_for('order_view', bn=bn), not btns))
            prod.next_bar(*btns)
            return redirect(back)
        rows = _procs(bn, code)
        for r in rows:
            r['x'] = json.loads(r['extra_json'] or '{}')
        srcs = []
        if code == 'BM':
            srcs = [r for r in _rolls("AND rl.batch_no=?", (bn,)) if r['remaining'] > 1e-9]
        else:
            for r in _procs(bn, 'BM'):
                r['available'] = inventory.balance(r['proc_no'], 'BM_OUT')
                if r['available'] > 1e-9:
                    srcs.append(r)
        return render_template('bandage_stage.html', nav='bandage', bn=bn, b=w, code=code, title=STAGE_TITLE[code],
                               rows=rows, srcs=srcs, machines=_machines('BM' if code == 'BM' else 'BW'), fmt=fmt_qty,
                               product=it, progress=mfg.progress(bn), stage=stage, st=mfg.order_state(bn),
                               roll_len=num_unit(it.get('length_m')))

    def _machine_of(f, letter):
        ms = _machines(letter)
        want = s(f.get('machine'))
        if want and any(m['machine_code'] == want for m in ms):
            return want
        return ms[0]['machine_code'] if ms else None

    def _do_machine(w, it, f):
        bn = w['batch_no']
        roll_no = s(f.get('roll_no'))
        rl = next((r for r in _rolls("AND rl.roll_no=? AND rl.batch_no=?", (roll_no, bn))), None)
        out, rej = num(f.get('output_rolls')), num(f.get('reject_rolls'), 0) or 0
        rlen = num_unit(it.get('length_m'))              # طول الرول من بيانات المنتج
        inp = num(f.get('input_len')) or (rl['remaining'] if rl else None)
        scrap = num(f.get('scrap'), 0) or 0
        if not rl:
            return 'اختر جامبو رول مخصصًا لهذه التشغيلة (خصّصه أولًا)'
        if rl['stock_status'] != 'مفرج':
            return 'الجامبو غير مفرج عنه'
        if not inp or inp <= 0 or not out or out <= 0 or out != int(out) or rej < 0 or rej != int(rej) or scrap < 0:
            return 'الناتج (رولات صحيحة) يجب أن يكون أكبر من صفر، والمرفوض والهالك غير سالبين'
        if inp - rl['remaining'] > 1e-6:
            return f'طول المدخل {fmt_qty(inp)} م أكبر من المتاح في الجامبو {fmt_qty(rl["remaining"])} م'
        tol = 1 + float(db.setting('bandage_yield_tolerance_pct', '2') or 2) / 100
        if rlen and (out + rej) * rlen > inp * tol:
            return f'الناتج ({int(out + rej)} رول × {fmt_qty(rlen)} م) أكبر من طول المدخل {fmt_qty(inp)} م — راجع الأرقام'
        sunit = s(f.get('scrap_unit')) or 'كجم'
        with db.tx() as con:
            proc = mfg.alloc_proc_no(con, bn, 'BM')
            _insert_proc(con, proc, bn, 'BM', None, roll_no, w['item_code'], _machine_of(f, 'BM'), inp, out, rej, scrap, 'رول',
                         dict(roll_len=rlen, width=rl.get('width_cm')), s(f.get('notes')), sunit)
            inventory.move(con, roll_no, 'RM', proc, 'USED', inp, 'م', bn, mfg.BANDAGE, 'proc', proc)
            inventory.post(con, proc, 'BM_OUT', out, 'رول', bn, mfg.BANDAGE, 'proc', proc, note=f'من {roll_no}')
            if rej:
                inventory.post(con, proc, 'REJECT', rej, 'رول', bn, mfg.BANDAGE, 'reject', proc)
            if scrap:
                inventory.post(con, proc, 'SCRAP', scrap, sunit, bn, mfg.BANDAGE, 'scrap', proc)
            con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                        (proc, roll_no, 'JUMBO', inp, 'م', proc))
        db.log('create', 'proc_batches', proc, f'in={inp:g}m; out={int(out)}; rej={int(rej)}')
        return dict(msg=f'سُجّل {proc}: {int(out)} رول سليم — بانتظار التغليف')

    def _do_wrap(w, it, f):
        bn, src = w['batch_no'], s(f.get('src'))
        p = db.one("SELECT * FROM proc_batches WHERE proc_no=? AND batch_no=? AND stage_code='BM'", (src, bn)) if src else None
        if not p:
            return 'اختر دفعة ناتجة فعليًا من ماكينة الأربطة'
        avail = inventory.balance(src, 'BM_OUT')
        good = num(f.get('good'))
        rej = num(f.get('reject'), 0) or 0
        inp = good + rej if good is not None else None
        if good is None or good < 0 or rej < 0 or not inp:
            return 'الكمية السليمة يجب أن تكون رقمًا صحيحًا وغير سالب'
        if inp - avail > 1e-6:
            return f'الكمية {fmt_qty(inp)} أكبر من المتاح من المرحلة السابقة {fmt_qty(avail)}'
        with db.tx() as con:
            proc = mfg.alloc_proc_no(con, bn, 'BW')
            _insert_proc(con, proc, bn, 'BW', src, src, w['item_code'], _machine_of(f, 'BW'), inp, good, rej, 0, 'رول', {},
                         s(f.get('notes')))
            if good:
                inventory.move(con, src, 'BM_OUT', proc, 'BW_OUT', good, 'رول', bn, mfg.BANDAGE, 'proc', proc)
            if rej:
                inventory.move(con, src, 'BM_OUT', proc, 'REJECT', rej, 'رول', bn, mfg.BANDAGE, 'reject', proc)
        db.log('create', 'proc_batches', proc, f'in={inp:g}; wrapped={good:g}; rej={rej:g}')
        return dict(msg=f'سُجّل {proc}: {int(good)} رول مغلَّف')
