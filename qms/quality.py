# -*- coding: utf-8 -*-
"""قسم الجودة — قوالب وسجلات (إفراج / يومي / دوري) لخطوط الشاش: QC وQA.

المبدأ: القالب بيانات لا كود. تفاصيل نظام الجودة (البنود، الحدود، التكرار) تُعدَّل من
شاشة «قوالب الجودة»، وكل سجل يحفظ نسخة من القالب وقت تعبئته فلا يتغير بتعديله لاحقًا.
"""
import json, re, datetime
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, constants
from util import s, num, doc_scope
import qc_seed

KIND_AR = {k: v['ar'] for k, v in qc_seed.KINDS.items()}
AREA_AR = {k: v['ar'] for k, v in qc_seed.AREAS.items()}
DEPT_AR = qc_seed.DEPTS
ROUTE_AR = qc_seed.ROUTES
FIELD_TYPES = {'check': 'مطابق / غير مطابق', 'number': 'رقم', 'dim': 'بُعد بسماحية عن المقاس',
               'expect': 'رقم = قيمة التشغيلة', 'match': 'نص مطبوع يُطابَق آليًا',
               'text': 'نص قصير', 'textarea': 'نص طويل', 'select': 'قائمة اختيار'}
NOMINALS = {'size_l': 'طول مقاس الأمر', 'size_w': 'عرض مقاس الأمر'}
EXPECTS = {'per_envelope': 'مسحات في المغلف', 'env_per_box': 'مغلفات في البوكس',
           'boxes_per_carton': 'بوكسات في الكرتونة'}
MATCHES = {'batch_no': 'رقم التشغيلة', 'item_code': 'كود الصنف', 'batch_date': 'تاريخ التشغيلة',
           'cycle_no': 'رقم دورة التعقيم'}
SIG_QC_RECORD = 'اعتماد سجل جودة'


# ------------------------------------------------------------------ منطق القوالب
def get_template(code):
    t = db.one('SELECT * FROM qc_templates WHERE code=?', (code,))
    if t:
        t['fields'] = json.loads(t['fields_json'] or '[]')
        t['req_list'] = json.loads(t.get('requires') or '[]')
    return t


def has_lines(area):
    return bool(qc_seed.AREAS.get(area, {}).get('stage'))


def route_of(w):
    return qc_seed.ROUTE_OF.get((w or {}).get('route'))


def build_ctx(bn):
    """القيم المرجعية للتشغيلة التي تُقارَن بها بنود dim / expect / match."""
    w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,)) if bn else None
    if not w:
        return {}
    ctx = dict(batch_no=w['batch_no'], item_code=w.get('item_code'), batch_date=w.get('batch_start_date'))
    size = w.get('size')
    if not size and w.get('fold_machine'):
        size = (db.one('SELECT size_locked FROM machines WHERE machine_code=?', (w['fold_machine'],)) or {}).get('size_locked')
    nums = re.findall(r'\d+(?:\.\d+)?', size or '')
    if nums:
        ctx['size_w'] = float(nums[0])
        ctx['size_l'] = float(nums[1] if len(nums) > 1 else nums[0])
    pk = db.one("""SELECT per_envelope, env_per_box, boxes_per_carton FROM packaging
                   WHERE batch_no=? ORDER BY doc_no DESC LIMIT 1""", (bn,))
    if pk:
        ctx.update({k: v for k, v in pk.items() if v})
    ctx['cycle_nos'] = [r['cycle_no'] for r in db.q(
        'SELECT DISTINCT cycle_no FROM cycle_loads WHERE batch_no=?', (bn,))]
    return ctx


def _norm(x):
    return re.sub(r'[\s\-_/.\\:،,]', '', str(x or '')).upper()


