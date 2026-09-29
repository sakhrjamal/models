# -*- coding: utf-8 -*-
"""الإنتاج المبسّط (v14): المنتج يقود كل شيء، والمستخدم يدخل فقط ما لا يعرفه النظام.

  أمر الإنتاج  = المنتج + الكمية                    (الباقي من بيانات المنتج / BOM / المسار)
  السليتر      = اختيار جامبو ← خطة قص تلقائية ← سب رول SR-0001..                (بلا إدخال عرض/عدد)
  الطي         = اختيار سب رول + الكمية الفعلية فقط  (الاستهلاك والمخزون والأرقام تلقائية)
  إنهاء الإنتاج ← إشعار واحد للجودة ← موافقة ← تخزين  (انظر qa.py و lines.warehouse)

كل السندات تحتفظ بالتتبع الكامل: جامبو ← سب رول ← سند طي ← منتج نهائي ← موافقة ← مخزن.
"""
import datetime, json
from flask import render_template, request, redirect, url_for, flash, abort, g, session, jsonify

import db, auth, mfg, inventory, notify, forms
from util import s, num, batch_stem, fmt_qty, doc_scope, MONTHS


class SubRollUsed(Exception):
    def __init__(self, doc):
        super().__init__(doc)
        self.doc = doc


def used_msg(doc):
    return f'هذا الرول الفرعي تم استخدامه مسبقا في سند الإنتاج رقم {doc}.'


def next_bar(*buttons):
    """أزرار «الخطوة التالية» تظهر مرة واحدة في الصفحة التالية: (نص, رابط[, أساسي])."""
    session['_next'] = [dict(label=b[0], url=b[1], primary=(len(b) < 3 or b[2])) for b in buttons]


def order_or_404(bn):
    w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND COALESCE(order_type,'إنتاج')='إنتاج'", (bn,))
    if not w:
        abort(404)
    auth.need_line(mfg.route_of_wo(w))
    return w


def next_url(st):
    n = st.get('next') or {}
    return url_for(n['endpoint'], bn=st['batch_no']) if n.get('endpoint') else None


def visible_orders(limit=400, where='', args=()):
    rows = db.q(f"""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' {where}
                    ORDER BY batch_start_date DESC, rowid DESC LIMIT {int(limit)}""", tuple(args))
    lines = auth.user_lines()
    out = []
    with db.reuse():                      # اتصال واحد لحساب حالة عشرات الأوامر
        for w in rows:
            if lines and mfg.route_of_wo(w) not in lines:
                continue
            w['st'] = mfg.order_state(w)
            w['nurl'] = next_url(w['st'])
            out.append(w)
    return out


def jumbo_rows(w, item):
    """جامبو رول متاح لهذا الأمر: مفرج، غير مخصص، ومن خامة المنتج (BOM)."""
    prefs = mfg.prefixes_of_class('ROLL')
    ph = ','.join('?' * len(prefs)) or "''"
    args = list(prefs)
    where = ''
    raw = w.get('raw_item') or item.get('raw_item')
    if raw:
        where = ' AND rl.item_code=?'
        args.append(raw)
    rows = db.q(f"""SELECT rl.*, i.description, rc.supplier_lot slot
                    FROM rolls rl JOIN items i ON i.item_code=rl.item_code
                    LEFT JOIN receipts rc ON rc.grn_no=rl.grn_no
                    WHERE i.prefix IN ({ph}) AND rl.stock_status='مفرج' AND IFNULL(rl.batch_no,'')=''
                      AND IFNULL(rl.use_status,'متاح') IN ('متاح','') {where} ORDER BY rl.roll_no""", tuple(args))
    for r in rows:
        r['jw'] = r.get('width_cm') or mfg._setting_f('default_jumbo_width_cm', 120)
        r['jw_default'] = not r.get('width_cm')
    return rows


