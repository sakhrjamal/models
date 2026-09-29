# -*- coding: utf-8 -*-
"""الصيانة: وقائية (جدول سنوي + أوامر شهرية ببنود فحص) وطارئة (بلاغ عطل ← استلام ← إنجاز ← اعتماد) وتوقف الآلات.

نموذج الجدول (XXXQP-12.F02): الآلات السبع، أرقامها، المدة 30 يومًا، وأشهر السنة. بنود الفحص مقترحة وقابلة للتعديل.
"""
import calendar, datetime, json
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, notify
from util import s, num, fmt_qty

MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'June', 'July', 'Aug', 'Sept', 'Oct', 'Nov', 'Dec']
MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
ST_AR = {'Planned': 'مخطط', 'Reported': 'بلاغ جديد', 'In Progress': 'قيد التنفيذ', 'Completed': 'مكتمل', 'Verified': 'معتمد', 'Cancelled': 'ملغى'}
ST_CLS = {'Planned': '', 'Reported': 'bad', 'In Progress': 'warn', 'Completed': 'ok', 'Verified': 'ok', 'Cancelled': ''}
KIND_AR = {'PM': 'وقائية', 'EM': 'طارئة'}
SEV_AR = ('عالية', 'متوسطة', 'منخفضة')
RES = ('مطابق', 'غير مطابق', 'لا ينطبق')
OPEN_ST = ('Planned', 'Reported', 'In Progress')


def now_s():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def today():
    return datetime.date.today().isoformat()


def form_no(kind):
    return db.setting('maint_form_pm', '') if kind == 'PM' else db.setting('maint_form_em', '')


def _month_bounds(y, m):
    return f'{y}-{m:02d}-01', f'{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}'


def machine_down(code):
    """هل توجد صيانة طارئة مفتوحة أوقفت هذه الماكينة؟ (تحذير في شاشات الإنتاج؛ لا يمنع العمل)."""
    if not code:
        return None
    return db.one("""SELECT o.order_no, o.reported_at, e.name_ar FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                     WHERE o.kind='EM' AND o.production_stopped=1 AND o.status IN ('Reported','In Progress') AND e.machine_code=?
                     ORDER BY o.id DESC LIMIT 1""", (code,))


def ensure_month(y, m):
    """يولّد أوامر الصيانة الوقائية المستحقة لشهر (سند واحد لكل آلة فعّالة). آمن على التكرار. يعيد عدد ما أُنشئ."""
    a, b = _month_bounds(y, m)
    made = []
    with db.tx() as con:
        for e in con.execute('SELECT * FROM maint_equipment WHERE active=1 ORDER BY sr, id').fetchall():
            exists = con.execute("""SELECT 1 FROM maint_orders WHERE kind='PM' AND equip_id=? AND status<>'Cancelled'
                                    AND planned_date BETWEEN ? AND ?""", (e['id'], a, b)).fetchone()
            if exists:
                continue
            nd = e['next_due'] or a
            if nd > b:
                continue
            planned = max(nd, a)
            no = db.alloc(con, f'PM-{y}', f'PM-{y}-{{n6}}')
            con.execute("INSERT INTO maint_orders(order_no,kind,equip_id,status,planned_date,created_by) VALUES(?,?,?,?,?,?)",
                        (no, 'PM', e['id'], 'Planned', planned, 'system'))
            if not e['next_due']:
                con.execute('UPDATE maint_equipment SET next_due=? WHERE id=?', (planned, e['id']))
            made.append((no, e['name_ar']))
        if made:
            notify.push(con, 'pm_due', f'أوامر صيانة وقائية مستحقة لشهر {MONTHS_AR[m - 1]} {y}: {len(made)} آلة',
                        '، '.join(x[1] for x in made), url_for('maint_orders', kind='PM', status='open'), None, None, ('maint',), actor='system')
    return len(made)