def _contains_token(printed, expected):
    """expected داخل printed كرمز كامل (لا يكفي أن يكون جزءًا من رقم أطول)."""
    p, e = _norm(printed), _norm(expected)
    if not e:
        return True
    i = p.find(e)
    while i != -1:
        before_ok = i == 0 or not (p[i - 1].isdigit() and e[0].isdigit())
        after = p[i + len(e):i + len(e) + 1]
        after_ok = not (after and after.isdigit() and e[-1].isdigit())
        if before_ok and after_ok:
            return True
        i = p.find(e, i + 1)
    return False


def _date_matches(printed, iso):
    """مطابقة تاريخ مطبوع لتاريخ التشغيلة. التاريخ الكامل يُقارَن بدقة (لا يكفي الشهر والسنة
    إن طُبع يوم مختلف)، ويُقبل الشهر/السنة فقط إن طُبع الشهر والسنة وحدهما."""
    try:
        d = datetime.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return True
    digits = re.sub(r'\D', '', str(printed or ''))
    by_len = {8: ('%d%m%Y', '%Y%m%d'), 6: ('%d%m%y', '%y%m%d', '%m%Y', '%Y%m'), 4: ('%m%y',)}
    if digits and len(digits) in by_len:
        return digits in {d.strftime(f) for f in by_len[len(digits)]}
    txt = _norm(printed)
    return any(x in txt for x in (d.strftime('%b%y').upper(), d.strftime('%b%Y').upper()))


def evaluate(fields, form, ctx=None):
    """يقرأ قيم النموذج ويقيّمها. يعيد (values, fails, missing, expected).

    fails: قائمة {id,label,reason}. expected: {id: القيمة المتوقعة للعرض والتدقيق}.
    """
    ctx = ctx or {}
    values, fails, missing, expected = {}, [], [], {}

    def fail(f, reason):
        fails.append(dict(id=f['id'], label=f['label'], reason=reason))

    for f in fields:
        fid, raw = f['id'], (form.get(f'f_{f["id"]}') or '').strip()
        values[fid] = raw or None
        if not raw:
            if f.get('required'):
                missing.append(f['label'])
            continue
        t = f.get('type', 'text')
        if t in ('number', 'dim', 'expect'):
            v = num(raw)
            if v is None:
                missing.append(f'{f["label"]} (رقم غير صالح)')
                values[fid] = None
                continue
            values[fid] = v
            if t == 'number':
                if f.get('min') is not None and v < float(f['min']):
                    fail(f, f'{v:g} أقل من الحد الأدنى {float(f["min"]):g}')
                if f.get('max') is not None and v > float(f['max']):
                    fail(f, f'{v:g} أعلى من الحد الأقصى {float(f["max"]):g}')
            elif t == 'dim':
                nom, tol = ctx.get(f.get('nominal_from')), float(f.get('tol') or 0)
                if nom is not None:
                    expected[fid] = f'{nom:g} ± {tol:g}'
                    if abs(v - nom) > tol + 1e-9:
                        fail(f, f'{v:g} خارج {nom:g} ± {tol:g} سم')
            else:
                exp = ctx.get(f.get('expect_from'))
                if exp is not None:
                    expected[fid] = f'{exp:g}'
                    if abs(v - float(exp)) > 1e-9:
                        fail(f, f'المدخل {v:g} والمتوقع {float(exp):g}')
        elif t == 'match':
            to = f.get('match_to')
            if to == 'batch_no' and ctx.get('batch_no'):
                expected[fid] = ctx['batch_no']
                ok = _contains_token(raw, ctx['batch_no'])
            elif to == 'item_code' and ctx.get('item_code'):
                expected[fid] = ctx['item_code']
                ok = _contains_token(raw, ctx['item_code'])
            elif to == 'batch_date' and ctx.get('batch_date'):
                expected[fid] = ctx['batch_date']
                ok = _date_matches(raw, ctx['batch_date'])
            elif to == 'cycle_no' and ctx.get('cycle_nos') is not None:
                expected[fid] = '، '.join(ctx['cycle_nos']) or '—'
                ok = _norm(raw) in {_norm(c) for c in ctx['cycle_nos']}
            else:
                continue
            if not ok:
                fail(f, f'المطبوع «{raw}» لا يطابق المتوقع «{expected[fid]}»')
        elif t == 'check' and constants.is_nonconform(raw):
            fail(f, 'غير مطابق')
        elif t == 'select' and raw in (f.get('fail_values') or []):
            fail(f, raw)
    return values, fails, missing, expected