def _year(d=None):
    return (d or datetime.date.today()).strftime('%Y')


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    # ============================================================ لوحة الرئيسية
    @route('/', 'index')
    def index():
        u = g.user
        prod = auth.can('prod_view')
        qual = auth.can('qual_view')
        orders = visible_orders(500, "AND IFNULL(status,'')<>'مغلقة'") if prod else []
        by = {}
        for w in orders:
            by[w['st']['stage']] = by.get(w['st']['stage'], 0) + 1
        active = [w for w in orders if w['st']['stage'] in ('new', 'slitting', 'ready_fold', 'production')]
        pend_q = db.one("SELECT COUNT(*) n FROM work_orders WHERE final_status=?", (mfg.FS_PENDING,))['n'] if (qual or prod) else 0
        held = db.one("SELECT COUNT(*) n FROM work_orders WHERE final_status IN (?,?)", (mfg.FS_REJECTED, mfg.FS_HOLD))['n']
        raw_pending = db.one("SELECT COUNT(*) n FROM receipts WHERE inspection_no IS NULL")['n'] if qual else 0
        to_store = db.one("SELECT COUNT(*) n FROM work_orders WHERE final_status=?", (mfg.FS_APPROVED,))['n']
        done_today = db.one("SELECT COUNT(*) n FROM approvals WHERE decision='Approved' AND date(ts)=date('now','localtime')")['n']
        pending_list = db.q("""SELECT w.batch_no, w.wo_no, w.item_code, w.qty_required, w.uom, w.produced_qty, w.finished_at
                               FROM work_orders w WHERE w.final_status=? ORDER BY w.finished_at""", (mfg.FS_PENDING,)) if qual else []
        return render_template('home.html', nav='home', prod=prod, qual=qual, by=by, active=active[:12], pend_q=pend_q,
                               held=held, raw_pending=raw_pending, to_store=to_store, done_today=done_today,
                               pending_list=pending_list, notes=notify.for_user(u, limit=5, only_unread=True),
                               store=auth.can('warehouse'), stages=mfg.STAGE_UI, fmt=fmt_qty)

    # ============================================================ أوامر الإنتاج
    @route('/orders', 'wo_list')
    def wo_list():
        stage = s(request.args.get('stage'))
        rows = visible_orders(300)
        counts = {}
        for w in rows:
            counts[w['st']['stage']] = counts.get(w['st']['stage'], 0) + 1
        if stage:
            rows = [w for w in rows if w['st']['stage'] == stage]
        return render_template('wo_list.html', nav='orders', rows=rows, stage=stage, counts=counts,
                               stages=mfg.STAGE_UI, total=sum(counts.values()), fmt=fmt_qty)

    @route('/api/items/search', 'api_items_search')
    def api_items_search():
        """بحث أصناف المنتجات التامة القابلة للتصنيع — منتقي أمر الإنتاج."""
        q_ = (request.args.get('q') or '').strip()
        like = f'%{q_}%'
        rows = db.q("""SELECT i.item_code, i.description, i.size, i.ply, i.sterile, i.route_code, i.uom, r.name_ar route_name
                       FROM items i JOIN routes r ON r.code=i.route_code AND r.active=1
                       WHERE i.status='نشط' AND (i.item_code LIKE ? OR i.description LIKE ?)
                         AND (i.route_code<>'FULL_GAUZE' OR i.machine_code IS NOT NULL)
                       ORDER BY i.item_code LIMIT 40""", (like, like))
        return jsonify(items=rows)

    @route('/api/order_preview', 'api_order_preview')
    def api_order_preview():
        """كل ما سيُعبَّأ تلقائيًا للأمر: بيانات المنتج والمسار والاحتياج ورقم التشغيلة المتوقع."""
        it = db.one("SELECT * FROM items WHERE item_code=?", ((request.args.get('item') or '').strip().upper(),))
        if not it or not it.get('route_code'):
            return jsonify(ok=False, msg='الصنف غير موجود أو بلا مسار تصنيع — عرّفه من «الأصناف»')
        qty = num(request.args.get('qty'), 0)
        card = mfg.product_card(it, qty)
        letter = mfg.batch_letter(it['route_code'], it)
        if letter:
            _, stem = batch_stem(request.args.get('date'), letter)
            card['batch_preview'] = f'{stem}{forms.next_batch_seq(stem):03d}'
        warn = list(card.get('req', {}).get('missing', []) and
                    ['بيانات المنتج ناقصة: ' + '، '.join(card['req']['missing']) + ' — أكملها من «الأصناف»'] or [])
        if it['route_code'] == mfg.FULL and not it.get('machine_code'):
            warn.append('لا توجد ماكينة طي محددة للصنف في بيانات المنتج')
        if it['route_code'] == mfg.FULL and card.get('req', {}).get('ok') and not jumbo_rows({'raw_item': it.get('raw_item')}, it):
            warn.append('لا يوجد حاليًا جامبو رول مفرج عنه للخامة المطلوبة (لن يمنع إصدار الأمر)')
        card['warn'] = warn
        return jsonify(ok=True, card=card, qty_fmt=fmt_qty(qty))

    @route('/orders/new', 'wo_new', methods=['GET', 'POST'])
    @auth.require('wo_issue')
    def wo_new():
        """أمر إنتاج جديد = المنتج + الكمية فقط. التاريخ والأولوية اختياريان."""
        if request.method == 'POST':
            f = request.form
            code = (s(f.get('item_code')) or '').upper()
            it = db.one("SELECT * FROM items WHERE item_code=? AND status='نشط'", (code,))
            back = url_for('wo_new')
            if not it or not it.get('route_code') or not mfg.route(it['route_code']):
                flash('اختر المنتج من القائمة — الصنف غير موجود أو بلا مسار تصنيع في بيانات المنتج', 'bad')
                return redirect(back)
            qty = num(f.get('qty_required'))
            if not qty or qty <= 0:
                flash('الكمية المطلوبة يجب أن تكون أكبر من صفر', 'bad')
                return redirect(back)
            rc = it['route_code']
            req = mfg.requirements(it, qty) if rc == mfg.FULL else {}
            if rc == mfg.FULL:
                if not it.get('machine_code'):
                    flash(f'الصنف {code} بلا ماكينة طي في بيانات المنتج', 'bad')
                    return redirect(back)
                if not req['ok']:
                    flash('بيانات المنتج ناقصة: ' + '، '.join(req['missing']) + ' — أكملها من «الأصناف» ثم أعد المحاولة', 'bad')
                    return redirect(back)
            letter = mfg.batch_letter(rc, it)
            if not letter:
                flash(f'لا يوجد حرف ترقيم لمسار/ماكينة الصنف {code}', 'bad')
                return redirect(back)
            today = datetime.date.today().isoformat()
            batch_date = s(f.get('batch_date')) or today
            d, stem = batch_stem(batch_date, letter)
            r_ = mfg.route(rc)
            unit = it.get('uom') or r_['base_uom'] or 'قطعة'
            prio = s(f.get('priority')) if s(f.get('priority')) in ('عادي', 'مستعجل') else 'عادي'
            try:
                with db.tx() as con:
                    seq = forms.next_batch_seq(stem)
                    batch = f'{stem}{seq:03d}'
                    scope, fmt = doc_scope('WO', today)
                    wo_no = db.alloc(con, scope, fmt)
                    con.execute("""INSERT INTO work_orders(wo_no,issue_date,issued_by,prod_manager,order_type,batch_kind,
                          item_code,size,ply,xray,mesh,route,qty_required,uom,due_date,month_code,yy,dd,seq,stage_code,
                          batch_no,machine_letter,machine_batch_no,batch_start_date,fold_machine,std_width_cm,status,notes,
                          sr_spec_key,sr_needed,pieces_per_sr,jumbo_needed,route_code,priority,raw_item)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (wo_no, today, g.user['full_name'], g.user['full_name'], 'إنتاج', r_['name_ar'],
                                 code, it.get('size'), it.get('ply'), it.get('xray'), it.get('mesh'),
                                 it.get('sterile') or 'غير معقم', qty, unit, s(f.get('due_date')),
                                 MONTHS[d.month - 1],
                                 int(d.strftime('%y')), d.day, seq, letter, batch, letter, batch, d.isoformat(),
                                 it.get('machine_code') if rc == mfg.FULL else None, req.get('sr_width'), 'صادر',
                                 s(f.get('notes')),
                                 f"{it['machine_code']}-{it['ply']}" if rc == mfg.FULL and it.get('ply') else None,
                                 req.get('sr_needed'), req.get('yield_per_sr'), req.get('jumbo_needed'), rc, prio,
                                 it.get('raw_item')))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            db.log('create', 'work_orders', batch, f'{wo_no}; {code}; {qty:g} {unit}; route={rc}')
            flash(f'صدر أمر الإنتاج {wo_no} — رقم التشغيلة {batch}', 'ok')
            first = ('ابدأ خطة القص', url_for('slit_work', bn=batch)) if rc == mfg.FULL else None
            next_bar(*(([first] if first else []) + [('عرض الأمر', url_for('order_view', bn=batch), not first),
                                                      ('أمر جديد', url_for('wo_new'), False)]))
            return redirect(url_for('order_view', bn=batch))
        return render_template('wo_new.html', nav='orders', today_iso=datetime.date.today().isoformat())

    @route('/orders/<path:bn>/finish', 'order_finish', methods=['POST'])
    def order_finish(bn):
        """إنهاء الإنتاج: إشعار واحد للجودة بانتظار الموافقة على التخزين."""
        auth.need('enter')
        w = order_or_404(bn)
        st = mfg.order_state(w)
        back = url_for('order_view', bn=bn)
        if w.get('final_status'):
            flash('أُنهي إنتاج هذه الدفعة مسبقًا', 'bad')
            return redirect(back)
        if not st['can_finish']:
            flash('لا يمكن الإنهاء قبل تسجيل أي إنتاج فعلي', 'bad')
            return redirect(back)
        if st['remaining'] > 1e-6 and not request.form.get('short_ok'):
            flash(f'الكمية المنتجة ({fmt_qty(st["produced"])}) أقل من المطلوب ({fmt_qty(st["required"])}) — أكّد الإنهاء بالكمية الفعلية', 'bad')
            return redirect(back)
        qty, unit = st['produced'], st['unit']
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        with db.tx() as con:
            con.execute("""UPDATE work_orders SET final_status=?, finished_at=?, finished_by=?, produced_qty=?,
                           status='قيد التنفيذ' WHERE batch_no=? AND final_status IS NULL""",
                        (mfg.FS_PENDING, now, g.user['full_name'], qty, bn))
            notify.push(con, 'production_complete',
                        f'تم الانتهاء من إنتاج الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، والكمية النهائية {fmt_qty(qty)} {unit}، '
                        f'وهي بانتظار موافقة الجودة للتخزين.',
                        f'{w["item_code"]} · المطلوب {fmt_qty(st["required"])} {unit}',
                        url_for('quality_review', bn=bn), bn, mfg.route_of_wo(w), ('qc_sign',))
        db.log('finish_production', 'work_orders', bn, f'{fmt_qty(qty)} {unit} of {fmt_qty(st["required"])}')
        flash(f'أُنهي الإنتاج وأُرسلت الدفعة {bn} إلى الجودة ({fmt_qty(qty)} {unit})', 'ok')
        next_bar(('العودة لقائمة الأوامر', url_for('wo_list')), ('أمر جديد', url_for('wo_new'), False)
                 ) if auth.can('wo_issue') else next_bar(('العودة لقائمة الأوامر', url_for('wo_list')))
        return redirect(back)

    @route('/orders/<path:bn>', 'order_view')
    def order_view(bn):
        w = order_or_404(bn)
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        st = mfg.order_state(w)
        rc = st['route']
        ctx = dict(nav='orders', w=w, st=st, it=it, prog=mfg.progress(bn), nurl=next_url(st), fmt=fmt_qty,
                   card=mfg.product_card(it, w['qty_required']) if it else {}, fs_ar=mfg.FS_AR)
        if rc == mfg.FULL:
            plans = db.q('SELECT * FROM cutting_plans WHERE batch_no=? ORDER BY plan_no', (bn,))
            for p in plans:
                p['subs'] = db.q('SELECT * FROM subrolls WHERE plan_no=? ORDER BY tag_no', (p['plan_no'],))
            ctx['plans'] = plans
            ctx['docs'] = db.q("""SELECT * FROM folding_out WHERE batch_no=? ORDER BY id""", (bn,))
        ctx['approval'] = mfg.approval_of(bn)
        ctx['equiv'] = mfg.pack_equiv(w['item_code'], st['produced'], st['unit']) if it else []
        ctx['editable'] = not w.get('final_status') or w['final_status'] == mfg.FS_PENDING
        return render_template('order_view.html', **ctx)

    # ============================================================ السليتر
    def _slit_orders():
        rows = [w for w in visible_orders(300, "AND IFNULL(route_code,'FULL_GAUZE')='FULL_GAUZE' AND final_status IS NULL")
                if w['st']['stage'] in ('new', 'slitting', 'ready_fold', 'production')]
        return rows

    @route('/slitter', 'slit')
    def slit():
        rows = [w for w in _slit_orders() if w['st']['sr_executed'] < max(w['st'].get('sr_needed', 0), 1)
                or w['st']['stage'] in ('new', 'slitting')]
        free = db.one("""SELECT COUNT(*) n FROM rolls rl JOIN items i ON i.item_code=rl.item_code
                         WHERE i.prefix IN ({}) AND rl.stock_status='مفرج' AND IFNULL(rl.batch_no,'')=''
                         AND IFNULL(rl.use_status,'متاح') IN ('متاح','')""".format(
                             ','.join('?' * len(mfg.prefixes_of_class('ROLL'))) or "''"),
                      tuple(mfg.prefixes_of_class('ROLL')))['n']
        return render_template('slit_list.html', nav='slit', rows=rows, free=free, fmt=fmt_qty)

    def _plan_no(con):
        return db.alloc(con, f'CP-{_year()}', f'CP-{_year()}-{{n6}}')

    def _make_plan(con, w, it, roll, run):
        """يُنشئ خطة قص واحدة لجامبو ويُولّد السب رول من بيانات المنتج (لا إدخال عرض/عدد)."""
        bn = w['batch_no']
        jw = float(roll.get('width_cm') or mfg._setting_f('default_jumbo_width_cm', 120))
        req = mfg.requirements(it, w['qty_required'], jw)
        srw, n_sub, edge = req['sr_width'], req['per_jumbo'], req['edge_each']
        if not req['ok'] or n_sub < 1:
            raise ValueError('بيانات المنتج ناقصة أو عرض الجامبو لا يتسع لسب رول واحد: ' + '، '.join(req['missing']))
        plan_no = _plan_no(con)
        status = 'منفّذ' if run else 'مخطط'
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        who = g.user['full_name']
        con.execute("""INSERT INTO cutting_plans(plan_no,batch_no,roll_no,jumbo_width,usable_width,sr_width,n_sub,edge_each,
                       status,created_by,executed_by,executed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan_no, bn, roll['roll_no'], jw, req['usable_width'], srw, n_sub, edge, status, who,
                     who if run else None, now if run else None))
        xray_grade = 'X-Ray' if w.get('xray') == 'WITH X-RAY' else ('Plain' if w.get('xray') == 'WITHOUT X-RAY' else None)
        length = roll.get('length_m')
        today = datetime.date.today().isoformat()
        tags = []
        for _ in range(n_sub):
            tag = db.alloc(con, 'SR', 'SR-{n4}')
            sr_code = forms._sr_code_for(con, srw, w.get('xray'), w.get('mesh'))
            con.execute("""INSERT INTO subrolls(tag_no,roll_no,item_code,doc_no,sdate,wo_no,batch_no,sr_code,xray_grade,xray,mesh,
                           dest_machine,ply,target_size,length_m,width_cm,std_width_cm,width_check,area_cm2,operator,
                           stock_status,slit_batch,plan_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (tag, roll['roll_no'], roll['item_code'], plan_no, today, w['wo_no'], bn, sr_code, xray_grade,
                         w.get('xray'), w.get('mesh'), w.get('fold_machine'), w.get('ply'), w.get('size'), length, srw,
                         srw, 'مطابق — عرض قياسي من بيانات المنتج', forms.area(length, srw) if length else None, who,
                         'متاح' if run else 'مخطط', bn, plan_no))
            tags.append(tag)
        # استهلاك الجامبو يُسجَّل تلقائيًا (لا شاشة «تسجيل الاستهلاك»)
        con.execute("""INSERT INTO slitting(doc_no,sdate,wo_no,batch_no,roll_no,item_code,supplier_lot,length_m,width_cm,area_cm2,
                       operator,purpose,doc_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan_no, today, w['wo_no'], bn, roll['roll_no'], roll['item_code'], roll.get('slot') or roll.get('supplier_lot'),
                     length, jw, forms.area(length, jw) if length else None, who, 'قص تلقائي حسب بيانات المنتج', plan_no))
        con.execute("UPDATE rolls SET batch_no=?, plan_no=?, use_status=?, issue_date=? WHERE roll_no=?",
                    (bn, plan_no, 'مستهلك' if run else 'مخصص', today, roll['roll_no']))
        con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
        return plan_no, tags, n_sub

    @route('/slitter/<path:bn>', 'slit_work', methods=['GET', 'POST'])
    def slit_work(bn):
        w = order_or_404(bn)
        if mfg.route_of_wo(w) != mfg.FULL:
            flash('هذا الأمر لا يمر بالسليتر (مساره يحدده المنتج)', 'bad')
            return redirect(url_for('order_view', bn=bn))
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for('slit_work', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر — لا يمكن إضافة خطط قص', 'bad')
                return redirect(back)
            roll_no = s(f.get('roll_no'))
            roll = next((r for r in jumbo_rows(w, it) if r['roll_no'] == roll_no), None)
            if not roll:
                other = db.one('SELECT * FROM rolls WHERE roll_no=?', (roll_no,)) if roll_no else None
                if other and other.get('plan_no'):
                    flash(f'الجامبو {roll_no} داخل خطة قص أخرى ({other["plan_no"]}) ولا يدخل إلا خطة واحدة', 'bad')
                else:
                    flash('الجامبو غير متاح: يجب أن يكون مفرجًا عنه من الجودة وغير مستخدم ومن خامة المنتج', 'bad')
                return redirect(back)
            st = mfg.order_state(w)
            unused = db.one("SELECT COUNT(*) n FROM subrolls WHERE batch_no=? AND stock_status IN ('مخطط','متاح')", (bn,))['n']
            yld = float(w.get('pieces_per_sr') or 0)
            if yld and unused * yld >= st['remaining'] - 1e-6 and unused > 0:
                flash(f'لديك {unused} سب رول (مخطط/متاح) تكفي المتبقي من الأمر — لا حاجة لجامبو إضافي', 'bad')
                return redirect(back)
            run = f.get('mode', 'run') == 'run'
            try:
                with db.tx() as con:
                    fresh = con.execute("SELECT plan_no FROM cutting_plans WHERE roll_no=?", (roll_no,)).fetchone()
                    if fresh:
                        raise ValueError(f'الجامبو {roll_no} داخل الخطة {fresh["plan_no"]} — يدخل خطة واحدة فقط')
                    plan_no, tags, n_sub = _make_plan(con, w, it, roll, run)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            except Exception as e:                    # noqa: BLE001 — الفهرس الفريد: جامبو واحد في خطة واحدة
                if 'UNIQUE' in str(e).upper():
                    flash(f'الجامبو {roll_no} داخل خطة قص أخرى ولا يدخل إلا خطة واحدة', 'bad')
                    return redirect(back)
                raise
            db.log('create', 'cutting_plans', plan_no, f'{bn}; {roll_no}; {n_sub} sub-rolls; {"executed" if run else "planned"}')
            if run:
                flash(f'تم القص: {n_sub} سب رول ({tags[0]} … {tags[-1]}) بعرض {fmt_qty(w.get("std_width_cm"))} سم — خطة {plan_no}', 'ok')
                next_bar(('استخدام السب رول في ماكينة الطي', url_for('fold_work', bn=bn)),
                         ('عرض الرولات الفرعية', url_for('order_view', bn=bn) + '#subs', False),
                         ('طباعة بطاقات السب رول', url_for('tags', b=bn), False))
            else:
                flash(f'حُفظت خطة القص {plan_no} ({n_sub} سب رول) — أكّد التنفيذ عند القص الفعلي', 'ok')
            return redirect(back)
        st = mfg.order_state(w)
        plans = db.q('SELECT * FROM cutting_plans WHERE batch_no=? ORDER BY plan_no', (bn,))
        for p in plans:
            p['subs'] = db.q('SELECT * FROM subrolls WHERE plan_no=? ORDER BY tag_no', (p['plan_no'],))
            p['used'] = any(x['stock_status'] == 'مستهلك' for x in p['subs'])
            p['jumbo_status'] = {'مخصص': 'مخصص للقص', 'مستهلك': 'مستهلك (مقصوص)'}.get(
                (db.one('SELECT use_status FROM rolls WHERE roll_no=?', (p['roll_no'],)) or {}).get('use_status'), 'متاح')
        card = mfg.product_card(it, w['qty_required'])
        planned_sr = db.one("SELECT COUNT(*) n FROM subrolls WHERE batch_no=?", (bn,))['n']
        return render_template('slit_work.html', nav='slit', w=w, st=st, plans=plans, card=card, req=card.get('req', {}),
                               jumbos=jumbo_rows(w, it), planned_sr=planned_sr, fmt=fmt_qty, editable=not w.get('final_status'),
                               can_delete_done=auth.can('wo_issue'))

    @route('/slitter/plan/<plan_no>/confirm', 'slit_confirm', methods=['POST'])
    def slit_confirm(plan_no):
        auth.need('enter')
        p = db.one('SELECT * FROM cutting_plans WHERE plan_no=?', (plan_no,))
        if not p:
            abort(404)
        w = order_or_404(p['batch_no'])
        if p['status'] == 'منفّذ':
            flash('الخطة منفّذة بالفعل', 'bad')
            return redirect(url_for('slit_work', bn=p['batch_no']))
        with db.tx() as con:
            now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
            con.execute("UPDATE cutting_plans SET status='منفّذ', executed_by=?, executed_at=? WHERE plan_no=?",
                        (g.user['full_name'], now, plan_no))
            con.execute("UPDATE subrolls SET stock_status='متاح' WHERE plan_no=? AND stock_status='مخطط'", (plan_no,))
            con.execute("UPDATE rolls SET use_status='مستهلك' WHERE roll_no=?", (p['roll_no'],))
        db.log('execute', 'cutting_plans', plan_no, p['batch_no'])
        flash(f'تم تنفيذ القص — أصبح السب رول متاحًا للطي ({p["n_sub"]})', 'ok')
        next_bar(('استخدام السب رول في ماكينة الطي', url_for('fold_work', bn=w['batch_no'])),
                 ('العودة للسليتر', url_for('slit_work', bn=w['batch_no']), False))
        return redirect(url_for('slit_work', bn=w['batch_no']))

    @route('/slitter/plan/<plan_no>/delete', 'slit_plan_delete', methods=['POST'])
    def slit_plan_delete(plan_no):
        auth.need('enter')
        p = db.one('SELECT * FROM cutting_plans WHERE plan_no=?', (plan_no,))
        if not p:
            abort(404)
        w = order_or_404(p['batch_no'])
        back = url_for('slit_work', bn=p['batch_no'])
        if p['status'] == 'منفّذ' and not auth.can('wo_issue'):
            flash('الخطة نُفّذت فعليًا — حذفها من صلاحية مدير المصنع فقط', 'bad')
            return redirect(back)
        with db.tx() as con:
            used = con.execute("SELECT COUNT(*) n FROM subrolls WHERE plan_no=? AND stock_status NOT IN ('مخطط','متاح')",
                               (plan_no,)).fetchone()['n']
            if used:
                flash('لا يمكن حذف الخطة: استُخدم أحد سب رولاتها في الطي — احذف سند الطي أولًا', 'bad')
                return redirect(back)
            snap = json.dumps(dict(p), ensure_ascii=False, default=str)
            con.execute('DELETE FROM subrolls WHERE plan_no=?', (plan_no,))
            con.execute('DELETE FROM slitting WHERE doc_no=?', (plan_no,))
            con.execute("UPDATE rolls SET batch_no=NULL, plan_no=NULL, use_status='متاح', issue_date=NULL WHERE roll_no=?",
                        (p['roll_no'],))
            con.execute('DELETE FROM cutting_plans WHERE plan_no=?', (plan_no,))
        db.log('delete', 'cutting_plans', plan_no, snap)
        flash(f'حُذفت الخطة {plan_no} وعاد الجامبو {p["roll_no"]} متاحًا', 'ok')
        return redirect(back)

    @route('/tags', 'tags')
    def tags():
        bn = (request.args.get('b') or '').strip()
        g.crumb_args = {'bn': bn}
        return render_template('tags.html', bn=bn, b=forms.batch(bn) if bn else None,
                               subs=db.q('SELECT * FROM subrolls WHERE batch_no=? ORDER BY tag_no', (bn,)) if bn else [])

    # ============================================================ الطي
    @route('/folding', 'fold')
    def fold():
        rows = [w for w in visible_orders(300, "AND IFNULL(route_code,'FULL_GAUZE')='FULL_GAUZE' AND final_status IS NULL")
                if w['st']['stage'] in ('slitting', 'ready_fold', 'production')]
        for w in rows:
            w['avail'] = len(mfg.avail_subrolls(w))
        return render_template('fold_list.html', nav='fold', rows=rows, fmt=fmt_qty)

    def _fold_header(w, it):
        return mfg.product_card(it, w['qty_required'])

    def _validate_qty(w, qty):
        if qty is None or qty <= 0:
            return 'الكمية الفعلية يجب أن تكون أكبر من صفر'
        return None

    def _warn_qty(w, qty):
        y = float(w.get('pieces_per_sr') or 0)
        if y and qty > y * 1.1:
            flash(f'تنبيه: الكمية {fmt_qty(qty)} أكبر من إنتاجية السب رول المعيارية ({fmt_qty(y)}) — راجع الرقم', 'warn')

    def _withdraw_pending(con, w):
        """أي تعديل على سند بعد إرسال الدفعة للجودة يسحب طلب الاعتماد ليُعاد الإنهاء بالكميات الجديدة."""
        if w.get('final_status') == mfg.FS_PENDING:
            con.execute("UPDATE work_orders SET final_status=NULL, finished_at=NULL, finished_by=NULL, produced_qty=NULL "
                        "WHERE batch_no=?", (w['batch_no'],))
            return True
        return False

    @route('/folding/<path:bn>', 'fold_work', methods=['GET', 'POST'])
    def fold_work(bn):
        w = order_or_404(bn)
        if mfg.route_of_wo(w) != mfg.FULL:
            flash('هذا الأمر لا يمر بماكينة الطي (مساره يحدده المنتج)', 'bad')
            return redirect(url_for('order_view', bn=bn))
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for('fold_work', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر — لا يمكن تسجيل سندات جديدة', 'bad')
                return redirect(back)
            tag, qty = s(f.get('tag_no')), num(f.get('qty_good'))
            if not tag:
                flash('اختر السب رول', 'bad')
                return redirect(back)
            err = _validate_qty(w, qty)
            if err:
                flash(err, 'bad')
                return redirect(back)
            now = datetime.datetime.now()
            t0 = s(f.get('t0')) or now.strftime('%H:%M')
            try:
                doc_no = _save_doc(w, it, tag, qty, s(f.get('notes')), t0, now)
            except SubRollUsed as e:
                flash(used_msg(e.doc), 'bad')
                return redirect(back)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            _warn_qty(w, qty)
            w2 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
            st = mfg.order_state(w2)
            flash(f'تم حفظ سند الإنتاج {doc_no} — المنتج {fmt_qty(st["produced"])} من {fmt_qty(st["required"])} {st["unit"]}'
                  f' · المتبقي {fmt_qty(st["remaining"])}', 'ok')
            btns = []
            if mfg.avail_subrolls(w2) and st['remaining'] > 1e-6:
                btns.append(('استخدام السب رول التالي', back))
            elif st['remaining'] > 1e-6:
                btns.append(('استكمال القص', url_for('slit_work', bn=bn)))
            if st['can_finish'] and (st['remaining'] <= 1e-6 or not btns):
                btns.insert(0, ('إنهاء الإنتاج', url_for('order_view', bn=bn) + '#finish'))
            btns.append(('العودة لأمر الإنتاج', url_for('order_view', bn=bn), not btns))
            next_bar(*btns)
            return redirect(back)
        st = mfg.order_state(w)
        docs = db.q('SELECT * FROM folding_out WHERE batch_no=? ORDER BY id DESC', (bn,))
        subs = mfg.avail_subrolls(w)
        return render_template('fold_work.html', nav='fold', w=w, st=st, card=_fold_header(w, it), subs=subs, docs=docs,
                               editable=not w.get('final_status') or w['final_status'] == mfg.FS_PENDING,
                               t0=datetime.datetime.now().strftime('%H:%M'), fmt=fmt_qty,
                               next_doc=db.peek_number(f'FOLD-{_year()}', f'FOLD-{_year()}-{{n6}}'))

    def _save_doc(w, it, tag, qty, notes, t0, now):
        """يحفظ سند طي: يستهلك السب رول ذريًا ويُنشئ المخرج وقيد المخزون. يرفع SubRollUsed عند التكرار."""
        bn = w['batch_no']
        machine = w.get('fold_machine')
        pa = forms.piece_area_cm2(machine, w.get('std_width_cm')) if machine else None
        chk = forms.machine_size_check(machine, w.get('size'))
        unit = mfg.production_unit(w)
        try:
            with db.tx() as con:
                fresh = con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone()
                if fresh['final_status']:
                    raise ValueError('أُنهي إنتاج هذا الأمر')
                sr = con.execute('SELECT * FROM subrolls WHERE tag_no=?', (tag,)).fetchone()
                if not sr:
                    raise ValueError('السب رول غير موجود')
                sr = dict(sr)
                if sr['stock_status'] != 'متاح':
                    if sr['stock_status'] == 'مخطط':
                        raise ValueError('السب رول ضمن خطة قص لم تُنفّذ بعد — أكّد تنفيذ القص أولًا')
                    raise SubRollUsed(sr.get('used_doc') or '—')
                ok, why = mfg.subroll_fits(dict(fresh), sr)
                if not ok:
                    raise ValueError(why)
                doc_no = db.alloc(con, f'FOLD-{_year()}', f'FOLD-{_year()}-{{n6}}')
                today = now.date().isoformat()
                cur = con.execute("""UPDATE subrolls SET stock_status='مستهلك', used_doc=?, consumed_by_batch=?, consumed_date=?
                                     WHERE tag_no=? AND stock_status='متاح'""", (doc_no, bn, today, tag))
                if cur.rowcount != 1:
                    again = con.execute('SELECT used_doc FROM subrolls WHERE tag_no=?', (tag,)).fetchone()
                    raise SubRollUsed((again and again['used_doc']) or '—')
                who = g.user['full_name']
                L = sr.get('length_m')
                con.execute("""INSERT INTO folding_in(doc_no,fdate,machine_code,size,operator,wo_no,batch_no,tag_no,roll_no,sr_code,
                               xray_grade,length_used_m,width_cm,area_used_cm2,fully_used,notes,sr_source_batch)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc_no, today, machine, w.get('size'), who, w['wo_no'], bn, tag, sr['roll_no'], sr.get('sr_code'),
                             sr.get('xray_grade'), L, sr.get('width_cm'), forms.area(L, sr.get('width_cm')) if L else None,
                             'نعم', notes, sr.get('slit_batch') or sr.get('batch_no')))
                con.execute("""INSERT INTO folding_out(doc_no,fdate,machine_code,batch_no,route,item_code,ply,size,piece_area_cm2,
                               qty_good,unit,tag_no,operator,start_time,end_time,machine_size_check,notes,created_by,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc_no, today, machine, bn, w.get('route'), w['item_code'], w.get('ply'), w.get('size'), pa, qty, unit,
                             tag, who, t0, now.strftime('%H:%M'), chk, notes, g.user['username'],
                             now.isoformat(sep=' ', timespec='seconds')))
                inventory.post(con, bn, 'PROD_OUT', qty, unit, bn, mfg.FULL, 'fold', doc_no, note=f'سند طي من {tag}')
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
        except SubRollUsed:
            raise
        except Exception as e:                    # noqa: BLE001 — الفهرس الفريد يمنع الاستخدام المزدوج حتى في التسابق
            if 'UNIQUE' in str(e).upper() and 'tag_no' in str(e):
                r = db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tag,))
                raise SubRollUsed((r or {}).get('doc_no') or '—')
            raise
        db.log('create', 'folding_out', doc_no, f'{bn}; {tag}; {fmt_qty(qty)} {unit}')
        return doc_no

    def _doc_or_404(doc_no):
        fo = db.one('SELECT * FROM folding_out WHERE doc_no=? AND tag_no IS NOT NULL', (doc_no,))
        if not fo:
            abort(404)
        w = order_or_404(fo['batch_no'])
        return fo, w

    def _editable(w):
        return not w.get('final_status') or w['final_status'] == mfg.FS_PENDING

    @route('/folding/doc/<doc_no>/edit', 'fold_edit', methods=['GET', 'POST'])
    def fold_edit(doc_no):
        fo, w = _doc_or_404(doc_no)
        bn = w['batch_no']
        g.crumb_args = {'bn': bn}
        back = url_for('fold_work', bn=bn)
        if not _editable(w):
            flash(f'لا يمكن تعديل السند: حالة الدفعة «{mfg.FS_AR.get(w["final_status"], w["final_status"])}»', 'bad')
            return redirect(back)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            new_tag, qty = s(f.get('tag_no')) or fo['tag_no'], num(f.get('qty_good'))
            err = _validate_qty(w, qty)
            if err:
                flash(err, 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            old_qty, old_tag = float(fo['qty_good'] or 0), fo['tag_no']
            unit = fo.get('unit') or mfg.production_unit(w)
            try:
                with db.tx() as con:
                    cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
                    if cur['final_status'] not in (None, mfg.FS_PENDING):
                        raise ValueError('لا يمكن التعديل بعد اعتماد/رفض الدفعة')
                    if new_tag != old_tag:
                        sr = con.execute('SELECT * FROM subrolls WHERE tag_no=?', (new_tag,)).fetchone()
                        if not sr:
                            raise ValueError('السب رول غير موجود')
                        if sr['stock_status'] != 'متاح':
                            raise SubRollUsed(sr['used_doc'] or '—')
                        ok, why = mfg.subroll_fits(cur, dict(sr))
                        if not ok:
                            raise ValueError(why)
                        con.execute("""UPDATE subrolls SET stock_status='متاح', used_doc=NULL, consumed_by_batch=NULL,
                                       consumed_date=NULL WHERE tag_no=?""", (old_tag,))
                        if con.execute("""UPDATE subrolls SET stock_status='مستهلك', used_doc=?, consumed_by_batch=?, consumed_date=?
                                          WHERE tag_no=? AND stock_status='متاح'""",
                                       (doc_no, bn, datetime.date.today().isoformat(), new_tag)).rowcount != 1:
                            raise SubRollUsed('—')
                        sr = dict(sr)
                        con.execute("""UPDATE folding_in SET tag_no=?, roll_no=?, sr_code=?, xray_grade=?, length_used_m=?, width_cm=?,
                                       area_used_cm2=?, sr_source_batch=? WHERE doc_no=?""",
                                    (new_tag, sr['roll_no'], sr.get('sr_code'), sr.get('xray_grade'), sr.get('length_m'), sr.get('width_cm'),
                                     forms.area(sr.get('length_m'), sr.get('width_cm')) if sr.get('length_m') else None,
                                     sr.get('slit_batch') or sr.get('batch_no'), doc_no))
                    delta = qty - old_qty
                    if abs(delta) > 1e-9:
                        inventory.post(con, bn, 'PROD_OUT', delta, unit, bn, mfg.FULL, 'fold_edit', doc_no,
                                       note=f'تعديل سند: {fmt_qty(old_qty)} ← {fmt_qty(qty)}')
                    now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
                    con.execute("""UPDATE folding_out SET qty_good=?, tag_no=?, notes=?, updated_by=?, updated_at=? WHERE doc_no=?""",
                                (qty, new_tag, s(f.get('notes')), g.user['username'], now, doc_no))
                    con.execute('UPDATE folding_in SET notes=? WHERE doc_no=?', (s(f.get('notes')), doc_no))
                    withdrawn = _withdraw_pending(con, cur)
            except SubRollUsed as e:
                flash(used_msg(e.doc), 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            db.log('edit', 'folding_out', doc_no, f'qty {fmt_qty(old_qty)}→{fmt_qty(qty)}; sub-roll {old_tag}→{new_tag}')
            flash(f'تم تعديل السند {doc_no} وأُعيد احتساب الإنتاج والمخزون' +
                  (' — سُحب طلب اعتماد الجودة، أعد إنهاء الإنتاج' if withdrawn else ''), 'ok')
            next_bar(('العودة لأمر الإنتاج', url_for('order_view', bn=bn)), ('سندات الطي', back, False))
            return redirect(back)
        subs = mfg.avail_subrolls(w)
        cur_sr = db.one('SELECT * FROM subrolls WHERE tag_no=?', (fo['tag_no'],))
        if cur_sr and all(x['tag_no'] != cur_sr['tag_no'] for x in subs):
            subs = [cur_sr] + subs
        return render_template('fold_edit.html', nav='fold', w=w, fo=fo, subs=subs, fmt=fmt_qty,
                               card=_fold_header(w, db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}))

    @route('/folding/doc/<doc_no>/delete', 'fold_delete', methods=['POST'])
    def fold_delete(doc_no):
        auth.need('enter')
        fo, w = _doc_or_404(doc_no)
        bn = w['batch_no']
        back = url_for('fold_work', bn=bn)
        if not _editable(w):
            flash(f'لا يمكن حذف السند: حالة الدفعة «{mfg.FS_AR.get(w["final_status"], w["final_status"])}»', 'bad')
            return redirect(back)
        unit = fo.get('unit') or mfg.production_unit(w)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            if cur['final_status'] not in (None, mfg.FS_PENDING):
                flash('لا يمكن الحذف بعد اعتماد/رفض الدفعة', 'bad')
                return redirect(back)
            snap = json.dumps(dict(fo), ensure_ascii=False, default=str)
            # عكس المعاملة: السب رول يعود متاحًا، المخرج يُحذف، وقيد المخزون يُعكس بقيد سالب
            con.execute("""UPDATE subrolls SET stock_status='متاح', used_doc=NULL, consumed_by_batch=NULL, consumed_date=NULL
                           WHERE tag_no=?""", (fo['tag_no'],))
            con.execute('DELETE FROM folding_in WHERE doc_no=?', (doc_no,))
            con.execute('DELETE FROM folding_out WHERE doc_no=?', (doc_no,))
            inventory.post(con, bn, 'PROD_OUT', -float(fo['qty_good'] or 0), unit, bn, mfg.FULL, 'fold_delete', doc_no,
                           note=f'عكس قيد — حذف السند {doc_no}')
            withdrawn = _withdraw_pending(con, cur)
        db.log('delete', 'folding_out', doc_no, snap)
        flash(f'حُذف السند {doc_no} وعاد السب رول {fo["tag_no"]} متاحًا وخُصمت الكمية {fmt_qty(fo["qty_good"])} من الإنتاج' +
              (' — سُحب طلب اعتماد الجودة، أعد إنهاء الإنتاج' if withdrawn else ''), 'ok')
        return redirect(back)

    # ============================================================ حذف سندات الخطوط الأخرى (عكس المعاملة)
    def _reverse_ledger(con, ref_doc, why):
        """يعكس قيود دفتر المخزون لسند بقيود سالبة (لا حذف من الدفتر) — لا يعكس القيد الواحد مرتين."""
        done = {int(str(r['move_id'])[4:]) for r in con.execute(
            "SELECT move_id FROM stock_tx WHERE ref_type='reverse' AND move_id LIKE 'rev-%'").fetchall()
            if str(r['move_id'])[4:].isdigit()}
        n = 0
        for r in con.execute("SELECT * FROM stock_tx WHERE ref_doc=? AND ref_type<>'reverse' ORDER BY id", (ref_doc,)).fetchall():
            if r['id'] in done:
                continue
            con.execute("""INSERT INTO stock_tx(owner,stage,qty,unit,batch_no,route_code,location,ref_type,ref_doc,move_id,note,created_by)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r['owner'], r['stage'], -r['qty'], r['unit'], r['batch_no'], r['route_code'], r['location'],
                         'reverse', ref_doc, f"rev-{r['id']}", why, g.user['username']))
            n += 1
        return n

    def _safe_next(default):
        n = request.form.get('next') or ''
        return n if (n.startswith('/') and not n.startswith('//')) else default

    @route('/production/proc/<path:proc_no>/delete', 'proc_delete', methods=['POST'])
    def proc_delete(proc_no):
        """حذف سند مرحلة (تعبئة SP / ماكينة الأربطة / التغليف) مع عكس المخزون. التعديل = حذف ثم إعادة إدخال."""
        auth.need('enter')
        p = db.one('SELECT * FROM proc_batches WHERE proc_no=?', (proc_no,))
        if not p:
            abort(404)
        w = order_or_404(p['batch_no'])
        bn = w['batch_no']
        back = url_for('order_view', bn=bn)
        if w.get('final_status') not in (None, mfg.FS_PENDING):
            flash(f'لا يمكن حذف السند: حالة الدفعة «{mfg.FS_AR.get(w["final_status"], w["final_status"])}»', 'bad')
            return redirect(back)
        child = db.one('SELECT proc_no FROM proc_batches WHERE parent_proc=?', (proc_no,))
        if child:
            flash(f'لا يمكن حذف السند: استُخدم ناتجه في السند {child["proc_no"]} — احذفه أولًا', 'bad')
            return redirect(back)
        snap = json.dumps(dict(p), ensure_ascii=False, default=str)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            if cur['final_status'] not in (None, mfg.FS_PENDING):
                flash('تغيّرت حالة الدفعة — لا يمكن الحذف', 'bad')
                return redirect(back)
            _reverse_ledger(con, proc_no, f'عكس قيد — حذف السند {proc_no}')
            con.execute('DELETE FROM batch_links WHERE child=?', (proc_no,))
            con.execute('DELETE FROM proc_batches WHERE proc_no=?', (proc_no,))
            withdrawn = _withdraw_pending(con, cur)
        db.log('delete', 'proc_batches', proc_no, snap)
        flash(f'حُذف السند {proc_no} وعُكست قيوده في المخزون' +
              (' — سُحب طلب اعتماد الجودة، أعد إنهاء الإنتاج' if withdrawn else ''), 'ok')
        return redirect(_safe_next(back))

    @route('/production/alloc/<doc_no>/delete', 'alloc_delete', methods=['POST'])
    def alloc_delete(doc_no):
        """إلغاء تخصيص خام (لوط SP / جامبو رباط) لم يُستهلك بعد."""
        auth.need('enter')
        a = db.one('SELECT * FROM allocations WHERE doc_no=? AND voided=0', (doc_no,))
        if not a:
            abort(404)
        w = order_or_404(a['batch_no'])
        bn = w['batch_no']
        back = _safe_next(url_for('order_view', bn=bn))
        if w.get('final_status') not in (None, mfg.FS_PENDING):
            flash('لا يمكن إلغاء التخصيص بعد اعتماد/رفض الدفعة', 'bad')
            return redirect(back)
        with db.tx() as con:
            if a['source_type'] == 'JUMBO':
                if con.execute("SELECT 1 FROM proc_batches WHERE batch_no=? AND source_ref=? AND stage_code='BM'",
                               (bn, a['source_ref'])).fetchone():
                    flash('لا يمكن الإلغاء: شُغّل هذا الجامبو في ماكينة الأربطة — احذف سند التشغيل أولًا', 'bad')
                    return redirect(back)
                con.execute("UPDATE rolls SET batch_no=NULL, use_status=NULL, issue_date=NULL WHERE roll_no=?", (a['source_ref'],))
                con.execute("DELETE FROM batch_links WHERE child=? AND parent=? AND link_type='JUMBO'", (bn, a['source_ref']))
            else:
                if inventory.balance(bn, 'ISSUED', con) + 1e-9 < a['qty']:
                    flash('لا يمكن الإلغاء: جزء من الخام المصروف استُهلك في التعبئة — احذف سند التعبئة أولًا', 'bad')
                    return redirect(back)
                _reverse_ledger(con, doc_no, f'عكس قيد — إلغاء التخصيص {doc_no}')
                con.execute("UPDATE batch_links SET qty=qty-? WHERE child=? AND parent=? AND link_type='SP_LOT'",
                            (a['qty'], bn, a['source_ref']))
                con.execute("DELETE FROM batch_links WHERE child=? AND parent=? AND link_type='SP_LOT' AND qty<=0.000001",
                            (bn, a['source_ref']))
            con.execute('UPDATE allocations SET voided=1 WHERE doc_no=?', (doc_no,))
            withdrawn = _withdraw_pending(con, dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone()))
        db.log('delete', 'allocations', doc_no, json.dumps(dict(a), ensure_ascii=False, default=str))
        flash(f'أُلغي التخصيص {doc_no}' + (' — سُحب طلب اعتماد الجودة' if withdrawn else ''), 'ok')
        return redirect(back)

    # ============================================================ المكتملة والسجل
    @route('/completed', 'completed_list')
    def completed_list():
        f = s(request.args.get('s'))
        rows = [w for w in visible_orders(300, "AND final_status IS NOT NULL")]
        if f:
            rows = [w for w in rows if w['final_status'] == f]
        return render_template('completed_list.html', nav='completed', rows=rows, f=f, fs_ar=mfg.FS_AR, fs_cls=mfg.FS_CLS,
                               fmt=fmt_qty)

    @route('/production/log', 'production_log')
    def production_log():
        tables = ('work_orders', 'cutting_plans', 'folding_out', 'proc_batches', 'allocations')
        ph = ','.join('?' * len(tables))
        rows = db.q(f"""SELECT ts, username, action, table_name, record_key, details FROM audit_log
                        WHERE table_name IN ({ph}) ORDER BY id DESC LIMIT 300""", tables)
        ar = {'create': 'إنشاء', 'edit': 'تعديل', 'delete': 'حذف', 'execute': 'تنفيذ القص',
              'finish_production': 'إنهاء الإنتاج', 'update': 'تحديث', 'close': 'إغلاق', 'reopen': 'إعادة فتح'}
        tb = {'work_orders': 'أمر إنتاج', 'cutting_plans': 'خطة قص', 'folding_out': 'سند طي', 'proc_batches': 'سند مرحلة',
              'allocations': 'تخصيص خام'}
        for r in rows:
            r['action_ar'] = ar.get(r['action'], r['action'])
            r['table_ar'] = tb.get(r['table_name'], r['table_name'])
            r['bad'] = r['action'] == 'delete'
        return render_template('production_log.html', nav='log', rows=rows)
