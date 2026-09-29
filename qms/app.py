# -*- coding: utf-8 -*-
"""
نظام إدارة الإنتاج والتتبع — مصنع الشاش والأربطة الطبية
تطبيق محلي: Flask + SQLite. يُحزَّم كملف exe واحد.
"""
import os, secrets, datetime, re
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort, session, g)
import db, trace, forms, auth, backup, constants, ncr, mfg, notify, inventory, access, ops

app = Flask(__name__)


def _secret_key():
    """مفتاح جلسة ثابت محفوظ على القرص — لا يتغير بين عمليات التشغيل."""
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    p = os.path.join(os.path.dirname(db.DB_PATH), 'secret.key')
    try:
        if os.path.exists(p):
            return open(p, 'rb').read()
        k = secrets.token_bytes(32)
        with open(p, 'wb') as fh:
            fh.write(k)
        return k
    except OSError:
        return secrets.token_bytes(32)


app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)

MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

# رؤوس مسارات لا تتطلب تسجيل دخول
_PUBLIC_ENDPOINTS = {'login', 'static', 'health'}


@app.before_request
def _security_gate():
    auth.load_logged_in_user()
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)

    ep = request.endpoint or ''
    if ep in _PUBLIC_ENDPOINTS:
        return

    if not g.user:
        if request.method == 'GET':
            return redirect(url_for('login', next=request.full_path))
        abort(401)

    if request.method == 'POST':
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF', '')
        if not sent or not secrets.compare_digest(sent, session.get('_csrf', '')):
            abort(400, 'رمز التحقق (CSRF) غير صالح — أعد تحميل الصفحة')

    if g.user['must_change_pw'] and ep not in ('account', 'logout'):
        flash('يجب تغيير كلمة المرور قبل متابعة العمل', 'warn')
        return redirect(url_for('account'))

    # فصل الإنتاج عن الجودة في الخادم: فتح رابط قسم لا تملك صلاحيته = 403 حتى لو اختفت الأيقونة
    need = access.required(ep)
    if need and not any(auth.can(p) for p in need):
        abort(403)


@app.context_processor
def _inject():
    import navmenu
    u = g.get('user')
    return dict(me=u, can=auth.can, ROLE_AR=constants.ROLE_AR, can_line=auth.line_ok, mfg=mfg,
                csrf_token=lambda: session.get('_csrf', ''),
                menu=navmenu.build() if u else [],
                crumbs=navmenu.crumbs() if u else [],
                next_actions=session.pop('_next', None) if u else None,
                operators=ops.list_ops, op_default=ops.default, ico=_ico,
                notif_unread=notify.unread_count(u) if u else 0)


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def _err(e):
    import logging
    code = getattr(e, 'code', 500)
    if code == 500:
        logging.getLogger('qms').exception('خطأ غير متوقع في %s', request.path)
    msg = {400: 'طلب غير صالح', 401: 'يلزم تسجيل الدخول', 403: 'صلاحيتك لا تسمح بهذا الإجراء',
           404: 'الصفحة غير موجودة', 500: 'حدث خطأ داخلي — راجع سجل الأخطاء'}.get(code, 'خطأ')
    detail = getattr(e, 'description', '') if code == 400 else ''
    return render_template('error.html', code=code, msg=msg, detail=detail), code


@app.route('/health')
def health():
    return 'ok'

# ---------------------------------------------------------------- أدوات
def cls_for(text):
    """يصنّف نص حالة إلى ok / warn / bad لتلوين الشارة.

    تُفحص العبارات السلبية أولًا حتى لا يُصنَّف «غير مفرج» أو «لم تُفرج»
    خطأً على أنه «مطابق» لمجرد احتوائه على كلمة «مفرج».
    """
    if not text:
        return ''
    t = str(text)
    bad = ('غير مطابق', 'مرفوض', 'رفض', 'موقوف', 'تجاوز', 'انحراف', 'موجب — غير مطابق',
           'موجب', 'لم تُفرج', 'غير متوفر', 'فشل', 'محظور')
    warn = ('غير مفرج', 'لم تصدر', 'لم تُصدر', 'حجر', 'محجوز', 'معلّق', 'بانتظار',
            'مفتوح', 'قيد', 'تحذير', 'جزئي', 'فارق', 'نقص', 'ناقص')
    good = ('سالب — مطابق', 'مفرج عنها', 'مفرج', 'مطابق', 'قبول', 'مكتمل', 'اجتاز',
            'ضمن', 'كافٍ', 'جاهز', 'سليم', 'تم')
    for k in bad:
        if k in t:
            return 'bad'
    for k in warn:
        if k in t:
            return 'warn'
    for k in good:
        if k in t:
            return 'ok'
    return ''
app.jinja_env.globals['cls_for'] = cls_for


def _ico(name):
    import navmenu
    from markupsafe import Markup
    return Markup('<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
                  'stroke-linejoin="round"><path d="%s"/></svg>' % navmenu.icon(name))
app.jinja_env.globals['today']   = lambda: datetime.date.today().isoformat()


# ---------------------------------------------------------------- الدخول والحساب
@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.get('user'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        sent = request.form.get('_csrf') or ''
        if not sent or not secrets.compare_digest(sent, session.get('_csrf', '')):
            abort(400)
        u = auth.attempt_login(request.form.get('username'), request.form.get('password'))
        if not u:
            db.log('login_failed', 'users', request.form.get('username') or '?', actor='anon')
            flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'bad')
            return redirect(url_for('login'))
        db.log('login', 'users', u['username'])
        nxt = request.args.get('next') or ''
        if nxt.startswith('/'):
            return redirect(nxt)
        return redirect(url_for('index'))
    from flask import get_flashed_messages
    msgs = get_flashed_messages(with_categories=True)
    err = next((m for c, m in msgs if c == 'bad'), None)
    note = next((m for c, m in msgs if c != 'bad'), None)
    return render_template('login.html', err=err, note=note)


@app.route('/logout')
def logout():
    if g.get('user'):
        db.log('logout', 'users', g.user['username'])
    auth.logout()
    flash('تم تسجيل الخروج', 'ok')
    return redirect(url_for('login'))


@app.route('/account', methods=['GET', 'POST'])
def account():
    if not g.get('user'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        cur = request.form.get('current_pw') or ''
        new = request.form.get('new_pw') or ''
        rep = request.form.get('repeat_pw') or ''
        if not auth.verify_pw(g.user['pw_hash'], cur):
            flash('كلمة المرور الحالية غير صحيحة', 'bad')
        elif len(new) < 6:
            flash('كلمة المرور الجديدة يجب ألا تقل عن 6 خانات', 'bad')
        elif new != rep:
            flash('كلمة المرور الجديدة وتأكيدها غير متطابقين', 'bad')
        else:
            db.run('UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?',
                   (auth.hash_pw(new), g.user['id']))
            db.log('change_password', 'users', g.user['username'])
            flash('تم تحديث كلمة المرور', 'ok')
            return redirect(url_for('index'))
        return redirect(url_for('account'))
    return render_template('account.html', nav='')

from util import num, s, num_unit, doc_scope, lot_taken
from util import as_date as _as_date


def cycle_scope(date_text=None):
    """نطاق + قالب رقم دورة EO: SEP-2610-EO-001."""
    d = _as_date(date_text)
    stem = f"{MONTHS[d.month-1]}-{d.strftime('%y')}{d.day:02d}-EO"
    return stem, stem + '-{n3}'


def next_doc_no(prefix, table=None, col=None, date_text=None):
    """الرقم المتوقّع التالي (عرض فقط، بلا حجز). الحجز يتم داخل db.alloc عند الحفظ."""
    scope, fmt = doc_scope(prefix, date_text)
    return db.peek_number(scope, fmt)


def next_cycle_no(date_text=None):
    scope, fmt = cycle_scope(date_text)
    return db.peek_number(scope, fmt)


def duration_hours(d1, t1, d2, t2=None):
    """فرق الساعات. إن غاب تاريخ النهاية ‎d2‎ يُفترض نفس يوم البداية،
    ومع تجاوز منتصف الليل يُضاف يوم واحد. يدعم أيضًا مدى يتجاوز 24 ساعة
    عندما يُدخَل تاريخ نهاية صريح."""
    if not d1 or not t1 or not t2:
        return None
    try:
        a = datetime.datetime.fromisoformat(f"{d1}T{t1}")
        if d2:
            b = datetime.datetime.fromisoformat(f"{d2}T{t2}")
        else:
            b = datetime.datetime.fromisoformat(f"{d1}T{t2}")
            if b < a:
                b += datetime.timedelta(days=1)
        h = (b - a).total_seconds() / 3600
        return round(h, 2) if h >= 0 else None
    except (TypeError, ValueError):
        return None


# توافق مع النداءات القديمة
def hours_between(d1, t1, d2, t2):
    return duration_hours(d1, t1, d2, t2)


def cycle_duration_hours(date_text, start_time, end_time, end_date=None):
    return duration_hours(date_text, start_time, end_date, end_time)


def cycle_state(cycle_no):
    """اشتقاق حالة دورة التعقيم من نتائجها وحمولاتها — بمقارنة قيم قياسية لا نصوص حرة."""
    C = constants
    c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,)) or {}
    loads = db.q('SELECT * FROM cycle_loads WHERE cycle_no=?', (cycle_no,))
    ci_vals = [c.get('ci_external'), c.get('ci_internal')] + [x.get('ci_load') for x in loads]
    controls_bad = any(C.is_nonconform(v) for v in (c.get('ctrl_pos'), c.get('ctrl_neg')))
    bi = C.canon(c.get('bi_result'), C.BI_RESULTS)
    if any(C.is_nonconform(v) for v in ci_vals) or controls_bad or bi == C.BI_POS:
        return C.CYC_REJECTED
    if not c.get('end_time'):
        return C.CYC_RUNNING
    all_out = bool(loads) and all(x.get('boxes_out') is not None for x in loads)
    cis_present = [v for v in ci_vals if v is not None]
    cis_ok = bool(cis_present) and len(cis_present) == len(ci_vals) and all(C.is_conform(v) for v in cis_present)
    ctrl_ok = (C.canon(c.get('ctrl_pos'), (C.CTRL_POS_OK, 'موجب')) in (C.CTRL_POS_OK, 'موجب')
               and C.canon(c.get('ctrl_neg'), (C.CTRL_NEG_OK, 'سالب')) in (C.CTRL_NEG_OK, 'سالب'))
    if bi == C.BI_NEG and ctrl_ok and cis_ok and all_out:
        return C.CYC_PASSED
    if bi in (None, C.BI_PENDING, C.BI_INCUB) and cis_ok and all_out:
        return C.CYC_INCUB
    return C.CYC_REVIEW

