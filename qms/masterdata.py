# -*- coding: utf-8 -*-
"""إدارة البيانات الرئيسية (مدير النظام): تعديل كامل للأصناف والأكواد مع معرّف ثابت uid وتغيير آمن للكود،
Packaging Configuration، Sub Roll Master، المشغلون، الصلاحيات، تنبيهات البيانات، بطاقات المواد، المخزون العام.

الكود Display Code لا يكسر السجلات: عند تغييره تُحدَّث كل الجداول التابعة في تعامل واحد ويُحفظ التاريخ في item_code_history
و uid ثابت لا يتغير.
"""
import re, math
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg, inventory, constants
from util import s, num, fmt_qty

CATS = ('Raw Material', 'Packaging Material', 'Semi-Finished', 'Finished Product', 'Spare / Other')
CAT_AR = {'Raw Material': 'مادة خام', 'Packaging Material': 'مواد تعبئة', 'Semi-Finished': 'نصف مصنع',
          'Finished Product': 'منتج تام', 'Spare / Other': 'قطع غيار / أخرى'}
CODE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$')
# أعمدة نصية تحمل أكواد أصناف (تُحدَّث تبعًا عند تغيير الكود)
CODE_TEXT_COLS = [('items', 'raw_item'), ('items', 'sp_item'), ('work_orders', 'raw_item'), ('subrolls', 'sr_code'),
                  ('cutting_plans', 'sr_code'), ('intermediates', 'sp_code'), ('ster_records', 'sp_code')]
CODE_LIST_COLS = [('items', 'pack_code'), ('items', 'master_box'), ('items', 'box_code'), ('pack_spec', 'pack_code'),
                  ('pack_spec', 'env_code'), ('pack_spec', 'box_code'), ('pack_spec', 'carton_code')]


def rename_item(con, old, new, by):
    """يغيّر Display Code لصنف في تعامل واحد. لا يمس uid. يرفع ValueError عند التعارض."""
    if not CODE_RE.match(new or ''):
        raise ValueError('الكود يجب أن يبدأ بحرف/رقم ويحوي أحرفًا إنجليزية وأرقامًا و . _ - فقط (حتى 40 خانة)')
    row = con.execute('SELECT * FROM items WHERE item_code=?', (old,)).fetchone()
    if not row:
        raise ValueError('الصنف غير موجود')
    dup = con.execute('SELECT item_code FROM items WHERE UPPER(item_code)=UPPER(?) AND item_code<>?', (new, old)).fetchone()
    if dup:
        raise ValueError(f'الكود {new} موجود مسبقًا — لا يُسمح بتكرار الأكواد')
    con.execute('PRAGMA defer_foreign_keys=ON')
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    n = 0
    for t in tables:
        cols = [c[1] for c in con.execute(f'PRAGMA table_info({t})')]
        if 'item_code' in cols and t != 'item_code_history':
            n += con.execute(f'UPDATE {t} SET item_code=? WHERE item_code=?', (new, old)).rowcount
    for t, c in CODE_TEXT_COLS:
        con.execute(f'UPDATE {t} SET {c}=? WHERE {c}=?', (new, old))
    for t, c in CODE_LIST_COLS:                       # قوائم مفصولة بفواصل: استبدال العنصر كاملًا فقط
        for r in con.execute(f"SELECT rowid rid, {c} v FROM {t} WHERE {c} LIKE ?", (f'%{old}%',)).fetchall():
            parts = [p.strip() for p in str(r['v']).split(',')]
            if old in parts:
                con.execute(f'UPDATE {t} SET {c}=? WHERE rowid=?', (', '.join(new if p == old else p for p in parts), r['rid']))
    con.execute('INSERT INTO item_code_history(uid,old_code,new_code,by_user) VALUES(?,?,?,?)', (row['uid'], old, new, by))
    return n


