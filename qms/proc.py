# -*- coding: utf-8 -*-
"""ما بعد الطي (v15): تعبئة غير المعقم، والمعقم: كرتون وسيط SP ← سجل فرز/عد/تغليف/بوكس واحد ← دورة تعقيم ← تهوية.

  غير معقم :  مسحات الطي (100 مسحة = 1 باكت) ← باكت ← كرتون (حسب Packaging Configuration) ← إفراج ← مخزن
  معقم     :  مسحات الطي ← كرتون وسيط SP (باركود) ← إفراج SP ← سجل معالجة واحد يُحدَّث (فرز وعد ← تغليف ← بوكس)
              ← إفراج قبل التعقيم ← دورة تعقيم (عدة LOTs) ← تهوية ← إفراج نهائي ← مخزن

كل مرحلة تأخذ مخرجات السابقة بالهوية نفسها (باركود/رقم سند/LOT) ولا تُطلب مواصفات الصنف مرة ثانية.
الوحدات: الطي = مسحة. غير المعقم: تعبئة (باكت/كرتون). المعقم: تغليف (مغلف/بوكس/كرتون).
"""
import datetime, json, math
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, notify, prod
from util import s, num, fmt_qty, batch_stem

REC_AR = {'Sorting': 'الفرز والعد', 'Packaging': 'التغليف', 'Boxing': 'البوكسات',
          'Ready for Pre-Sterilization QC': 'بانتظار الإفراج قبل التعقيم', 'Pre-Sterilization Hold': 'معلّق قبل التعقيم',
          'Rejected Pre-Sterilization': 'مرفوض قبل التعقيم',
          'Released for Sterilization': 'جاهز للتعقيم', 'In Sterilization': 'في التعقيم',
          'Sterilization Completed': 'اكتمل التعقيم', 'Transferred to Aeration': 'في التهوية',
          'Aeration Completed': 'اكتملت التهوية', 'Pending Final QC Release': 'بانتظار الإفراج النهائي'}
INTER_AR = {'Pending SP Release': 'بانتظار إفراج SP', 'Released for Sorting': 'مُفرج للفرز والعد',
            'Rejected': 'مرفوض', 'Hold': 'معلّق', 'Consumed': 'داخل سجل معالجة'}
CYC_AR = {'In Progress': 'قيد التعقيم', 'Sterilization Completed': 'اكتمل التعقيم', 'Transferred to Aeration': 'في التهوية',
          'Aeration Completed': 'اكتملت التهوية'}
EDITABLE_REC = ('Sorting', 'Packaging', 'Boxing', 'Ready for Pre-Sterilization QC', 'Pre-Sterilization Hold')


def now_s():
    return datetime.datetime.now().isoformat(sep=' ', timespec='minutes')


def _year():
    return datetime.date.today().strftime('%Y')