# ---------------------------------------------------------------- التتبع
@app.route('/trace')
def trace_view():
    b = (request.args.get('b') or '').strip()
    w = db.one('SELECT route_code, order_type, final_status FROM work_orders WHERE batch_no=?', (b,)) if b else None
    if w and (w.get('order_type') or 'إنتاج') == 'إنتاج' and (mfg.route_of_wo(w) != mfg.FULL or mfg.is_v14(dict(w, batch_no=b))):
        return redirect(url_for('genealogy', b=b))          # SP / الرباط: تقرير الأصل والفرع الموحّد
    d = trace.batch_chain(b) if b else None
    return render_template('trace.html', nav='trace', b=b, d=d)

@app.route('/trace/print')
def trace_print():
    b = (request.args.get('b') or '').strip()
    if not b: return redirect(url_for('trace_view'))
    return render_template('trace_print.html', b=b, d=trace.batch_chain(b))

@app.route('/forward')
def forward():
    lot = (request.args.get('lot') or '').strip()
    rows = trace.batch_forward(lot) if lot else []
    return render_template('forward.html', nav='trace', lot=lot, rows=rows)

# ---------------------------------------------------------------- الأصناف
@app.route('/items')
def items():
    code = (request.args.get('code') or '').strip().upper()
    item = db.one('SELECT * FROM items WHERE UPPER(item_code)=?', (code,)) if code else None
    mtx = None
    if item and item.get('machine_code') and item.get('ply'):
        mtx = db.one('SELECT * FROM slit_matrix WHERE key=?',
                     (f"{item['machine_code']}-{item['ply']}",))
    sel = {k: (request.args.get(k) or '') for k in
           ('size','ply','xray','mesh','sterile')}
    found = []
    if all(sel.values()):
        key = f"|{sel['size']}|{sel['ply']}|{sel['xray']}|{sel['mesh']}|{sel['sterile']}"
        found = db.q('SELECT * FROM items WHERE search_key IN (?,?)',
                     ('GS'+key, 'SP'+key))
    q = (request.args.get('q') or '').strip()
    hits = db.q("""SELECT item_code,category_ar,size,ply,xray,mesh,sterile,description
                   FROM items WHERE item_code LIKE ? OR description LIKE ?
                   ORDER BY item_code LIMIT 60""", (f'%{q}%', f'%{q}%')) if q else []
    return render_template('items.html', nav='items', code=code, item=item, mtx=mtx,
                           sel=sel, found=found, q=q, hits=hits)

# ---------------------------------------------------------------- استلام وفحص المواد الخام
@app.route('/receipts')
def receipts():
    cls = (request.args.get('cls') or '').upper()
    prefs = mfg.prefixes_of_class(cls) if cls else []
    rows = db.q("""SELECT r.*, s.name supplier_name, i.decision, it.prefix,
                        (SELECT COUNT(*) FROM rolls x WHERE x.grn_no=r.grn_no) roll_count,
                        (SELECT COUNT(*) FROM rolls x WHERE x.grn_no=r.grn_no AND x.stock_status='مفرج') released_rolls,
                        (SELECT COUNT(*) FROM rolls x WHERE x.grn_no=r.grn_no AND x.stock_status='مرفوض') rejected_rolls
                 FROM receipts r
                 LEFT JOIN suppliers s ON s.supplier_id=r.supplier_id
                 LEFT JOIN inspections i ON i.inspection_no=r.inspection_no
                 LEFT JOIN items it ON it.item_code=r.item_code
                 ORDER BY r.receipt_date DESC, r.grn_no DESC LIMIT 400""")
    if cls:
        rows = [r for r in rows if r.get('prefix') in prefs]
    for r in rows:
        r['mclass'] = mfg.mat_class(r.get('prefix'))
    return render_template('receipts.html', nav='receipts', rows=rows, cls=cls,
                           class_ar={'ROLL': 'رولات الشاش', 'SP': 'خام SP', 'JUMBO': 'جامبو الأربطة'})