def alerts():
    """تعارضات ونواقص في Master Data — تُعرض لمدير النظام ولا تُفترض لها قيم."""
    out = []
    prods = db.q("SELECT * FROM items WHERE prefix='GS' AND status='نشط' AND route_code='FULL_GAUZE' ORDER BY item_code")
    nomatch, nomachine, nosp, noraw, npc, nenv = [], [], [], [], [], []
    for it in prods:
        srw, _ = mfg.item_sr_width(it)
        if not it.get('machine_code'):
            nomachine.append(it['item_code'])
        if srw and not mfg.match_subrolls(it):
            nomatch.append(f"{it['item_code']} (عرض {srw:g})")
        if it.get('sterile') == 'معقم' and not it.get('sp_item'):
            nosp.append(it['item_code'])
        if not it.get('raw_item'):
            noraw.append(it['item_code'])
        ps = mfg.pack_spec(it['item_code'])
        if it.get('sterile') == 'معقم':
            if not (ps.get('swabs_per_envelope') and ps.get('swabs_per_box')):
                nenv.append(it['item_code'])
        elif not ps.get('swabs_per_pack'):
            npc.append(it['item_code'])
    if nomatch:
        out.append(dict(level='bad', title='لا يوجد Sub Roll مطابق في Master Data', link=url_for('master_subrolls'), rows=nomatch,
                        detail='عرض السب رول في مصفوفة القص/بيانات المنتج لا يطابق أي كود Sub Roll في ملف Excel (العرض + Plain/X-Ray + Mesh). '
                               'راجع مصفوفة القص أو أكواد Sub Roll — لم أعدّل أيًّا منها.'))
    if nomachine:
        out.append(dict(level='bad', title='أصناف بلا ماكينة طي', link=url_for('master_products'), rows=nomachine, detail=''))
    if npc:
        out.append(dict(level='warn', title='غير معقم: عدد المسحات في الباكت غير معرَّف', link=url_for('pack_config_list', f='incomplete'),
                        rows=npc, detail='أكمله من Packaging Configuration (الكراتين تُدخل يدويًا في سند التعبئة).'))
    if nenv:
        out.append(dict(level='warn', title='معقم: مسحات المغلف (5/10) أو مسحات البوكس غير معرَّفة', link=url_for('pack_config_list', f='incomplete'),
                        rows=nenv, detail='غير موجود في ملف Excel. أكمله من Packaging Configuration.'))
    if nosp:
        out.append(dict(level='warn', title='منتجات معقمة بلا كود SP مطابق', link=url_for('master_products'), rows=nosp, detail=''))
    if noraw:
        out.append(dict(level='info', title='أصناف بلا خامة (BOM) محددة', link=url_for('master_products'), rows=noraw[:40],
                        detail=f'{len(noraw)} صنف: يُقترح الجامبو المطابق للميش والكاشف تلقائيًا في السليتر حتى تُحدَّد الخامة.'))
    old_units = db.q("SELECT doc_no FROM folding_out WHERE unit IN ('باكت','باك') AND tag_no IS NOT NULL")
    if old_units:
        out.append(dict(level='bad', title='سندات طي مسجّلة بوحدة «باكت» (يجب أن تكون مسحات)', link=url_for('production_log'),
                        rows=[r['doc_no'] for r in old_units], detail='عدّل كل سند (تعديل ← الوحدة مسحة) ليُعاد احتسابه — لم أحوّلها تلقائيًا.'))
    import printing
    if not printing.letterhead_path():
        out.append(dict(level='warn', title='الورق الرسمي (Letterhead) لم يُرفع بعد', link=url_for('settings_admin'), rows=[],
                        detail='ارفعه من الإعدادات ليُستخدم في كل السندات والتقارير المطبوعة.'))
    if not db.q('SELECT 1 FROM operators WHERE active=1 LIMIT 1'):
        out.append(dict(level='bad', title='لا يوجد مشغلون فعّالون', link=url_for('operators_admin'), rows=[], detail=''))
    return out


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    # ------------------------------------------------------------ الأصناف والأكواد (كل العائلات)
    @route('/admin/codes/<path:code>/rename', 'master_rename', methods=['POST'])
    @auth.require('master')
    def master_rename(code):
        new = (request.form.get('new_code') or '').strip()
        back = url_for('master_product', code=code)
        try:
            with db.tx() as con:
                n = rename_item(con, code, new, g.user['username'])
        except ValueError as e:
            flash(str(e), 'bad')
            return redirect(back)
        db.log('rename_code', 'items', code, f'{code} → {new}; {n} records re-pointed')
        flash(f'تغيّر الكود {code} إلى {new} — كل السجلات التاريخية بقيت مرتبطة بالصنف نفسه (المعرّف الثابت uid) وسُجّل التغيير', 'ok')
        return redirect(url_for('master_product', code=new))

    # ------------------------------------------------------------ Packaging Configuration
    def _sync_pack_config(con, it, ps):
        """يحدّث pack_config (المستعمل في مواضع أخرى) من Packaging Configuration."""
        con.execute('DELETE FROM pack_config WHERE item_code=?', (it['item_code'],))
        rows = []
        if it.get('sterile') == 'معقم':
            if ps.get('swabs_per_envelope'):
                rows.append(('envelope', 'مغلف', ps['swabs_per_envelope']))
            if ps.get('swabs_per_box') and ps.get('swabs_per_envelope'):
                rows.append(('box', 'بوكس', ps['swabs_per_box'] / ps['swabs_per_envelope']))
            if ps.get('boxes_per_carton'):
                rows.append(('carton', 'كرتون', ps['boxes_per_carton']))
        else:
            if ps.get('swabs_per_pack'):
                rows.append(('pack', 'باكت', ps['swabs_per_pack']))
            if ps.get('packs_per_carton'):
                rows.append(('carton', 'كرتون', ps['packs_per_carton']))
        for i, (u, ar, per) in enumerate(rows, 1):
            con.execute('INSERT INTO pack_config(item_code,level,unit,unit_ar,per_parent,allow_partial) VALUES(?,?,?,?,?,1)',
                        (it['item_code'], i, u, ar, per))

    @route('/admin/pack-config', 'pack_config_list')
    @auth.require('master')
    def pack_config_list():
        f = s(request.args.get('f'))
        q_ = s(request.args.get('q'))
        rows = db.q("SELECT * FROM items WHERE prefix='GS' AND status='نشط' ORDER BY item_code")
        out = []
        for it in rows:
            if q_ and q_.lower() not in (it['item_code'] + (it.get('description') or '')).lower():
                continue
            ps = mfg.pack_spec(it['item_code'])
            ster = it.get('sterile') == 'معقم'
            need = ('swabs_per_envelope', 'swabs_per_box') if ster else ('swabs_per_pack',)
            incomplete = any(not ps.get(k) for k in need)
            if f == 'incomplete' and not incomplete:
                continue
            if f in ('sterile', 'non_sterile') and (ster != (f == 'sterile')):
                continue
            out.append(dict(it=it, ps=ps, ster=ster, incomplete=incomplete))
        return render_template('pack_config_list.html', nav='admin', rows=out, f=f, q=q_, fmt=fmt_qty)

    @route('/admin/pack-config/<code>', 'pack_config_edit', methods=['GET', 'POST'])
    @auth.require('master')
    def pack_config_edit(code):
        it = db.one('SELECT * FROM items WHERE item_code=?', (code,))
        if not it:
            abort(404)
        ps = mfg.pack_spec(code)
        ster = it.get('sterile') == 'معقم'
        if request.method == 'POST':
            f = request.form
            v = dict(swabs_per_pack=num(f.get('swabs_per_pack')), packs_per_carton=num(f.get('packs_per_carton')),
                     swabs_per_envelope=num(f.get('swabs_per_envelope')), swabs_per_box=num(f.get('swabs_per_box')),
                     boxes_per_carton=num(f.get('boxes_per_carton')), pack_code=s(f.get('pack_code')), env_code=s(f.get('env_code')),
                     box_code=s(f.get('box_code')), carton_code=s(f.get('carton_code')))
            for k in ('swabs_per_pack', 'packs_per_carton', 'swabs_per_envelope', 'swabs_per_box', 'boxes_per_carton'):
                if v[k] is not None and v[k] <= 0:
                    flash('الأعداد يجب أن تكون أكبر من صفر', 'bad')
                    return redirect(url_for('pack_config_edit', code=code))
            if ster and v['swabs_per_envelope'] and v['swabs_per_box'] and (v['swabs_per_box'] % v['swabs_per_envelope']):
                flash('مسحات البوكس يجب أن تقبل القسمة على مسحات المغلف (مغلفات صحيحة في البوكس)', 'bad')
                return redirect(url_for('pack_config_edit', code=code))
            if ster and v['swabs_per_envelope'] and v['swabs_per_envelope'] not in (5, 10):
                flash('تنبيه: المعتاد 5 أو 10 مسحات في المغلف — تأكد من القيمة', 'warn')
            cols = ', '.join(v)
            with db.tx() as con:
                con.execute('INSERT OR IGNORE INTO pack_spec(item_code) VALUES(?)', (code,))
                con.execute(f"UPDATE pack_spec SET {', '.join(k + '=?' for k in v)}, updated_by=?, updated_at=datetime('now','localtime') "
                            "WHERE item_code=?", (*v.values(), g.user['username'], code))
                _sync_pack_config(con, it, v)
            db.log('update', 'pack_spec', code, str({k: x for k, x in v.items() if x is not None}))
            flash('حُفظ تكوين التعبئة', 'ok')
            return redirect(url_for('pack_config_list'))
        epb = (ps.get('swabs_per_box') / ps['swabs_per_envelope']) if (ps.get('swabs_per_box') and ps.get('swabs_per_envelope')) else None
        return render_template('pack_config_edit.html', nav='admin', it=it, ps=ps, ster=ster, epb=epb, fmt=fmt_qty)

    # ------------------------------------------------------------ Sub Roll Master
    @route('/admin/subrolls', 'master_subrolls', methods=['GET', 'POST'])
    @auth.require('master')
    def master_subrolls():
        if request.method == 'POST':
            code = s(request.form.get('code'))
            w = num(request.form.get('ref_width_cm'))
            if not db.one("SELECT 1 FROM items WHERE item_code=? AND prefix='SR'", (code,)) or not w or w <= 0:
                flash('كود Sub Roll أو العرض غير صحيح', 'bad')
            else:
                db.run('UPDATE items SET ref_width_cm=? WHERE item_code=?', (w, code))
                db.log('update', 'items', code, f'ref_width_cm={w}')
                flash(f'حُدّث العرض المرجعي للكود {code}', 'ok')
            return redirect(url_for('master_subrolls'))
        srs = db.q("SELECT * FROM items WHERE prefix='SR' ORDER BY item_code")
        prods = db.q("SELECT * FROM items WHERE prefix='GS' AND status='نشط' AND route_code='FULL_GAUZE'")
        use = {}
        for p in prods:
            for m in mfg.match_subrolls(p):
                use.setdefault(m['item_code'], []).append(p['item_code'])
        for r in srs:
            r['products'] = use.get(r['item_code'], [])
        orphans = [p['item_code'] for p in prods if not mfg.match_subrolls(p)]
        return render_template('master_subrolls.html', nav='admin', rows=srs, orphans=orphans)

    # ------------------------------------------------------------ المشغلون
    @route('/admin/operators', 'operators_admin', methods=['GET', 'POST'])
    @auth.require('master')
    def operators_admin():
        if request.method == 'POST':
            f = request.form
            act = f.get('act')
            name = s(f.get('name'))
            if act == 'add':
                if not name:
                    flash('اكتب الاسم', 'bad')
                elif db.one('SELECT 1 FROM operators WHERE name=?', (name,)):
                    flash('الاسم موجود مسبقًا', 'bad')
                else:
                    db.run('INSERT INTO operators(name,dept) VALUES(?,?)', (name, s(f.get('dept')) or 'production'))
                    db.log('create', 'operators', name, s(f.get('dept')) or 'production')
                    flash(f'أُضيف المشغل {name}', 'ok')
            elif act in ('toggle', 'rename'):
                oid = int(f.get('id') or 0)
                o = db.one('SELECT * FROM operators WHERE id=?', (oid,))
                if o:
                    if act == 'toggle':
                        db.run('UPDATE operators SET active=? WHERE id=?', (0 if o['active'] else 1, oid))
                    elif name and not db.one('SELECT 1 FROM operators WHERE name=? AND id<>?', (name, oid)):
                        db.run('UPDATE operators SET name=? WHERE id=?', (name, oid))
                    db.log('update', 'operators', o['name'], act)
            return redirect(url_for('operators_admin'))
        return render_template('operators_admin.html', nav='admin', rows=db.q('SELECT * FROM operators ORDER BY dept, id'))

    @route('/admin/roles', 'roles_admin')
    @auth.require('admin')
    def roles_admin():
        perms = sorted(constants.ALL_PERMS)
        desc = {'view': 'الرئيسية والإشعارات والتتبع', 'prod_view': 'عرض شاشات الإنتاج', 'qual_view': 'عرض شاشات الجودة',
                'enter': 'تسجيل وتعديل سندات الإنتاج', 'wo_issue': 'إصدار أوامر الإنتاج', 'wo_close': 'إغلاق الأوامر وإبطال الشحن',
                'reports': 'التقارير', 'audit': 'سجل التدقيق', 'qc_record': 'فحص الخام وسجلات المتابعة', 'qc_sign': 'قرارات الإفراج',
                'qc_template': 'تعديل قوالب الجودة', 'ncr': 'عدم المطابقة', 'admin': 'المستخدمون والنسخ والإعدادات',
                'receive': 'استلام الخام', 'warehouse': 'المخزن والشحن', 'route_override': 'استثناء المسار', 'master': 'البيانات الرئيسية'}
        return render_template('roles_admin.html', nav='admin', perms=perms, desc=desc, roles=constants.ROLE_AR, P=constants.PERMS)

    @route('/admin/data-alerts', 'data_alerts')
    @auth.require('master')
    def data_alerts():
        return render_template('data_alerts.html', nav='admin', alerts=alerts())

    # ------------------------------------------------------------ بطاقات المواد + المخزون العام
    def _card_form(f, code):
        cat = s(f.get('category')) if s(f.get('category')) in CATS else 'Raw Material'
        return dict(name_ar=s(f.get('name_ar')), name_en=s(f.get('name_en')), description=s(f.get('description')), uom=s(f.get('uom')) or 'قطعة',
                    barcode=s(f.get('barcode')), lot_tracking=1 if f.get('lot_tracking') else 0, roll_tracking=1 if f.get('roll_tracking') else 0,
                    supplier_name=s(f.get('supplier_name')), min_stock=num(f.get('min_stock')), location=s(f.get('location')),
                    status='نشط' if f.get('active', 'on') else 'موقوف', notes=s(f.get('notes')),
                    category_en=cat, category_ar=CAT_AR[cat])

    def card_balance(code):
        return float(db.one("SELECT IFNULL(SUM(qty),0) n FROM stock_tx WHERE owner=? AND stage='RM'", (f'MC:{code}',))['n'])

    @route('/inventory/cards', 'inv_cards')
    def inv_cards():
        q_, cat = s(request.args.get('q')), s(request.args.get('cat'))
        where, args = ['1=1'], []
        if q_:
            where.append('(item_code LIKE ? OR description LIKE ? OR name_ar LIKE ? OR name_en LIKE ?)')
            args += [f'%{q_}%'] * 4
        if cat:
            where.append('category_en=?'); args.append(cat)
        rows = db.q(f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY prefix, item_code LIMIT 300", tuple(args))
        for r in rows:
            r['bal'] = card_balance(r['item_code'])
        return render_template('inv_cards.html', nav='inv', rows=rows, q=q_, cat=cat, cats=CATS, cat_ar=CAT_AR, fmt=fmt_qty)

    @route('/inventory/cards/new', 'inv_card_new', methods=['GET', 'POST'])
    @auth.require('master')
    def inv_card_new():
        if request.method == 'POST':
            f = request.form
            code = (s(f.get('item_code')) or '').strip()
            if not CODE_RE.match(code):
                flash('الكود مطلوب (أحرف/أرقام إنجليزية و . _ -)', 'bad')
                return redirect(url_for('inv_card_new'))
            if db.one('SELECT 1 FROM items WHERE UPPER(item_code)=UPPER(?)', (code,)):
                flash('الكود موجود مسبقًا — لا تكرار للأكواد', 'bad')
                return redirect(url_for('inv_card_new'))
            v = _card_form(f, code)
            prefix = (re.match(r'^[A-Za-z]+', code) or [None])[0] or 'MC'
            with db.tx() as con:
                con.execute("""INSERT INTO items(item_code,prefix,category_ar,category_en,description,uom,barcode,lot_tracking,roll_tracking,
                               supplier_name,min_stock,location,status,notes,name_ar,name_en,uid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,lower(hex(randomblob(16))))""",
                            (code, prefix.upper(), v['category_ar'], v['category_en'], v['description'], v['uom'], v['barcode'],
                             v['lot_tracking'], v['roll_tracking'], v['supplier_name'], v['min_stock'], v['location'], v['status'],
                             v['notes'], v['name_ar'], v['name_en']))
            db.log('create', 'items', code, f'material card; {v["category_en"]}')
            flash(f'أُنشئت بطاقة المادة {code}', 'ok')
            return redirect(url_for('inv_card', code=code))
        return render_template('inv_card.html', nav='inv', it={}, new=True, cats=CATS, cat_ar=CAT_AR, bal=0, moves=[], fmt=fmt_qty)

    @route('/inventory/cards/<path:code>', 'inv_card', methods=['GET', 'POST'])
    def inv_card(code):
        it = db.one('SELECT * FROM items WHERE item_code=?', (code,))
        if not it:
            abort(404)
        if request.method == 'POST':
            f = request.form
            act = f.get('act')
            if act == 'save':
                auth.need('master')
                v = _card_form(f, code)
                with db.tx() as con:
                    con.execute("""UPDATE items SET category_ar=?, category_en=?, description=?, uom=?, barcode=?, lot_tracking=?, roll_tracking=?,
                                   supplier_name=?, min_stock=?, location=?, status=?, notes=?, name_ar=?, name_en=? WHERE item_code=?""",
                                (v['category_ar'], v['category_en'], v['description'] or it.get('description'), v['uom'], v['barcode'], v['lot_tracking'],
                                 v['roll_tracking'], v['supplier_name'], v['min_stock'], v['location'], v['status'], v['notes'],
                                 v['name_ar'], v['name_en'], code))
                db.log('update', 'items', code, 'material card')
                flash('حُفظت البطاقة', 'ok')
            elif act in ('receipt', 'issue', 'adjust'):
                auth.need('warehouse')
                q = num(f.get('qty'))
                if not q or q <= 0:
                    flash('الكمية يجب أن تكون أكبر من صفر', 'bad')
                    return redirect(url_for('inv_card', code=code))
                sign = -1 if act == 'issue' else 1
                try:
                    with db.tx() as con:
                        if sign < 0 and card_balance(code) - q < -1e-9:
                            raise inventory.InsufficientStock(f'الصرف {q:g} أكبر من الرصيد {card_balance(code):g}')
                        inventory.post(con, f'MC:{code}', 'RM', sign * q, it.get('uom') or 'قطعة', None, None, 'card_' + act, code,
                                       s(f.get('location')) or it.get('location'), note=s(f.get('note')) or {'receipt': 'استلام', 'issue': 'صرف', 'adjust': 'تسوية'}[act])
                except inventory.InsufficientStock as e:
                    flash(str(e), 'bad')
                    return redirect(url_for('inv_card', code=code))
                db.log('create', 'stock_tx', code, f'{act} {q:g}')
                flash('سُجّلت الحركة', 'ok')
            return redirect(url_for('inv_card', code=code))
        moves = db.q("SELECT * FROM stock_tx WHERE owner=? ORDER BY id DESC LIMIT 60", (f'MC:{code}',))
        rolls = db.q('SELECT status_n, COUNT(*) n FROM (SELECT COALESCE(use_status, stock_status) status_n FROM rolls WHERE item_code=?) GROUP BY status_n', (code,))
        return render_template('inv_card.html', nav='inv', it=it, new=False, cats=CATS, cat_ar=CAT_AR, bal=card_balance(code), moves=moves,
                               rolls=rolls, fmt=fmt_qty)

    @route('/inventory/raw', 'inv_raw')
    def inv_raw():
        st_ = s(request.args.get('st'))
        rows = db.q("""SELECT r.*, i.description, rc.supplier_lot slot, sp.name supplier FROM rolls r LEFT JOIN items i ON i.item_code=r.item_code
                       LEFT JOIN receipts rc ON rc.grn_no=r.grn_no LEFT JOIN suppliers sp ON sp.supplier_id=rc.supplier_id
                       ORDER BY r.roll_no DESC LIMIT 400""")
        for r in rows:
            r['use'] = r.get('use_status') or 'متاح'
        if st_:
            rows = [r for r in rows if r['use'] == st_ or r['stock_status'] == st_]
        return render_template('inv_raw.html', nav='inv', rows=rows, st=st_, fmt=fmt_qty)

    @route('/inventory/moves', 'inv_moves')
    def inv_moves():
        b, stg = s(request.args.get('b')), s(request.args.get('stage'))
        where, args = ['1=1'], []
        if b:
            where.append('(batch_no=? OR owner=? OR ref_doc=?)'); args += [b, b, b]
        if stg:
            where.append('stage=?'); args.append(stg)
        rows = db.q(f"SELECT * FROM stock_tx WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 300", tuple(args))
        return render_template('inv_moves.html', nav='inv', rows=rows, b=b, stage=stg, stages=inventory.STAGES, fmt=fmt_qty)

    # ------------------------------------------------------------ قوائم الإنتاج الفرعية
    @route('/production/subrolls', 'subroll_list')
    def subroll_list():
        st_, bn = s(request.args.get('st')), s(request.args.get('b'))
        where, args = ['1=1'], []
        if st_:
            where.append('stock_status=?'); args.append(st_)
        if bn:
            where.append('batch_no=?'); args.append(bn)
        rows = db.q(f"SELECT * FROM subrolls WHERE {' AND '.join(where)} ORDER BY tag_no DESC LIMIT 400", tuple(args))
        return render_template('subroll_list.html', nav='prod', rows=rows, st=st_, b=bn, fmt=fmt_qty)

    @route('/production/intermediates', 'inter_list')
    def inter_list():
        st_, bn = s(request.args.get('st')), s(request.args.get('b'))
        where, args = ['1=1'], []
        if st_:
            where.append('status=?'); args.append(st_)
        if bn:
            where.append('batch_no=?'); args.append(bn)
        rows = db.q(f"SELECT * FROM intermediates WHERE {' AND '.join(where)} ORDER BY barcode DESC LIMIT 400", tuple(args))
        import proc
        return render_template('inter_list.html', nav='prod', rows=rows, st=st_, b=bn, fmt=fmt_qty, inter_ar=proc.INTER_AR)

    @route('/production/packing', 'ns_list')
    def ns_list():
        import prod
        rows = [w for w in prod.visible_orders(300, "AND IFNULL(route_code,'FULL_GAUZE')='FULL_GAUZE' AND req_swabs IS NOT NULL "
                                                    "AND route<>'معقم' AND final_status IS NULL") if w['st']['docs']]
        return render_template('ns_list.html', nav='prod', rows=rows, fmt=fmt_qty)
