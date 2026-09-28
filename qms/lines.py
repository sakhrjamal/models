# -*- coding: utf-8 -*-
"""خطوط الإنتاج: لوحات الخطوط، WIP، مخزن المنتج التام، الإشعارات، ومهام الجودة.

كل شيء هنا يقرأ من مسارات mfg وحركات inventory، فلا يعرف اسم منتج بعينه.
"""
import datetime, json
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, notify, ncr, quality
from util import s, num, doc_scope
from inventory import InsufficientStock


# ------------------------------------------------------------------ WIP
def full_wip():
    """WIP مسار الشاش الكامل مشتق من جداول الطي/الفرز/التغليف القائمة (لا قيود مخزون لها)."""
    rows = []
    for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج'
                     AND COALESCE(route_code,'FULL_GAUZE')='FULL_GAUZE' AND status IN ('صادر','قيد التنفيذ','مكتملة')"""):
        bn = w['batch_no']
        one = lambda sql: float(list(db.one(sql, (bn,)).values())[0] or 0)          # noqa: E731
        cart = one('SELECT IFNULL(SUM(qty),0) FROM cartons WHERE batch_no=?') - one(
            'SELECT IFNULL(SUM(qty_in),0) FROM sorting WHERE batch_no=?')
        sorted_ = one('SELECT IFNULL(SUM(pieces_used),0) FROM sorting WHERE batch_no=?') - one(
            'SELECT IFNULL(SUM(groups_in*per_envelope),0) FROM packaging WHERE batch_no=?')
        packed = one('SELECT IFNULL(SUM(env_good*per_envelope),0) FROM packaging WHERE batch_no=?')
        rel, unit, _ = mfg.released_qty(bn)
        base = dict(batch_no=bn, wo_no=w['wo_no'], item_code=w['item_code'], route_code='FULL_GAUZE',
                    status=w['status'], location='خط الإنتاج', derived=True)
        if cart > 1e-6:
            rows.append(dict(base, label='كراتين طي — بانتظار الفرز', qty=cart, unit='قطعة'))
        if sorted_ > 1e-6:
            rows.append(dict(base, label='مفروز — بانتظار التغليف', qty=sorted_, unit='قطعة'))
        if packed > 1e-6 and not rel:
            rows.append(dict(base, label='مغلَّف — بانتظار التعقيم / الفحص النهائي', qty=packed, unit='قطعة'))
        if rel and rel - mfg.fg_received(bn) > 1e-6:
            rows.append(dict(base, label='مفرج عنه — بانتظار استلام المخزن', qty=rel - mfg.fg_received(bn), unit=unit))
    return rows


def wip_rows(route=None):
    rows = []
    for b in inventory.buckets(kind='WIP', route=route):
        w = db.one('SELECT wo_no, item_code, status, route_code FROM work_orders WHERE batch_no=?', (b['batch_no'],)) or {}
        rows.append(dict(batch_no=b['batch_no'], wo_no=w.get('wo_no'), item_code=w.get('item_code'),
                         route_code=b.get('route_code') or w.get('route_code'), status=w.get('status'),
                         label=b['label'], qty=b['qty'], unit=b['unit'], location=b.get('location') or 'خط الإنتاج',
                         owner=b['owner'], derived=False))
    if route in (None, mfg.FULL):
        rows += full_wip()
    lines = auth.user_lines()
    if lines:
        rows = [r for r in rows if r['route_code'] in lines]
    return rows


# ------------------------------------------------------------------ إحصاءات الخط
def _pending_raw(cls):
    ph = ','.join('?' * len(mfg.prefixes_of_class(cls))) or "''"
    return db.q(f"""SELECT r.grn_no, r.item_code, r.supplier_lot, r.qty, r.uom, r.receipt_date, r.stock_status
                    FROM receipts r JOIN items i ON i.item_code=r.item_code
                    WHERE i.prefix IN ({ph}) AND r.inspection_no IS NULL ORDER BY r.receipt_date""",
                tuple(mfg.prefixes_of_class(cls)))


def _hold_raw(cls):
    ph = ','.join('?' * len(mfg.prefixes_of_class(cls))) or "''"
    return db.q(f"""SELECT r.grn_no, r.stock_status FROM receipts r JOIN items i ON i.item_code=r.item_code
                    WHERE i.prefix IN ({ph}) AND r.stock_status IN ('مرفوض','حجر') AND r.inspection_no IS NOT NULL""",
                tuple(mfg.prefixes_of_class(cls)))


def awaiting_final_qc(route):
    """أوامر مسار اكتملت مراحلها التشغيلية وتنتظر الفحص النهائي/الإفراج."""
    out = []
    for w in db.q("""SELECT * FROM work_orders WHERE route_code=? AND COALESCE(order_type,'إنتاج')='إنتاج'
                     AND status IN ('صادر','قيد التنفيذ')""", (route,)):
        p = mfg.progress(w['batch_no'])
        steps = [x for x in p['steps'] if x['code'] not in ('FINALQC', 'WH')]
        if steps and all(x['frac'] >= 1 for x in steps) and not mfg.final_release(w['batch_no']):
            out.append(w)
    return out


def line_stats(route_code):
    today = datetime.date.today().isoformat()
    r = mfg.route(route_code)
    orders = db.q("""SELECT * FROM work_orders WHERE COALESCE(route_code,'FULL_GAUZE')=? AND
                     COALESCE(order_type,'إنتاج')='إنتاج' AND status IN ('صادر','قيد التنفيذ')""", (route_code,))
    cls = r['input_kind']
    pending = len(_pending_raw(cls)) + len(awaiting_final_qc(route_code))
    wip = wip_rows(route_code)
    wip_by_unit = {}
    for x in wip:
        wip_by_unit[x['unit']] = wip_by_unit.get(x['unit'], 0) + x['qty']
    done_today = db.one("""SELECT COUNT(*) n FROM proc_batches WHERE route_code=? AND work_date=?""", (route_code, today))['n']
    if route_code == mfg.FULL:
        done_today = sum(db.one(q_, (today,))['n'] for q_ in (
            "SELECT COUNT(*) n FROM folding_out WHERE fdate=?", "SELECT COUNT(*) n FROM sorting WHERE sdate=?",
            "SELECT COUNT(*) n FROM packaging WHERE pdate=?"))
    released_today = db.one("""SELECT COUNT(*) n FROM work_orders w WHERE COALESCE(w.route_code,'FULL_GAUZE')=? AND (
                       EXISTS(SELECT 1 FROM releases x WHERE x.batch_no=w.batch_no AND x.issue_date=? AND x.decision='مفرج عنها') OR
                       EXISTS(SELECT 1 FROM qc_records q JOIN qc_templates t ON t.code=q.template_code
                              WHERE t.is_final=1 AND q.batch_no=w.batch_no AND q.rec_date=? AND q.decision='مفرج'))""",
                            (route_code, today, today))['n']
    ncr_open = db.one("""SELECT COUNT(*) n FROM deviations d JOIN work_orders w ON w.batch_no=d.batch_no
                         WHERE d.status<>'مغلق' AND COALESCE(w.route_code,'FULL_GAUZE')=?""", (route_code,))['n']
    return dict(route=r, active=len(orders), pending_qc=pending, wip_units=wip_by_unit, wip_batches=len({x['batch_no'] for x in wip}),
                done_today=done_today, released_today=released_today,
                rejected_hold=ncr_open + len(_hold_raw(cls)), orders=orders)


def visible_routes():
    lines = auth.user_lines()
    return [r for r in mfg.routes() if not lines or r['code'] in lines]


# ------------------------------------------------------------------ المسارات
def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    # ---------------------------------------------------------- لوحات الخطوط
    @route('/line/<code>', 'line_view')
    def line_view(code):
        r = mfg.route(code)
        if not r:
            abort(404)
        if not auth.line_ok(code):
            abort(403)
        st = line_stats(code)
        for o in st['orders']:
            o['prog'] = mfg.progress(o['batch_no'])
        cls = r['input_kind']
        recent = db.q("""SELECT proc_no ref, stage_code st, work_date d, operator, machine, qty_out, unit
                         FROM proc_batches WHERE route_code=? ORDER BY created_at DESC LIMIT 12""", (code,))
        return render_template('line_view.html', nav='line', st=st, r=r, wip=wip_rows(code),
                               pending_raw=_pending_raw(cls), final_qc=awaiting_final_qc(code), recent=recent)

    # ---------------------------------------------------------- WIP والمنتج التام
    @route('/wip', 'wip')
    def wip():
        rc = s(request.args.get('route'))
        return render_template('wip.html', nav='wip', rows=wip_rows(rc), sel=rc, routes=visible_routes(),
                               losses=[b for b in inventory.buckets(kind='LOSS')][:40])

    @route('/fg', 'fg')
    def fg():
        rows = db.q("""SELECT f.batch_no, w.item_code, w.wo_no, COALESCE(w.route_code,'FULL_GAUZE') route_code, f.unit,
                              SUM(f.qty) received, MAX(f.location) location, MAX(f.rdate) last_date
                       FROM fg_receipts f LEFT JOIN work_orders w ON w.batch_no=f.batch_no
                       GROUP BY f.batch_no, f.unit ORDER BY MAX(f.id) DESC""")
        for r in rows:
            r['shipped'] = float(db.one("SELECT IFNULL(SUM(qty),0) n FROM shipments WHERE batch_no=? AND IFNULL(voided,0)=0",
                                        (r['batch_no'],))['n'])
            r['on_hand'] = r['received'] - r['shipped']
        return render_template('fg.html', nav='fg', rows=rows)

    def _pending_wh():
        out = []
        for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' ORDER BY rowid DESC LIMIT 300"""):
            rel, unit, ref = mfg.released_qty(w['batch_no'])
            if rel and rel - mfg.fg_received(w['batch_no']) > 1e-6:
                out.append(dict(w, released=rel, unit=unit, ref=ref, received=mfg.fg_received(w['batch_no']),
                                pending=rel - mfg.fg_received(w['batch_no'])))
        lines = auth.user_lines()
        return [x for x in out if not lines or (x.get('route_code') or mfg.FULL) in lines]

    @route('/warehouse', 'warehouse', methods=['GET', 'POST'])
    def warehouse():
        """استلام المنتج التام في المخزن — فقط بعد الإفراج النهائي، وبما لا يتجاوز المفرج."""
        if request.method == 'POST':
            auth.need('warehouse')
            f = request.form
            bn, back = s(f.get('batch_no')), url_for('warehouse')
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) if bn else None
            if not w:
                flash('اختر التشغيلة', 'bad'); return redirect(back)
            rel, unit, ref = mfg.released_qty(bn)
            if rel <= 0:
                flash('لا يمكن إدخال المنتج التام قبل الفحص النهائي والإفراج', 'bad'); return redirect(back)
            if not (s(f.get('location')) and s(f.get('received_by')) and s(f.get('rdate'))):
                flash('التاريخ وموقع المخزن واسم المستلم إلزامية', 'bad'); return redirect(back)
            opened = ncr.open_for_batch(bn)
            if opened:
                flash('لا يجوز الاستلام مع عدم مطابقة مفتوحة: ' + '، '.join(x['dev_no'] for x in opened), 'bad')
                return redirect(back)
            done = mfg.fg_received(bn)
            qty = num(f.get('qty'))
            if not qty or qty <= 0 or qty - (rel - done) > 1e-6:
                flash(f'الكمية غير صحيحة — المتبقي للاستلام {rel - done:g} {unit}', 'bad'); return redirect(back)
            rc = mfg.route_of_wo(w)
            auth.need_line(rc)
            scope, fmt = doc_scope('FGR', f.get('rdate'))
            with db.tx() as con:
                doc = db.alloc(con, scope, fmt)
                con.execute("""INSERT INTO fg_receipts(doc_no,rdate,batch_no,qty,unit,location,received_by,release_ref,notes,created_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""", (doc, s(f.get('rdate')), bn, qty, unit, s(f.get('location')),
                                                             s(f.get('received_by')), ref, s(f.get('notes')), g.user['username']))
                mid = f'fg-{doc}'
                inventory.post(con, bn, 'FG', qty, unit, bn, rc, 'warehouse', doc, s(f.get('location')), move_id=mid)
                fs = mfg.final_stage(w)
                if fs:                                            # تفريغ حصة الكمية من WIP الأخير
                    share = qty / (rel - done)
                    for b in inventory.buckets(batch_no=bn):
                        if b['stage'] == fs and b['qty'] > 1e-9:
                            take = b['qty'] if share >= 1 - 1e-9 else b['qty'] * share
                            inventory.post(con, b['owner'], fs, -take, b['unit'], bn, rc, 'warehouse', doc, move_id=mid)
                if qty >= rel - done - 1e-6:
                    notify.push(con, 'stocked', f'استُلمت التشغيلة {bn} بالكامل في مخزن المنتج التام ({doc})',
                                f'{w["item_code"]} — {rel:g} {unit}', url_for('fg'), bn, rc, ('warehouse', 'wo_issue'))
            db.log('create', 'fg_receipts', doc, f'{bn}; {qty:g} {unit}; {s(f.get("location"))}')
            flash(f'تم استلام {qty:g} {unit} من {bn} في المخزن — السند {doc}', 'ok')
            return redirect(back)
        recent = db.q('SELECT * FROM fg_receipts ORDER BY id DESC LIMIT 30')
        return render_template('warehouse.html', nav='warehouse', pending=_pending_wh(), recent=recent)

    # ---------------------------------------------------------- الإشعارات
    @route('/notifications', 'notifications', methods=['GET', 'POST'])
    def notifications():
        if request.method == 'POST':
            ids = [int(x) for x in request.form.getlist('id') if x.isdigit()]
            notify.mark_read(g.user, ids or None)
            return redirect(request.form.get('next') or url_for('notifications'))
        return render_template('notifications.html', nav='notif', rows=notify.for_user(g.user, limit=150))

    @route('/notifications/go/<int:nid>', 'notification_go')
    def notification_go(nid):
        n = db.one('SELECT * FROM notifications WHERE id=?', (nid,))
        if n and notify.visible(g.user, n):
            notify.mark_read(g.user, [nid])
            return redirect(n['link'] or url_for('notifications'))
        return redirect(url_for('notifications'))

    # ---------------------------------------------------------- مهام الجودة
    @route('/quality/tasks', 'quality_tasks')
    def quality_tasks():
        raw = {cls: _pending_raw(cls) for cls in ('ROLL', 'SP', 'JUMBO')}
        final = {r['code']: awaiting_final_qc(r['code']) for r in mfg.routes()}
        open_ncr = db.one("SELECT COUNT(*) n FROM deviations WHERE status<>'مغلق'")['n']
        return render_template('quality_tasks.html', nav='quality', raw=raw, final=final, routes=mfg.routes(),
                               open_ncr=open_ncr, due=[x for x in quality.due_items() if x['due']][:12],
                               labels={'ROLL': 'رولات الشاش', 'SP': 'خام SP', 'JUMBO': 'جامبو رول الأربطة'})

    @route('/quality/final', 'quality_final')
    def quality_final():
        rc = s(request.args.get('route')) or mfg.FULL
        r = mfg.route(rc) or abort(404)
        rows = []
        for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(route_code,'FULL_GAUZE')=? AND
                         COALESCE(order_type,'إنتاج')='إنتاج' ORDER BY rowid DESC LIMIT 60""", (rc,)):
            rows.append(dict(w=w, checks=[c for c in quality.batch_checks(w) if c['tpl']['area'] != 'general' or True],
                             fr=mfg.final_release(w['batch_no']), prog=mfg.progress(w['batch_no'])))
        return render_template('quality_final.html', nav='quality', rows=rows, r=r, routes=mfg.routes())