@app.route('/receipts/new', methods=['GET','POST'])
def receipt_new():
    cls_sel = (request.args.get('cls') or request.form.get('cls') or '').upper()
    if request.method == 'POST':
        auth.need('receive')
        f = request.form
        receipt_date = s(f.get('receipt_date')) or datetime.date.today().isoformat()
        manual_grn = (s(f.get('grn_no')) or '').upper() or None
        supplier_name = s(f.get('supplier_name'))
        item_code = (s(f.get('item_code')) or '').upper()
        supplier_lot = s(f.get('supplier_lot'))
        try:
            roll_count = int(float(f.get('roll_count') or 0))
        except ValueError:
            roll_count = 0

        if not supplier_name or not item_code or not supplier_lot or roll_count < 1:
            flash('المورد والصنف وLOT المورد وعدد الرولات بيانات إلزامية', 'bad')
            return redirect(url_for('receipt_new'))
        dup = lot_taken(supplier_lot)
        if dup:
            flash(f'رقم LOT المورّد «{supplier_lot}» مستخدم مسبقًا في الاستلام {dup} — لا يُقبل نفس الرقم لاستلامين مختلفين', 'bad')
            return redirect(url_for('receipt_new'))
        if roll_count > 500:
            flash('عدد الرولات كبير بشكل غير معقول (الحد 500)', 'bad')
            return redirect(url_for('receipt_new'))
        if manual_grn and db.one('SELECT 1 FROM receipts WHERE grn_no=?', (manual_grn,)):
            flash(f'رقم سند الاستلام {manual_grn} مستخدم مسبقًا', 'bad')
            return redirect(url_for('receipt_new'))
        item = db.one("SELECT * FROM items WHERE item_code=? AND prefix IN ('RR','PBT')", (item_code,))
        if not item:
            flash('الصنف غير موجود أو ليس من أصناف المواد الخام المعتمدة', 'bad')
            return redirect(url_for('receipt_new'))
        mclass = mfg.mat_class(item['prefix'])
        if mclass == 'JUMBO' and not num_unit(f.get('length_m'), num_unit(item.get('length_m'))):
            flash('طول الجامبو رول إلزامي لتتبع الاستهلاك', 'bad')
            return redirect(url_for('receipt_new', cls='JUMBO'))

        country = s(f.get('country'))
        width = num_unit(f.get('width_cm'), num_unit(item.get('width_cm')))
        length = num_unit(f.get('length_m'), num_unit(item.get('length_m')))
        weight = num(f.get('weight_kg'))
        location = s(f.get('qc_location')) or 'منطقة الحجر'
        qty = num(f.get('qty'), roll_count)
        uom = s(f.get('uom')) or item.get('uom') or 'رول'
        coa = 'متوفر' if f.get('coa') == 'yes' else 'غير متوفر'
        scope, fmt = doc_scope('GRN', receipt_date)

        try:
            with db.tx() as con:
                key = ''.join(supplier_lot.split()).upper()
                clash = next((r['grn_no'] for r in con.execute('SELECT grn_no, supplier_lot FROM receipts').fetchall()
                              if ''.join(str(r['supplier_lot'] or '').split()).upper() == key), None)
                if clash:                                   # فحص داخل التعامل يمنع التسابق بين نافذتين
                    flash(f'رقم LOT المورّد «{supplier_lot}» مستخدم مسبقًا في الاستلام {clash}', 'bad')
                    return redirect(url_for('receipt_new'))
                sup = con.execute('SELECT * FROM suppliers WHERE name=?', (supplier_name,)).fetchone()
                if sup:
                    supplier_id = sup['supplier_id']
                    if country and not sup['country']:
                        con.execute('UPDATE suppliers SET country=? WHERE supplier_id=?', (country, supplier_id))
                else:
                    supplier_id = con.execute('INSERT INTO suppliers(name,country,active) VALUES(?,?,1)',
                                              (supplier_name, country)).lastrowid
                grn_no = db.use_number(con, manual_grn, scope, fmt)
                con.execute("""INSERT INTO receipts(grn_no,receipt_date,kind,supplier_id,country,po_no,
                            invoice_no,bl_no,item_code,supplier_lot,mfg_date,expiry_date,qty,uom,coa,coa_no,
                            pack_cond,qc_location,stock_status,received_by,notes)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (grn_no, receipt_date, s(f.get('kind')) or 'رولات شاش خام', supplier_id, country,
                             s(f.get('po_no')), s(f.get('invoice_no')), s(f.get('bl_no')), item_code,
                             supplier_lot, s(f.get('mfg_date')), s(f.get('expiry_date')), qty, uom,
                             coa, s(f.get('coa_no')), s(f.get('pack_cond')), location, 'حجر',
                             s(f.get('received_by')), s(f.get('notes'))))
                con.execute('UPDATE receipts SET created_by=?, wh_location=? WHERE grn_no=?',
                            (g.user['username'], location, grn_no))
                for i in range(1, roll_count + 1):
                    roll_no = f"{grn_no}-R{i:03d}"
                    area = forms.area(length, width) if length and width else None
                    con.execute("""INSERT INTO rolls(roll_no,grn_no,item_code,supplier_lot,shipment_seq,
                                roll_seq,width_cm,length_m,weight_kg,area_cm2,xray_grade,mesh,stock_status,
                                location,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (roll_no, grn_no, item_code, supplier_lot, 1, i, width, length, weight,
                                 area, item.get('xray_grade'), item.get('mesh'), 'حجر', location,
                                 'أُنشئ تلقائيًا من سند الاستلام'))
                    if mclass == 'JUMBO':       # رصيد الجامبو بالمتر في دفتر المخزون
                        inventory.post(con, roll_no, 'RM', length, 'م', None, mfg.BANDAGE, 'receipt', grn_no, location)
                notify.push(con, 'qc_pending',
                            f'تم استلام {"جامبو رول للأربطة" if mclass == "JUMBO" else "رولات شاش خام"} '
                            f'{grn_no} ({roll_count} رول) وينتظر فحص الجودة والإفراج',
                            f'{item_code} · LOT المورد {supplier_lot}', url_for('receipt_view', grn_no=grn_no), grn_no,
                            mfg.BANDAGE if mclass == 'JUMBO' else mfg.FULL, ('qc_record',))
        except Exception:
            flash('تعذّر حفظ سند الاستلام — لم يُحفظ شيء. راجع سجل الأخطاء.', 'bad')
            raise
        db.log('create','receipts',grn_no, f'{roll_count} rolls / lot {supplier_lot}')
        flash(f'تم إنشاء سند الاستلام {grn_no} ووضع {roll_count} رول في الحجر لحين فحص الجودة', 'ok')
        return redirect(url_for('receipt_view', grn_no=grn_no))

    d = datetime.date.today().isoformat()
    return render_template('receipt_new.html', nav='receipts',
                           grn_no=next_doc_no('GRN','receipts','grn_no',d), today_date=d,
                           cls_sel=cls_sel, items=db.q("""SELECT i.item_code,i.description,i.width_cm,i.length_m,i.uom,m.class
                                         FROM items i JOIN mat_classes m ON m.prefix=i.prefix
                                         WHERE i.prefix IN ('RR','PBT') AND i.status='نشط'
                                           AND (?='' OR m.class=?) ORDER BY i.item_code""", (cls_sel, cls_sel)),
                           suppliers=db.q('SELECT name,country FROM suppliers WHERE active=1 ORDER BY name'))

@app.route('/receipt/<path:grn_no>', methods=['GET','POST'])
def receipt_view(grn_no):
    rec = db.one("""SELECT r.*, s.name supplier_name
                    FROM receipts r LEFT JOIN suppliers s ON s.supplier_id=r.supplier_id
                    WHERE r.grn_no=?""", (grn_no,))
    if not rec:
        abort(404)
    if request.method == 'POST':
        f = request.form
        act = f.get('act')
        if act == 'inspect':
            auth.need('qc_record')
            if rec.get('inspection_no'):
                flash('يوجد فحص جودة مسجل لهذا الاستلام بالفعل', 'bad')
                return redirect(url_for('receipt_view', grn_no=grn_no))
            decision = constants.canon(s(f.get('decision')), constants.QC_DECISIONS)
            if decision not in constants.QC_DECISIONS:
                flash('اختر قرار الجودة', 'bad')
                return redirect(url_for('receipt_view', grn_no=grn_no))
            justification = s(f.get('justification'))
            if decision == constants.QC_COND and not justification:
                flash('القبول المشروط يتطلب كتابة المبرر/الشرط', 'bad')
                return redirect(url_for('receipt_view', grn_no=grn_no))
            if not auth.check_esign(f.get('esign_pw')):
                flash('التوقيع الإلكتروني غير صحيح — أعد إدخال كلمة مرورك لاعتماد قرار الجودة', 'bad')
                return redirect(url_for('receipt_view', grn_no=grn_no))
            insp_date = s(f.get('insp_date')) or datetime.date.today().isoformat()
            status = constants.QC_TO_STOCK[decision]
            scope, fmt = doc_scope('QC-RM', insp_date)
            with db.tx() as con:
                inspection_no = db.alloc(con, scope, fmt)
                con.execute("""INSERT INTO inspections(inspection_no,insp_date,grn_no,item_code,
                            supplier_lot,sample_size,c1,c2,c3,c4,c5,c6,c7,c8,c9,decision,
                            justification,inspector,qa_officer,notes)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (inspection_no, insp_date, grn_no, rec['item_code'], rec['supplier_lot'],
                             s(f.get('sample_size')), s(f.get('c1')), s(f.get('c2')), s(f.get('c3')),
                             s(f.get('c4')), s(f.get('c5')), s(f.get('c6')), s(f.get('c7')),
                             s(f.get('c8')), s(f.get('c9')), decision, justification,
                             s(f.get('inspector')) or g.user['full_name'],
                             s(f.get('qa_officer')) or g.user['full_name'], s(f.get('qc_notes'))))
                con.execute('UPDATE receipts SET inspection_no=?, stock_status=? WHERE grn_no=?',
                            (inspection_no, status, grn_no))
                con.execute('UPDATE rolls SET inspection_no=?, stock_status=? WHERE grn_no=?',
                            (inspection_no, status, grn_no))
                con.execute(*auth.signature_row(constants.SIG_QC_DECISION, 'inspections', inspection_no))
            db.log('qc_decision','inspections',inspection_no, f'{grn_no}: {decision} -> {status}')
            it_ = db.one('SELECT prefix FROM items WHERE item_code=?', (rec['item_code'],)) or {}
            mc_ = mfg.mat_class(it_.get('prefix'))
            rt_ = {'JUMBO': mfg.BANDAGE, 'SP': mfg.SP}.get(mc_, mfg.FULL)
            what = {'JUMBO': 'الجامبو رول', 'SP': 'خام الشاش نصف المصنع SP'}.get(mc_, 'رولات الشاش الخام')
            if status != 'مفرج':          # إشعار واحد فقط عند رفض/تعليق الخام؛ الإفراج لا يحتاج إشعارًا
                with db.tx() as con_:
                    notify.push(con_, 'qc_' + ('reject' if status == 'مرفوض' else 'hold'),
                                f'{what} {grn_no}: قرار الجودة «{decision}»', rec['item_code'] or '',
                                url_for('receipt_view', grn_no=grn_no), grn_no, rt_, ('wo_issue', 'receive'))
            if status == 'مفرج':
                flash(f'تم اعتماد الفحص {inspection_no} وإفراج الرولات للإنتاج', 'ok')
            elif status == 'مرفوض':
                flash(f'تم تسجيل الرفض في الفحص {inspection_no} وحظر الرولات من الإنتاج', 'bad')
            else:
                flash(f'تم تسجيل الفحص {inspection_no} والرولات ما زالت تحت الحجر', 'ok')
        elif act == 'roll_update':
            auth.need('enter')
            roll_no = s(f.get('roll_no'))
            rl = db.one('SELECT * FROM rolls WHERE roll_no=? AND grn_no=?', (roll_no, grn_no))
            if not rl:
                flash('الرول غير موجود في هذا الاستلام', 'bad')
                return redirect(url_for('receipt_view', grn_no=grn_no))
            width = num_unit(f.get('width_cm'), rl.get('width_cm'))
            length = num_unit(f.get('length_m'), rl.get('length_m'))
            db.run("""UPDATE rolls SET width_cm=?,length_m=?,weight_kg=?,area_cm2=?,location=?,notes=?
                      WHERE roll_no=? AND grn_no=?""",
                   (width, length, num(f.get('weight_kg')), forms.area(length,width),
                    s(f.get('location')), s(f.get('roll_notes')), roll_no, grn_no))
            db.log('update','rolls',roll_no,'dimensions/weight/location')
            flash(f'تم تحديث بيانات الرول {roll_no}', 'ok')
        return redirect(url_for('receipt_view', grn_no=grn_no))

    inspection = db.one('SELECT * FROM inspections WHERE inspection_no=?', (rec.get('inspection_no'),)) \
                 if rec.get('inspection_no') else None
    rolls = db.q('SELECT * FROM rolls WHERE grn_no=? ORDER BY roll_seq, roll_no', (grn_no,))
    it_ = db.one('SELECT prefix FROM items WHERE item_code=?', (rec['item_code'],)) or {}
    mclass = mfg.mat_class(it_.get('prefix')) or 'ROLL'
    labels = {r['seq']: r['label_ar'] for r in db.q('SELECT seq,label_ar FROM rm_checks WHERE class=?', (mclass,))}
    return render_template('receipt_detail.html', nav='receipts', rec=rec, mclass=mclass, labels=labels,
                           inspection=inspection, rolls=rolls,
                           next_inspection=next_doc_no('QC-RM','inspections','inspection_no'))

# ---------------------------------------------------------------- مراحل ما بعد الأسليتر
def prod_batch_or_none(bn):
    """أمر إنتاج فقط — مراحل الطي فما بعد لا تعمل برقم أمر تقطيع."""
    b = forms.batch(bn) if bn else None
    if b and (b.get('order_type') or 'إنتاج') != 'إنتاج':
        return None
    return b


def default_sheet_doc(prefix, bn, date_text, tables):
    """رقم سند الورقة الافتراضي: آخر سند لهذه التشغيلة في اليوم نفسه (الورقة الواحدة تحمل عدة أسطر)
    وإلا الرقم التالي المقترح."""
    d = _as_date(date_text)
    for t, col in tables:
        r = db.one(f"SELECT doc_no FROM {t} WHERE batch_no=? AND {col}=? AND doc_no LIKE ? "
                   f"ORDER BY id DESC LIMIT 1", (bn, d.isoformat(), prefix + '-%'))
        if r and r['doc_no']:
            return r['doc_no']
    return next_doc_no(prefix, date_text=d.isoformat())


def require_fields(f, *names):
    """يعيد قائمة أسماء الحقول الإلزامية الناقصة."""
    return [n for n in names if not s(f.get(n))]


FIELD_AR = {'fdate': 'التاريخ', 'shift': 'الوردية', 'operator': 'اسم المشغّل',
            'qty_good': 'الكمية السليمة', 'sdate': 'التاريخ', 'pdate': 'التاريخ'}


def qc_gate(area, bn, line):
    """ربط الإنتاج بإفراج الخط. يعيد True إذا مُنع التسجيل (والرسالة في flash)."""
    mode = db.setting('qc_gate_mode', 'warn')
    if mode == 'off':
        return False
    q = """SELECT 1 FROM qc_records r JOIN qc_templates t ON t.code=r.template_code
           WHERE t.kind='release' AND t.area=? AND r.batch_no=? AND r.decision='مفرج'"""
    args = [area, bn]
    if line:
        q += " AND (r.line_code=? OR r.line_code IS NULL)"
        args.append(line)
    if db.one(q + " LIMIT 1", tuple(args)):
        return False
    msg = 'لا يوجد إفراج جودة مسجّل لهذا الخط والتشغيلة — سجّل «إفراج» من قسم الجودة قبل التشغيل'
    if mode == 'block':
        flash(msg, 'bad')
        return True
    flash('تنبيه: ' + msg, 'warn')
    return False


# ---------------------------------------------------------------- الفرز
@app.route('/sorting', methods=['GET','POST'])
def sorting():
    bn = (request.args.get('b') or request.form.get('batch_no') or '').strip()
    if request.method == 'POST':
        auth.need('enter')
        f = request.form
        bn = s(f.get('batch_no')) or ''
        manual_doc = (s(f.get('doc_no')) or '').upper() or None
        carton_code = s(f.get('carton_code'))
        if not bn or not carton_code:
            flash('رقم التشغيلة وكرتونة المصدر بيانات إلزامية', 'bad')
            return redirect(url_for('sorting', b=bn))
        if not prod_batch_or_none(bn):
            flash('رقم التشغيلة غير موجود أو أنه أمر تقطيع', 'bad')
            return redirect(url_for('sorting'))
        miss = require_fields(f, 'sdate', 'shift', 'operator')
        if miss:
            flash('حقول إلزامية ناقصة: ' + '، '.join(FIELD_AR[m] for m in miss), 'bad')
            return redirect(url_for('sorting', b=bn))
        if manual_doc and db.one('SELECT 1 FROM sorting WHERE doc_no=?', (manual_doc,)):
            flash(f'رقم سند الفرز {manual_doc} مستخدم مسبقًا', 'bad')
            return redirect(url_for('sorting', b=bn))
        carton = db.one('SELECT * FROM cartons WHERE carton_code=?', (carton_code,))
        if not carton:
            flash('كرتونة المصدر غير موجودة', 'bad')
            return redirect(url_for('sorting', b=bn))
        if carton.get('batch_no') != bn:
            flash(f'الكرتونة {carton_code} تخص التشغيلة {carton.get("batch_no")} وليس {bn}', 'bad')
            return redirect(url_for('sorting', b=bn))

        used_before = db.one('SELECT IFNULL(SUM(qty_in),0) n FROM sorting WHERE carton_code=?',
                             (carton_code,))['n'] or 0
        remaining = max((carton.get('qty') or 0) - used_before, 0)
        qty_in = num(f.get('qty_in'), remaining) or 0
        try:
            per_group = int(float(f.get('per_group') or 0))
        except (TypeError, ValueError):
            per_group = 0
        scrap = num(f.get('scrap'), 0) or 0
        if qty_in <= 0 or per_group <= 0:
            flash('الكمية الداخلة وعدد القطع في المجموعة يجب أن يكونا أكبر من صفر', 'bad')
            return redirect(url_for('sorting', b=bn))
        if qty_in - remaining > 0.0001:
            flash(f'الكمية المطلوبة ({qty_in:g}) تتجاوز المتبقي في الكرتونة ({remaining:g})', 'bad')
            return redirect(url_for('sorting', b=bn))
        if scrap < 0 or scrap > qty_in:
            flash('كمية التالف غير صحيحة', 'bad')
            return redirect(url_for('sorting', b=bn))

        usable = qty_in - scrap
        groups = int(usable // per_group)
        pieces_used = groups * per_group
        variance = round(qty_in - pieces_used - scrap, 4)
        used_after = used_before + qty_in
        new_status = 'مستهلكة بالفرز' if used_after >= (carton.get('qty') or 0) - .0001 else 'متاحة جزئيًا'
        sdate = s(f.get('sdate'))
        scope, fmt = doc_scope('SORT', sdate)
        bw_ = forms.batch(bn) or {}
        auth.need_line(mfg.route_of_wo(bw_))
        try:
          with db.tx() as con:
            doc_no = db.use_number(con, manual_doc, scope, fmt)
            con.execute("""INSERT INTO sorting(doc_no,sdate,shift,batch_no,carton_code,item_code,
                      qty_in,per_group,groups,pieces_used,scrap,variance,operator,emp_id,count_review,notes)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (doc_no, sdate, s(f.get('shift')), bn, carton_code,
                    carton.get('item_code'), qty_in, per_group, groups, pieces_used, scrap, variance,
                    s(f.get('operator')), s(f.get('emp_id')), s(f.get('count_review')), s(f.get('notes'))))
            con.execute('UPDATE cartons SET status=?, consumed_date=? WHERE carton_code=?',
                        (new_status, sdate, carton_code))
            con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            if mfg.route_of_wo(bw_) == mfg.SP:        # مسار SP: قيود مخزون حقيقية
                if pieces_used:
                    inventory.move(con, bn, 'ISSUED', bn, 'SORTED', pieces_used, 'قطعة', bn, mfg.SP, 'proc', doc_no)
                if scrap:
                    inventory.move(con, bn, 'ISSUED', bn, 'SCRAP', scrap, 'قطعة', bn, mfg.SP, 'scrap', doc_no)
        except inventory.InsufficientStock as e:
            flash(str(e), 'bad')
            return redirect(url_for('sorting', b=bn))
        db.log('create','sorting',doc_no, f'{carton_code}; groups={groups}; variance={variance}')
        if variance:
            flash(f'تم تسجيل سند الفرز {doc_no}. يوجد متبقي غير مكتمل مجموعة: {variance:g} قطعة.', 'warn')
        else:
            flash(f'تم تسجيل سند الفرز {doc_no} — {groups:g} مجموعة', 'ok')
        return redirect(url_for('sorting', b=bn))

    b = prod_batch_or_none(bn)
    cartons = []
    rows = []
    if bn and b:
        cartons = db.q("""SELECT c.*,
                         IFNULL((SELECT SUM(s.qty_in) FROM sorting s WHERE s.carton_code=c.carton_code),0) used_qty,
                         c.qty-IFNULL((SELECT SUM(s.qty_in) FROM sorting s WHERE s.carton_code=c.carton_code),0) remaining_qty
                         FROM cartons c WHERE c.batch_no=?
                         AND c.qty-IFNULL((SELECT SUM(s.qty_in) FROM sorting s WHERE s.carton_code=c.carton_code),0) > 0.0001
                         ORDER BY c.cdate, c.carton_code""", (bn,))
        rows = db.q('SELECT * FROM sorting WHERE batch_no=? ORDER BY sdate, doc_no', (bn,))
    return render_template('sorting.html', nav='sorting', bn=bn if b else '', b=b,
                           batches=forms.batches(kind='إنتاج'), cartons=cartons, rows=rows,
                           next_doc=next_doc_no('SORT','sorting','doc_no'))

# ---------------------------------------------------------------- التغليف
@app.route('/packaging', methods=['GET','POST'])
def packaging():
    bn = (request.args.get('b') or request.form.get('batch_no') or '').strip()
    if request.method == 'POST':
        auth.need('enter')
        f = request.form
        bn = s(f.get('batch_no')) or ''
        sort_doc = s(f.get('sort_doc_no'))
        manual_doc = (s(f.get('doc_no')) or '').upper() or None
        if not bn or not sort_doc:
            flash('رقم التشغيلة وسند الفرز بيانات إلزامية', 'bad')
            return redirect(url_for('packaging', b=bn))
        if not prod_batch_or_none(bn):
            flash('رقم التشغيلة غير موجود أو أنه أمر تقطيع', 'bad')
            return redirect(url_for('packaging'))
        miss = require_fields(f, 'pdate', 'shift', 'operator')
        if miss:
            flash('حقول إلزامية ناقصة: ' + '، '.join(FIELD_AR[m] for m in miss), 'bad')
            return redirect(url_for('packaging', b=bn))
        if manual_doc and db.one('SELECT 1 FROM packaging WHERE doc_no=?', (manual_doc,)):
            flash(f'رقم سند التغليف {manual_doc} مستخدم مسبقًا', 'bad')
            return redirect(url_for('packaging', b=bn))
        sr = db.one('SELECT * FROM sorting WHERE doc_no=?', (sort_doc,))
        if not sr:
            flash('سند الفرز غير موجود', 'bad')
            return redirect(url_for('packaging', b=bn))
        if sr.get('batch_no') != bn:
            flash(f'سند الفرز {sort_doc} يخص التشغيلة {sr.get("batch_no")} وليس {bn}', 'bad')
            return redirect(url_for('packaging', b=bn))

        used_before = db.one('SELECT IFNULL(SUM(groups_in),0) n FROM packaging WHERE sort_doc_no=?',
                             (sort_doc,))['n'] or 0
        remaining_groups = max((sr.get('groups') or 0) - used_before, 0)
        groups_in = num(f.get('groups_in'), remaining_groups) or 0
        if groups_in <= 0 or groups_in - remaining_groups > .0001:
            flash(f'عدد المجموعات غير صحيح. المتاح من سند الفرز {remaining_groups:g} مجموعة', 'bad')
            return redirect(url_for('packaging', b=bn))
        try:
            per_env = int(float(f.get('per_envelope') or sr.get('per_group') or 0))
        except (TypeError, ValueError):
            per_env = 0
        if per_env <= 0:
            flash('عدد المسحات في المغلف يجب أن يكون أكبر من صفر', 'bad')
            return redirect(url_for('packaging', b=bn))
        if sr.get('per_group') and per_env != sr.get('per_group'):
            flash(f'عدد المسحات في المغلف ({per_env}) لا يطابق حجم مجموعة الفرز ({sr.get("per_group")})', 'bad')
            return redirect(url_for('packaging', b=bn))

        env_good = num(f.get('env_good'), 0) or 0
        env_scrap = num(f.get('env_scrap'), 0) or 0
        if env_good < 0 or env_scrap < 0 or env_good + env_scrap - groups_in > .0001:
            flash('مجموع المغلفات السليمة والتالفة لا يجوز أن يتجاوز المجموعات الداخلة', 'bad')
            return redirect(url_for('packaging', b=bn))

        def recon_text(diff, label=''):
            if abs(diff) <= .0001:
                return 'مطابق'
            return f'فارق {label}: {diff:+g}' if label else f'فارق: {diff:+g}'

        film_issued = num(f.get('film_issued'), 0) or 0
        film_used = num(f.get('film_used'), 0) or 0
        film_waste = num(f.get('film_waste'), 0) or 0
        film_returned = num(f.get('film_returned'), 0) or 0
        film_any = any(s(f.get(k)) for k in ('film_issued','film_used','film_waste','film_returned'))
        film_recon = recon_text(film_issued-film_used-film_waste-film_returned) if film_any else 'غير مطلوب'

        env_packed = num(f.get('env_packed'), env_good) or 0
        try:
            env_per_box = int(float(f.get('env_per_box') or 0))
            boxes_per_carton = int(float(f.get('boxes_per_carton') or 0))
        except (TypeError, ValueError):
            env_per_box = boxes_per_carton = 0
        import math
        boxes = math.ceil(env_packed/env_per_box) if env_packed and env_per_box else 0
        cartons = math.ceil(boxes/boxes_per_carton) if boxes and boxes_per_carton else 0
        boxes_issued = num(f.get('boxes_issued'), boxes) or 0
        boxes_scrap = num(f.get('boxes_scrap'), 0) or 0
        cartons_issued = num(f.get('cartons_issued'), cartons) or 0
        cartons_scrap = num(f.get('cartons_scrap'), 0) or 0

        env_recon = recon_text(groups_in-env_good-env_scrap)
        bdiff = boxes_issued-boxes-boxes_scrap
        cdiff = cartons_issued-cartons-cartons_scrap
        box_recon = 'مطابق' if abs(bdiff) <= .0001 and abs(cdiff) <= .0001 \
                    else f'فارق بوكس {bdiff:+g} / كرتون {cdiff:+g}'
        seal_check = s(f.get('seal_check')) or 'معلّق'
        code_verify = s(f.get('code_verify')) or 'معلّق'
        required_recon = (env_recon == 'مطابق' and box_recon == 'مطابق' and
                          film_recon in ('مطابق','غير مطلوب') and seal_check == 'مطابق' and code_verify == 'مطابق')
        doc_status = 'مكتمل' if required_recon else 'قيد المراجعة'
        w = forms.batch(bn) or {}
        pdate = s(f.get('pdate'))
        scope, fmt = doc_scope('PKG', pdate)
        auth.need_line(mfg.route_of_wo(w))

        try:
         with db.tx() as con:
            doc_no = db.use_number(con, manual_doc, scope, fmt)
            con.execute("""INSERT INTO packaging(doc_no,pdate,shift,batch_no,machine_batch_no,item_code,size,ply,
                      per_envelope,sort_doc_no,groups_in,film_code,film_lot,box_code,box_lot,mb_code,mb_lot,
                      operator,emp_id,env_good,env_scrap,film_issued,film_used,film_waste,film_returned,film_recon,
                      seal_check,code_verify,filled_date,env_packed,env_per_box,boxes,boxes_per_carton,cartons,
                      boxes_issued,boxes_scrap,cartons_issued,cartons_scrap,env_recon,box_recon,doc_status,notes)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (doc_no, pdate, s(f.get('shift')), bn, w.get('machine_batch_no'),
                    w.get('item_code') or sr.get('item_code'), w.get('size'), w.get('ply'), per_env, sort_doc, groups_in,
                    s(f.get('film_code')), s(f.get('film_lot')), s(f.get('box_code')), s(f.get('box_lot')),
                    s(f.get('mb_code')), s(f.get('mb_lot')), s(f.get('operator')), s(f.get('emp_id')),
                    env_good, env_scrap, film_issued, film_used, film_waste, film_returned, film_recon,
                    seal_check, code_verify, s(f.get('filled_date')) or pdate, env_packed, env_per_box,
                    boxes, boxes_per_carton, cartons, boxes_issued, boxes_scrap, cartons_issued, cartons_scrap,
                    env_recon, box_recon, doc_status, s(f.get('notes'))))
            con.execute("UPDATE work_orders SET status='قيد التنفيذ' WHERE batch_no=? AND status='صادر'", (bn,))
            if mfg.route_of_wo(w) == mfg.SP:          # مسار SP: مجموعات الفرز ← مغلفات (WIP) / مرفوض
                if env_good:
                    inventory.move(con, bn, 'SORTED', bn, 'PACKED', env_good * per_env, 'قطعة', bn, mfg.SP, 'proc', doc_no)
                if env_scrap:
                    inventory.move(con, bn, 'SORTED', bn, 'REJECT', env_scrap * per_env, 'قطعة', bn, mfg.SP, 'reject', doc_no)
        except inventory.InsufficientStock as e:
            flash(str(e), 'bad')
            return redirect(url_for('packaging', b=bn))
        db.log('create','packaging',doc_no, f'{sort_doc}; groups={groups_in}; status={doc_status}')
        if doc_status == 'مكتمل':
            flash(f'تم إقفال سند التغليف {doc_no} — المطابقات سليمة', 'ok')
        else:
            flash(f'تم حفظ سند التغليف {doc_no} بحالة «قيد المراجعة» — راجع المطابقات والفحوص', 'warn')
        return redirect(url_for('packaging', b=bn))

    b = prod_batch_or_none(bn)
    sorts = []
    rows = []
    if bn and b:
        sorts = db.q("""SELECT s.*,
                         IFNULL((SELECT SUM(p.groups_in) FROM packaging p WHERE p.sort_doc_no=s.doc_no),0) used_groups,
                         s.groups-IFNULL((SELECT SUM(p.groups_in) FROM packaging p WHERE p.sort_doc_no=s.doc_no),0) remaining_groups
                         FROM sorting s WHERE s.batch_no=?
                         AND s.groups-IFNULL((SELECT SUM(p.groups_in) FROM packaging p WHERE p.sort_doc_no=s.doc_no),0) > .0001
                         ORDER BY s.sdate, s.doc_no""", (bn,))
        rows = db.q('SELECT * FROM packaging WHERE batch_no=? ORDER BY pdate, doc_no', (bn,))
    mats = db.q("SELECT item_code,prefix,description FROM items WHERE prefix IN ('PK','BX','MB') ORDER BY prefix,item_code")
    return render_template('packaging.html', nav='packaging', bn=bn if b else '', b=b,
                           batches=forms.batches(kind='إنتاج'), sorts=sorts, rows=rows, mats=mats,
                           next_doc=next_doc_no('PKG','packaging','doc_no'))

# ---------------------------------------------------------------- التعقيم والإفراج
@app.route('/cycles')
def cycles():
    rows = db.q("""SELECT c.*,
                     (SELECT COUNT(*) FROM cycle_loads l WHERE l.cycle_no=c.cycle_no) loads,
                     (SELECT COUNT(DISTINCT batch_no) FROM cycle_loads l WHERE l.cycle_no=c.cycle_no) batches,
                     (SELECT IFNULL(SUM(boxes_in),0) FROM cycle_loads l WHERE l.cycle_no=c.cycle_no) bin,
                     (SELECT IFNULL(SUM(boxes_out),0) FROM cycle_loads l WHERE l.cycle_no=c.cycle_no) bout,
                     a.status aer
                   FROM cycles c LEFT JOIN aeration a ON a.cycle_no=c.cycle_no
                   ORDER BY c.cycle_date DESC, c.cycle_no DESC LIMIT 100""")
    return render_template('cycles.html', nav='cycles', rows=rows)

@app.route('/cycles/new', methods=['GET','POST'])
def cycle_new():
    if request.method == 'POST':
        auth.need('enter')
        f = request.form
        cycle_date = s(f.get('cycle_date')) or datetime.date.today().isoformat()
        manual_no = (s(f.get('cycle_no')) or '').upper() or None
        selected = f.getlist('pack_doc_no')
        if not selected:
            flash('اختر سند تغليف واحدًا على الأقل لتحميل الدورة', 'bad')
            return redirect(url_for('cycle_new'))
        if not s(f.get('operator')):
            flash('اسم مشغّل التعقيم إلزامي', 'bad')
            return redirect(url_for('cycle_new'))
        if manual_no and db.one('SELECT 1 FROM cycles WHERE cycle_no=?', (manual_no,)):
            flash(f'رقم الدورة {manual_no} مستخدم مسبقًا', 'bad')
            return redirect(url_for('cycle_new'))

        loads = []
        for doc in selected:
            p = db.one("""SELECT p.*, w.route FROM packaging p
                           LEFT JOIN work_orders w ON w.batch_no=p.batch_no
                           WHERE p.doc_no=?""", (doc,))
            if not p or p.get('route') != 'معقم' or p.get('doc_status') != 'مكتمل':
                flash(f'سند التغليف {doc} غير مؤهل للتعقيم', 'bad')
                return redirect(url_for('cycle_new'))
            if not quality.pack_release_ok(p['batch_no']):
                mode = db.setting('qc_gate_mode', 'warn')
                msg = f'التشغيلة {p["batch_no"]} بلا إفراج ضمان الجودة للتعقيم (QA-PKS-REL)'
                if mode == 'block':
                    flash(msg + ' — لا يجوز تحميلها في الدورة', 'bad')
                    return redirect(url_for('cycle_new'))
                if mode == 'warn':
                    flash('تنبيه: ' + msg, 'warn')
            used = db.one('SELECT IFNULL(SUM(boxes_in),0) n FROM cycle_loads WHERE pack_doc_no=?', (doc,))['n'] or 0
            remaining = max((p.get('boxes') or 0) - used, 0)
            boxes = num(f.get(f'boxes_{doc}'), remaining) or 0
            cartons = num(f.get(f'cartons_{doc}'), 0) or 0
            if boxes <= 0 or boxes - remaining > .0001:
                flash(f'كمية البوكسات لسند {doc} غير صحيحة. المتبقي {remaining:g}', 'bad')
                return redirect(url_for('cycle_new'))
            if cartons < 0:
                flash(f'عدد الكراتين في سند {doc} غير صحيح', 'bad')
                return redirect(url_for('cycle_new'))
            loads.append((p, boxes, cartons, s(f.get(f'position_{doc}'))))

        d = _as_date(cycle_date)
        start_time = s(f.get('start_time'))
        scope, fmt = cycle_scope(cycle_date)
        with db.tx() as con:
            cycle_no = db.use_number(con, manual_no, scope, fmt)
            seq = int(cycle_no.rsplit('-', 1)[-1]) if cycle_no.rsplit('-', 1)[-1].isdigit() else None
            con.execute("""INSERT INTO cycles(cycle_no,cdate,month_code,yy,dd,seq,cycle_date,machine_code,
                      operator,emp_id,start_time,machine_report_no,gas_lot,load_pattern,bi_count,status,notes)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (cycle_no, cycle_date, MONTHS[d.month-1], int(d.strftime('%y')), d.day, seq,
                    cycle_date, s(f.get('machine_code')) or 'EO-01', s(f.get('operator')), s(f.get('emp_id')),
                    start_time, s(f.get('machine_report_no')), s(f.get('gas_lot')), s(f.get('load_pattern')),
                    int(num(f.get('bi_count'), 0) or 0), constants.CYC_RUNNING, s(f.get('notes'))))
            for p, boxes, cartons, pos in loads:
                con.execute("""INSERT INTO cycle_loads(cycle_no,pack_doc_no,batch_no,item_code,size,ply,
                          boxes_in,cartons_in,position,pack_status_at_load,notes)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (cycle_no, p.get('doc_no'), p.get('batch_no'), p.get('item_code'), p.get('size'), p.get('ply'),
                        boxes, cartons, pos, p.get('doc_status'), None))
        db.log('create','cycles',cycle_no, f'loads={len(loads)}')
        flash(f'تم إنشاء دورة التعقيم {cycle_no} وتحميل {len(loads)} سند/حمولة', 'ok')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))

    packs = db.q("""SELECT p.*, w.route,
                    IFNULL((SELECT SUM(l.boxes_in) FROM cycle_loads l WHERE l.pack_doc_no=p.doc_no),0) loaded_boxes,
                    p.boxes-IFNULL((SELECT SUM(l.boxes_in) FROM cycle_loads l WHERE l.pack_doc_no=p.doc_no),0) remaining_boxes
                    FROM packaging p JOIN work_orders w ON w.batch_no=p.batch_no
                    WHERE w.route='معقم' AND p.doc_status='مكتمل'
                    AND p.boxes-IFNULL((SELECT SUM(l.boxes_in) FROM cycle_loads l WHERE l.pack_doc_no=p.doc_no),0) > .0001
                    ORDER BY p.pdate, p.doc_no""")
    return render_template('cycle_new.html', nav='cycles', packs=packs,
                           next_cycle=next_cycle_no(), machines=db.q("SELECT * FROM machines WHERE stage='التعقيم' AND active=1"))

@app.route('/cycle/<path:cycle_no>')
def cycle_view(cycle_no):
    c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
    if not c: abort(404)
    loads = trace.cycle_contents(cycle_no)
    aer = db.one('SELECT * FROM aeration WHERE cycle_no=?', (cycle_no,))
    received = db.q('SELECT * FROM post_ster_receipts WHERE cycle_no=? ORDER BY batch_no, doc_no', (cycle_no,))
    batches = db.q("""SELECT l.batch_no, MAX(l.item_code) item_code,
                       SUM(COALESCE(l.boxes_out,l.boxes_in)) boxes,
                       SUM(COALESCE(l.cartons_in,0)) cartons,
                       MAX(CASE WHEN r.doc_no IS NOT NULL THEN 1 ELSE 0 END) received
                       FROM cycle_loads l
                       LEFT JOIN post_ster_receipts r ON r.cycle_no=l.cycle_no AND r.batch_no=l.batch_no
                       WHERE l.cycle_no=? GROUP BY l.batch_no ORDER BY l.batch_no""", (cycle_no,))
    return render_template('cycle.html', nav='cycles', c=c, loads=loads, aer=aer,
                           received=received, receive_batches=batches,
                           min_aer=db.setting('aeration_min_h','24'),
                           temp_min=db.setting('aeration_temp_min','18'),
                           temp_max=db.setting('aeration_temp_max','35'))

@app.route('/cycle/<path:cycle_no>/results', methods=['POST'])
def cycle_results(cycle_no):
    auth.need('enter')
    c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
    if not c: abort(404)
    f = request.form
    cycle_date = s(f.get('cycle_date')) or c.get('cycle_date')
    start_time = s(f.get('start_time')) or c.get('start_time')
    end_time = s(f.get('end_time'))
    end_date = s(f.get('end_date'))
    duration = duration_hours(cycle_date, start_time, end_date, end_time) if end_time else None
    if end_time and duration is None:
        flash('تعذر حساب مدة الدورة. تحقق من تاريخ ووقت البداية والنهاية.', 'bad')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))

    C = constants
    ci_ext = C.canon(s(f.get('ci_external')), C.CI_RESULTS)
    ci_int = C.canon(s(f.get('ci_internal')), C.CI_RESULTS)
    bi_res = C.canon(s(f.get('bi_result')), C.BI_RESULTS)
    ctrl_pos = C.canon(s(f.get('ctrl_pos')), (C.CTRL_POS_OK, 'موجب', C.NONCONFORM))
    ctrl_neg = C.canon(s(f.get('ctrl_neg')), (C.CTRL_NEG_OK, 'سالب', C.NONCONFORM))

    load_updates = []
    for l in db.q('SELECT * FROM cycle_loads WHERE cycle_no=?', (cycle_no,)):
        boxes_out = num(f.get(f'boxes_out_{l["id"]}'), None)
        if boxes_out is not None and (boxes_out < 0 or boxes_out - (l.get('boxes_in') or 0) > .0001):
            flash(f'الكمية الخارجة للحمولة {l.get("pack_doc_no")} غير صحيحة', 'bad')
            return redirect(url_for('cycle_view', cycle_no=cycle_no))
        var = round((boxes_out or 0) - (l.get('boxes_in') or 0), 4) if boxes_out is not None else None
        load_updates.append((boxes_out, var, C.canon(s(f.get(f'ci_load_{l["id"]}')), C.CI_RESULTS),
                             s(f.get(f'load_notes_{l["id"]}')), l['id']))

    with db.tx() as con:
        for lu in load_updates:
            con.execute('UPDATE cycle_loads SET boxes_out=?, variance=?, ci_load=?, notes=? WHERE id=?', lu)
        con.execute("""UPDATE cycles SET cycle_date=?, machine_code=?, operator=?, emp_id=?, start_time=?, end_time=?,
                  duration_h=?, machine_report_no=?, gas_lot=?, load_pattern=?, ci_external=?, ci_internal=?,
                  bi_count=?, bi_start=?, bi_end=?, bi_result=?, ctrl_pos=?, ctrl_neg=?, notes=? WHERE cycle_no=?""",
               (cycle_date, s(f.get('machine_code')) or c.get('machine_code'), s(f.get('operator')),
                s(f.get('emp_id')), start_time, end_time, duration, s(f.get('machine_report_no')),
                s(f.get('gas_lot')), s(f.get('load_pattern')), ci_ext, ci_int,
                int(num(f.get('bi_count'), 0) or 0), s(f.get('bi_start')), s(f.get('bi_end')),
                bi_res, ctrl_pos, ctrl_neg, s(f.get('notes')), cycle_no))
    state = cycle_state(cycle_no)     # بعد تثبيت التعامل حتى يقرأ القيم المحدّثة
    db.run('UPDATE cycles SET status=? WHERE cycle_no=?', (state, cycle_no))
    db.log('update','cycles',cycle_no, f'status={state}')
    flash(f'تم تحديث نتائج دورة {cycle_no} — الحالة: {state}',
          'ok' if state == C.CYC_PASSED else ('bad' if state == C.CYC_REJECTED else 'warn'))
    return redirect(url_for('cycle_view', cycle_no=cycle_no))

@app.route('/cycle/<path:cycle_no>/aeration', methods=['POST'])
def aeration_save(cycle_no):
    auth.need('enter')
    c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
    if not c: abort(404)
    f = request.form
    in_date = s(f.get('in_date'))
    in_time = s(f.get('in_time'))
    out_date = s(f.get('out_date'))
    out_time = s(f.get('out_time'))
    duration = duration_hours(in_date, in_time, out_date, out_time)
    min_h = num(f.get('min_required_h'), num(db.setting('aeration_min_h','24'),24)) or 24
    temp_min = num(db.setting('aeration_temp_min','18'),18) or 18
    temp_max = num(db.setting('aeration_temp_max','35'),35) or 35
    ti = num(f.get('temp_in'))
    to = num(f.get('temp_out'))
    dur_eval = 'مطابق' if duration is not None and duration >= min_h else 'غير مطابق — مدة أقل من المعتمد'
    temp_ok = ti is not None and to is not None and temp_min <= ti <= temp_max and temp_min <= to <= temp_max
    temp_eval = 'ضمن المدى' if temp_ok else 'غير مطابق — الحرارة خارج المدى'
    if c.get('status') == 'مرفوضة':
        status = 'موقوفة — الدورة مرفوضة'
    elif dur_eval == 'مطابق' and temp_ok and c.get('status') in ('اجتازت التعقيم','مكتملة'):
        status = 'مكتملة — جاهزة للاستلام'
    elif dur_eval == 'مطابق' and temp_ok:
        status = 'مكتملة زمنيًا — بانتظار BI'
    else:
        status = 'قيد المراجعة'
    cartons = db.one('SELECT IFNULL(SUM(cartons_in),0) n FROM cycle_loads WHERE cycle_no=?', (cycle_no,))['n'] or 0
    db.run("""INSERT OR REPLACE INTO aeration(cycle_no,ster_date,out_machine_time,in_date,in_time,out_date,out_time,
              duration_h,min_required_h,delta_h,duration_eval,temp_in,temp_out,temp_eval,forced,cartons,
              supervisor,emp_id,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
           (cycle_no, c.get('cycle_date'), c.get('end_time'), in_date, in_time, out_date, out_time,
            duration, min_h, round(duration-min_h,2) if duration is not None else None, dur_eval,
            ti, to, temp_eval, s(f.get('forced')) or 'لا', cartons, s(f.get('supervisor')),
            s(f.get('emp_id')), status, s(f.get('notes'))))
    db.log('update','aeration',cycle_no, f'status={status}; duration={duration}')
    flash(f'تم حفظ سجل التهوية — {status}', 'ok' if status=='مكتملة — جاهزة للاستلام' else ('bad' if status.startswith('موقوفة') else 'warn'))
    return redirect(url_for('cycle_view', cycle_no=cycle_no))

@app.route('/cycle/<path:cycle_no>/receive', methods=['POST'])
def post_ster_receive(cycle_no):
    auth.need('enter')
    c = db.one('SELECT * FROM cycles WHERE cycle_no=?', (cycle_no,))
    aer = db.one('SELECT * FROM aeration WHERE cycle_no=?', (cycle_no,))
    if not c or not aer: abort(404)
    if aer.get('status') != 'مكتملة — جاهزة للاستلام' or c.get('status') not in ('اجتازت التعقيم','مكتملة'):
        flash('لا يمكن استلام المنتج بعد التعقيم قبل اجتياز الدورة واكتمال التهوية', 'bad')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))
    f = request.form
    bn = s(f.get('batch_no'))
    if not bn:
        flash('اختر رقم التشغيلة', 'bad')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))
    if db.one('SELECT 1 FROM post_ster_receipts WHERE cycle_no=? AND batch_no=?', (cycle_no,bn)):
        flash('تم تسجيل استلام هذه التشغيلة من هذه الدورة مسبقًا', 'warn')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))
    agg = db.one("""SELECT MAX(item_code) item_code, SUM(COALESCE(boxes_out,boxes_in)) boxes,
                    SUM(COALESCE(cartons_in,0)) cartons FROM cycle_loads
                    WHERE cycle_no=? AND batch_no=?""", (cycle_no,bn))
    if not agg or not agg.get('item_code'):
        flash('التشغيلة ليست ضمن حمولة هذه الدورة', 'bad')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))
    rdate = s(f.get('rdate')) or datetime.date.today().isoformat()
    manual_doc = (s(f.get('doc_no')) or '').upper() or None
    if manual_doc and db.one('SELECT 1 FROM post_ster_receipts WHERE doc_no=?', (manual_doc,)):
        flash(f'رقم سند الاستلام {manual_doc} مستخدم مسبقًا', 'bad')
        return redirect(url_for('cycle_view', cycle_no=cycle_no))
    boxes = num(f.get('boxes'), agg.get('boxes')) or 0
    cartons = num(f.get('cartons'), agg.get('cartons')) or 0
    manual_qc = s(f.get('quarantine_card'))
    dscope, dfmt = doc_scope('PSR', rdate)
    qscope, qfmt = doc_scope('QRT', rdate)
    with db.tx() as con:
        doc_no = db.use_number(con, manual_doc, dscope, dfmt)
        qcard = db.use_number(con, manual_qc, qscope, qfmt)
        con.execute("""INSERT INTO post_ster_receipts(doc_no,rdate,cycle_no,batch_no,item_code,boxes,cartons,qc_location,
                  stock_status,aeration_status,handed_by,received_by,quarantine_card,final_status,notes)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (doc_no, rdate, cycle_no, bn, agg.get('item_code'), boxes, cartons, s(f.get('qc_location')),
                'حجر', aer.get('status'), s(f.get('handed_by')), s(f.get('received_by')), qcard,
                'بانتظار الإفراج', s(f.get('notes'))))
    db.log('create','post_ster_receipts',doc_no, f'{cycle_no}; {bn}; boxes={boxes}')
    flash(f'تم استلام التشغيلة {bn} بعد التعقيم ووضعها في الحجر حتى الإفراج النهائي', 'ok')
    return redirect(url_for('cycle_view', cycle_no=cycle_no))

# ---------------------------------------------------------------- شهادات الإفراج
def _release_candidates():
    """التشغيلات التي اجتازت التعقيم والتهوية واستُلمت في الحجر ولم تصدر لها شهادة بعد."""
    return db.q("""
        SELECT DISTINCT w.batch_no, w.item_code, w.size, w.ply, w.xray, w.mesh, w.wo_no,
               p.cycle_no,
               (SELECT MIN(cy.cycle_date) FROM cycles cy WHERE cy.cycle_no=p.cycle_no) ster_date,
               (SELECT IFNULL(SUM(x.boxes),0) FROM post_ster_receipts x WHERE x.batch_no=w.batch_no) qty_boxes,
               (SELECT IFNULL(SUM(x.cartons),0) FROM post_ster_receipts x WHERE x.batch_no=w.batch_no) cartons
        FROM work_orders w
        JOIN post_ster_receipts p ON p.batch_no = w.batch_no
        JOIN cycles c ON c.cycle_no = p.cycle_no
        JOIN aeration a ON a.cycle_no = p.cycle_no
        WHERE c.status IN (?, 'مكتملة')
          AND a.status = ?
          AND w.batch_no NOT IN (SELECT batch_no FROM releases WHERE batch_no IS NOT NULL)
        ORDER BY w.batch_no
    """, (constants.CYC_PASSED, constants.AER_READY))


@app.route('/releases')
def releases():
    rows = db.q('SELECT * FROM releases ORDER BY issue_date DESC, release_no DESC LIMIT 200')
    return render_template('releases.html', nav='rel', rows=rows,
                           pending=_release_candidates() if auth.can('qc_sign') else [])


REL_CHECKS = [(1, 'فحص المواد الواردة'), (2, 'مطابقة كميات الإنتاج'), (3, 'الفحص أثناء التشغيل'),
              (4, 'سند التغليف'), (5, 'بارامترات دورة التعقيم'), (6, 'المؤشر الكيميائي'),
              (7, 'المؤشر البيولوجي'), (8, 'التهوية'), (9, 'متبقيات EO و ECH')]


@app.route('/releases/new', methods=['GET', 'POST'])
@auth.require('qc_sign')
def release_new():
    batch = (request.args.get('b') or request.form.get('batch_no') or '').strip()
    cand = {c['batch_no']: c for c in _release_candidates()}

    if request.method == 'POST':
        if batch not in cand:
            flash('هذه التشغيلة غير مؤهلة لإصدار شهادة إفراج (لم تكتمل خطواتها أو صدرت لها شهادة)', 'bad')
            return redirect(url_for('releases'))
        f = request.form
        decision = constants.canon(s(f.get('decision')), constants.REL_DECISIONS)
        if decision not in constants.REL_DECISIONS:
            flash('اختر قرار الإفراج', 'bad')
            return redirect(url_for('release_new', b=batch))
        checks = {i: constants.canon(s(f.get(f'r{i}')), constants.CI_RESULTS) or s(f.get(f'r{i}'))
                  for i, _ in REL_CHECKS}
        if decision == constants.REL_RELEASE and ncr.open_for_batch(batch):
            flash('لا يجوز الإفراج مع عدم مطابقة مفتوحة: ' + '، '.join(x['dev_no'] for x in ncr.open_for_batch(batch)), 'bad')
            return redirect(url_for('release_new', b=batch))
        if decision == constants.REL_RELEASE and any(constants.is_nonconform(v) for v in checks.values()):
            flash('لا يجوز الإفراج مع وجود بند غير مطابق — صحّح القرار أو البند', 'bad')
            return redirect(url_for('release_new', b=batch))
        if not auth.check_esign(f.get('esign_pw')):
            flash('التوقيع الإلكتروني غير صحيح — أعد إدخال كلمة مرورك لإصدار الشهادة', 'bad')
            return redirect(url_for('release_new', b=batch))

        c = cand[batch]
        issue_date = s(f.get('issue_date')) or datetime.date.today().isoformat()
        ster_date = c.get('ster_date')
        try:
            shelf_m = int(num(f.get('shelf_life_m'), num(db.setting('shelf_life_m', '60'), 60)) or 60)
        except (TypeError, ValueError):
            shelf_m = 60
        expiry = None
        if ster_date:
            try:
                d0 = datetime.date.fromisoformat(ster_date)
                y, m = divmod((d0.month - 1) + shelf_m, 12)
                expiry = d0.replace(year=d0.year + y, month=m + 1).isoformat()
            except (ValueError, TypeError):
                expiry = None
        scope, fmt = doc_scope('REL', issue_date)
        with db.tx() as con:
            release_no = db.alloc(con, scope, fmt)
            con.execute("""INSERT INTO releases(release_no,issue_date,batch_no,item_code,size,ply,xray,mesh,
                      wo_no,cycle_no,ster_date,shelf_life_m,expiry_date,qty_boxes,cartons,
                      r1,r2,r3,r4,r5,r6,r7,r8,r9,residue_report_no,decision,qa_officer,sign_date,notes)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (release_no, issue_date, batch, c.get('item_code'), c.get('size'), c.get('ply'),
                    c.get('xray'), c.get('mesh'), c.get('wo_no'), c.get('cycle_no'), ster_date,
                    shelf_m, expiry, num(f.get('qty_boxes'), c.get('qty_boxes')),
                    num(f.get('cartons'), c.get('cartons')),
                    checks[1], checks[2], checks[3], checks[4], checks[5], checks[6], checks[7],
                    checks[8], checks[9], s(f.get('residue_report_no')), decision,
                    g.user['full_name'], issue_date, s(f.get('notes'))))
            new_stock = constants.REL_TO_PSR[decision]
            con.execute("""UPDATE post_ster_receipts SET release_no=?, release_date=?, final_status=?, stock_status=?
                      WHERE batch_no=?""", (release_no, issue_date, decision, new_stock, batch))
            con.execute("UPDATE work_orders SET status=? WHERE batch_no=?", (constants.WO_DONE, batch))
            con.execute(*auth.signature_row(constants.SIG_RELEASE, 'releases', release_no))
            if decision == constants.REL_RELEASE:
                notify.push(con, 'ready_wh', f'تم الإفراج النهائي للتشغيلة {batch} وأصبحت جاهزة للمخزن',
                            f'{c.get("item_code")} — شهادة {release_no}', url_for('warehouse'), batch,
                            mfg.route_of_wo(db.one('SELECT route_code FROM work_orders WHERE batch_no=?', (batch,))),
                            ('warehouse', 'wo_issue'))
        db.log('release', 'releases', release_no, f'{batch}: {decision}')
        flash(f'صدرت شهادة الإفراج {release_no} — القرار: {decision}',
              'ok' if decision == constants.REL_RELEASE else 'bad')
        return redirect(url_for('trace_view', b=batch))

    if batch and batch not in cand:
        flash('هذه التشغيلة غير مؤهلة لإصدار شهادة إفراج', 'warn')
        batch = ''
    d = trace.batch_chain(batch) if batch else None
    return render_template('release_new.html', nav='rel', checks=REL_CHECKS,
                           batch=batch, cand=cand.get(batch), pending=list(cand.values()), d=d,
                           shelf_default=db.setting('shelf_life_m', '60'))


