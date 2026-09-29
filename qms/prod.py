# -*- coding: utf-8 -*-
"""الإنتاج المبسّط (v14): المنتج يقود كل شيء، والمستخدم يدخل فقط ما لا يعرفه النظام.

  أمر الإنتاج  = المنتج + الكمية                    (الباقي من بيانات المنتج / BOM / المسار)
  السليتر      = اختيار جامبو ← خطة قص تلقائية ← سب رول SR-0001..                (بلا إدخال عرض/عدد)
  الطي         = اختيار سب رول + الكمية الفعلية فقط  (الاستهلاك والمخزون والأرقام تلقائية)
  إنهاء الإنتاج ← إشعار واحد للجودة ← موافقة ← تخزين  (انظر qa.py و lines.warehouse)

كل السندات تحتفظ بالتتبع الكامل: جامبو ← سب رول ← سند طي ← منتج نهائي ← موافقة ← مخزن.
"""
import datetime, json, re
from flask import render_template, request, redirect, url_for, flash, abort, g, session, jsonify

import db, auth, mfg, inventory, notify, forms, ops
from util import s, num, batch_stem, fmt_qty, doc_scope, MONTHS


class SubRollUsed(Exception):
    def __init__(self, doc):
        super().__init__(doc)
        self.doc = doc


def used_msg(doc):
    return f'تم استخدام هذا Sub Roll مسبقا في سند رقم {doc}.'


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


def _num_of(v):
    import migrate_v15
    return migrate_v15.width_from_text(v)


def jumbo_rows(w, item):
    """جامبو رول متاح لهذا الأمر: مفرج، Available فقط، ومن خامة المنتج (BOM) أو مطابق لمواصفاته (ميش/كاشف/عرض)."""
    prefs = mfg.prefixes_of_class('ROLL')
    ph = ','.join('?' * len(prefs)) or "''"
    args = list(prefs)
    where = ''
    raw = w.get('raw_item') or item.get('raw_item')
    if raw:
        where = ' AND rl.item_code=?'
        args.append(raw)
    rows = db.q(f"""SELECT rl.*, i.description, i.ref_width_cm iw, i.length_m il, i.mesh imesh, i.xray ixray,
                           rc.supplier_lot slot, s.name supplier
                    FROM rolls rl JOIN items i ON i.item_code=rl.item_code
                    LEFT JOIN receipts rc ON rc.grn_no=rl.grn_no LEFT JOIN suppliers s ON s.supplier_id=rc.supplier_id
                    WHERE i.prefix IN ({ph}) AND rl.stock_status='مفرج' AND IFNULL(rl.batch_no,'')=''
                      AND IFNULL(rl.use_status,'متاح') IN ('متاح','') {where} ORDER BY rl.roll_no""", tuple(args))
    srw = w.get('std_width_cm') or 0
    out = []
    for r in rows:
        if not raw and ((r['imesh'] or None) != (item.get('mesh') or None) or (r['ixray'] or None) != (item.get('xray') or None)):
            continue
        r['ref_w'] = r.get('ref_width_cm') or r.get('iw') or _num_of(r.get('width_cm'))
        r['ref_l'] = r.get('ref_length_m') or _num_of(r.get('il')) or r.get('length_m')
        r['def_w'] = r.get('actual_width_cm') or r['ref_w'] or mfg._setting_f('default_jumbo_width_cm', 120)
        r['def_l'] = r.get('actual_length_m') or r['ref_l'] or mfg._setting_f('default_sr_length_m', 2000)
        r['ref_missing'] = not r['ref_w']
        if srw and r['ref_w'] and r['ref_w'] < srw:
            continue
        out.append(r)
    return out


def get_operator(f, dept='production'):
    """المشغل من القائمة الرئيسية فقط (لا نص حر). يرفع ValueError إن لم يُختر مشغل صالح."""
    name = ops.resolve(f.get('operator'), dept)
    if not name:
        raise ValueError('اختر المشغّل من القائمة')
    ops.remember(dept, name)
    return name


def withdraw_pending(con, w):
    """أي تعديل على الإنتاج بعد إرسال الدفعة للجودة يسحب طلب الاعتماد ليُعاد بالكميات الجديدة."""
    if w.get('final_status') in (mfg.FS_PENDING, mfg.FS_APPROVED, mfg.FS_HOLD, mfg.FS_REJECTED):
        con.execute("UPDATE work_orders SET final_status=NULL, finished_at=NULL, finished_by=NULL, produced_qty=NULL, "
                    "approved_qty=NULL, approved_by=NULL, approved_at=NULL WHERE batch_no=?", (w['batch_no'],))
        return True
    return False



