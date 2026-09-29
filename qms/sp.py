# -*- coding: utf-8 -*-
"""مسار الشاش نصف المصنع SP (Semi-Finished Gauze Packing).

  استلام خام SP (حجر) ← فحص الجودة والإفراج ← تخصيص للإنتاج ←
      غير معقم: تعبئة (قطعة ← باك ← بوكس ← كرتون) ← فحص نهائي ← مخزن
      معقم:     فرز ← تعبئة أولية ← تعقيم ← فحص/إفراج ← مخزن   (شاشات الفرز والتغليف والتعقيم المشتركة)

الخام SP لا يدخل الأسليتر ولا الطي. كل حركة كمية قيد في دفتر المخزون (inventory).
"""
import json, datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, notify, ncr
from util import s, num, doc_scope, fmt_qty
from inventory import InsufficientStock

CLS = 'SP'


def sp_prefixes():
    return mfg.prefixes_of_class(CLS)


def lot_balance(grn):
    return inventory.balance(grn, 'RM')


def compatible_lots(item, only_available=True):
    """لوطات SP المفرج عنها والمطابقة لمواصفات الصنف النهائي (المقاس، الطبقات، الكاشف، الميش)."""
    ph = ','.join('?' * len(sp_prefixes())) or "''"
    rows = db.q(f"""SELECT r.grn_no, r.supplier_lot, r.item_code, r.qty, r.uom, r.receipt_date, r.expiry_date,
                          r.wh_location, r.qc_location, i.size, i.ply, i.xray, i.mesh
                   FROM receipts r JOIN items i ON i.item_code=r.item_code
                   WHERE i.prefix IN ({ph}) AND r.stock_status='مفرج' ORDER BY r.receipt_date, r.grn_no""",
                tuple(sp_prefixes()))
    out, today = [], datetime.date.today().isoformat()
    for r in rows:
        if (r['size'], r['ply'], r['xray'], r['mesh']) != (item.get('size'), item.get('ply'),
                                                          item.get('xray'), item.get('mesh')):
            continue
        r['available'] = lot_balance(r['grn_no'])
        r['expired'] = bool(r['expiry_date'] and r['expiry_date'] < today)
        if only_available and (r['available'] <= 1e-9 or r['expired']):
            continue
        out.append(r)
    return out


def allocated(bn):
    return float(db.one('SELECT IFNULL(SUM(qty),0) n FROM allocations WHERE batch_no=? AND voided=0', (bn,))['n'])


def lot_ledger(grn):
    """رصيد لوط SP: مستلم / مصروف / سليم / مرفوض / هالك / متبقٍ.

    الناتج الفعلي يُقاس على مستوى أمر الإنتاج، فإن غذّته عدة لوطات يُوزَّع بنسبة ما صُرف من كل لوط.
    """
    rec = db.one('SELECT * FROM receipts WHERE grn_no=?', (grn,)) or {}
    issued = good = rej = scrap = 0.0
    for a in db.q("""SELECT batch_no, SUM(qty) q FROM allocations WHERE source_ref=? AND voided=0
                     GROUP BY batch_no""", (grn,)):
        tot = allocated(a['batch_no'])
        share = a['q'] / tot if tot else 0
        o = mfg.output(a['batch_no'])
        issued += a['q']
        good += o['good'] * share
        rej += o['reject'] * share
        scrap += o['scrap'] * share
    return dict(grn=grn, received=float(rec.get('qty') or 0), issued=issued, good=round(good, 2),
                reject=round(rej, 2), scrap=round(scrap, 2), remaining=lot_balance(grn), unit=rec.get('uom'))