# ---------------------------------------------------------------- إغلاق التشغيلة
@app.route('/wo/<path:batch_no>/close', methods=['POST'])
@auth.require('wo_close')
def wo_close(batch_no):
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (batch_no,))
    if not w:
        abort(404)
    new = constants.WO_CLOSED if w['status'] != constants.WO_CLOSED else constants.WO_RUNNING
    db.run('UPDATE work_orders SET status=? WHERE batch_no=?', (new, batch_no))
    db.log('close' if new == constants.WO_CLOSED else 'reopen', 'work_orders', batch_no)
    flash(f'التشغيلة {batch_no}: {new}', 'ok')
    return redirect(request.referrer or url_for('wo_list'))


# ---------------------------------------------------------------- إدارة المستخدمين
@app.route('/admin/users', methods=['GET', 'POST'])
@auth.require('admin')
def users_admin():
    if request.method == 'POST':
        f = request.form
        act = f.get('act')
        if act == 'add':
            un = (s(f.get('username')) or '').lower()
            fn = s(f.get('full_name'))
            role = f.get('role') if f.get('role') in constants.ROLES else 'viewer'
            valid_lines = {r['code'] for r in mfg.routes()}
            lines = ','.join(x for x in f.getlist('lines') if x in valid_lines)
            pw = f.get('password') or ''
            if not un or not fn or len(pw) < 6:
                flash('اسم المستخدم والاسم الكامل إلزاميان، وكلمة المرور 6 خانات على الأقل', 'bad')
            elif db.one('SELECT 1 FROM users WHERE username=?', (un,)):
                flash('اسم المستخدم مستخدم مسبقًا', 'bad')
            else:
                db.run("""INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw,lines)
                          VALUES(?,?,?,?,1,1,?)""", (un, fn, auth.hash_pw(pw), role, lines or None))
                db.log('create', 'users', un, f'role={role}')
                flash(f'أُضيف المستخدم {un}', 'ok')
        elif act == 'toggle':
            uid = num(f.get('id'))
            u = db.one('SELECT * FROM users WHERE id=?', (uid,))
            if u and u['id'] != g.user['id']:
                db.run('UPDATE users SET active=1-active WHERE id=?', (uid,))
                db.log('update', 'users', u['username'], 'active toggled')
                flash('تم التحديث', 'ok')
            else:
                flash('لا يمكنك تعطيل حسابك', 'bad')
        elif act == 'role':
            uid = num(f.get('id'))
            role = f.get('role') if f.get('role') in constants.ROLES else None
            u = db.one('SELECT * FROM users WHERE id=?', (uid,))
            if u and role and u['id'] != g.user['id']:
                db.run('UPDATE users SET role=? WHERE id=?', (role, uid))
                db.log('update', 'users', u['username'], f'role={role}')
                flash('تم تحديث الدور', 'ok')
            else:
                flash('تعذّر تحديث الدور', 'bad')
        elif act == 'lines':
            uid = num(f.get('id'))
            u = db.one('SELECT * FROM users WHERE id=?', (uid,))
            valid_lines = {r['code'] for r in mfg.routes()}
            lines = ','.join(x for x in f.getlist('lines') if x in valid_lines)
            if u:
                db.run('UPDATE users SET lines=? WHERE id=?', (lines or None, uid))
                db.log('update', 'users', u['username'], f'lines={lines or "all"}')
                flash('تم تحديث خطوط الإنتاج المسموحة', 'ok')
        elif act == 'reset':
            uid = num(f.get('id'))
            u = db.one('SELECT * FROM users WHERE id=?', (uid,))
            newpw = f.get('password') or ''
            if u and len(newpw) >= 6:
                db.run('UPDATE users SET pw_hash=?, must_change_pw=1 WHERE id=?',
                       (auth.hash_pw(newpw), uid))
                db.log('reset_password', 'users', u['username'])
                flash(f'أُعيد ضبط كلمة مرور {u["username"]} — يجب تغييرها عند أول دخول', 'ok')
            else:
                flash('كلمة المرور الجديدة قصيرة', 'bad')
        return redirect(url_for('users_admin'))
    rows = db.q('SELECT * FROM users ORDER BY active DESC, role DESC, username')
    return render_template('users.html', nav='admin', rows=rows, roles=constants.ROLES, all_routes=mfg.routes())


