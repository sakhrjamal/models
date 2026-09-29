# -*- coding: utf-8 -*-
"""WIP ومخزن المنتج التام والإشعارات (v14: الاعتماد ثم التخزين بضغطة واحدة).

كل شيء هنا يقرأ من مسارات mfg وحركات inventory، فلا يعرف اسم منتج بعينه.
"""
import datetime, json
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, notify, ncr
from util import s, num, doc_scope, fmt_qty
from inventory import InsufficientStock


# ------------------------------------------------------------------ WIP
def full_wip():
    """WIP مسار الشاش الكامل مشتق من جداول الطي/الفرز/التغليف القائمة (لا قيود مخزون لها)."""
    rows = []
    for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج'
                     AND COALESCE(route_code,'FULL_GAUZE')='FULL_GAUZE' AND status IN ('صادر','قيد التنفيذ','مكتملة')"""):
        bn = w['batch_no']
        if w.get('final_status') or db.one('SELECT 1 FROM cutting_plans WHERE batch_no=?', (bn,)):
            continue                      # أوامر v14: WIP في دفتر المخزون (PROD_OUT)
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
        """دفعات معتمدة (أو مفرج عنها قديمًا) بانتظار التخزين."""
        out = []
        for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج' ORDER BY rowid DESC LIMIT 300"""):
            rel, unit, ref = mfg.released_qty(w['batch_no'])
            pending = rel - mfg.fg_received(w['batch_no']) if rel else 0
            if pending > 1e-6:
                out.append(dict(w, released=rel, unit=unit, ref=ref, pending=pending,
                                equiv=mfg.pack_equiv(w['item_code'], pending, unit),
                                st=mfg.order_state(w)))
        lines = auth.user_lines()
        return [x for x in out if not lines or (x.get('route_code') or mfg.FULL) in lines]

    @route('/warehouse', 'warehouse', methods=['GET', 'POST'])
    def warehouse():
        """تخزين الدفعة المعتمدة بضغطة واحدة: الكمية = المعتمدة، الموقع الافتراضي من الإعدادات."""
        default_loc = db.setting('fg_location', 'مخزن المنتج التام')
        if request.method == 'POST':
            auth.need('warehouse')
            f = request.form
            bn, back = s(f.get('batch_no')), url_for('warehouse')
            w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) if bn else None
            if not w:
                flash('اختر الدفعة', 'bad'); return redirect(back)
            rel, unit, ref = mfg.released_qty(bn)
            if rel <= 0:
                flash('لا يمكن تخزين دفعة لم تعتمدها الجودة', 'bad'); return redirect(back)
            opened = ncr.open_for_batch(bn)
            if opened:
                flash('لا يجوز التخزين مع عدم مطابقة مفتوحة: ' + '، '.join(x['dev_no'] for x in opened), 'bad')
                return redirect(back)
            done = mfg.fg_received(bn)
            qty = rel - done
            if qty <= 1e-6:
                flash('هذه الدفعة مخزّنة بالكامل', 'bad'); return redirect(back)
            rc = mfg.route_of_wo(w)
            auth.need_line(rc)
            loc = s(f.get('location')) or default_loc
            today = datetime.date.today().isoformat()
            scope, fmt = doc_scope('FGR', today)
            with db.tx() as con:
                doc = db.alloc(con, scope, fmt)
                con.execute("""INSERT INTO fg_receipts(doc_no,rdate,batch_no,qty,unit,location,received_by,release_ref,notes,created_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""", (doc, today, bn, qty, unit, loc, g.user['full_name'], ref,
                                                             s(f.get('notes')), g.user['username']))
                mid = f'fg-{doc}'
                inventory.post(con, bn, 'FG', qty, unit, bn, rc, 'warehouse', doc, loc, move_id=mid)
                fs = mfg.final_stage(w)
                if fs:                                            # تفريغ WIP الأخير كاملًا
                    for b in inventory.buckets(batch_no=bn):
                        if b['stage'] == fs and b['qty'] > 1e-9:
                            inventory.post(con, b['owner'], fs, -b['qty'], b['unit'], bn, rc, 'warehouse', doc, move_id=mid)
                if w.get('final_status'):
                    con.execute("UPDATE work_orders SET final_status=?, status='مكتملة', stored_at=? WHERE batch_no=?",
                                (mfg.FS_STORED, datetime.datetime.now().isoformat(sep=' ', timespec='seconds'), bn))
            db.log('create', 'fg_receipts', doc, f'{bn}; {fmt_qty(qty)} {unit}; {loc}')
            flash(f'تم تخزين {fmt_qty(qty)} {unit} من الدفعة {bn} في {loc} — السند {doc}', 'ok')
            return redirect(back)
        recent = db.q('SELECT * FROM fg_receipts ORDER BY id DESC LIMIT 20')
        return render_template('warehouse.html', nav='warehouse', pending=_pending_wh(), recent=recent, default_loc=default_loc,
                               fmt=fmt_qty)

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