# ------------------------------------------------------------------ المسارات
def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    def _sp_batch(bn):
        w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND route_code=? AND COALESCE(order_type,'إنتاج')='إنتاج'",
                   (bn, mfg.SP)) if bn else None
        return w

    @route('/sp/receive', 'sp_receive', methods=['GET', 'POST'])
    def sp_receive():
        """استلام خام شاش نصف مصنع SP — يدخل الحجر بحالة Pending QC."""
        auth.need('receive')
        auth.need_line(mfg.SP)
        if request.method == 'POST':
            f = request.form
            rdate = s(f.get('receipt_date')) or datetime.date.today().isoformat()
            item = db.one("SELECT * FROM items WHERE item_code=? AND status='نشط'", ((s(f.get('item_code')) or '').upper(),))
            supplier, lot = s(f.get('supplier_name')), s(f.get('supplier_lot'))
            qty, pk = num(f.get('qty')), num(f.get('packages'))
            back = url_for('sp_receive')
            if not item or item['prefix'] not in sp_prefixes():
                flash('اختر صنف خام SP من القائمة (فئة الشاش نصف المصنع)', 'bad'); return redirect(back)
            if not (supplier and lot):
                flash('المورد ولوط المورد بيانات إلزامية', 'bad'); return redirect(back)
            if not qty or qty <= 0 or not pk or pk <= 0:
                flash('الكمية وعدد العبوات/الأكياس يجب أن يكونا أكبر من صفر', 'bad'); return redirect(back)
            if s(f.get('expiry_date')) and s(f.get('mfg_date')) and f['expiry_date'] < f['mfg_date']:
                flash('تاريخ الانتهاء قبل تاريخ التصنيع', 'bad'); return redirect(back)
            manual = (s(f.get('grn_no')) or '').upper() or None
            if manual and db.one('SELECT 1 FROM receipts WHERE grn_no=?', (manual,)):
                flash(f'رقم الاستلام {manual} مستخدم مسبقًا', 'bad'); return redirect(back)
            scope, fmt = doc_scope('SPR', rdate)
            uom = s(f.get('uom')) or item.get('uom') or 'قطعة'
            with db.tx() as con:
                sup = con.execute('SELECT * FROM suppliers WHERE name=?', (supplier,)).fetchone()
                sid = sup['supplier_id'] if sup else con.execute(
                    'INSERT INTO suppliers(name,country,active) VALUES(?,?,1)', (supplier, s(f.get('country')))).lastrowid
                grn = db.use_number(con, manual, scope, fmt)
                con.execute("""INSERT INTO receipts(grn_no,receipt_date,kind,supplier_id,country,po_no,invoice_no,item_code,
                            supplier_lot,mfg_date,expiry_date,qty,uom,coa,coa_no,pack_cond,qc_location,stock_status,
                            received_by,notes,packages,wh_location,created_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (grn, rdate, 'شاش نصف مصنع SP', sid, s(f.get('country')), s(f.get('po_no')),
                             s(f.get('invoice_no')), item['item_code'], lot, s(f.get('mfg_date')), s(f.get('expiry_date')),
                             qty, uom, 'متوفر' if f.get('coa') == 'yes' else 'غير متوفر', s(f.get('coa_no')),
                             s(f.get('pack_cond')), 'منطقة الحجر', 'حجر', s(f.get('received_by')) or g.user['full_name'], s(f.get('notes')),
                             int(pk), s(f.get('wh_location')) or 'منطقة الحجر', g.user['username']))
                inventory.post(con, grn, 'RM', qty, uom, None, mfg.SP, 'receipt', grn, s(f.get('wh_location')) or 'منطقة الحجر')
                notify.push(con, 'qc_pending',
                            f'تم استلام خام شاش نصف مصنع SP رقم {grn} (اللوط {lot}) بكمية {qty:g} {uom} وهو بانتظار الفحص والإفراج',
                            item['item_code'], url_for('receipt_view', grn_no=grn), grn, mfg.SP, ('qc_record',))
            db.log('create', 'receipts', grn, f'SP; {item["item_code"]}; lot {lot}; {qty:g}')
            flash(f'سُجّل استلام خام SP {grn} — حالته: بانتظار فحص الجودة (Pending QC)', 'ok')
            return redirect(url_for('receipt_view', grn_no=grn))
        today = datetime.date.today().isoformat()
        items = db.q("""SELECT item_code, description, size, ply, xray, mesh, sterile, uom FROM items
                        WHERE prefix IN (%s) AND status='نشط' ORDER BY item_code""" % ','.join('?' * len(sp_prefixes())),
                     tuple(sp_prefixes()))
        return render_template('sp_receive.html', nav='sp', items=items, today_date=today,
                               grn_no=db.peek_number(*doc_scope('SPR', today)),
                               suppliers=db.q('SELECT name,country FROM suppliers WHERE active=1 ORDER BY name'))

    @route('/sp/lots', 'sp_lots')
    def sp_lots():
        """دفتر لوطات SP: مستلم / مصروف / سليم / مرفوض / هالك / متبقٍ."""
        ph = ','.join('?' * len(sp_prefixes())) or "''"
        rows = db.q(f"""SELECT r.*, i.description FROM receipts r JOIN items i ON i.item_code=r.item_code
                        WHERE i.prefix IN ({ph}) ORDER BY r.receipt_date DESC, r.grn_no DESC LIMIT 200""",
                    tuple(sp_prefixes()))
        for r in rows:
            r['led'] = lot_ledger(r['grn_no'])
        return render_template('sp_lots.html', nav='sp', rows=rows)

    @route('/sp/<path:bn>/alloc', 'sp_alloc', methods=['GET', 'POST'])
    def sp_alloc(bn):
        """تخصيص خام SP مفرج عنه لأمر إنتاج — لا يُقبل غير المفرج ولا أكبر من المتاح. الكمية المقترحة جاهزة."""
        w = _sp_batch(bn)
        if not w:
            flash('اختر أمر إنتاج على مسار SP', 'bad')
            return redirect(url_for('sp_home'))
        auth.need_line(mfg.SP)
        item = db.one('SELECT * FROM items WHERE item_code=?', (w['item_code'],)) or {}
        back = url_for('sp_alloc', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            grn = s(f.get('grn_no'))
            lot = next((x for x in compatible_lots(item, only_available=False) if x['grn_no'] == grn), None)
            rec = db.one('SELECT stock_status FROM receipts WHERE grn_no=?', (grn,)) if grn else None
            need = max(float(w.get('qty_required') or 0) - allocated(bn), 0)
            qty = num(f.get('qty'), min(lot['available'], need) if lot else None)
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر', 'bad'); return redirect(back)
            if not rec or rec['stock_status'] != 'مفرج':
                flash('لا يجوز استخدام خام SP غير مفرج عنه من الجودة', 'bad'); return redirect(back)
            if not lot:
                flash('لوط SP لا يطابق مواصفات الصنف (المقاس / الطبقات / الكاشف / الميش)', 'bad'); return redirect(back)
            if lot['expired']:
                flash('لوط SP منتهي الصلاحية', 'bad'); return redirect(back)
            if not qty or qty <= 0:
                flash('الكمية يجب أن تكون أكبر من صفر', 'bad'); return redirect(back)
            if qty - lot['available'] > 1e-6:
                flash(f'الكمية {fmt_qty(qty)} أكبر من المتاح في اللوط {fmt_qty(lot["available"])}', 'bad'); return redirect(back)
            if qty - need > 1e-6:
                flash(f'الكمية {fmt_qty(qty)} تتجاوز المتبقي من احتياج الأمر {fmt_qty(need)}', 'bad'); return redirect(back)
            today = datetime.date.today().isoformat()
            ascope, afmt = doc_scope('ALC', today)
            try:
                with db.tx() as con:
                    doc = db.alloc(con, ascope, afmt)
                    inventory.move(con, grn, 'RM', bn, 'ISSUED', qty, lot['uom'] or 'قطعة', bn, mfg.SP, 'alloc', doc)
                    con.execute("""INSERT INTO allocations(doc_no,batch_no,route_code,source_type,source_ref,qty,unit,
                                alloc_date,operator,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (doc, bn, mfg.SP, 'SP', grn, qty, lot['uom'] or 'قطعة', today,
                                 g.user['full_name'], g.user['username']))
                    con.execute("""INSERT INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)
                                ON CONFLICT(child,parent,link_type) DO UPDATE SET qty=qty+excluded.qty""",
                                (bn, grn, 'SP_LOT', qty, lot['uom'], doc))
                    con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            except InsufficientStock as e:
                flash(str(e), 'bad'); return redirect(back)
            db.log('create', 'allocations', doc, f'{bn}; {grn}; {qty:g}')
            flash(f'تم تخصيص {fmt_qty(qty)} من اللوط {grn} للتشغيلة {bn}', 'ok')
            import prod
            prod.next_bar(('تسجيل التعبئة', url_for('sp_pack', bn=bn)), ('أمر الإنتاج', url_for('order_view', bn=bn), False))
            return redirect(back)
        return render_template('sp_alloc.html', nav='sp', bn=bn, b=w, lots=compatible_lots(item, only_available=False),
                               allocs=db.q('SELECT * FROM allocations WHERE batch_no=? AND voided=0 ORDER BY id', (bn,)),
                               issued_bal=inventory.balance(bn, 'ISSUED'), allocated=allocated(bn),
                               progress=mfg.progress(bn), fmt=fmt_qty,
                               need=max(float(w.get('qty_required') or 0) - allocated(bn), 0))

    @route('/sp', 'sp_home')
    def sp_home():
        """أوامر إنتاج مسار SP المفتوحة مع الإجراء التالي."""
        import prod
        rows = prod.visible_orders(200, "AND route_code='SP_GAUZE' AND final_status IS NULL AND status IN ('صادر','قيد التنفيذ')")
        return render_template('line_orders.html', nav='sp', title='خط الشاش نصف المصنع SP', rows=rows, fmt=fmt_qty, gen='sp',
                               sub='تخصيص خام SP مفرج عنه ← التعبئة — ثم إنهاء الإنتاج وإرساله للجودة', extra='')

    @route('/sp/<path:bn>/pack', 'sp_pack', methods=['GET', 'POST'])
    def sp_pack(bn):
        """تعبئة SP (معقم وغير معقم): يُدخل المستخدم السليم/المرفوض/الهالك فقط؛ الباك والبوكس والكرتون تُحسب تلقائيًا."""
        w = _sp_batch(bn)
        if not w:
            flash('اختر أمر إنتاج على مسار SP', 'bad')
            return redirect(url_for('sp_home'))
        auth.need_line(mfg.SP)
        pm = mfg.pack_map(w['item_code'])
        avail = inventory.balance(bn, 'ISSUED')
        back = url_for('sp_pack', bn=bn)
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            if w.get('final_status'):
                flash('أُنهي إنتاج هذا الأمر', 'bad'); return redirect(back)
            good = num(f.get('good'), 0) or 0
            rej, scrap = num(f.get('reject'), 0) or 0, num(f.get('scrap'), 0) or 0
            if good < 0 or rej < 0 or scrap < 0 or good + rej + scrap <= 0:
                flash('الكميات يجب ألا تكون سالبة ولا كلها صفرًا', 'bad'); return redirect(back)
            if good + rej + scrap - avail > 1e-6:
                flash(f'المجموع {fmt_qty(good + rej + scrap)} أكبر من المصروف المتاح {fmt_qty(avail)} — خصّص خام SP أولًا', 'bad')
                return redirect(back)
            lots = ', '.join(r['source_ref'] for r in db.q(
                'SELECT DISTINCT source_ref FROM allocations WHERE batch_no=? AND voided=0', (bn,)))
            now = datetime.datetime.now()
            equiv = {e['unit']: e['qty'] for e in mfg.pack_equiv(w['item_code'], good, 'قطعة')}
            try:
                with db.tx() as con:
                    proc = mfg.alloc_proc_no(con, bn, 'SPK')
                    con.execute("""INSERT INTO proc_batches(proc_no,batch_no,route_code,stage_code,source_ref,item_code,operator,
                                work_date,end_time,qty_in,qty_out,qty_reject,qty_scrap,scrap_unit,unit,extra_json,notes,created_by)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (proc, bn, mfg.SP, 'SPK', lots, w['item_code'], g.user['full_name'], now.date().isoformat(),
                                 now.strftime('%H:%M'), good + rej + scrap, good, rej, scrap, 'قطعة', 'قطعة',
                                 json.dumps(dict(pieces=good, equiv=equiv), ensure_ascii=False), s(f.get('notes')),
                                 g.user['username']))
                    u = 'قطعة'
                    if good:
                        inventory.move(con, bn, 'ISSUED', proc, 'SPK_OUT', good, u, bn, mfg.SP, 'proc', proc)
                    if rej:
                        inventory.move(con, bn, 'ISSUED', proc, 'REJECT', rej, u, bn, mfg.SP, 'reject', proc)
                    if scrap:
                        inventory.move(con, bn, 'ISSUED', proc, 'SCRAP', scrap, u, bn, mfg.SP, 'scrap', proc)
                    con.execute("INSERT OR IGNORE INTO batch_links(child,parent,link_type,qty,unit,ref_doc) VALUES(?,?,?,?,?,?)",
                                (proc, bn, 'PROC', good, u, proc))
                    con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            except InsufficientStock as e:
                flash(str(e), 'bad'); return redirect(back)
            db.log('create', 'proc_batches', proc, f'good={good:g}; rej={rej:g}; scrap={scrap:g}')
            flash(f'سُجّلت التعبئة {proc}: {fmt_qty(good)} قطعة سليمة', 'ok')
            import prod
            st = mfg.order_state(bn)
            nx = st['next']
            btns = []
            if nx and nx['kind'] in ('work', 'finish') and nx.get('endpoint'):
                btns.append((nx['label'], url_for(nx['endpoint'], bn=bn) + ('#finish' if nx['kind'] == 'finish' else '')))
            btns.append(('أمر الإنتاج', url_for('order_view', bn=bn), not btns))
            prod.next_bar(*btns)
            return redirect(back)
        rows = db.q("SELECT * FROM proc_batches WHERE batch_no=? AND stage_code='SPK' ORDER BY proc_no", (bn,))
        for r in rows:
            r['x'] = json.loads(r['extra_json'] or '{}')
        return render_template('sp_pack.html', nav='sp', bn=bn, b=w, pm=pm, rows=rows, issued_bal=avail, fmt=fmt_qty,
                               progress=mfg.progress(bn), st=mfg.order_state(bn),
                               equiv_unit_ar={'pack': 'باكت', 'box': 'بوكس', 'carton': 'كرتون'})