def doc_gate(w):
    """هل يُسمح بتعديل/حذف سند الطي؟ مستخدم الإنتاج: قبل الإنهاء أو أثناء انتظار الجودة. مدير النظام/المدير: حتى
    بعد الاعتماد (يُسحب الاعتماد) ولا يُسمح بعد التخزين قبل إلغاء التخزين."""
    fs = w.get('final_status')
    if fs == mfg.FS_STORED:
        return False, 'الدفعة مخزّنة — ألغِ تخزينها أولًا (مدير النظام: صفحة الدفعة ← إلغاء التخزين) ثم عدّل السند'
    if fs in (None, mfg.FS_PENDING):
        return True, ''
    if auth.can('master'):
        return True, ''
    return False, f'حالة الدفعة «{mfg.FS_AR.get(fs, fs)}» — التعديل بعد الاعتماد/الرفض من صلاحية مدير النظام'



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
        if it['route_code'] == mfg.FULL:
            rq = card.get('req', {})
            if rq.get('ok') and not jumbo_rows({'raw_item': it.get('raw_item'), 'std_width_cm': rq.get('sr_width')}, it):
                warn.append('لا يوجد حاليًا جامبو رول مفرج عنه مناسب (لن يمنع إصدار الأمر)')
            if not rq.get('sr_matches'):
                warn.append('لا يوجد Sub Roll مطابق لمواصفات المنتج في Master Data — راجع الأكواد (يمكن اختياره يدويًا عند القص)')
            ps = mfg.pack_spec(it['item_code'])
            need_keys = ('swabs_per_envelope', 'swabs_per_box', 'boxes_per_carton') if card['variant'] == 'sterile' \
                else ('swabs_per_pack', 'packs_per_carton')
            if any(not ps.get(k) for k in need_keys):
                warn.append('Packaging Configuration ناقص لهذا الصنف — يُكمله مدير النظام من «تكوين التعبئة»')
            if card['variant'] == 'sterile' and not it.get('sp_item'):
                warn.append('لا يوجد كود SP مطابق لهذا المنتج المعقم في Master Data')
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
            unit = req.get('commercial_unit') if rc == mfg.FULL else (it.get('uom') or r_['base_uom'] or 'قطعة')
            req_swabs = req.get('swabs_needed') if rc == mfg.FULL else None
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
                          sr_spec_key,sr_needed,pieces_per_sr,jumbo_needed,route_code,priority,raw_item,req_swabs)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (wo_no, today, g.user['full_name'], g.user['full_name'], 'إنتاج', r_['name_ar'],
                                 code, it.get('size'), it.get('ply'), it.get('xray'), it.get('mesh'),
                                 it.get('sterile') or 'غير معقم', qty, unit, s(f.get('due_date')),
                                 MONTHS[d.month - 1],
                                 int(d.strftime('%y')), d.day, seq, letter, batch, letter, batch, d.isoformat(),
                                 it.get('machine_code') if rc == mfg.FULL else None, req.get('sr_width'), 'صادر',
                                 s(f.get('notes')),
                                 f"{it['machine_code']}-{it['ply']}" if rc == mfg.FULL and it.get('ply') else None,
                                 req.get('sr_needed'), req.get('yield_per_sr'), req.get('jumbo_needed'), rc, prio,
                                 it.get('raw_item'), req_swabs))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            db.log('create', 'work_orders', batch, f'{wo_no}; {code}; {qty:g} {unit}' + (f' = {req_swabs:g} مسحة' if req_swabs else '') + f'; route={rc}')
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
        v15 = st['v15']
        short = (st['remaining'] > 1e-6) or (v15 and st['unpacked'] > 1e-6)
        if short and not request.form.get('short_ok'):
            flash(f'الكمية المنتجة ({fmt_qty(st["produced"])} {st["unit"]}) أقل من المطلوب ({fmt_qty(st["required"])})'
                  + (f' أو توجد {fmt_qty(st["unpacked"])} مسحة غير معبأة' if v15 and st['unpacked'] > 1e-6 else '')
                  + ' — أكّد الإنهاء بالكمية الفعلية', 'bad')
            return redirect(back)
        qty, unit = (st['fg_qty'], st['fg_unit']) if v15 else (st['produced'], st['unit'])
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        with db.tx() as con:
            con.execute("""UPDATE work_orders SET final_status=?, finished_at=?, finished_by=?, produced_qty=?,
                           status='قيد التنفيذ' WHERE batch_no=? AND final_status IS NULL""",
                        (mfg.FS_PENDING, now, g.user['full_name'], qty, bn))
            notify.push(con, 'production_complete',
                        f'تم الانتهاء من إنتاج الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، والكمية النهائية {fmt_qty(qty)} {unit}، '
                        f'وهي بانتظار موافقة الجودة للتخزين.',
                        f'{w["item_code"]} · مسحات الطي {fmt_qty(st["produced"])} من {fmt_qty(st["required"])}' if v15
                        else f'{w["item_code"]} · المطلوب {fmt_qty(st["required"])} {unit}',
                        url_for('quality_review', bn=bn), bn, mfg.route_of_wo(w), ('qc_sign',))
        db.log('finish_production', 'work_orders', bn, f'{fmt_qty(qty)} {unit}; folded {fmt_qty(st["produced"])}/{fmt_qty(st["required"])}')
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
        if rc == mfg.FULL:
            ctx['ns_docs'] = db.q('SELECT * FROM ns_packing WHERE batch_no=? ORDER BY doc_no', (bn,))
            ctx['inters'] = db.q('SELECT * FROM intermediates WHERE batch_no=? ORDER BY barcode', (bn,))
            ctx['recs'] = db.q('SELECT * FROM ster_records WHERE batch_no=? ORDER BY rec_no', (bn,))
            ctx['cycles'] = db.q('SELECT DISTINCT c.cycle_no, c.status FROM cycles c JOIN cycle_loads l ON l.cycle_no=c.cycle_no WHERE l.batch_no=?', (bn,))
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

    def _run_no(con):
        """رقم تشغيل السليتر تلقائيًا بنظام الترقيم القائم (شهر-يوم-SL-تسلسل): SEP-2629-SL-001."""
        d, stem = batch_stem(datetime.date.today().isoformat(), 'SL')
        top = 0
        for r in con.execute("SELECT plan_no FROM cutting_plans WHERE plan_no LIKE ?", (stem + '%',)).fetchall():
            m = re.search(r'-(\d+)$', r['plan_no'] or '')
            if m:
                top = max(top, int(m.group(1)))
        for r in con.execute("SELECT batch_no FROM work_orders WHERE batch_no LIKE ?", (stem + '%',)).fetchall():
            m = re.search(r'-(\d+)$', r['batch_no'] or '')
            if m:
                top = max(top, int(m.group(1)))
        scope = f'RUN:{stem}'
        con.execute('INSERT OR IGNORE INTO counters(scope,n) VALUES(?,0)', (scope,))
        con.execute('UPDATE counters SET n=? WHERE scope=? AND n<?', (top, scope, top))
        return db.alloc(con, scope, stem + '{n3}')

    def _make_plan(con, w, it, roll, a):
        """سند سليتر واحد لجامبو: يحجز الجامبو ذريًا (Available ← Allocated/Consumed) ثم يولّد السب رول من
        بيانات المنتج وكود Sub Roll من Master Data — لا عرض/عدد يدوي."""
        bn = w['batch_no']
        run = a['run']
        today = datetime.date.today().isoformat()
        status_roll = 'مستهلك' if run else 'مخصص'
        # حجز ذرّي: لا يمر إلا إن كان الجامبو Available فعلًا (حتى مع نافذتين مفتوحتين)
        run_no = _run_no(con)
        cur = con.execute("""UPDATE rolls SET batch_no=?, plan_no=?, use_status=?, issue_date=?, actual_width_cm=?, actual_length_m=?
                             WHERE roll_no=? AND IFNULL(use_status,'متاح') IN ('متاح','') AND IFNULL(batch_no,'')=''
                               AND stock_status='مفرج'""",
                          (bn, run_no, status_roll, today, a['actual_width'], a['actual_length'], roll['roll_no']))
        if cur.rowcount != 1:
            raise ValueError(f'الجامبو {roll["roll_no"]} لم يعد متاحًا (مخصص أو قيد القص أو مستهلك)')
        req = mfg.requirements(it, w['qty_required'], a['actual_width'], a['actual_length'])
        srw, n_sub, edge = req['sr_width'], req['per_jumbo'], req['edge_each']
        if not req['ok'] or n_sub < 1:
            raise ValueError('بيانات المنتج ناقصة أو عرض الجامبو الفعلي لا يتسع لسب رول واحد: ' + '، '.join(req['missing']))
        now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
        op = a['operator']
        con.execute("""INSERT INTO cutting_plans(plan_no,batch_no,roll_no,jumbo_width,usable_width,sr_width,n_sub,edge_each,
                       status,created_by,executed_by,executed_at,operator,actual_width,actual_length,ref_width,ref_length,sr_code,sr_manual)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_no, bn, roll['roll_no'], a['actual_width'], req['usable_width'], srw, n_sub, edge,
                     'منفّذ' if run else 'مخطط', op, op if run else None, now if run else None, op, a['actual_width'],
                     a['actual_length'], roll.get('ref_w'), roll.get('ref_l'), a['sr_code'], a['sr_manual']))
        xray_grade = 'X-Ray' if w.get('xray') == 'WITH X-RAY' else ('Plain' if w.get('xray') == 'WITHOUT X-RAY' else None)
        tags = []
        for _ in range(n_sub):
            tag = db.alloc(con, 'SR', 'SR-{n4}')
            con.execute("""INSERT INTO subrolls(tag_no,barcode,roll_no,item_code,doc_no,run_no,sdate,wo_no,batch_no,sr_code,sr_manual,
                           xray_grade,xray,mesh,dest_machine,ply,target_size,length_m,width_cm,std_width_cm,width_check,area_cm2,
                           operator,stock_status,slit_batch,plan_no,supplier_lot,supplier_roll_no)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (tag, tag, roll['roll_no'], roll['item_code'], run_no, run_no, today, w['wo_no'], bn, a['sr_code'],
                         a['sr_manual'], xray_grade, w.get('xray'), w.get('mesh'), w.get('fold_machine'), w.get('ply'),
                         w.get('size'), a['actual_length'], srw, srw, 'مطابق — عرض قياسي من بيانات المنتج',
                         forms.area(a['actual_length'], srw), op, 'متاح' if run else 'مخطط', bn, run_no,
                         roll.get('supplier_lot'), roll.get('supplier_roll_no')))
            tags.append(tag)
        # استهلاك الجامبو يُسجَّل تلقائيًا (لا شاشة «تسجيل الاستهلاك»)
        con.execute("""INSERT INTO slitting(doc_no,sdate,wo_no,batch_no,roll_no,item_code,supplier_lot,length_m,width_cm,area_cm2,
                       operator,purpose,doc_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_no, today, w['wo_no'], bn, roll['roll_no'], roll['item_code'], roll.get('supplier_lot'),
                     a['actual_length'], a['actual_width'], forms.area(a['actual_length'], a['actual_width']), op,
                     'قص حسب بيانات المنتج', run_no))
        con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
        return run_no, tags, n_sub

    @route('/slitter/<path:bn>', 'slit_work', methods=['GET', 'POST'])
    def slit_work(bn):
        w = order_or_404(bn)
        if mfg.route_of_wo(w) != mfg.FULL:
            flash('هذا الأمر لا يمر بالسليتر (مساره يحدده المنتج)', 'bad')
            return redirect(url_for('order_view', bn=bn))
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for('slit_work', bn=bn)
        matches = mfg.match_subrolls(it)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر — لا يمكن إضافة سندات سليتر', 'bad')
                return redirect(back)
            roll_no = s(f.get('roll_no'))
            roll = next((r for r in jumbo_rows(w, it) if r['roll_no'] == roll_no), None)
            if not roll:
                other = db.one('SELECT * FROM rolls WHERE roll_no=?', (roll_no,)) if roll_no else None
                if other and (other.get('plan_no') or other.get('use_status') in ('مخصص', 'قيد القص', 'مستهلك')):
                    flash(f'الجامبو {roll_no} حالته «{other.get("use_status") or "مستخدم"}» في السند {other.get("plan_no") or "—"} — لا يُستخدم مرتين', 'bad')
                else:
                    flash('الجامبو غير متاح: يجب أن يكون مفرجًا عنه من الجودة وبحالة Available ومناسبًا للمنتج', 'bad')
                return redirect(back)
            try:
                operator = get_operator(f)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            aw, al = num(f.get('actual_width')) or roll['def_w'], num(f.get('actual_length')) or roll['def_l']
            if not aw or aw <= 0 or not al or al <= 0:
                flash('العرض والطول الفعليان يجب أن يكونا أكبر من صفر', 'bad')
                return redirect(back)
            # كود Sub Roll: من Master Data فقط
            chosen, manual = s(f.get('sr_code')), 0
            if len(matches) == 1:
                chosen = matches[0]['item_code']
            elif len(matches) > 1:
                if chosen not in {m['item_code'] for m in matches}:
                    flash('يوجد أكثر من Sub Roll مطابق — اختر الكود المناسب من الخيارات المطابقة', 'bad')
                    return redirect(back)
            else:
                if not chosen or not db.one("SELECT 1 FROM items WHERE item_code=? AND prefix='SR'", (chosen,)):
                    flash('لا يوجد Sub Roll مطابق لمواصفات المنتج. يرجى مراجعة Master Data أو اختيار الكود يدويًا.', 'bad')
                    return redirect(back)
                manual = 1
            st = mfg.order_state(w)
            unused = db.one("SELECT COUNT(*) n FROM subrolls WHERE batch_no=? AND stock_status IN ('مخطط','متاح')", (bn,))['n']
            yld = float(w.get('pieces_per_sr') or 0)
            if yld and unused * yld >= st['remaining'] - 1e-6 and unused > 0:
                flash(f'لديك {unused} سب رول (مخطط/متاح) تكفي المتبقي من الأمر — لا حاجة لجامبو إضافي', 'bad')
                return redirect(back)
            run = f.get('mode', 'run') == 'run'
            try:
                with db.tx() as con:
                    plan_no, tags, n_sub = _make_plan(con, w, it, roll, dict(
                        run=run, actual_width=aw, actual_length=al, operator=operator, sr_code=chosen, sr_manual=manual))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            except Exception as e:                    # noqa: BLE001 — الفهرس الفريد: جامبو واحد في سند واحد
                if 'UNIQUE' in str(e).upper():
                    flash(f'الجامبو {roll_no} داخل سند سليتر آخر ولا يدخل إلا سندًا واحدًا', 'bad')
                    return redirect(back)
                raise
            db.log('create', 'cutting_plans', plan_no, f'{bn}; {roll_no}; {n_sub} sub-rolls; {chosen}{" (يدوي)" if manual else ""}; '
                   f'{"executed" if run else "planned"}')
            if abs(aw - (roll.get('ref_w') or aw)) > 0.05 or abs(al - (roll.get('ref_l') or al)) > 0.5:
                flash(f'العرض/الطول الفعلي ({aw:g} سم / {al:g} م) يختلف عن المرجعي ({roll.get("ref_w") or "—"} / {roll.get("ref_l") or "—"})', 'warn')
            if run:
                flash(f'تم القص — السند {plan_no}: {n_sub} سب رول ({tags[0]} … {tags[-1]}) كود {chosen}', 'ok')
                next_bar(('طباعة بطاقات Sub Roll', url_for('print_label', kind='subroll', key=plan_no)),
                         ('استخدام السب رول في ماكينة الطي', url_for('fold_work', bn=bn), False),
                         ('عرض الرولات الفرعية', url_for('order_view', bn=bn) + '#subs', False))
            else:
                flash(f'حُفظ سند السليتر {plan_no} ({n_sub} سب رول) — أكّد التنفيذ عند القص الفعلي', 'ok')
            return redirect(back)
        st = mfg.order_state(w)
        plans = db.q('SELECT * FROM cutting_plans WHERE batch_no=? ORDER BY plan_no', (bn,))
        for p in plans:
            p['subs'] = db.q('SELECT * FROM subrolls WHERE plan_no=? ORDER BY tag_no', (p['plan_no'],))
            p['used'] = any(x['stock_status'] == 'مستهلك' for x in p['subs'])
            p['jumbo_status'] = {'مخصص': 'Allocated — مخصص', 'قيد القص': 'In Slitting — قيد القص',
                                 'مستهلك': 'Consumed — مستهلك'}.get(
                (db.one('SELECT use_status FROM rolls WHERE roll_no=?', (p['roll_no'],)) or {}).get('use_status'), 'Available — متاح')
        card = mfg.product_card(it, w['qty_required'])
        planned_sr = db.one("SELECT COUNT(*) n FROM subrolls WHERE batch_no=?", (bn,))['n']
        sr_all = db.q("SELECT item_code, description FROM items WHERE prefix='SR' AND status='نشط' ORDER BY item_code") if not matches else []
        return render_template('slit_work.html', nav='slit', w=w, st=st, plans=plans, card=card, req=card.get('req', {}),
                               jumbos=jumbo_rows(w, it), planned_sr=planned_sr, fmt=fmt_qty, editable=not w.get('final_status'),
                               can_delete_done=auth.can('wo_issue'), matches=matches, sr_all=sr_all)

    @route('/slitter/plan/<plan_no>/start', 'slit_start', methods=['POST'])
    def slit_start(plan_no):
        """بدء القص الفعلي: الجامبو In Slitting."""
        auth.need('enter')
        p = db.one('SELECT * FROM cutting_plans WHERE plan_no=?', (plan_no,))
        if not p:
            abort(404)
        order_or_404(p['batch_no'])
        with db.tx() as con:
            if con.execute("UPDATE rolls SET use_status='قيد القص' WHERE roll_no=? AND use_status='مخصص'", (p['roll_no'],)).rowcount:
                con.execute('UPDATE cutting_plans SET start_at=? WHERE plan_no=?',
                            (datetime.datetime.now().isoformat(sep=' ', timespec='seconds'), plan_no))
        db.log('start', 'cutting_plans', plan_no, p['roll_no'])
        return redirect(url_for('slit_work', bn=p['batch_no']))

    @route('/slitter/plan/<plan_no>/confirm', 'slit_confirm', methods=['POST'])
    def slit_confirm(plan_no):
        auth.need('enter')
        p = db.one('SELECT * FROM cutting_plans WHERE plan_no=?', (plan_no,))
        if not p:
            abort(404)
        w = order_or_404(p['batch_no'])
        if p['status'] == 'منفّذ':
            flash('السند منفّذ بالفعل', 'bad')
            return redirect(url_for('slit_work', bn=p['batch_no']))
        with db.tx() as con:
            now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
            con.execute("UPDATE cutting_plans SET status='منفّذ', executed_by=?, executed_at=? WHERE plan_no=?",
                        (p.get('operator') or g.user['full_name'], now, plan_no))
            con.execute("UPDATE subrolls SET stock_status='متاح' WHERE plan_no=? AND stock_status='مخطط'", (plan_no,))
            con.execute("UPDATE rolls SET use_status='مستهلك' WHERE roll_no=?", (p['roll_no'],))
        db.log('execute', 'cutting_plans', plan_no, p['batch_no'])
        flash(f'تم تنفيذ القص — أصبح السب رول متاحًا للطي ({p["n_sub"]})', 'ok')
        next_bar(('طباعة بطاقات Sub Roll', url_for('print_label', kind='subroll', key=plan_no)),
                 ('استخدام السب رول في ماكينة الطي', url_for('fold_work', bn=w['batch_no']), False))
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
            flash('السند نُفّذ فعليًا — حذفه من صلاحية مدير المصنع فقط', 'bad')
            return redirect(back)
        with db.tx() as con:
            used = con.execute("SELECT COUNT(*) n FROM subrolls WHERE plan_no=? AND stock_status NOT IN ('مخطط','متاح')",
                               (plan_no,)).fetchone()['n']
            if used:
                flash('لا يمكن حذف السند: استُخدم أحد سب رولاته في الطي — احذف سند الطي أولًا', 'bad')
                return redirect(back)
            snap = json.dumps(dict(p), ensure_ascii=False, default=str)
            con.execute('DELETE FROM subrolls WHERE plan_no=?', (plan_no,))
            con.execute('DELETE FROM slitting WHERE doc_no=?', (plan_no,))
            con.execute("""UPDATE rolls SET batch_no=NULL, plan_no=NULL, use_status='متاح', issue_date=NULL,
                           actual_width_cm=NULL, actual_length_m=NULL WHERE roll_no=?""", (p['roll_no'],))
            con.execute('DELETE FROM cutting_plans WHERE plan_no=?', (plan_no,))
        db.log('delete', 'cutting_plans', plan_no, snap)
        flash(f'حُذف السند {plan_no} وعاد الجامبو {p["roll_no"]} متاحًا', 'ok')
        return redirect(back)

    # ============================================================ الطي
    @route('/folding', 'fold')
    def fold():
        rows = [w for w in visible_orders(300, "AND IFNULL(route_code,'FULL_GAUZE')='FULL_GAUZE' AND final_status IS NULL")
                if w['st']['stage'] in ('slitting', 'ready_fold', 'production')]
        for w in rows:
            w['avail'] = len(mfg.avail_subrolls(w))
        return render_template('fold_list.html', nav='fold', rows=rows, fmt=fmt_qty)

    def _waste_limit(it):
        v = it.get('waste_limit_pct')
        return float(v) if v not in (None, '') else mfg._setting_f('scrap_limit', 0.03) * 100.0

    def _calc(w, sr, good, rejected):
        """الكمية النظرية والهدر: مساحة السب رول الفعلية ÷ استهلاك المسحة القياسي."""
        theo = mfg.theoretical_swabs(w.get('fold_machine'), w.get('std_width_cm'), sr.get('length_m'), sr.get('width_cm'))
        if not theo:
            return dict(theo=None, waste=None, pct=None, yield_pct=None)
        waste = max(theo - good, 0.0)
        return dict(theo=theo, waste=waste, pct=round(waste / theo * 100, 2), yield_pct=round(good / theo * 100, 2))

    def _to_swabs(it, qty, unit):
        """يحوّل كمية مكتوبة بوحدة (مسحة/باكت/بوكس) إلى مسحات حسب Packaging Configuration."""
        u = mfg.norm_unit(unit)
        if u in ('swab', ''):
            return qty
        ps = mfg.pack_spec(it['item_code'])
        per = ps.get('swabs_per_pack') if u == 'pack' else (ps.get('swabs_per_box') if u == 'box' else None)
        if not per:
            raise ValueError('لا يوجد تحويل لهذه الوحدة في Packaging Configuration')
        return qty * per

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
                flash('اختر السب رول (أو امسح الباركود)', 'bad')
                return redirect(back)
            if qty is None or qty <= 0:
                flash('عدد المسحات السليمة يجب أن يكون أكبر من صفر', 'bad')
                return redirect(back)
            try:
                operator = get_operator(f)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            now = datetime.datetime.now()
            t0 = s(f.get('t0')) or now.strftime('%H:%M')
            try:
                doc_no, calc, inter = _save_doc(w, it, tag, qty, num(f.get('rejected'), 0) or 0, operator, s(f.get('notes')), t0, now)
            except SubRollUsed as e:
                flash(used_msg(e.doc), 'bad')
                return redirect(back)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            if calc['theo']:
                lim = _waste_limit(it)
                msg = f'الطي {doc_no}: نظري {fmt_qty(calc["theo"])} · سليم {fmt_qty(qty)} · هدر {fmt_qty(calc["waste"])} ({calc["pct"]:g}%) · Yield {calc["yield_pct"]:g}%'
                if calc['pct'] > lim:
                    flash(msg + f' — تحذير: الهدر أعلى من الحد ({lim:g}%)', 'warn')
                else:
                    flash(msg, 'ok')
                if qty > calc['theo'] * 1.02:
                    flash(f'تنبيه: السليم {fmt_qty(qty)} أعلى من النظري {fmt_qty(calc["theo"])} — راجع الرقم أو الأبعاد', 'warn')
            else:
                flash(f'تم حفظ سند الطي {doc_no} (لا يمكن حساب النظري: بيانات ماكينة/طول ناقصة)', 'warn')
            w2 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
            st = mfg.order_state(w2)
            btns = []
            if inter:
                btns.append(('طباعة بطاقة الكرتون الوسيط', url_for('print_label', kind='intermediate', key=inter)))
            if mfg.avail_subrolls(w2) and st['remaining'] > 1e-6:
                btns.append(('استخدام السب رول التالي', back, not btns))
            elif st['remaining'] > 1e-6:
                btns.append(('استكمال القص', url_for('slit_work', bn=bn), not btns))
            if st['v15'] and not st['sterile'] and st['unpacked'] > 1e-6:
                btns.append(('تعبئة', url_for('ns_pack', bn=bn), not btns))
            btns.append(('العودة لأمر الإنتاج', url_for('order_view', bn=bn), not btns))
            next_bar(*btns)
            return redirect(back)
        st = mfg.order_state(w)
        docs = db.q('SELECT * FROM folding_out WHERE batch_no=? ORDER BY id DESC', (bn,))
        subs = mfg.avail_subrolls(w)
        for x in subs:
            x['theo'] = mfg.theoretical_swabs(w.get('fold_machine'), w.get('std_width_cm'), x.get('length_m'), x.get('width_cm'))
        pre = s(request.args.get('tag'))
        return render_template('fold_work.html', nav='fold', w=w, st=st, card=mfg.product_card(it, w['qty_required']), subs=subs,
                               docs=docs, editable=not w.get('final_status') or w['final_status'] == mfg.FS_PENDING,
                               t0=datetime.datetime.now().strftime('%H:%M'), fmt=fmt_qty, pre=pre, limit=_waste_limit(it),
                               next_doc=db.peek_number(f'FOLD-{_year()}', f'FOLD-{_year()}-{{n6}}'))

    def _save_doc(w, it, tag, qty, rejected, operator, notes, t0, now):
        """يحفظ سند طي: يستهلك السب رول ذريًا ويُنشئ المخرج (مسحات) والنظري والهدر وقيد المخزون، ولمنتج معقم
        كرتونًا وسيطًا SP. يرفع SubRollUsed عند التكرار."""
        bn = w['batch_no']
        machine = w.get('fold_machine')
        pa = forms.piece_area_cm2(machine, w.get('std_width_cm')) if machine else None
        chk = forms.machine_size_check(machine, w.get('size'))
        inter = None
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
                        raise ValueError('السب رول ضمن سند سليتر لم يُنفّذ بعد — أكّد تنفيذ القص أولًا')
                    raise SubRollUsed(sr.get('used_doc') or '—')
                ok, why = mfg.subroll_fits(dict(fresh), sr)
                if not ok:
                    raise ValueError(why)
                calc = _calc(dict(fresh), sr, qty, rejected)
                doc_no = db.alloc(con, f'FOLD-{_year()}', f'FOLD-{_year()}-{{n6}}')
                today = now.date().isoformat()
                cur = con.execute("""UPDATE subrolls SET stock_status='مستهلك', used_doc=?, consumed_by_batch=?, consumed_date=?
                                     WHERE tag_no=? AND stock_status='متاح'""", (doc_no, bn, today, tag))
                if cur.rowcount != 1:
                    again = con.execute('SELECT used_doc FROM subrolls WHERE tag_no=?', (tag,)).fetchone()
                    raise SubRollUsed((again and again['used_doc']) or '—')
                L = sr.get('length_m')
                con.execute("""INSERT INTO folding_in(doc_no,fdate,machine_code,size,operator,wo_no,batch_no,tag_no,roll_no,sr_code,
                               xray_grade,length_used_m,width_cm,area_used_cm2,fully_used,notes,sr_source_batch)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc_no, today, machine, w.get('size'), operator, w['wo_no'], bn, tag, sr['roll_no'], sr.get('sr_code'),
                             sr.get('xray_grade'), L, sr.get('width_cm'), forms.area(L, sr.get('width_cm')) if L else None,
                             'نعم', notes, sr.get('slit_batch') or sr.get('batch_no')))
                con.execute("""INSERT INTO folding_out(doc_no,fdate,machine_code,batch_no,route,item_code,ply,size,piece_area_cm2,
                               qty_good,unit,tag_no,operator,start_time,end_time,machine_size_check,notes,created_by,created_at,
                               theoretical_qty,waste_qty,rejected_qty,waste_pct)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc_no, today, machine, bn, w.get('route'), w['item_code'], w.get('ply'), w.get('size'), pa, qty, mfg.SWAB,
                             tag, operator, t0, now.strftime('%H:%M'), chk, notes, g.user['username'],
                             now.isoformat(sep=' ', timespec='seconds'), calc['theo'], calc['waste'], rejected or None, calc['pct']))
                inventory.post(con, bn, 'PROD_OUT', qty, mfg.SWAB, bn, mfg.FULL, 'fold', doc_no, note=f'سند طي من {tag}')
                if mfg.variant_for(fresh) == mfg.STERILE:
                    inter = db.alloc(con, 'SPC', 'SPC-{n6}')
                    con.execute("""INSERT INTO intermediates(barcode,batch_no,item_code,sp_code,sp_source,fold_doc,tag_no,qty,prod_date,operator)
                                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (inter, bn, w['item_code'], it.get('sp_item'), 'INTERNAL_PRODUCTION', doc_no, tag, qty, today, operator))
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
        except SubRollUsed:
            raise
        except Exception as e:                    # noqa: BLE001 — الفهرس الفريد يمنع الاستخدام المزدوج حتى في التسابق
            if 'UNIQUE' in str(e).upper() and 'tag_no' in str(e):
                r = db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tag,))
                raise SubRollUsed((r or {}).get('doc_no') or '—')
            raise
        db.log('create', 'folding_out', doc_no, f'{bn}; {tag}; {fmt_qty(qty)} مسحة; نظري {calc["theo"]}')
        return doc_no, calc, inter

    def _doc_or_404(doc_no):
        fo = db.one('SELECT * FROM folding_out WHERE doc_no=? AND tag_no IS NOT NULL', (doc_no,))
        if not fo:
            abort(404)
        w = order_or_404(fo['batch_no'])
        return fo, w

    def fold_deps(w, fo, new_swabs, deleting=False):
        """اعتماديات السند: blocks تمنع الحفظ (يجب عكس سند لاحق أولًا)، وresets تُلغى تلقائيًا بعد تأكيد المدير."""
        bn, blocks, resets = w['batch_no'], [], []
        old = float(fo['qty_good'] or 0)
        if mfg.is_v15(w):
            if mfg.variant_for(w) == mfg.STERILE:
                ic = db.one('SELECT * FROM intermediates WHERE fold_doc=?', (fo['doc_no'],))
                if ic:
                    if ic['rec_no']:
                        blocks.append(f'الكرتون الوسيط {ic["barcode"]} مُدخَل في سجل الفرز {ic["rec_no"]} — احذف/عدّل ذلك السجل أولًا')
                    elif ic['status'] != 'Pending SP Release' and (deleting or abs(new_swabs - old) > 1e-9):
                        resets.append(f'قرار إفراج SP للكرتون {ic["barcode"]} ({ic["status"]}) سيُلغى ويعود «بانتظار الإفراج»')
            else:
                packed = float(db.one('SELECT IFNULL(SUM(swabs_in),0) n FROM ns_packing WHERE batch_no=?', (bn,))['n'])
                folded_after = float(db.one('SELECT IFNULL(SUM(qty_good),0) n FROM folding_out WHERE batch_no=?', (bn,))['n']) \
                    - old + (0 if deleting else new_swabs)
                if packed > folded_after + 1e-9:
                    docs = '، '.join(r['doc_no'] for r in db.q('SELECT doc_no FROM ns_packing WHERE batch_no=? ORDER BY doc_no', (bn,)))
                    blocks.append(f'سندات التعبئة ({docs}) تستهلك {fmt_qty(packed)} مسحة وهي أكبر من إنتاج الطي بعد التعديل '
                                  f'({fmt_qty(folded_after)}) — عدّل/احذف سند التعبئة أولًا')
        fs = w.get('final_status')
        if fs in (mfg.FS_PENDING, mfg.FS_APPROVED, mfg.FS_HOLD, mfg.FS_REJECTED) and (deleting or abs(new_swabs - old) > 1e-9):
            resets.append(f'قرار الجودة الحالي ({mfg.FS_AR.get(fs)}) سيُسحب وتعود الدفعة لتُنهى من جديد')
        return dict(blocks=blocks, resets=resets)

    def _apply_intermediate(con, doc_no, qty, deleting, tag=None, operator=None):
        ic = con.execute('SELECT * FROM intermediates WHERE fold_doc=?', (doc_no,)).fetchone()
        if not ic:
            return
        if deleting:
            con.execute('DELETE FROM intermediates WHERE fold_doc=?', (doc_no,))
        else:
            con.execute("""UPDATE intermediates SET qty=?, status='Pending SP Release', tag_no=COALESCE(?,tag_no),
                           operator=COALESCE(?,operator) WHERE fold_doc=?""", (qty, tag, operator, doc_no))

    @route('/folding/doc/<doc_no>/edit', 'fold_edit', methods=['GET', 'POST'])
    def fold_edit(doc_no):
        fo, w = _doc_or_404(doc_no)
        bn = w['batch_no']
        g.crumb_args = {'bn': bn}
        it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for('fold_work', bn=bn)
        ok, why = doc_gate(w)
        if not ok:
            flash(why, 'bad')
            return redirect(back)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            new_tag = s(f.get('tag_no')) or fo['tag_no']
            try:
                qty_in = num(f.get('qty_good'))
                if qty_in is None or qty_in <= 0:
                    raise ValueError('عدد المسحات يجب أن يكون أكبر من صفر')
                qty = _to_swabs(it, qty_in, s(f.get('unit')) or mfg.SWAB)
                operator = get_operator(f)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            old_qty, old_tag = float(fo['qty_good'] or 0), fo['tag_no']
            deps = fold_deps(w, fo, qty)
            if deps['blocks']:
                for b_ in deps['blocks']:
                    flash(b_, 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            if deps['resets'] and not f.get('confirm'):
                for r_ in deps['resets']:
                    flash('تحذير قبل الحفظ: ' + r_, 'warn')
                subs = mfg.avail_subrolls(w)
                cur_sr = db.one('SELECT * FROM subrolls WHERE tag_no=?', (fo['tag_no'],))
                if cur_sr and all(x['tag_no'] != cur_sr['tag_no'] for x in subs):
                    subs = [cur_sr] + subs
                return render_template('fold_edit.html', nav='fold', w=w, fo=dict(fo, qty_good=qty_in), subs=subs, fmt=fmt_qty,
                                       need_confirm=True, resets=deps['resets'], sel_unit=s(f.get('unit')) or mfg.SWAB,
                                       card=mfg.product_card(it, w['qty_required']), sel_op=operator)
            rejected = num(f.get('rejected'), 0) or 0
            try:
                with db.tx() as con:
                    cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
                    if new_tag != old_tag:
                        sr = con.execute('SELECT * FROM subrolls WHERE tag_no=?', (new_tag,)).fetchone()
                        if not sr:
                            raise ValueError('السب رول غير موجود')
                        if sr['stock_status'] != 'متاح':
                            raise SubRollUsed(sr['used_doc'] or '—')
                        ok2, why2 = mfg.subroll_fits(cur, dict(sr))
                        if not ok2:
                            raise ValueError(why2)
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
                    srrow = dict(con.execute('SELECT * FROM subrolls WHERE tag_no=?', (new_tag,)).fetchone())
                    calc = _calc(cur, srrow, qty, rejected)
                    delta = qty - old_qty
                    if abs(delta) > 1e-9:
                        inventory.post(con, bn, 'PROD_OUT', delta, mfg.SWAB, bn, mfg.FULL, 'fold_edit', doc_no,
                                       note=f'تعديل سند: {fmt_qty(old_qty)} ← {fmt_qty(qty)}')
                    now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
                    con.execute("""UPDATE folding_out SET qty_good=?, unit=?, tag_no=?, operator=?, notes=?, updated_by=?, updated_at=?,
                                   theoretical_qty=?, waste_qty=?, waste_pct=?, rejected_qty=? WHERE doc_no=?""",
                                (qty, mfg.SWAB, new_tag, operator, s(f.get('notes')), g.user['username'], now, calc['theo'],
                                 calc['waste'], calc['pct'], rejected or None, doc_no))
                    con.execute('UPDATE folding_in SET notes=?, operator=? WHERE doc_no=?', (s(f.get('notes')), operator, doc_no))
                    _apply_intermediate(con, doc_no, qty, False, new_tag, operator)
                    withdrawn = withdraw_pending(con, cur)
            except SubRollUsed as e:
                flash(used_msg(e.doc), 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(url_for('fold_edit', doc_no=doc_no))
            db.log('edit', 'folding_out', doc_no, f'qty {fmt_qty(old_qty)}→{fmt_qty(qty)} مسحة; sub-roll {old_tag}→{new_tag}; op {operator}')
            flash(f'تم تعديل السند {doc_no} وأُعيد احتساب الإنتاج والنظري والهدر والمخزون' +
                  (' — سُحب قرار الجودة وتعود الدفعة لتُنهى من جديد' if withdrawn else ''), 'ok')
            next_bar(('العودة لأمر الإنتاج', url_for('order_view', bn=bn)), ('سندات الطي', back, False))
            return redirect(back)
        subs = mfg.avail_subrolls(w)
        cur_sr = db.one('SELECT * FROM subrolls WHERE tag_no=?', (fo['tag_no'],))
        if cur_sr and all(x['tag_no'] != cur_sr['tag_no'] for x in subs):
            subs = [cur_sr] + subs
        return render_template('fold_edit.html', nav='fold', w=w, fo=fo, subs=subs, fmt=fmt_qty, need_confirm=False, resets=[],
                               sel_unit=mfg.SWAB, card=mfg.product_card(it, w['qty_required']), sel_op=fo.get('operator'))

    @route('/folding/doc/<doc_no>/delete', 'fold_delete', methods=['POST'])
    def fold_delete(doc_no):
        auth.need('enter')
        fo, w = _doc_or_404(doc_no)
        bn = w['batch_no']
        back = url_for('fold_work', bn=bn)
        ok, why = doc_gate(w)
        if not ok:
            flash(why, 'bad')
            return redirect(back)
        deps = fold_deps(w, fo, 0, deleting=True)
        if deps['blocks']:
            for b_ in deps['blocks']:
                flash(b_, 'bad')
            return redirect(back)
        if deps['resets'] and not request.form.get('confirm'):
            for r_ in deps['resets']:
                flash('تحذير: ' + r_ + ' — أعد الضغط على «حذف» مع التأكيد', 'warn')
            return render_template('confirm_delete.html', nav='fold', w=w, fo=fo, resets=deps['resets'], fmt=fmt_qty)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            snap = json.dumps(dict(fo), ensure_ascii=False, default=str)
            # عكس المعاملة: السب رول يعود متاحًا، المخرج يُحذف، وقيد المخزون يُعكس بقيد سالب
            con.execute("""UPDATE subrolls SET stock_status='متاح', used_doc=NULL, consumed_by_batch=NULL, consumed_date=NULL
                           WHERE tag_no=?""", (fo['tag_no'],))
            con.execute('DELETE FROM folding_in WHERE doc_no=?', (doc_no,))
            con.execute('DELETE FROM folding_out WHERE doc_no=?', (doc_no,))
            inventory.post(con, bn, 'PROD_OUT', -float(fo['qty_good'] or 0), mfg.SWAB, bn, mfg.FULL, 'fold_delete', doc_no,
                           note=f'عكس قيد — حذف السند {doc_no}')
            _apply_intermediate(con, doc_no, 0, True)
            withdrawn = withdraw_pending(con, cur)
        db.log('delete', 'folding_out', doc_no, snap)
        flash(f'حُذف السند {doc_no} وعاد السب رول {fo["tag_no"]} متاحًا وخُصمت {fmt_qty(fo["qty_good"])} مسحة من الإنتاج' +
              (' — سُحب قرار الجودة وتعود الدفعة لتُنهى من جديد' if withdrawn else ''), 'ok')
        return redirect(back)

    # ============================================================ حذف سندات الخطوط الأخرى (عكس المعاملة)
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
            inventory.reverse(con, proc_no, f'عكس قيد — حذف السند {proc_no}')
            con.execute('DELETE FROM batch_links WHERE child=?', (proc_no,))
            con.execute('DELETE FROM proc_batches WHERE proc_no=?', (proc_no,))
            withdrawn = withdraw_pending(con, cur)
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
                inventory.reverse(con, doc_no, f'عكس قيد — إلغاء التخصيص {doc_no}')
                con.execute("UPDATE batch_links SET qty=qty-? WHERE child=? AND parent=? AND link_type='SP_LOT'",
                            (a['qty'], bn, a['source_ref']))
                con.execute("DELETE FROM batch_links WHERE child=? AND parent=? AND link_type='SP_LOT' AND qty<=0.000001",
                            (bn, a['source_ref']))
            con.execute('UPDATE allocations SET voided=1 WHERE doc_no=?', (doc_no,))
            withdrawn = withdraw_pending(con, dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone()))
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