def _kpis(y):
    t = today()
    pm_open = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM' AND status IN ('Planned','In Progress')")['n']
    overdue = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM' AND status IN ('Planned','In Progress') AND planned_date<?", (t,))['n']
    em_open = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='EM' AND status IN ('Reported','In Progress')")['n']
    planned = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM' AND status<>'Cancelled' AND planned_date BETWEEN ? AND ?", (f'{y}-01-01', min(t, f'{y}-12-31')))['n']
    done = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM' AND status IN ('Completed','Verified') AND planned_date BETWEEN ? AND ?", (f'{y}-01-01', min(t, f'{y}-12-31')))['n']
    down = db.one("SELECT IFNULL(SUM(downtime_h),0) n FROM maint_orders WHERE kind='EM' AND substr(reported_at,1,4)=?", (str(y),))['n']
    return dict(pm_open=pm_open, overdue=overdue, em_open=em_open, compliance=round(done / planned * 100) if planned else None,
                planned=planned, done=done, downtime=round(down, 1))


def _grid(y):
    rows = []
    t = today()
    for e in db.q('SELECT * FROM maint_equipment WHERE active=1 ORDER BY sr, id'):
        cells = []
        for m in range(1, 13):
            a, b = _month_bounds(y, m)
            os_ = db.q("""SELECT * FROM maint_orders WHERE kind='PM' AND equip_id=? AND status<>'Cancelled' AND planned_date BETWEEN ? AND ?
                          ORDER BY id""", (e['id'], a, b))
            if not os_:
                cells.append(dict(txt='—', cls='none', order=None))
                continue
            o = os_[0]
            day = o['planned_date'][8:10]
            if o['status'] in ('Completed', 'Verified'):
                cells.append(dict(txt=f'✔ {day}', cls='ok', order=o['order_no']))
            elif o['planned_date'] < t:
                cells.append(dict(txt=f'{day} ⚠', cls='late', order=o['order_no']))
            else:
                cells.append(dict(txt=day, cls='plan', order=o['order_no']))
        rows.append(dict(e=e, cells=cells))
    return rows


def _order_or_404(no):
    o = db.one('SELECT o.*, e.no eq_no, e.name_ar, e.name_en, e.machine_code, e.freq_days, e.location FROM maint_orders o '
               'JOIN maint_equipment e ON e.id=o.equip_id WHERE o.order_no=?', (no,))
    if not o:
        abort(404)
    return o


