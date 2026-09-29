# -*- coding: utf-8 -*-
"""الشحن وصرف مواد التعبئة.

  • الشحن (WH-FRM-007): فقط من تشغيلة مفرج عنها، وبكمية لا تتجاوز المفرج مطروحًا منه
    المشحون سابقًا، ولا مع عدم مطابقة مفتوحة. الإبطال بسبب وتوقيع إلكتروني.
  • صرف المواد: سند صرف مواد التعبئة (فيلم، بوكس، كرتون) لكل تشغيلة بLOT المادة وأمين المخزن.
"""
import datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, ncr, mfg
from util import s, num, doc_scope

MAT_KINDS = ('فيلم / مغلف', 'بوكس', 'كرتون خارجي', 'أخرى')


def released_qty(bn):
    """(كمية مستلمة بالمخزن, وحدة, مرجع الإفراج): لا يُشحن إلا ما أُفرج عنه واستُلم في مخزن المنتج التام."""
    rel, unit, ref = mfg.released_qty(bn)
    if not rel:
        return 0.0, None, None
    return mfg.fg_received(bn), unit, ref


def shipped_qty(bn):
    return float(db.one("SELECT IFNULL(SUM(qty),0) n FROM shipments WHERE batch_no=? AND IFNULL(voided,0)=0", (bn,))['n'])


def available(bn):
    rel, unit, ref = released_qty(bn)
    return max(rel - shipped_qty(bn), 0), rel, unit, ref