def lines_for(area):
    stage = qc_seed.AREAS.get(area, {}).get('stage')
    if not stage:
        return []
    return db.q("SELECT machine_code, name FROM machines WHERE stage=? AND active=1 ORDER BY machine_code",
                (stage,))


def batches_for(area, route='all'):
    kind = 'تقطيع' if area == 'slitter' else 'إنتاج'
    rows = db.q("""SELECT batch_no, item_code, size, ply, fold_machine, route, batch_start_date, status
                   FROM work_orders WHERE COALESCE(order_type,'إنتاج')=?
                     AND status IN ('صادر','قيد التنفيذ')
                   ORDER BY batch_start_date DESC, batch_no DESC LIMIT 60""", (kind,))
    if route != 'all':
        rows = [r for r in rows if route_of(r) == route]
    return rows


def _parse_ts(d, t):
    try:
        return datetime.datetime.fromisoformat(f"{d}T{(t or '00:00')[:5]}")
    except (TypeError, ValueError):
        return None


def due_items():
    """جدول الاستحقاق: كل قالب يومي/دوري × كل خط في منطقته ← آخر تسجيل وهل هو مستحق."""
    now = datetime.datetime.now()
    out = []
    for t in db.q("""SELECT * FROM qc_templates WHERE active=1 AND kind IN ('daily','periodic')
                     ORDER BY area, dept, kind"""):
        freq = t.get('freq_hours')
        for ln in (lines_for(t['area']) or [dict(machine_code=None, name='—')]):
            last = db.one("""SELECT id, rec_no, rec_date, rec_time, result FROM qc_records
                             WHERE template_code=? AND COALESCE(line_code,'')=COALESCE(?,'')
                             ORDER BY rec_date DESC, COALESCE(rec_time,'') DESC, id DESC LIMIT 1""",
                          (t['code'], ln['machine_code']))
            ts = _parse_ts(last['rec_date'], last['rec_time']) if last else None
            if not last:
                state, hrs = 'لم يُسجَّل بعد', None
            elif not freq:
                state, hrs = 'سجّل', None
            else:
                hrs = round((now - ts).total_seconds() / 3600, 1) if ts else None
                if t['kind'] == 'daily':
                    state = 'مستحق' if last['rec_date'] != now.date().isoformat() else 'مُنجَز اليوم'
                else:
                    state = 'مستحق' if (hrs is None or hrs >= freq) else 'ضمن المدة'
            out.append(dict(tpl=t, line=ln, last=last, state=state, hours=hrs,
                            due=state in ('مستحق', 'لم يُسجَّل بعد')))
    return out


def latest_record(code, batch_no):
    return db.one("""SELECT id, rec_no, result, decision FROM qc_records
                     WHERE template_code=? AND batch_no=? ORDER BY id DESC LIMIT 1""", (code, batch_no))


def batch_checks(w):
    """قائمة فحوص/إفراجات تشغيلة: كل قالب ينطبق عليها وآخر سجل لها."""
    if (w.get('order_type') or 'إنتاج') == 'تقطيع':
        tpls = db.q("""SELECT * FROM qc_templates WHERE active=1 AND area='slitter'
                       AND kind IN ('release','inspection') ORDER BY dept DESC, code""")
    else:
        r = route_of(w) or 'all'
        tpls = [t for t in db.q("""SELECT * FROM qc_templates WHERE active=1 AND area<>'slitter'
                                   AND kind IN ('release','inspection') ORDER BY area, dept DESC, code""")
                if t['route'] in ('all', r)]
    return [dict(tpl=t, rec=latest_record(t['code'], w['batch_no'])) for t in tpls]