def _hours(a, b):
    try:
        fa = datetime.datetime.strptime(a[:16], '%Y-%m-%d %H:%M')
        fb = datetime.datetime.strptime(b[:16], '%Y-%m-%d %H:%M')
        return max(round((fb - fa).total_seconds() / 3600, 2), 0.0)
    except (TypeError, ValueError):
        return None


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    app.jinja_env.globals['machine_down'] = machine_down

    # ------------------------------------------------------------------ اللوحة
    @route('/maintenance', 'maint_home')
    def maint_home():
        n = datetime.date.today()
        ensure_month(n.year, n.month)
        k = _kpis(n.year)
        open_pm = db.q("""SELECT o.*, e.name_ar, e.no eq_no FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                          WHERE o.kind='PM' AND o.status IN ('Planned','In Progress') ORDER BY o.planned_date LIMIT 30""")
        open_em = db.q("""SELECT o.*, e.name_ar, e.no eq_no FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                          WHERE o.kind='EM' AND o.status IN ('Reported','In Progress') ORDER BY o.id DESC LIMIT 30""")
        down = db.q("""SELECT e.name_ar, e.no, COUNT(*) n, IFNULL(SUM(o.downtime_h),0) h FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                       WHERE o.kind='EM' AND substr(o.reported_at,1,4)=? GROUP BY e.id ORDER BY h DESC""", (str(n.year),))
        return render_template('maint_home.html', nav='maint', k=k, open_pm=open_pm, open_em=open_em, down=down, t=today(),
                               st_ar=ST_AR, st_cls=ST_CLS, year=n.year, fmt=fmt_qty)

    # ------------------------------------------------------------------ الجدول السنوي
    @route('/maintenance/plan', 'maint_plan')
    def maint_plan():
        y = int(request.args.get('y') or datetime.date.today().year)
        return render_template('maint_plan.html', nav='maint', year=y, grid=_grid(y), months=MONTHS_EN, months_ar=MONTHS_AR,
                               form=form_no('PM'), k=_kpis(y), cur_month=datetime.date.today().month, cur_year=datetime.date.today().year)

    @route('/maintenance/generate', 'maint_generate', methods=['POST'])
    def maint_generate():
        y, m = int(request.form.get('y') or 0), int(request.form.get('m') or 0)
        if not (2000 < y < 2100 and 1 <= m <= 12):
            abort(400)
        n = ensure_month(y, m)
        db.log('create', 'maint_orders', f'{y}-{m:02d}', f'generated {n}')
        flash(f'أُنشئ {n} أمر صيانة وقائية لشهر {MONTHS_AR[m - 1]} {y}' if n else 'لا توجد أوامر جديدة مستحقة لهذا الشهر', 'ok' if n else 'warn')
        return redirect(url_for('maint_plan', y=y))

    # ------------------------------------------------------------------ قائمة الأوامر
    @route('/maintenance/orders', 'maint_orders')
    def maint_orders():
        a = request.args
        where, args = ['1=1'], []
        if a.get('kind') in ('PM', 'EM'):
            where.append('o.kind=?'); args.append(a['kind'])
        if a.get('status') == 'open':
            where.append("o.status IN ('Planned','Reported','In Progress')")
        elif a.get('status') in ST_AR:
            where.append('o.status=?'); args.append(a['status'])
        if a.get('eq'):
            where.append('o.equip_id=?'); args.append(int(a['eq']))
        if s(a.get('dfrom')):
            where.append('COALESCE(o.planned_date,substr(o.reported_at,1,10))>=?'); args.append(a['dfrom'])
        if s(a.get('dto')):
            where.append('COALESCE(o.planned_date,substr(o.reported_at,1,10))<=?'); args.append(a['dto'])
        rows = db.q(f"""SELECT o.*, e.name_ar, e.no eq_no FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                        WHERE {' AND '.join(where)} ORDER BY o.id DESC LIMIT 400""", tuple(args))
        return render_template('maint_orders.html', nav='maint', rows=rows, a=a, st_ar=ST_AR, st_cls=ST_CLS, kind_ar=KIND_AR,
                               equip=db.q('SELECT id,name_ar,no FROM maint_equipment ORDER BY sr,id'), t=today())

    # ------------------------------------------------------------------ أمر صيانة (تنفيذ)
    @route('/maintenance/order/<no>', 'maint_order', methods=['GET', 'POST'])
    def maint_order(no):
        o = _order_or_404(no)
        back = url_for('maint_order', no=no)
        if request.method == 'POST':
            f = request.form
            act = f.get('act')
            user = g.user['full_name']
            if act in ('start', 'save', 'complete', 'cancel') and not auth.can('maint'):
                abort(403)
            if act == 'verify' and not auth.can('maint_verify'):
                abort(403)
            if o['status'] in ('Verified', 'Cancelled') and act != 'raise_em':
                flash('الأمر مغلق', 'bad'); return redirect(back)
            if act == 'start':
                if o['status'] not in ('Planned', 'Reported'):
                    flash('الأمر ليس بانتظار البدء', 'bad'); return redirect(back)
                with db.tx() as con:
                    con.execute("UPDATE maint_orders SET status='In Progress', start_at=?, assigned_to=? WHERE order_no=? AND status IN ('Planned','Reported')",
                                (now_s(), user, no))
                    if o['kind'] == 'PM' and not con.execute('SELECT 1 FROM maint_results WHERE order_id=?', (o['id'],)).fetchone():
                        for it in con.execute('SELECT * FROM maint_checklist WHERE equip_id=? AND active=1 ORDER BY seq', (o['equip_id'],)).fetchall():
                            con.execute('INSERT INTO maint_results(order_id,seq,item_ar) VALUES(?,?,?)', (o['id'], it['seq'], it['item_ar']))
                db.log('update', 'maint_orders', no, 'start')
                flash('بدأ التنفيذ', 'ok'); return redirect(back)
            if act in ('save', 'complete'):
                if o['status'] != 'In Progress':
                    flash('ابدأ التنفيذ أولًا', 'bad'); return redirect(back)
                with db.tx() as con:
                    if o['kind'] == 'PM':
                        for r in con.execute('SELECT * FROM maint_results WHERE order_id=?', (o['id'],)).fetchall():
                            v = s(f.get(f'r{r["id"]}'))
                            con.execute('UPDATE maint_results SET result=?, note=? WHERE id=?',
                                        (v if v in RES else None, s(f.get(f'n{r["id"]}')), r['id']))
                        con.execute('UPDATE maint_orders SET notes=? WHERE order_no=?', (s(f.get('notes')), no))
                    else:
                        con.execute('UPDATE maint_orders SET cause=?, action_taken=?, parts_used=?, notes=? WHERE order_no=?',
                                    (s(f.get('cause')), s(f.get('action_taken')), s(f.get('parts_used')), s(f.get('notes')), no))
                if act == 'save':
                    flash('حُفظ', 'ok'); return redirect(back)
                # إتمام
                if o['kind'] == 'PM':
                    rows = db.q('SELECT * FROM maint_results WHERE order_id=?', (o['id'],))
                    if any(not r['result'] for r in rows):
                        flash('أجب عن كل بنود الفحص (مطابق / غير مطابق / لا ينطبق) قبل الإتمام', 'bad'); return redirect(back)
                    bad = [r for r in rows if r['result'] == 'غير مطابق']
                    if any(r['result'] == 'غير مطابق' and not r['note'] for r in rows):
                        flash('اكتب ملاحظة لكل بند غير مطابق', 'bad'); return redirect(back)
                    end = now_s()
                    with db.tx() as con:
                        con.execute("UPDATE maint_orders SET status='Completed', end_at=?, done_by=?, result=? WHERE order_no=?",
                                    (end, user, 'به ملاحظات' if bad else 'مطابق', no))
                        nxt = (datetime.date.fromisoformat(end[:10]) + datetime.timedelta(days=int(o['freq_days'] or 30))).isoformat()
                        con.execute('UPDATE maint_equipment SET last_done=?, next_due=? WHERE id=?', (end[:10], nxt, o['equip_id']))
                    db.log('update', 'maint_orders', no, f'completed; next_due {nxt}')
                    flash('اكتملت الصيانة الوقائية' + (f' — {len(bad)} بند غير مطابق: يمكنك فتح بلاغ صيانة طارئة' if bad else '') +
                          f' — الاستحقاق التالي {nxt}', 'warn' if bad else 'ok')
                else:
                    o2 = db.one('SELECT * FROM maint_orders WHERE order_no=?', (no,))
                    if not (o2['cause'] and o2['action_taken']):
                        flash('اكتب سبب العطل والإجراء المنفَّذ قبل الإتمام', 'bad'); return redirect(back)
                    end = now_s()
                    h = _hours(o2['reported_at'] if o2['production_stopped'] else (o2['start_at'] or o2['reported_at']), end)
                    with db.tx() as con:
                        con.execute("UPDATE maint_orders SET status='Completed', end_at=?, done_by=?, downtime_h=?, result='تم الإصلاح' WHERE order_no=?",
                                    (end, user, h, no))
                        if o2['production_stopped']:
                            notify.push(con, 'em_done', f'أُصلحت {o["name_ar"]} (بلاغ {no}) — يمكن استئناف التشغيل', o2['problem'] or '',
                                        url_for('maint_order', no=no), no, None, ('enter', 'prod_view'))
                    db.log('update', 'maint_orders', no, f'em completed; downtime {h}h')
                    flash(f'اكتمل الإصلاح — زمن التوقف {h} ساعة', 'ok')
                return redirect(back)
            if act == 'verify':
                if o['status'] != 'Completed':
                    flash('الأمر ليس مكتملًا', 'bad'); return redirect(back)
                db.run("UPDATE maint_orders SET status='Verified', verified_by=?, verified_at=? WHERE order_no=?", (user, now_s(), no))
                db.log('update', 'maint_orders', no, 'verified')
                flash('اعتُمد الأمر', 'ok'); return redirect(back)
            if act == 'cancel':
                if o['status'] not in ('Planned', 'Reported'):
                    flash('لا يُلغى إلا الأمر الذي لم يبدأ', 'bad'); return redirect(back)
                db.run("UPDATE maint_orders SET status='Cancelled', notes=COALESCE(notes||' | ','')||? WHERE order_no=?", ('ألغاه ' + user, no))
                db.log('update', 'maint_orders', no, 'cancelled')
                flash('أُلغي الأمر', 'ok'); return redirect(url_for('maint_orders'))
            if act == 'raise_em':
                if not auth.can('maint_report'):
                    abort(403)
                bad = db.q("SELECT item_ar, note FROM maint_results WHERE order_id=? AND result='غير مطابق'", (o['id'],))
                txt = 'بنود غير مطابقة في الصيانة الوقائية ' + no + ': ' + '؛ '.join(f"{r['item_ar']} ({r['note']})" for r in bad)
                new = _new_em(o['equip_id'], txt, 'متوسطة', 0, parent=no)
                flash(f'فُتح بلاغ الصيانة الطارئة {new}', 'ok'); return redirect(url_for('maint_order', no=new))
            abort(400)
        results = db.q('SELECT * FROM maint_results WHERE order_id=? ORDER BY seq', (o['id'],)) if o['kind'] == 'PM' else []
        child = db.q('SELECT order_no FROM maint_orders WHERE parent_order=?', (no,))
        return render_template('maint_order.html', nav='maint', o=o, results=results, st_ar=ST_AR, st_cls=ST_CLS, kind_ar=KIND_AR,
                               res=RES, form=form_no(o['kind']), child=child, t=today())

    def _new_em(equip_id, problem, severity, stopped, parent=None):
        with db.tx() as con:
            y = datetime.date.today().year
            no = db.alloc(con, f'EM-{y}', f'EM-{y}-{{n6}}')
            con.execute("""INSERT INTO maint_orders(order_no,kind,equip_id,status,reported_by,reported_at,problem,severity,production_stopped,parent_order,created_by)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (no, 'EM', equip_id, 'Reported', g.user['full_name'], now_s(), problem, severity,
                                                            1 if stopped else 0, parent, g.user['username']))
            e = con.execute('SELECT name_ar FROM maint_equipment WHERE id=?', (equip_id,)).fetchone()
            notify.push(con, 'em_reported', f'بلاغ عطل طارئ {no}: {e["name_ar"]} — الخطورة {severity}' + (' — الإنتاج متوقف' if stopped else ''),
                        problem[:200], url_for('maint_order', no=no), no, None, ('maint',))
        db.log('create', 'maint_orders', no, f'EM {equip_id}; {severity}; stopped={stopped}')
        return no

    # ------------------------------------------------------------------ بلاغ عطل طارئ
    @route('/maintenance/report', 'maint_report', methods=['GET', 'POST'])
    def maint_report():
        if request.method == 'POST':
            f = request.form
            eq = db.one('SELECT * FROM maint_equipment WHERE id=? AND active=1', (int(f.get('equip_id') or 0),))
            problem = s(f.get('problem'))
            sev = f.get('severity') if f.get('severity') in SEV_AR else 'متوسطة'
            if not eq or not problem:
                flash('اختر الآلة واكتب وصف العطل', 'bad'); return redirect(url_for('maint_report'))
            no = _new_em(eq['id'], problem, sev, bool(f.get('stopped')))
            flash(f'أُرسل بلاغ الصيانة الطارئة {no} إلى فريق الصيانة', 'ok')
            return redirect(url_for('maint_order', no=no) if auth.can('maint_view') else url_for('maint_report'))
        mine = db.q("""SELECT o.*, e.name_ar FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id
                       WHERE o.kind='EM' AND o.reported_by=? ORDER BY o.id DESC LIMIT 15""", (g.user['full_name'],))
        return render_template('maint_report.html', nav='maint', equip=db.q('SELECT * FROM maint_equipment WHERE active=1 ORDER BY sr,id'),
                               sev=SEV_AR, mine=mine, st_ar=ST_AR, st_cls=ST_CLS, pre=request.args.get('eq'))

    # ------------------------------------------------------------------ المعدات وبنود الفحص (إدارة)
    @route('/maintenance/equipment', 'maint_equipment', methods=['GET', 'POST'])
    def maint_equipment():
        if request.method == 'POST':
            f = request.form
            act = f.get('act')
            if act == 'forms':
                for k in ('maint_form_pm', 'maint_form_em', 'maint_form_wo'):
                    db.run("INSERT INTO settings(key,value,note) VALUES(?,?,'') ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, s(f.get(k)) or ''))
                flash('حُفظت أرقام النماذج', 'ok')
            elif act in ('add', 'save'):
                no, ar = s(f.get('no')), s(f.get('name_ar'))
                fr = int(num(f.get('freq_days'), 30) or 30)
                if not no or not ar or fr < 1:
                    flash('رقم الآلة والاسم والمدة (أيام) إلزامية', 'bad'); return redirect(url_for('maint_equipment'))
                mc = s(f.get('machine_code'))
                if act == 'add':
                    if db.one('SELECT 1 FROM maint_equipment WHERE no=?', (no,)):
                        flash('رقم الآلة مستخدم', 'bad'); return redirect(url_for('maint_equipment'))
                    sr = (db.one('SELECT IFNULL(MAX(sr),0)+1 n FROM maint_equipment')['n'])
                    db.run('INSERT INTO maint_equipment(sr,no,name_en,name_ar,location,machine_code,freq_days,next_due) VALUES(?,?,?,?,?,?,?,?)',
                           (sr, no, s(f.get('name_en')), ar, s(f.get('location')), mc, fr, s(f.get('next_due'))))
                else:
                    eid = int(f.get('id') or 0)
                    if db.one('SELECT 1 FROM maint_equipment WHERE no=? AND id<>?', (no, eid)):
                        flash('رقم الآلة مستخدم', 'bad'); return redirect(url_for('maint_equipment'))
                    db.run("""UPDATE maint_equipment SET no=?, name_en=?, name_ar=?, location=?, machine_code=?, freq_days=?, next_due=COALESCE(?,next_due),
                              active=? WHERE id=?""", (no, s(f.get('name_en')), ar, s(f.get('location')), mc, fr, s(f.get('next_due')), 1 if f.get('active') else 0, eid))
                db.log('update', 'maint_equipment', no, act)
                flash('حُفظت الآلة', 'ok')
            return redirect(url_for('maint_equipment'))
        return render_template('maint_equipment.html', nav='maint', rows=db.q('SELECT * FROM maint_equipment ORDER BY sr,id'),
                               machines=db.q('SELECT machine_code,name FROM machines ORDER BY machine_code'),
                               forms=dict(pm=db.setting('maint_form_pm', ''), em=db.setting('maint_form_em', ''), wo=db.setting('maint_form_wo', '')),
                               n_items={r['equip_id']: r['n'] for r in db.q('SELECT equip_id, COUNT(*) n FROM maint_checklist WHERE active=1 GROUP BY equip_id')})

    @route('/maintenance/equipment/<int:eid>/checklist', 'maint_checklist', methods=['GET', 'POST'])
    def maint_checklist(eid):
        e = db.one('SELECT * FROM maint_equipment WHERE id=?', (eid,))
        if not e:
            abort(404)
        if request.method == 'POST':
            f = request.form
            act = f.get('act')
            if act == 'add':
                t = s(f.get('item_ar'))
                if t:
                    seq = db.one('SELECT IFNULL(MAX(seq),0)+1 n FROM maint_checklist WHERE equip_id=?', (eid,))['n']
                    db.run('INSERT INTO maint_checklist(equip_id,seq,item_ar,item_en,proposed) VALUES(?,?,?,?,0)', (eid, seq, t, s(f.get('item_en'))))
            elif act == 'save':
                for r in db.q('SELECT id FROM maint_checklist WHERE equip_id=?', (eid,)):
                    i = r['id']
                    t = s(f.get(f'a{i}'))
                    if t:
                        db.run('UPDATE maint_checklist SET item_ar=?, item_en=?, seq=?, active=?, proposed=CASE WHEN item_ar<>? THEN 0 ELSE proposed END WHERE id=?',
                               (t, s(f.get(f'e{i}')), int(num(f.get(f's{i}'), 0) or 0), 1 if f.get(f'k{i}') else 0, t, i))
            db.log('update', 'maint_checklist', e['no'], act)
            flash('حُفظت بنود الفحص', 'ok')
            return redirect(url_for('maint_checklist', eid=eid))
        return render_template('maint_checklist.html', nav='maint', e=e, items=db.q('SELECT * FROM maint_checklist WHERE equip_id=? ORDER BY seq, id', (eid,)))