def shippable_batches():
    out = []
    for w in db.q("SELECT batch_no,item_code,size,ply,route FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' ORDER BY rowid DESC LIMIT 200"):
        av, rel, unit, ref = available(w['batch_no'])
        if rel > 0:
            out.append(dict(w, available=av, released=rel, unit=unit, ref=ref))
    return out


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/shipping', 'shipping', methods=['GET', 'POST'])
    def shipping():
        if request.method == 'POST':
            auth.need('warehouse')
            f = request.form
            bn, cust = s(f.get('batch_no')), s(f.get('customer'))
            sdate = s(f.get('sdate')) or datetime.date.today().isoformat()
            shipper = s(f.get('shipper')) or g.user['full_name']
            back = url_for('shipping', b=bn or '')
            if not (bn and cust):
                flash('التشغيلة والعميل بيانات إلزامية', 'bad')
                return redirect(back)
            av, rel, unit, ref = available(bn)
            if rel <= 0:
                flash('التشغيلة غير مفرج عنها أو لم تُستلم في مخزن المنتج التام — لا يجوز شحنها', 'bad')
                return redirect(back)
            opened = ncr.open_for_batch(bn)
            if opened:
                flash('لا يجوز الشحن مع عدم مطابقة مفتوحة: ' + '، '.join(x['dev_no'] for x in opened), 'bad')
                return redirect(back)
            qty = num(f.get('qty'))
            if not qty or qty <= 0 or qty - av > 1e-6:
                flash(f'الكمية غير صحيحة — المتاح للشحن {av:g} {unit}', 'bad')
                return redirect(back)
            w = db.one('SELECT item_code FROM work_orders WHERE batch_no=?', (bn,))
            scope, fmt = doc_scope('SHP', sdate)
            with db.tx() as con:
                doc = db.use_number(con, s(f.get('doc_no')), scope, fmt)
                con.execute("""INSERT INTO shipments(doc_no,sdate,batch_no,item_code,customer,qty,uom,release_no,notes,
                               unit,shipper,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (doc, sdate, bn, w['item_code'], cust, qty, unit, ref, s(f.get('notes')), unit,
                             shipper, g.user['username']))
            db.log('create', 'shipments', doc, f'{bn}; {cust}; {qty:g}')
            flash(f'سُجّل الشحن {doc} — {qty:g} {unit} إلى {cust}', 'ok')
            return redirect(url_for('shipping', b=bn))
        b = (request.args.get('b') or '').strip()
        rows = db.q("""SELECT * FROM shipments WHERE (?='' OR batch_no=?) ORDER BY id DESC LIMIT 200""", (b, b))
        cur = None
        if b:
            av, rel, unit, ref = available(b)
            cur = dict(available=av, released=rel, unit=unit, ref=ref, shipped=shipped_qty(b))
        return render_template('shipping.html', nav='shipping', b=b, rows=rows, batches=shippable_batches(), cur=cur)

    @route('/shipping/<int:sid>/void', 'shipping_void', methods=['POST'])
    @auth.require('wo_close')
    def shipping_void(sid):
        sh = db.one('SELECT * FROM shipments WHERE id=?', (sid,))
        if not sh:
            abort(404)
        f = request.form
        if sh.get('voided'):
            flash('سند الشحن مُبطَل مسبقًا', 'bad')
        elif not s(f.get('reason')):
            flash('سبب الإبطال إلزامي', 'bad')
        elif not auth.check_esign(f.get('esign_pw')):
            flash('التوقيع الإلكتروني غير صحيح', 'bad')
        else:
            with db.tx() as con:
                con.execute('UPDATE shipments SET voided=1, void_reason=?, void_by=? WHERE id=?',
                            (s(f.get('reason')), g.user['username'], sid))
                con.execute(*auth.signature_row('إبطال سند شحن', 'shipments', sh['doc_no']))
            db.log('void', 'shipments', sh['doc_no'], s(f.get('reason')))
            flash(f'أُبطل سند الشحن {sh["doc_no"]} وعادت الكمية للمتاح', 'ok')
        return redirect(url_for('shipping', b=sh['batch_no']))

    @route('/materials', 'materials', methods=['GET', 'POST'])
    def materials():
        bn = (request.args.get('b') or request.form.get('batch_no') or '').strip()
        w = db.one("SELECT * FROM work_orders WHERE batch_no=? AND COALESCE(order_type,'إنتاج')='إنتاج'", (bn,)) if bn else None
        if request.method == 'POST':
            auth.need('enter')
            f = request.form
            code = (s(f.get('item_code')) or '').upper()
            it = db.one("SELECT item_code, description, uom FROM items WHERE item_code=? AND prefix IN ('PK','BX','MB')", (code,))
            q_ = num(f.get('qty_issued'))
            if not w:
                flash('اختر رقم التشغيلة (أمر إنتاج)', 'bad')
            elif not it:
                flash('اختر مادة تعبئة من الفهرس', 'bad')
            elif not q_ or q_ <= 0:
                flash('الكمية المصروفة يجب أن تكون أكبر من صفر', 'bad')
            elif not (s(f.get('lot')) and s(f.get('issue_date')) and s(f.get('storekeeper'))):
                flash('LOT المادة والتاريخ واسم أمين المخزن بيانات إلزامية', 'bad')
            else:
                db.run("""INSERT INTO bom(wo_no,batch_no,mat_kind,item_code,description,qty_issued,uom,saptco_ref,
                          issue_date,storekeeper,notes,lot,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (w.get('wo_no'), bn, s(f.get('mat_kind')) if f.get('mat_kind') in MAT_KINDS else 'أخرى',
                        it['item_code'], it['description'], q_, it.get('uom') or 'قطعة', s(f.get('saptco_ref')),
                        s(f.get('issue_date')), s(f.get('storekeeper')), s(f.get('notes')), s(f.get('lot')),
                        g.user['username']))
                db.log('create', 'bom', bn, f'{code}; {q_:g}; lot {s(f.get("lot"))}')
                flash(f'سُجّل صرف {q_:g} من {code} للتشغيلة {bn}', 'ok')
            return redirect(url_for('materials', b=bn))
        rows = db.q("SELECT * FROM bom WHERE batch_no=? AND IFNULL(voided,0)=0 ORDER BY id", (bn,)) if bn else []
        hint = []
        if w:
            it = db.one('SELECT pack_code, master_box FROM items WHERE item_code=?', (w['item_code'],)) or {}
            hint = [c.strip() for k in ('pack_code', 'master_box') for c in (it.get(k) or '').split(',') if c.strip()]
        mats = db.q("SELECT item_code, prefix, description FROM items WHERE prefix IN ('PK','BX','MB') AND status='نشط' ORDER BY prefix, item_code")
        mats.sort(key=lambda m: m['item_code'] not in hint)
        return render_template('materials.html', nav='materials', bn=bn, b=w, rows=rows, mats=mats, hint=hint,
                               kinds=MAT_KINDS, batches=db.q("""SELECT batch_no,item_code FROM work_orders
                               WHERE COALESCE(order_type,'إنتاج')='إنتاج' AND status IN ('صادر','قيد التنفيذ')
                               ORDER BY rowid DESC"""))