def pack_release_ok(bn):
    """هل للتشغيلة المعقمة إفراج ضمان جودة للتعقيم؟"""
    r = latest_record('QA-PKS-REL', bn)
    return bool(r and r['decision'] == 'مفرج')


def final_release(bn):
    """إفراج المنتج غير المعقم (QA-PKN-REL) إن وُجد."""
    return db.one("""SELECT rec_no, rec_date, decision, inspector FROM qc_records
                     WHERE template_code='QA-PKN-REL' AND batch_no=? AND decision='مفرج'
                     ORDER BY id DESC LIMIT 1""", (bn,))


# ------------------------------------------------------------------ المسارات
def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/quality', 'quality_home')
    def quality_home():
        due = due_items()
        recent = db.q("""SELECT r.*, t.title, t.kind, t.dept FROM qc_records r
                         JOIN qc_templates t ON t.code=r.template_code
                         ORDER BY r.id DESC LIMIT 15""")
        week = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        bad = db.q("""SELECT r.*, t.title FROM qc_records r JOIN qc_templates t ON t.code=r.template_code
                      WHERE r.result='غير مطابق' AND r.rec_date>=? ORDER BY r.id DESC LIMIT 15""", (week,))
        running = []
        for w in db.q("""SELECT * FROM work_orders WHERE status IN ('صادر','قيد التنفيذ')
                         ORDER BY COALESCE(order_type,'إنتاج') DESC, batch_start_date DESC LIMIT 40"""):
            running.append(dict(w=w, checks=batch_checks(w)))
        tpls = db.q("SELECT * FROM qc_templates WHERE active=1 ORDER BY area, dept, kind")
        return render_template('quality_home.html', nav='quality', due=due, recent=recent, bad=bad,
                               running=running, tpls=tpls, KIND_AR=KIND_AR, AREA_AR=AREA_AR,
                               DEPT_AR=DEPT_AR, gate=db.setting('qc_gate_mode', 'warn'))

    @route('/quality/new/<code>', 'quality_new', methods=['GET', 'POST'])
    def quality_new(code):
        t = get_template(code)
        if not t or not t['active']:
            abort(404)
        need = 'qc_sign' if t['dept'] == 'QA' else 'qc_record'
        if not auth.can(need):
            abort(403)
        is_release = t['kind'] == 'release'
        area, lines = t['area'], lines_for(t['area'])
        batch_needed = bool(t['needs_batch'] or is_release)
        if request.method == 'POST':
            f = request.form
            back = url_for('quality_new', code=code, b=f.get('batch_no') or '', line=f.get('line_code') or '')
            rec_date = s(f.get('rec_date'))
            shift, line = s(f.get('shift')), s(f.get('line_code'))
            batch, inspector = s(f.get('batch_no')), s(f.get('inspector'))
            if not (rec_date and shift and inspector) or (lines and not line):
                flash('التاريخ والوردية والخط واسم القائم بالفحص بيانات إلزامية', 'bad')
                return redirect(back)
            if lines and line not in {l['machine_code'] for l in lines}:
                flash('الخط غير صحيح لهذا النموذج', 'bad')
                return redirect(back)
            if batch_needed and not batch:
                flash('رقم التشغيلة إلزامي لهذا النموذج', 'bad')
                return redirect(back)
            item_code, w = None, None
            if batch:
                w = db.one('SELECT * FROM work_orders WHERE batch_no=?', (batch,))
                if not w:
                    flash('رقم التشغيلة غير موجود', 'bad')
                    return redirect(back)
                is_sl = (w.get('order_type') or 'إنتاج') == 'تقطيع'
                if area == 'slitter' and not is_sl or (area != 'slitter' and is_sl):
                    flash('رقم التشغيلة لا يناسب هذا النموذج: الأسليتر برقم SL وباقي المراحل برقم أمر الإنتاج', 'bad')
                    return redirect(back)
                if t['route'] != 'all' and route_of(w) != t['route']:
                    flash(f'هذا النموذج خاص بالمنتج {ROUTE_AR[t["route"]]} والتشغيلة {batch} مسارها {w.get("route")}', 'bad')
                    return redirect(back)
                if area == 'folding' and w.get('fold_machine') and w['fold_machine'] != line:
                    flash(f'التشغيلة {batch} مخصصة لماكينة {w["fold_machine"]} لا {line}', 'bad')
                    return redirect(back)
                item_code = w.get('item_code')
            ctx = build_ctx(batch)
            values, fails, missing, expected = evaluate(t['fields'], f, ctx)
            if missing:
                flash('بنود إلزامية ناقصة: ' + '، '.join(missing), 'bad')
                return redirect(back)
            decision = None
            if is_release:
                decision = s(f.get('decision'))
                if decision not in ('مفرج', 'غير مفرج'):
                    flash('اختر قرار الإفراج', 'bad')
                    return redirect(back)
                if decision == 'مفرج' and fails:
                    flash('لا يجوز الإفراج مع وجود بند غير مطابق: ' + '، '.join(x['label'] for x in fails), 'bad')
                    return redirect(back)
                if decision == 'مفرج' and t['req_list']:
                    lacking = []
                    for rc in t['req_list']:
                        last = latest_record(rc, batch)
                        if not last or last['result'] != 'مطابق':
                            rt = db.one('SELECT title FROM qc_templates WHERE code=?', (rc,))
                            lacking.append((rt or {}).get('title', rc))
                    if lacking:
                        flash('لا يجوز الإفراج قبل وجود سجل مطابق لـ: ' + '؛ '.join(lacking), 'bad')
                        return redirect(back)
                if not auth.check_esign(f.get('esign_pw')):
                    flash('التوقيع الإلكتروني غير صحيح — أعد إدخال كلمة مرورك لاعتماد الإفراج', 'bad')
                    return redirect(back)
            result = 'غير مطابق' if fails else 'مطابق'
            stored = dict(values)
            stored['__fails'] = fails
            stored['__exp'] = expected
            scope, fmt = doc_scope(t['code'], rec_date)
            with db.tx() as con:
                rec_no = db.alloc(con, scope, fmt)
                cur = con.execute("""INSERT INTO qc_records(rec_no,template_code,template_version,spec_json,
                        rec_date,rec_time,shift,batch_no,line_code,item_code,values_json,result,fail_count,
                        decision,inspector,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rec_no, t['code'], t['version'], t['fields_json'], rec_date,
                     s(f.get('rec_time')) or datetime.datetime.now().strftime('%H:%M'), shift, batch, line,
                     item_code, json.dumps(stored, ensure_ascii=False), result, len(fails), decision,
                     inspector, s(f.get('notes')), g.user['username']))
                if is_release:
                    con.execute(*auth.signature_row(SIG_QC_RECORD, 'qc_records', rec_no))
                # إفراج المنتج غير المعقم هو الإفراج النهائي للتشغيلة
                if is_release and decision == 'مفرج' and area == 'packaging' and t['route'] == 'non_sterile':
                    con.execute("UPDATE work_orders SET status=? WHERE batch_no=? AND status IN (?,?)",
                                (constants.WO_DONE, batch, constants.WO_ISSUED, constants.WO_RUNNING))
            db.log('create', 'qc_records', rec_no, f'{t["code"]}; {batch or "-"}; {line or "-"}; {result}'
                   + (f'; {decision}' if decision else ''))
            if fails:
                flash(f'سُجّل {rec_no} — غير مطابق: ' + '، '.join(f'{x["label"]} ({x["reason"]})' for x in fails), 'bad')
            else:
                flash(f'سُجّل {rec_no} — مطابق' + (f' · القرار: {decision}' if decision else ''), 'ok')
            return redirect(url_for('quality_record', rid=cur.lastrowid))

        line_sel = request.args.get('line') or ''
        batch_sel = request.args.get('b') or ''
        if not line_sel and len(lines) == 1:
            line_sel = lines[0]['machine_code']
        wsel = db.one('SELECT * FROM work_orders WHERE batch_no=?', (batch_sel,)) if batch_sel else None
        if wsel and area == 'folding' and not line_sel:
            line_sel = wsel.get('fold_machine') or ''
        ctx = build_ctx(batch_sel) if batch_sel else {}
        return render_template('quality_form.html', nav='quality', t=t, KIND_AR=KIND_AR, AREA_AR=AREA_AR,
                               DEPT_AR=DEPT_AR, ROUTE_AR=ROUTE_AR, lines=lines,
                               batches=batches_for(area, t['route']), ctx=ctx, MATCHES_AR=MATCHES,
                               line_sel=line_sel, batch_sel=batch_sel, is_release=is_release,
                               batch_needed=batch_needed, req_titles=[
                                   (db.one('SELECT title FROM qc_templates WHERE code=?', (c,)) or {}).get('title', c)
                                   for c in t['req_list']],
                               now_time=datetime.datetime.now().strftime('%H:%M'))

    @route('/quality/record/<int:rid>', 'quality_record')
    def quality_record(rid):
        r = db.one("""SELECT r.*, t.title, t.kind, t.dept, t.area FROM qc_records r
                      JOIN qc_templates t ON t.code=r.template_code WHERE r.id=?""", (rid,))
        if not r:
            abort(404)
        spec = json.loads(r['spec_json'] or '[]')
        vals = json.loads(r['values_json'] or '{}')
        fails = {x['id']: x['reason'] for x in (vals.get('__fails') or [])}
        exp = vals.get('__exp') or {}
        legacy = '__fails' not in vals
        rows = []
        for f in spec:
            v = vals.get(f['id'])
            bad = f['id'] in fails
            if legacy and v is not None:
                if f.get('type') == 'check':
                    bad = constants.is_nonconform(v)
                elif f.get('type') == 'number':
                    bad = (f.get('min') is not None and v < float(f['min'])) or \
                          (f.get('max') is not None and v > float(f['max']))
            rows.append(dict(f=f, v=v, bad=bad, why=fails.get(f['id']), exp=exp.get(f['id'])))
        return render_template('quality_record.html', nav='quality', r=r, rows=rows, KIND_AR=KIND_AR,
                               DEPT_AR=DEPT_AR, AREA_AR=AREA_AR,
                               sigs=auth.signatures_for('qc_records', r['rec_no']))

    @route('/quality/records', 'quality_records')
    def quality_records():
        a = request.args
        where, args = ['1=1'], []
        for col, key in (('r.template_code', 'tpl'), ('r.line_code', 'line'), ('r.result', 'result')):
            if s(a.get(key)):
                where.append(f'{col}=?'); args.append(a.get(key))
        if s(a.get('batch')):
            where.append('r.batch_no LIKE ?'); args.append(f"%{a.get('batch').strip()}%")
        if s(a.get('dfrom')):
            where.append('r.rec_date>=?'); args.append(a.get('dfrom'))
        if s(a.get('dto')):
            where.append('r.rec_date<=?'); args.append(a.get('dto'))
        rows = db.q(f"""SELECT r.*, t.title, t.kind, t.dept FROM qc_records r
                        JOIN qc_templates t ON t.code=r.template_code
                        WHERE {' AND '.join(where)} ORDER BY r.rec_date DESC, r.id DESC LIMIT 400""", tuple(args))
        return render_template('quality_records.html', nav='quality', rows=rows, a=a, KIND_AR=KIND_AR,
                               tpls=db.q('SELECT code,title FROM qc_templates ORDER BY code'),
                               lines=db.q("SELECT DISTINCT line_code FROM qc_records WHERE line_code IS NOT NULL"))

    # ---------------------------------------------------------- إدارة القوالب
    @route('/quality/templates', 'quality_templates')
    def quality_templates():
        rows = db.q("""SELECT t.*, (SELECT COUNT(*) FROM qc_records r WHERE r.template_code=t.code) n
                       FROM qc_templates t ORDER BY t.area, t.dept, t.kind, t.code""")
        for r in rows:
            r['nfields'] = len(json.loads(r['fields_json'] or '[]'))
        return render_template('quality_templates.html', nav='quality', rows=rows, KIND_AR=KIND_AR,
                               AREA_AR=AREA_AR, DEPT_AR=DEPT_AR, ROUTE_AR=ROUTE_AR)

    def _parse_fields(raw):
        """يتحقق من تعريف البنود القادم من المحرّر. يعيد (fields, خطأ)."""
        try:
            fields = json.loads(raw or '[]')
        except ValueError:
            return None, 'تعذّر قراءة بنود القالب'
        if not isinstance(fields, list) or not fields:
            return None, 'أضف بندًا واحدًا على الأقل'
        seen, out = set(), []
        for i, f in enumerate(fields, 1):
            label = (f.get('label') or '').strip()
            if not label:
                return None, f'البند رقم {i} بلا عنوان'
            fid = re.sub(r'[^a-z0-9_]', '', (f.get('id') or '').strip().lower()) or f'f{i}'
            while fid in seen:
                fid += '_'
            seen.add(fid)
            typ = f.get('type') if f.get('type') in FIELD_TYPES else 'text'
            item = dict(id=fid, label=label, type=typ, required=bool(f.get('required')))
            unit = (f.get('unit') or '').strip()
            if unit:
                item['unit'] = unit
            if typ == 'number':
                for k in ('min', 'max'):
                    v = num(f.get(k))
                    if v is not None:
                        item[k] = v
                if 'min' in item and 'max' in item and item['min'] > item['max']:
                    return None, f'البند «{label}»: الحد الأدنى أكبر من الأقصى'
            elif typ == 'dim':
                if f.get('nominal_from') not in NOMINALS:
                    return None, f'البند «{label}»: اختر المقاس المرجعي (طول/عرض)'
                tol = num(f.get('tol'))
                if tol is None or tol < 0:
                    return None, f'البند «{label}»: السماحية مطلوبة (سم)'
                item.update(nominal_from=f['nominal_from'], tol=tol, unit=unit or 'سم')
            elif typ == 'expect':
                if f.get('expect_from') not in EXPECTS:
                    return None, f'البند «{label}»: اختر القيمة المرجعية'
                item['expect_from'] = f['expect_from']
            elif typ == 'match':
                if f.get('match_to') not in MATCHES:
                    return None, f'البند «{label}»: اختر ما يُطابَق به'
                item['match_to'] = f['match_to']
            elif typ == 'select':
                opts = [o.strip() for o in (f.get('options') or '').replace('،', ',').split(',') if o.strip()] \
                    if isinstance(f.get('options'), str) else [str(o) for o in (f.get('options') or [])]
                if len(opts) < 2:
                    return None, f'البند «{label}»: القائمة تحتاج خيارين على الأقل'
                item['options'] = opts
                fv = f.get('fail_values')
                fv = [o.strip() for o in fv.replace('،', ',').split(',') if o.strip()] if isinstance(fv, str) else (fv or [])
                item['fail_values'] = [o for o in fv if o in opts]
            out.append(item)
        return out, None

    @route('/quality/templates/<code>', 'quality_template_edit', methods=['GET', 'POST'])
    @auth.require('qc_template')
    def quality_template_edit(code):
        t = get_template(code)
        if not t:
            abort(404)
        if request.method == 'POST':
            f = request.form
            fields, err = _parse_fields(f.get('fields_json'))
            title = s(f.get('title'))
            if err or not title:
                flash(err or 'عنوان النموذج إلزامي', 'bad')
                return redirect(url_for('quality_template_edit', code=code))
            freq = num(f.get('freq_hours'))
            if freq is not None and freq <= 0:
                flash('فاصل الاستحقاق يجب أن يكون أكبر من صفر أو فارغًا', 'bad')
                return redirect(url_for('quality_template_edit', code=code))
            route_ = f.get('route') if f.get('route') in ROUTE_AR else t['route']
            allc = {r['code'] for r in db.q('SELECT code FROM qc_templates')} - {code}
            req = [c for c in f.getlist('requires') if c in allc]
            db.run("""UPDATE qc_templates SET title=?, freq_hours=?, needs_batch=?, fields_json=?, active=?,
                      note=?, route=?, requires=?, version=version+1, updated_by=?,
                      updated_at=datetime('now','localtime') WHERE code=?""",
                   (title, freq, 1 if f.get('needs_batch') else 0,
                    json.dumps(fields, ensure_ascii=False), 1 if f.get('active') else 0,
                    s(f.get('note')), route_, json.dumps(req), g.user['username'], code))
            db.log('update', 'qc_templates', code, f'v{t["version"] + 1}; {len(fields)} fields')
            flash(f'حُفظ القالب {code} (الإصدار {t["version"] + 1}) — السجلات السابقة تحتفظ بنسختها', 'ok')
            return redirect(url_for('quality_templates'))
        others = db.q("SELECT code, title FROM qc_templates WHERE code<>? ORDER BY code", (code,))
        return render_template('quality_template_edit.html', nav='quality', t=t, KIND_AR=KIND_AR,
                               AREA_AR=AREA_AR, DEPT_AR=DEPT_AR, ROUTE_AR=ROUTE_AR, FIELD_TYPES=FIELD_TYPES,
                               NOMINALS=NOMINALS, EXPECTS=EXPECTS, MATCHES=MATCHES, others=others,
                               used=db.one('SELECT COUNT(*) n FROM qc_records WHERE template_code=?', (code,))['n'])

    @route('/quality/templates/new', 'quality_template_new', methods=['GET', 'POST'])
    @auth.require('qc_template')
    def quality_template_new():
        if request.method == 'POST':
            f = request.form
            code = re.sub(r'[^A-Za-z0-9_-]', '', (f.get('code') or '')).upper()
            dept, kind, area, rt = f.get('dept'), f.get('kind'), f.get('area'), f.get('route') or 'all'
            title = s(f.get('title'))
            if not code or not title or dept not in DEPT_AR or kind not in KIND_AR or area not in AREA_AR \
                    or rt not in ROUTE_AR:
                flash('الرمز والعنوان والإدارة والنوع والمنطقة بيانات إلزامية', 'bad')
                return redirect(url_for('quality_template_new'))
            if db.one('SELECT 1 FROM qc_templates WHERE code=?', (code,)):
                flash('رمز النموذج مستخدم مسبقًا', 'bad')
                return redirect(url_for('quality_template_new'))
            db.run("""INSERT INTO qc_templates(code,title,dept,kind,area,route,freq_hours,needs_batch,fields_json,
                      requires,note,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (code, title, dept, kind, area, rt, num(f.get('freq_hours')),
                    1 if kind in ('release', 'inspection') else 0,
                    json.dumps([qc_seed.NOTES], ensure_ascii=False), '[]',
                    'نموذج جديد — أضف بنوده من المحرّر', g.user['username']))
            db.log('create', 'qc_templates', code)
            flash('أُنشئ النموذج — أضف بنوده الآن', 'ok')
            return redirect(url_for('quality_template_edit', code=code))
        return render_template('quality_template_new.html', nav='quality', KIND_AR=KIND_AR,
                               AREA_AR=AREA_AR, DEPT_AR=DEPT_AR, ROUTE_AR=ROUTE_AR)