# ---------------------------------------------------------------- سجل التدقيق
@app.route('/admin/audit')
@auth.require('audit')
def audit_view():
    key = (request.args.get('k') or '').strip()
    if key:
        rows = db.q("""SELECT * FROM audit_log WHERE record_key LIKE ? OR details LIKE ?
                       ORDER BY id DESC LIMIT 500""", (f'%{key}%', f'%{key}%'))
    else:
        rows = db.q('SELECT * FROM audit_log ORDER BY id DESC LIMIT 300')
    sigs = db.q('SELECT * FROM signatures ORDER BY id DESC LIMIT 200')
    return render_template('audit.html', nav='admin', rows=rows, sigs=sigs, key=key)


# ---------------------------------------------------------------- النسخ الاحتياطي
@app.route('/admin/backup', methods=['GET', 'POST'])
@auth.require('admin')
def backup_admin():
    if request.method == 'POST' and request.form.get('act') == 'extra':
        path = (request.form.get('extra_dir') or '').strip()
        if path:
            ok, msg = backup.check_extra(path)
            if not ok:
                flash(f'المسار غير صالح للكتابة: {msg}', 'bad')
                return redirect(url_for('backup_admin'))
        db.run("INSERT INTO settings(key,value,note) VALUES('backup_extra_dir',?,'مجلد النسخ الاحتياطي الثاني') ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
        db.log('update', 'settings', 'backup_extra_dir', path)
        flash('حُفظ مكان النسخ الثاني — ستُنسخ إليه كل نسخة جديدة' if path else 'أُلغي المكان الثاني للنسخ', 'ok')
        return redirect(url_for('backup_admin'))
    if request.method == 'POST':
        try:
            dest = backup.make_backup('manual')
            db.log('backup', 'system', os.path.basename(dest) if dest else '?')
            flash('تم إنشاء نسخة احتياطية', 'ok')
        except Exception as e:
            import logging
            logging.getLogger('qms').exception('manual backup failed')
            flash(f'فشل إنشاء النسخة: {e}', 'bad')
        return redirect(url_for('backup_admin'))
    return render_template('backup.html', nav='admin', backups=backup.last_backups(30), folder=backup.backup_dir(),
                           extra=backup.extra_dir(), extra_last=db.setting('backup_extra_last', ''),
                           verify_last=db.setting('backup_verify_last', ''), data_dir=os.path.dirname(db.DB_PATH))


# ---------------------------------------------------------------- الجودة ولوحة المدير والتقارير
import quality, manager, ncr, logistics, lines, genealogy, master, prod, qa, settings_page, proc, printing, masterdata, monitor, importer, tracecenter, maintenance
import sp as sp_mod, bandage as bandage_mod
for _m in (lines, genealogy, master, sp_mod, bandage_mod, prod, qa, settings_page, proc, printing, masterdata, monitor, importer, tracecenter, maintenance):
    _m.register(app)
logistics.register(app)
ncr.register(app)
quality.register(app)
manager.register(app)