def ster_sync(bn):
    """المعقم: يطلب الإفراج النهائي آليًا حين تكتمل كل كراتين الدفعة (غير المرفوضة) وتنتظر السجلات الإفراج النهائي؛
    ويسحبه إن انكسر الشرط (كرتون جديد/سجل مفتوح). إشعار واحد فقط."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
    if not w or mfg.variant_for(w) != mfg.STERILE or w.get('final_status') not in (None, mfg.FS_PENDING):
        return
    recs = db.q('SELECT * FROM ster_records WHERE batch_no=?', (bn,))
    open_cartons = db.one("SELECT COUNT(*) n FROM intermediates WHERE batch_no=? AND rec_no IS NULL AND status<>'Rejected'", (bn,))['n']
    ok = bool(recs) and not open_cartons and all(r['status'] in ('Pending Final QC Release', 'Rejected Pre-Sterilization') for r in recs) \
        and any(r['status'] == 'Pending Final QC Release' for r in recs)
    with db.tx() as con:
        if ok and w.get('final_status') is None:
            qty = float(sum((r['boxes_actual'] or 0) for r in recs if r['status'] == 'Pending Final QC Release'))
            con.execute("""UPDATE work_orders SET final_status=?, finished_at=?, finished_by=?, produced_qty=?, status='قيد التنفيذ'
                           WHERE batch_no=? AND final_status IS NULL""",
                        (mfg.FS_PENDING, now_s(), 'النظام (بعد التهوية)', qty, bn))
            notify.push(con, 'production_complete',
                        f'اكتملت تهوية الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، والكمية النهائية {fmt_qty(qty)} بوكس، '
                        f'وهي بانتظار الإفراج النهائي بعد التعقيم.', w['item_code'], url_for('quality_review', bn=bn), bn,
                        mfg.FULL, ('qc_sign',))
        elif not ok and w.get('final_status') == mfg.FS_PENDING:
            con.execute("UPDATE work_orders SET final_status=NULL, finished_at=NULL, finished_by=NULL, produced_qty=NULL WHERE batch_no=?", (bn,))


def _order(bn):
    w = prod.order_or_404(bn)
    it = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
    return w, it


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    # =================================================================== غير المعقم: التعبئة
    def _unpacked(bn, exclude_doc=None):
        folded = float(db.one('SELECT IFNULL(SUM(qty_good),0) n FROM folding_out WHERE batch_no=?', (bn,))['n'])
        packed = float(db.one('SELECT IFNULL(SUM(swabs_in),0) n FROM ns_packing WHERE batch_no=? AND doc_no<>?',
                              (bn, exclude_doc or ''))['n'])
        return max(folded - packed, 0.0), folded

    def _fifo(con, doc_no, bn, swabs):
        """يربط التعبئة بسندات الطي (الأقدم أولًا) لتتبع الباكت إلى السب رول."""
        con.execute('DELETE FROM ns_pack_src WHERE doc_no=?', (doc_no,))
        left = swabs
        for fo in con.execute("SELECT doc_no, qty_good FROM folding_out WHERE batch_no=? ORDER BY id", (bn,)).fetchall():
            used = con.execute('SELECT IFNULL(SUM(swabs),0) FROM ns_pack_src WHERE fold_doc=?', (fo['doc_no'],)).fetchone()[0]
            take = min(left, max((fo['qty_good'] or 0) - used, 0))
            if take > 1e-9:
                con.execute('INSERT INTO ns_pack_src(doc_no,fold_doc,swabs) VALUES(?,?,?)', (doc_no, fo['doc_no'], take))
                left -= take
            if left <= 1e-9:
                break

    def _post_pack_ledger(con, doc_no, bn, swabs, packs):
        inventory.post(con, bn, 'PROD_OUT', -swabs, mfg.SWAB, bn, mfg.FULL, 'ns_pack', doc_no, note='تعبئة')
        inventory.post(con, bn, 'NSP_OUT', packs, 'باكت', bn, mfg.FULL, 'ns_pack', doc_no, note='باكتات معبأة')

    def _pack_calc(it, swabs, packs_in=None, cartons_in=None):
        ps = mfg.pack_spec(it['item_code'])
        spp, ppc = ps.get('swabs_per_pack'), ps.get('packs_per_carton')
        if not spp:
            raise ValueError('عدد المسحات في الباكت غير معرَّف — يُكمله مدير النظام من «تكوين التعبئة»')
        p_exp = swabs / spp
        packs = packs_in if packs_in is not None else math.floor(p_exp + 1e-9)
        c_exp = (packs / ppc) if ppc else None
        cartons = cartons_in if cartons_in is not None else (math.floor(c_exp + 1e-9) if c_exp is not None else None)
        return dict(spp=spp, ppc=ppc, p_exp=p_exp, packs=packs, c_exp=c_exp, cartons=cartons)

    @route('/packing/<path:bn>', 'ns_pack', methods=['GET', 'POST'])
    def ns_pack(bn):
        w, it = _order(bn)
        if not mfg.is_v15(w) or mfg.variant_for(w) == mfg.STERILE:
            flash('التعبئة لمنتجات الشاش غير المعقمة (المعقم له «تغليف» في سجل المعالجة)', 'bad')
            return redirect(url_for('order_view', bn=bn))
        back = url_for('ns_pack', bn=bn)
        unpacked, folded = _unpacked(bn)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            if w.get('final_status'):
                flash('أُنهي الإنتاج — لا سندات تعبئة جديدة', 'bad')
                return redirect(back)
            try:
                operator = prod.get_operator(f)
                swabs = num(f.get('swabs_in'), unpacked)
                if not swabs or swabs <= 0 or swabs - unpacked > 1e-6:
                    raise ValueError(f'عدد المسحات غير صحيح — غير المعبأ المتاح {fmt_qty(unpacked)} مسحة')
                calc = _pack_calc(it, swabs, num(f.get('packs_actual')), num(f.get('cartons_actual')))
                if calc['packs'] is None or calc['packs'] <= 0:
                    raise ValueError('عدد الباكتات يجب أن يكون أكبر من صفر')
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            with db.tx() as con:
                doc = db.alloc(con, f'PACK-{_year()}', f'PACK-{_year()}-{{n6}}')
                con.execute("""INSERT INTO ns_packing(doc_no,batch_no,swabs_in,swabs_per_pack,packs_per_carton,packs_expected,packs_actual,
                               cartons_expected,cartons_actual,operator,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc, bn, swabs, calc['spp'], calc['ppc'], calc['p_exp'], calc['packs'], calc['c_exp'], calc['cartons'],
                             operator, s(f.get('notes')), g.user['username']))
                _fifo(con, doc, bn, swabs)
                _post_pack_ledger(con, doc, bn, swabs, calc['packs'])
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            db.log('create', 'ns_packing', doc, f'{bn}; {fmt_qty(swabs)} مسحة → {fmt_qty(calc["packs"])} باكت')
            diff = calc['p_exp'] - calc['packs']
            flash(f'سند التعبئة {doc}: {fmt_qty(swabs)} مسحة ← {fmt_qty(calc["packs"])} باكت'
                  + (f' ← {fmt_qty(calc["cartons"])} كرتون' if calc['cartons'] is not None else '')
                  + (f' — فارق عن المتوقع {calc["p_exp"]:.2f} باكت' if abs(diff) > 0.005 else ''), 'ok')
            st = mfg.order_state(db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)))
            btns = [('طباعة السند', url_for('print_doc', kind='ns_pack', key=doc))]
            if st['unpacked'] > 1e-6:
                btns.append(('تعبئة المتبقي', back, False))
            else:
                btns.append(('إنهاء الإنتاج', url_for('order_view', bn=bn) + '#finish', False))
            btns.append(('أمر الإنتاج', url_for('order_view', bn=bn), False))
            prod.next_bar(*btns)
            return redirect(back)
        rows = db.q('SELECT * FROM ns_packing WHERE batch_no=? ORDER BY doc_no DESC', (bn,))
        ps = mfg.pack_spec(it['item_code'])
        pre = None
        if unpacked > 0 and ps.get('swabs_per_pack'):
            pre = _pack_calc(it, unpacked)
        return render_template('ns_pack.html', nav='fold', w=w, it=it, rows=rows, unpacked=unpacked, folded=folded, ps=ps, pre=pre,
                               st=mfg.order_state(w), fmt=fmt_qty, gate=prod.doc_gate(w)[0],
                               next_doc=db.peek_number(f'PACK-{_year()}', f'PACK-{_year()}-{{n6}}'))

    def _pack_or_404(doc_no):
        r = db.one('SELECT * FROM ns_packing WHERE doc_no=?', (doc_no,))
        if not r:
            abort(404)
        w, it = _order(r['batch_no'])
        return r, w, it

    @route('/packing/doc/<doc_no>/edit', 'ns_pack_edit', methods=['GET', 'POST'])
    def ns_pack_edit(doc_no):
        r, w, it = _pack_or_404(doc_no)
        bn = w['batch_no']
        g.crumb_args = {'bn': bn}
        back = url_for('ns_pack', bn=bn)
        ok, why = prod.doc_gate(w)
        if not ok:
            flash(why, 'bad')
            return redirect(back)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            unpacked, _ = _unpacked(bn, doc_no)
            try:
                operator = prod.get_operator(f)
                swabs = num(f.get('swabs_in'))
                if not swabs or swabs <= 0 or swabs - unpacked > 1e-6:
                    raise ValueError(f'عدد المسحات غير صحيح — المتاح لهذا السند {fmt_qty(unpacked)} مسحة')
                calc = _pack_calc(it, swabs, num(f.get('packs_actual')), num(f.get('cartons_actual')))
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(url_for('ns_pack_edit', doc_no=doc_no))
            if w.get('final_status') and not request.form.get('confirm'):
                flash(f'تحذير: قرار الجودة الحالي ({mfg.FS_AR.get(w["final_status"])}) سيُسحب بعد التعديل — أعد الحفظ مع تأكيد', 'warn')
                return render_template('ns_pack_edit.html', nav='fold', w=w, r=dict(r, swabs_in=swabs), fmt=fmt_qty, need_confirm=True)
            with db.tx() as con:
                cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
                inventory.reverse(con, doc_no, f'تعديل سند التعبئة {doc_no}')
                con.execute("""UPDATE ns_packing SET swabs_in=?, swabs_per_pack=?, packs_per_carton=?, packs_expected=?, packs_actual=?,
                               cartons_expected=?, cartons_actual=?, operator=?, notes=?, updated_by=?, updated_at=? WHERE doc_no=?""",
                            (swabs, calc['spp'], calc['ppc'], calc['p_exp'], calc['packs'], calc['c_exp'], calc['cartons'], operator,
                             s(f.get('notes')), g.user['username'], now_s(), doc_no))
                _fifo(con, doc_no, bn, swabs)
                _post_pack_ledger(con, doc_no, bn, swabs, calc['packs'])
                withdrawn = prod.withdraw_pending(con, cur)
            db.log('edit', 'ns_packing', doc_no, f'{fmt_qty(r["swabs_in"])}→{fmt_qty(swabs)} مسحة; packs {fmt_qty(calc["packs"])}')
            flash(f'تم تعديل سند التعبئة {doc_no} وأُعيد احتساب الباكتات والكراتين والمخزون' +
                  (' — سُحب قرار الجودة' if withdrawn else ''), 'ok')
            return redirect(back)
        return render_template('ns_pack_edit.html', nav='fold', w=w, r=r, fmt=fmt_qty, need_confirm=False,
                               unpacked=_unpacked(bn, doc_no)[0])

    @route('/packing/doc/<doc_no>/delete', 'ns_pack_delete', methods=['POST'])
    def ns_pack_delete(doc_no):
        auth.need('enter')
        r, w, it = _pack_or_404(doc_no)
        bn = w['batch_no']
        back = url_for('ns_pack', bn=bn)
        ok, why = prod.doc_gate(w)
        if not ok:
            flash(why, 'bad')
            return redirect(back)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            inventory.reverse(con, doc_no, f'عكس قيد — حذف سند التعبئة {doc_no}')
            con.execute('DELETE FROM ns_pack_src WHERE doc_no=?', (doc_no,))
            con.execute('DELETE FROM ns_packing WHERE doc_no=?', (doc_no,))
            withdrawn = prod.withdraw_pending(con, cur)
        db.log('delete', 'ns_packing', doc_no, json.dumps(dict(r), ensure_ascii=False, default=str))
        flash(f'حُذف سند التعبئة {doc_no} وعادت {fmt_qty(r["swabs_in"])} مسحة غير معبأة' +
              (' — سُحب قرار الجودة' if withdrawn else ''), 'ok')
        return redirect(back)

    # =================================================================== SP وارد من مورّد: يغذّي غير المعقم أو المعقم
    def _sp_order(bn):
        w, it = _order(bn)
        if not mfg.is_v15(w):
            abort(404)
        return w, it

    @route('/sp-supply/<path:bn>', 'sp_supply', methods=['GET', 'POST'])
    def sp_supply(bn):
        """استخدام LOT SP وارد (نصف مصنع، مفرج عنه) كمصدر مسحات لأمر شاش كامل — معقم أو غير معقم — بدل الطي الداخلي."""
        import sp as sp_mod
        w, it = _sp_order(bn)
        back = url_for('sp_supply', bn=bn)
        lots = [x for x in sp_mod.compatible_lots(it, only_available=True)]
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            grn = s(f.get('grn_no'))
            lot = next((x for x in lots if x['grn_no'] == grn), None)
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر', 'bad'); return redirect(back)
            if not lot:
                flash('LOT SP غير متاح: يجب أن يكون مفرجًا عنه، غير منتهٍ، ومطابقًا لمقاس/طبقات/كاشف/ميش المنتج', 'bad'); return redirect(back)
            qty = num(f.get('qty'), lot['available'])
            if not qty or qty <= 0 or qty - lot['available'] > 1e-6:
                flash(f'الكمية غير صحيحة — المتاح في الـ LOT {fmt_qty(lot["available"])}', 'bad'); return redirect(back)
            try:
                operator = prod.get_operator(f)
            except ValueError as e:
                flash(str(e), 'bad'); return redirect(back)
            today = datetime.date.today().isoformat()
            sterile = mfg.variant_for(w) == mfg.STERILE
            try:
                with db.tx() as con:
                    doc = db.alloc(con, f'SPIN-{_year()}', f'SPIN-{_year()}-{{n6}}')
                    unit = lot['uom'] or 'قطعة'
                    inventory.move(con, grn, 'RM', bn, 'PROD_OUT', qty, unit, bn, mfg.FULL, 'sp_supply', doc)
                    con.execute("""INSERT INTO allocations(doc_no,batch_no,route_code,source_type,source_ref,qty,unit,alloc_date,operator,created_by)
                                   VALUES(?,?,?,?,?,?,?,?,?,?)""", (doc, bn, mfg.FULL, 'SP', grn, qty, unit, today, operator, g.user['username']))
                    con.execute("""INSERT INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)
                                   ON CONFLICT(child,parent,link_type) DO UPDATE SET qty=qty+excluded.qty""",
                                (bn, grn, 'SP_LOT', qty, unit, doc))
                    con.execute("""INSERT INTO folding_out(doc_no,fdate,batch_no,route,item_code,qty_good,unit,operator,notes)
                                   VALUES(?,?,?,?,?,?,?,?,?)""", (doc, today, bn, w.get('route'), w['item_code'], qty, mfg.SWAB, operator,
                                                                  f'SP وارد من مورّد — الـ LOT {lot["supplier_lot"]} ({grn})'))
                    if sterile:
                        inter = db.alloc(con, 'SPC', 'SPC-{n6}')
                        con.execute("""INSERT INTO intermediates(barcode,batch_no,item_code,sp_code,sp_source,fold_doc,qty,prod_date,operator)
                                       VALUES(?,?,?,?,?,?,?,?,?)""", (inter, bn, w['item_code'], lot['item_code'], 'SUPPLIER_RECEIPT', doc, qty, today, operator))
                        notify.push(con, 'sp_release_needed', f'كرتون SP {inter} (وارد من مورّد، {fmt_qty(qty)} مسحة) للتشغيلة {bn} بانتظار إفراج الجودة (إفراج SP)',
                                    w['item_code'], url_for('quality_sp_review', bn=bn), bn, mfg.FULL, ('qc_sign',))
                    con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            except inventory.InsufficientStock as e:
                flash(str(e), 'bad'); return redirect(back)
            db.log('create', 'allocations', doc, f'{bn}; SP وارد {grn}; {fmt_qty(qty)}; {"معقم" if sterile else "غير معقم"}')
            flash(f'أُدخل {fmt_qty(qty)} مسحة من SP الوارد ({grn}) إلى التشغيلة {bn}' + (' — بانتظار إفراج SP من الجودة' if sterile else ' — جاهزة للتعبئة'), 'ok')
            return redirect(url_for('order_view', bn=bn))
        docs = db.q("SELECT a.*, r.supplier_lot FROM allocations a LEFT JOIN receipts r ON r.grn_no=a.source_ref WHERE a.batch_no=? AND a.doc_no LIKE 'SPIN-%' AND a.voided=0 ORDER BY a.id", (bn,))
        return render_template('sp_supply.html', nav='fold', w=w, it=it, lots=lots, docs=docs, fmt=fmt_qty, st=mfg.order_state(w))

    @route('/sp-supply/doc/<doc_no>/delete', 'sp_supply_delete', methods=['POST'])
    def sp_supply_delete(doc_no):
        auth.need('enter')
        a = db.one("SELECT * FROM allocations WHERE doc_no=? AND doc_no LIKE 'SPIN-%' AND voided=0", (doc_no,))
        if not a:
            abort(404)
        bn = a['batch_no']
        w, it = _sp_order(bn)
        back = url_for('sp_supply', bn=bn)
        ok, why = prod.doc_gate(w)
        if not ok:
            flash(why, 'bad'); return redirect(back)
        ic = db.one('SELECT * FROM intermediates WHERE fold_doc=?', (doc_no,))
        if ic and ic['rec_no']:
            flash(f'الكرتون {ic["barcode"]} داخل السجل {ic["rec_no"]} — احذف السجل أولًا', 'bad'); return redirect(back)
        if not ic:
            unpacked, _ = _unpacked(bn)
            if a['qty'] - unpacked > 1e-6:
                flash(f'تم تعبئة جزء من هذه الكمية — عدّل/احذف سند التعبئة أولًا (غير المعبأ {fmt_qty(unpacked)})', 'bad'); return redirect(back)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            inventory.reverse(con, doc_no, f'عكس قيد — حذف SP وارد {doc_no}')
            con.execute('DELETE FROM intermediates WHERE fold_doc=?', (doc_no,))
            con.execute('DELETE FROM folding_out WHERE doc_no=?', (doc_no,))
            con.execute('UPDATE allocations SET voided=1 WHERE doc_no=?', (doc_no,))
            con.execute('UPDATE batch_links SET qty=qty-? WHERE child=? AND parent=? AND link_type=?', (a['qty'], bn, a['source_ref'], 'SP_LOT'))
            prod.withdraw_pending(con, cur)
        db.log('delete', 'allocations', doc_no, f'{bn}; {a["source_ref"]}; {fmt_qty(a["qty"])}')
        flash(f'حُذف {doc_no} وعادت الكمية إلى LOT SP', 'ok')
        return redirect(back)

    # =================================================================== المعقم: كراتين وسيطة وسجل المعالجة
    @route('/sterile/<path:bn>', 'ster_order', methods=['GET'])
    def ster_order(bn):
        w, it = _order(bn)
        if not mfg.is_v15(w) or mfg.variant_for(w) != mfg.STERILE:
            flash('هذه الصفحة لمنتجات الشاش المعقمة', 'bad')
            return redirect(url_for('order_view', bn=bn))
        inter = db.q('SELECT * FROM intermediates WHERE batch_no=? ORDER BY barcode', (bn,))
        recs = db.q('SELECT * FROM ster_records WHERE batch_no=? ORDER BY rec_no', (bn,))
        avail = [x for x in inter if x['status'] == 'Released for Sorting' and not x['rec_no']]
        return render_template('ster_order.html', nav='ster', w=w, it=it, inter=inter, recs=recs, avail=avail, st=mfg.order_state(w),
                               fmt=fmt_qty, inter_ar=INTER_AR, rec_ar=REC_AR, ps=mfg.pack_spec(it['item_code']))

    @route('/sterile/new', 'ster_new', methods=['POST'])
    def ster_new():
        """يفتح سجل الفرز والعد من كراتين وسيطة مُفرَجة (باركود ممسوح أو محدد) — لا إعادة إدخال مواصفات."""
        auth.need('enter')
        f = request.form
        codes = [c.strip().upper() for c in (f.getlist('barcode') + (f.get('scan') or '').replace(',', '\n').split()) if c.strip()]
        back = url_for('ster_home_all')
        if not codes:
            flash('امسح باركود كرتون وسيط أو اختر كراتين', 'bad')
            return redirect(back)
        rows = [db.one('SELECT * FROM intermediates WHERE barcode=?', (c,)) for c in codes]
        if any(r is None for r in rows):
            flash('باركود غير معروف: ' + '، '.join(c for c, r in zip(codes, rows) if r is None), 'bad')
            return redirect(back)
        bns = {r['batch_no'] for r in rows}
        if len(bns) != 1:
            flash('كل كراتين السجل الواحد من دفعة إنتاج واحدة (Production Lot)', 'bad')
            return redirect(back)
        bn = bns.pop()
        for r in rows:
            if r['rec_no']:
                flash(f'الكرتون {r["barcode"]} مستخدم في السجل {r["rec_no"]}', 'bad')
                return redirect(url_for('ster_order', bn=bn))
            if r['status'] != 'Released for Sorting':
                flash(f'الكرتون {r["barcode"]} حالته «{INTER_AR.get(r["status"], r["status"])}» — لا يدخل الفرز قبل إفراج SP من الجودة', 'bad')
                return redirect(url_for('ster_order', bn=bn))
        w, it = _order(bn)
        total = sum(r['qty'] for r in rows)
        try:
            with db.tx() as con:
                rec = db.alloc(con, f'STR-{_year()}', f'STR-{_year()}-{{n6}}')
                con.execute("""INSERT INTO ster_records(rec_no,batch_no,item_code,sp_code,status,received_qty,created_by)
                               VALUES(?,?,?,?,'Sorting',?,?)""", (rec, bn, w['item_code'], rows[0]['sp_code'], total, g.user['username']))
                for r in rows:
                    cur = con.execute("UPDATE intermediates SET rec_no=?, status='Consumed' WHERE barcode=? AND rec_no IS NULL "
                                      "AND status='Released for Sorting'", (rec, r['barcode']))
                    if cur.rowcount != 1:
                        raise ValueError(f'الكرتون {r["barcode"]} لم يعد متاحًا')
                    con.execute('INSERT INTO ster_inputs(rec_no,barcode,qty) VALUES(?,?,?)', (rec, r['barcode'], r['qty']))
                con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
        except ValueError as e:
            flash(str(e), 'bad')
            return redirect(url_for('ster_order', bn=bn))
        except Exception as e:                        # noqa: BLE001 — barcode UNIQUE في ster_inputs يمنع التكرار عند التسابق
            if 'UNIQUE' in str(e).upper():
                flash('أحد الكراتين استُخدم في سجل آخر للتو', 'bad')
                return redirect(url_for('ster_order', bn=bn))
            raise
        db.log('create', 'ster_records', rec, f'{bn}; {len(rows)} cartons; {fmt_qty(total)}')
        return redirect(url_for('ster_record', rec_no=rec))

    @route('/sterile', 'ster_home_all')
    def ster_home_all():
        rows = prod.visible_orders(200, "AND IFNULL(route_code,'FULL_GAUZE')='FULL_GAUZE' AND req_swabs IS NOT NULL "
                                        "AND route='معقم' AND final_status IS NULL")
        recs = db.q("SELECT * FROM ster_records WHERE status IN ('Sorting','Packaging','Boxing') ORDER BY rec_no")
        return render_template('ster_home.html', nav='ster', rows=rows, recs=recs, fmt=fmt_qty, rec_ar=REC_AR)

    def _rec_or_404(rec_no):
        r = db.one('SELECT * FROM ster_records WHERE rec_no=?', (rec_no,))
        if not r:
            abort(404)
        w, it = _order(r['batch_no'])
        return r, w, it

    def _rec_ledger(con, r, w):
        """قيد المخزون عند اكتمال البوكسات: مسحات الطي (WIP) ← بوكسات معقمة جاهزة للتعقيم."""
        inventory.reverse(con, r['rec_no'], f'إعادة احتساب السجل {r["rec_no"]}')
        if r['boxes_actual'] is not None:
            inventory.post(con, w['batch_no'], 'PROD_OUT', -float(r['received_qty'] or 0), mfg.SWAB, w['batch_no'], mfg.FULL, 'ster', r['rec_no'])
            inventory.post(con, w['batch_no'], 'STR_OUT', float(r['boxes_actual']), 'بوكس', w['batch_no'], mfg.FULL, 'ster', r['rec_no'])

    @route('/sterile/record/<rec_no>', 'ster_record', methods=['GET', 'POST'])
    def ster_record(rec_no):
        r, w, it = _rec_or_404(rec_no)
        bn = w['batch_no']
        g.crumb_args = {'bn': bn}
        back = url_for('ster_record', rec_no=rec_no)
        ps = mfg.pack_spec(it['item_code'])
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            act = f.get('act')
            ok, why = prod.doc_gate(w)
            if r['status'] not in EDITABLE_REC and not auth.can('master'):
                flash('السجل تجاوز مرحلة الإفراج — التعديل من مدير النظام فقط', 'bad')
                return redirect(back)
            if not ok or (w.get('final_status') and not auth.can('master')):
                flash(why or 'لا يمكن التعديل بعد قرار الجودة', 'bad')
                return redirect(back)
            try:
                operator = prod.get_operator(f)
                upd, status = {}, None
                if act == 'sort':
                    acc, rej = num(f.get('accepted')), num(f.get('rejected'), 0) or 0
                    if acc is None or acc < 0 or rej < 0:
                        raise ValueError('المقبول والمرفوض يجب أن يكونا أرقامًا غير سالبة')
                    if acc + rej - r['received_qty'] > 1e-6:
                        raise ValueError(f'المقبول + المرفوض ({fmt_qty(acc + rej)}) أكبر من المستلم ({fmt_qty(r["received_qty"])})')
                    upd = dict(sort_accepted=acc, sort_rejected=rej, sort_operator=operator, sort_at=now_s(),
                               ppe=None, env_expected=None, env_actual=None, env_rejected=None, pack_operator=None, pack_at=None,
                               env_per_box=None, boxes_expected=None, boxes_actual=None, box_diff=None, box_operator=None,
                               box_at=None, boxes_per_carton=None, cartons=None)
                    status = 'Packaging'
                elif act == 'pack':
                    if r['sort_accepted'] is None:
                        raise ValueError('سجّل الفرز والعد أولًا')
                    ppe = ps.get('swabs_per_envelope')
                    if not ppe:
                        raise ValueError('عدد المسحات في المغلف (5 أو 10) غير معرَّف — يُكمله مدير النظام من «تكوين التعبئة»')
                    exp = r['sort_accepted'] / ppe
                    act_env = num(f.get('env_actual'), math.floor(exp + 1e-9))
                    rejp = num(f.get('env_rejected'), 0) or 0
                    if act_env is None or act_env <= 0 or rejp < 0 or rejp > act_env:
                        raise ValueError('عدد المغلفات غير صحيح')
                    upd = dict(ppe=ppe, env_expected=exp, env_actual=act_env, env_rejected=rejp, pack_operator=operator, pack_at=now_s(),
                               env_per_box=None, boxes_expected=None, boxes_actual=None, box_diff=None, box_operator=None,
                               box_at=None, boxes_per_carton=None, cartons=None)
                    status = 'Boxing'
                elif act == 'box':
                    if r['env_actual'] is None:
                        raise ValueError('سجّل التغليف أولًا')
                    spb = ps.get('swabs_per_box')
                    if not spb or not r['ppe']:
                        raise ValueError('عدد المسحات في البوكس/المغلف غير معرَّف في Packaging Configuration')
                    epb = spb / r['ppe']
                    tot = r['env_actual'] - (r['env_rejected'] or 0)
                    exp = tot / epb
                    boxes = num(f.get('boxes_actual'), math.floor(exp + 1e-9))
                    if boxes is None or boxes <= 0:
                        raise ValueError('عدد البوكسات يجب أن يكون أكبر من صفر')
                    bpc = ps.get('boxes_per_carton')
                    upd = dict(env_per_box=epb, boxes_expected=exp, boxes_actual=boxes, box_diff=round(exp - boxes, 4),
                               box_operator=operator, box_at=now_s(), boxes_per_carton=bpc,
                               cartons=num(f.get('cartons_actual'), (math.floor(boxes / bpc + 1e-9) if bpc else None)))
                    status = 'Ready for Pre-Sterilization QC'
                else:
                    raise ValueError('إجراء غير معروف')
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            reset = r['status'] in ('Released for Sterilization', 'Pre-Sterilization Hold', 'Rejected Pre-Sterilization') or \
                bool(r['pre_release_at'])
            if reset and not f.get('confirm'):
                flash('تحذير: هذا التعديل يُلغي قرار الإفراج قبل التعقيم ويحتاج إفراجًا جديدًا — أعد الحفظ مع تأكيد', 'warn')
                return render_template('ster_record.html', **_rec_ctx(r, w, it), need_confirm=True, post=f, act=act)
            if r['cycle_no']:
                flash(f'السجل داخل دورة التعقيم {r["cycle_no"]} — احذفه من الدورة أولًا', 'bad')
                return redirect(back)
            with db.tx() as con:
                cols = ', '.join(f'{k}=?' for k in upd)
                con.execute(f'UPDATE ster_records SET {cols}, status=?, pre_release_by=NULL, pre_release_at=? WHERE rec_no=?',
                            (*upd.values(), status, None, rec_no))
                r2 = dict(con.execute('SELECT * FROM ster_records WHERE rec_no=?', (rec_no,)).fetchone())
                _rec_ledger(con, r2, w)
                cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
                prod.withdraw_pending(con, cur)
            db.log('edit' if reset else 'update', 'ster_records', rec_no, f'{act}; {status}')
            ster_sync(bn)
            if status == 'Ready for Pre-Sterilization QC':
                with db.tx() as con2:
                    notify.push(con2, 'pre_release_needed', f'السجل {rec_no} ({fmt_qty(r2["boxes_actual"])} بوكس) للتشغيلة {bn} بانتظار الإفراج قبل التعقيم',
                                w['item_code'], url_for('quality_pre_review', rec_no=rec_no), bn, mfg.FULL, ('qc_sign',))
                flash(f'اكتمل السجل {rec_no}: {fmt_qty(r2["boxes_actual"])} بوكس — بانتظار الإفراج قبل التعقيم', 'ok')
                prod.next_bar(('طباعة بطاقات البوكس', url_for('print_label', kind='box', key=rec_no)),
                              ('طباعة السجل', url_for('print_doc', kind='ster_record', key=rec_no), False),
                              ('أمر الإنتاج', url_for('order_view', bn=bn), False))
            else:
                flash('تم الحفظ', 'ok')
            return redirect(back)
        return render_template('ster_record.html', **_rec_ctx(r, w, it), need_confirm=False, post={}, act=None)

    def _rec_ctx(r, w, it):
        ins = db.q('SELECT i.*, m.status mstatus, m.fold_doc, m.tag_no FROM ster_inputs i JOIN intermediates m ON m.barcode=i.barcode '
                   'WHERE i.rec_no=?', (r['rec_no'],))
        return dict(nav='ster', r=r, w=w, it=it, ins=ins, ps=mfg.pack_spec(it['item_code']), fmt=fmt_qty, rec_ar=REC_AR,
                    editable=(r['status'] in EDITABLE_REC or auth.can('master')) and prod.doc_gate(w)[0] and not r.get('cycle_no'),
                    equiv=mfg.equiv_from_swabs(it, r.get('sort_accepted') or 0))

    @route('/sterile/record/<rec_no>/delete', 'ster_record_delete', methods=['POST'])
    def ster_record_delete(rec_no):
        auth.need('enter')
        r, w, it = _rec_or_404(rec_no)
        bn = w['batch_no']
        back = url_for('ster_order', bn=bn)
        ok, why = prod.doc_gate(w)
        if not ok:
            flash(why, 'bad')
            return redirect(back)
        if r['cycle_no']:
            flash(f'السجل داخل دورة التعقيم {r["cycle_no"]} — احذف الدورة/السطر أولًا', 'bad')
            return redirect(back)
        if r['status'] not in EDITABLE_REC and not auth.can('master'):
            flash('السجل تجاوز مرحلة الإفراج — الحذف من مدير النظام فقط', 'bad')
            return redirect(back)
        with db.tx() as con:
            cur = dict(con.execute('SELECT * FROM work_orders WHERE batch_no=?', (bn,)).fetchone())
            inventory.reverse(con, rec_no, f'عكس قيد — حذف السجل {rec_no}')
            con.execute("UPDATE intermediates SET rec_no=NULL, status='Released for Sorting' WHERE rec_no=?", (rec_no,))
            con.execute('DELETE FROM ster_inputs WHERE rec_no=?', (rec_no,))
            con.execute('DELETE FROM ster_records WHERE rec_no=?', (rec_no,))
            prod.withdraw_pending(con, cur)
        db.log('delete', 'ster_records', rec_no, json.dumps(dict(r), ensure_ascii=False, default=str))
        ster_sync(bn)
        flash(f'حُذف السجل {rec_no} وعادت كراتينه الوسيطة «مُفرج للفرز والعد»', 'ok')
        return redirect(back)

    # =================================================================== دورات التعقيم والتهوية
    def _cycle_no(con):
        d, stem = batch_stem(datetime.date.today().isoformat(), 'EO')
        scope = stem.rstrip('-')                     # نفس عدّاد ونظام الترقيم القائم: SEP-2629-EO-001
        return db.alloc(con, scope, scope + '-{n3}')

    @route('/sterilization', 'ster_cycles')
    def ster_cycles():
        aer = request.args.get('v') == 'aeration'
        sts = ("'Sterilization Completed','Transferred to Aeration','Aeration Completed'" if aer else
               "'In Progress','Sterilization Completed','Transferred to Aeration','Aeration Completed'")
        rows = db.q(f"SELECT * FROM cycles WHERE status IN ({sts}) ORDER BY cycle_no DESC LIMIT 200")
        for c in rows:
            c['lines'] = db.q('SELECT * FROM cycle_loads WHERE cycle_no=?', (c['cycle_no'],))
        ready = db.q("SELECT * FROM ster_records WHERE status='Released for Sterilization' ORDER BY rec_no")
        return render_template('ster_cycles.html', nav='ster_cycles', aer=aer, rows=rows, ready=ready, cyc_ar=CYC_AR, fmt=fmt_qty)

    @route('/sterilization/new', 'ster_cycle_new', methods=['GET', 'POST'])
    def ster_cycle_new():
        auth.need('enter')
        ready = db.q("SELECT r.*, w.wo_no FROM ster_records r JOIN work_orders w ON w.batch_no=r.batch_no "
                     "WHERE r.status='Released for Sterilization' ORDER BY r.rec_no")
        if request.method == 'POST':
            f = request.form
            recs = f.getlist('rec_no')
            try:
                op = prod.get_operator(f, 'sterilization')
                if not recs:
                    raise ValueError('اختر سجلًا واحدًا على الأقل (LOT جاهز للتعقيم)')
                chamber = s(f.get('machine')) or 'EO-01'
                start = s(f.get('start_at')) or now_s()
                lines = []
                for rn in recs:
                    r = db.one("SELECT * FROM ster_records WHERE rec_no=? AND status='Released for Sterilization'", (rn,))
                    if not r:
                        raise ValueError(f'السجل {rn} غير مُفرج للتعقيم')
                    lines.append(r)
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(url_for('ster_cycle_new'))
            with db.tx() as con:
                cyc = _cycle_no(con)
                d = start[:10]
                con.execute("""INSERT INTO cycles(cycle_no,cycle_date,machine_code,operator,start_time,status,start_at)
                               VALUES(?,?,?,?,?,?,?)""", (cyc, d, chamber, op, start[11:16], 'In Progress', start))
                for r in lines:
                    it = con.execute('SELECT description FROM items WHERE item_code=?', (r['item_code'],)).fetchone()
                    con.execute("""INSERT INTO cycle_loads(cycle_no,batch_no,item_code,boxes_in,rec_no,pack_status_at_load)
                                   VALUES(?,?,?,?,?,?)""", (cyc, r['batch_no'], r['item_code'], r['boxes_actual'], r['rec_no'],
                                                            'Released for Sterilization'))
                    cur = con.execute("UPDATE ster_records SET status='In Sterilization', cycle_no=? WHERE rec_no=? "
                                      "AND status='Released for Sterilization'", (cyc, r['rec_no']))
                    if cur.rowcount != 1:
                        raise ValueError(f'السجل {r["rec_no"]} لم يعد جاهزًا')
            for r in lines:
                ster_sync(r['batch_no'])
            db.log('create', 'cycles', cyc, f'{len(lines)} lines; {op}')
            flash(f'أُنشئت دورة التعقيم {cyc} ({len(lines)} LOT) — طباعة الأغلفة والبوكسات تستخدم رقم الدورة تلقائيًا', 'ok')
            prod.next_bar(('طباعة بيانات الدورة', url_for('print_doc', kind='cycle', key=cyc)),
                          ('متابعة الدورة', url_for('ster_cycle', cycle_no=cyc), False))
            return redirect(url_for('ster_cycle', cycle_no=cyc))
        return render_template('ster_cycle_new.html', nav='ster_cycles', ready=ready, fmt=fmt_qty, now=now_s().replace(' ', 'T'),
                               chambers=db.q("SELECT machine_code, name FROM machines WHERE stage='التعقيم'"))

    @route('/sterilization/<path:cycle_no>', 'ster_cycle', methods=['GET', 'POST'])
    def ster_cycle(cycle_no):
        c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
        if not c:
            abort(404)
        lines = db.q('SELECT l.*, r.status rstatus, r.boxes_actual FROM cycle_loads l LEFT JOIN ster_records r ON r.rec_no=l.rec_no '
                     'WHERE l.cycle_no=?', (cycle_no,))
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            act = f.get('act')
            back = url_for('ster_cycle', cycle_no=cycle_no)
            recs = [l['rec_no'] for l in lines if l['rec_no']]
            try:
                if act == 'end':
                    if c['status'] != 'In Progress':
                        raise ValueError('الدورة ليست قيد التشغيل')
                    end = s(f.get('end_at')) or now_s()
                    if end < (c['start_at'] or ''):
                        raise ValueError('وقت النهاية قبل البداية')
                    new, rstat = 'Sterilization Completed', 'Sterilization Completed'
                    upd = dict(end_at=end, end_time=end[11:16], gas_lot=s(f.get('gas_lot')), machine_report_no=s(f.get('report_no')),
                               notes=s(f.get('notes')))
                elif act == 'aeration':
                    if c['status'] != 'Sterilization Completed':
                        raise ValueError('انقل إلى التهوية بعد اكتمال التعقيم فقط')
                    new, rstat = 'Transferred to Aeration', 'Transferred to Aeration'
                    upd = dict(aer_start_at=s(f.get('aer_start_at')) or now_s())
                elif act == 'aeration_done':
                    if c['status'] != 'Transferred to Aeration':
                        raise ValueError('الدورة ليست في التهوية')
                    op = prod.get_operator(f, 'sterilization')
                    end = s(f.get('aer_end_at')) or now_s()
                    hours = (datetime.datetime.fromisoformat(end.replace('T', ' ')) - datetime.datetime.fromisoformat(
                        (c['aer_start_at'] or end).replace('T', ' '))).total_seconds() / 3600
                    need_h = mfg._setting_f('aeration_min_h', 24)
                    if hours + 1e-9 < need_h and not f.get('confirm'):
                        flash(f'مدة التهوية {hours:.1f} ساعة أقل من الحد الأدنى {need_h:g} ساعة — أكّد الإنهاء إن كان مقصودًا', 'warn')
                        return redirect(back)
                    new, rstat = 'Aeration Completed', 'Pending Final QC Release'
                    upd = dict(aer_end_at=end, aer_operator=op)
                else:
                    raise ValueError('إجراء غير معروف')
            except ValueError as e:
                flash(str(e), 'bad')
                return redirect(back)
            with db.tx() as con:
                cols = ', '.join(f'{k}=?' for k in upd)
                con.execute(f'UPDATE cycles SET {cols}, status=? WHERE cycle_no=?', (*upd.values(), new, cycle_no))
                for rn in recs:
                    con.execute('UPDATE ster_records SET status=? WHERE rec_no=?', (rstat, rn))
            for b_ in {l['batch_no'] for l in lines}:
                ster_sync(b_)
            db.log('update', 'cycles', cycle_no, f'{act} → {new}')
            flash({'end': 'اكتمل التعقيم', 'aeration': 'نُقلت إلى التهوية', 'aeration_done': 'اكتملت التهوية — بانتظار الإفراج النهائي من الجودة'}[act], 'ok')
            return redirect(back)
        return render_template('ster_cycle.html', nav='ster_cycles', c=c, lines=lines, cyc_ar=CYC_AR, fmt=fmt_qty, rec_ar=REC_AR,
                               now=now_s().replace(' ', 'T'), min_h=mfg._setting_f('aeration_min_h', 24))

    @route('/sterilization/<path:cycle_no>/delete', 'ster_cycle_delete', methods=['POST'])
    def ster_cycle_delete(cycle_no):
        """حذف دورة (خطأ إدخال): تعود الـ LOTs «جاهز للتعقيم». مدير النظام فقط بعد اكتمال التعقيم."""
        auth.need('enter')
        c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
        if not c:
            abort(404)
        if c['status'] != 'In Progress' and not auth.can('master'):
            flash('حذف دورة اكتمل تعقيمها من صلاحية مدير النظام', 'bad')
            return redirect(url_for('ster_cycle', cycle_no=cycle_no))
        lines = db.q('SELECT * FROM cycle_loads WHERE cycle_no=?', (cycle_no,))
        with db.tx() as con:
            for l in lines:
                con.execute("UPDATE ster_records SET status='Released for Sterilization', cycle_no=NULL WHERE rec_no=?", (l['rec_no'],))
            con.execute('DELETE FROM cycle_loads WHERE cycle_no=?', (cycle_no,))
            con.execute('DELETE FROM cycles WHERE cycle_no=?', (cycle_no,))
            for b_ in {l['batch_no'] for l in lines}:
                w = con.execute('SELECT * FROM work_orders WHERE batch_no=?', (b_,)).fetchone()
                if w and w['final_status'] == mfg.FS_PENDING:
                    con.execute("UPDATE work_orders SET final_status=NULL, finished_at=NULL, finished_by=NULL, produced_qty=NULL WHERE batch_no=?", (b_,))
        db.log('delete', 'cycles', cycle_no, json.dumps(dict(c), ensure_ascii=False, default=str))
        flash(f'حُذفت الدورة {cycle_no} وعادت LOTsها جاهزة للتعقيم', 'ok')
        return redirect(url_for('ster_cycles'))
