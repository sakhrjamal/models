# -*- coding: utf-8 -*-
"""بيانات المنتجات (Product Master): مسار التصنيع، الفئة، وحدة الإنتاج، وتكوين التعبئة لكل صنف.

مسار الأمر يُحدَّد من هنا تلقائيًا — لا يختاره المستخدم عند إصدار الأمر. تغيير مسار صنف يُسجَّل في التدقيق
ولا يمس الأوامر القائمة (كل أمر يحفظ مساره وقت إصداره).
"""
import re
from flask import render_template, request, redirect, url_for, flash, abort, g

import db, auth, mfg
from util import s, num

UNITS = {'pack': 'باكت', 'box': 'بوكس', 'carton': 'كرتون'}
CATEGORIES = ('Gauze', 'Bandage', 'Gauze Bandage')
UOMS = ('قطعة', 'باكت', 'رول', 'بوكس', 'كرتون')


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    def _parse_pack(f):
        rows, seen = [], set()
        for lv in (1, 2, 3):
            unit = s(f.get(f'pc_unit_{lv}'))
            if not unit:
                continue
            per = num(f.get(f'pc_per_{lv}'))
            if unit not in UNITS or unit in seen:
                return None, 'وحدة التعبئة غير صحيحة أو مكررة'
            if not per or per <= 0:
                return None, f'عدد الوحدات في {UNITS[unit]} يجب أن يكون أكبر من صفر'
            seen.add(unit)
            rows.append((len(rows) + 1, unit, UNITS[unit], per, 1 if f.get(f'pc_part_{lv}') else 0))
        return rows, None

    @route('/master/products', 'master_products', methods=['GET', 'POST'])
    @auth.require('wo_issue')
    def master_products():
        if request.method == 'POST' and request.form.get('act') == 'bulk':
            codes = request.form.getlist('code')
            rc = s(request.form.get('route_code'))
            if not codes or (rc and not mfg.route(rc)):
                flash('اختر أصنافًا ومسارًا صحيحًا', 'bad')
            else:
                with db.tx() as con:
                    for c in codes:
                        old = con.execute('SELECT route_code FROM items WHERE item_code=?', (c,)).fetchone()
                        if old and old['route_code'] != rc:
                            con.execute('UPDATE items SET route_code=? WHERE item_code=?', (rc, c))
                db.log('route_change', 'items', f'{len(codes)} items', f'-> {rc or "بلا مسار"}')
                flash(f'حُدّث مسار {len(codes)} صنف — الأوامر القائمة لا تتأثر', 'ok')
            return redirect(url_for('master_products', **{k: v for k, v in request.args.items()}))
        a = request.args
        where, args = ["i.prefix NOT IN ('RR','SR','PK','BX','MB')"], []
        if s(a.get('q')):
            where.append('(i.item_code LIKE ? OR i.description LIKE ?)'); args += [f"%{a['q'].strip()}%"] * 2
        if s(a.get('route')):
            if a['route'] == 'none':
                where.append('i.route_code IS NULL')
            else:
                where.append('i.route_code=?'); args.append(a['route'])
        if s(a.get('cat')):
            where.append('i.product_category=?'); args.append(a['cat'])
        rows = db.q(f"""SELECT i.item_code, i.prefix, i.description, i.product_category, i.sterile, i.uom, i.route_code,
                               i.size, i.ply, r.name_ar route_name,
                               (SELECT COUNT(*) FROM pack_config p WHERE p.item_code=i.item_code) npack
                        FROM items i LEFT JOIN routes r ON r.code=i.route_code
                        WHERE {' AND '.join(where)} ORDER BY i.prefix, i.item_code LIMIT 300""", tuple(args))
        return render_template('master_products.html', nav='master', rows=rows, a=a, routes=mfg.routes(False),
                               cats=CATEGORIES)

    @route('/master/products/new', 'master_product_new', methods=['GET', 'POST'])
    @auth.require('wo_issue')
    def master_product_new():
        if request.method == 'POST':
            f = request.form
            code = re.sub(r'[^A-Za-z0-9._-]', '', (f.get('item_code') or '')).upper()
            desc, rc = s(f.get('description')), s(f.get('route_code'))
            m = re.match(r'^[A-Z]+', code or '')
            if not code or not desc or not m:
                flash('الكود والوصف إلزاميان (الكود يبدأ بأحرف)', 'bad'); return redirect(url_for('master_product_new'))
            if db.one('SELECT 1 FROM items WHERE item_code=?', (code,)):
                flash('الكود مستخدم مسبقًا', 'bad'); return redirect(url_for('master_product_new'))
            if rc and not mfg.route(rc):
                flash('مسار غير صحيح', 'bad'); return redirect(url_for('master_product_new'))
            db.run("""INSERT INTO items(item_code,prefix,category_ar,category_en,family,size,ply,xray,mesh,sterile,uom,width_cm,
                      length_m,machine_code,description,status,route_code,product_category)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'نشط',?,?)""",
                   (code, m.group(0), 'منتج تام', 'Finished Good', s(f.get('family')), s(f.get('size')),
                    int(num(f.get('ply'))) if num(f.get('ply')) else None, s(f.get('xray')), s(f.get('mesh')),
                    s(f.get('sterile')) or 'غير معقم', s(f.get('uom')) or 'قطعة', s(f.get('width_cm')),
                    s(f.get('length_m')), s(f.get('machine_code')), desc, rc, s(f.get('product_category'))))
            db.log('create', 'items', code, f'route={rc}')
            flash(f'أُضيف الصنف {code} — عرّف تكوين التعبئة الآن', 'ok')
            return redirect(url_for('master_product', code=code))
        return render_template('master_product_new.html', nav='master', routes=mfg.routes(), cats=CATEGORIES, uoms=UOMS,
                               machines=db.q("SELECT machine_code,name FROM machines WHERE stage='التقطيع والطي'"))

    @route('/master/products/<code>', 'master_product', methods=['GET', 'POST'])
    @auth.require('wo_issue')
    def master_product(code):
        it = db.one('SELECT * FROM items WHERE item_code=?', (code,))
        if not it:
            abort(404)
        if request.method == 'POST':
            f = request.form
            rc = s(f.get('route_code'))
            if rc and not mfg.route(rc):
                flash('مسار غير صحيح', 'bad'); return redirect(url_for('master_product', code=code))
            rows, err = _parse_pack(f)
            if err:
                flash(err, 'bad'); return redirect(url_for('master_product', code=code))
            with db.tx() as con:
                con.execute("""UPDATE items SET route_code=?, product_category=?, uom=?, description=?, width_cm=?,
                               length_m=?, status=?, sub_roll_width_cm=?, yield_per_sr=?, raw_item=?, machine_code=?,
                               pack_code=?, box_code=?, master_box=? WHERE item_code=?""",
                            (rc, s(f.get('product_category')), s(f.get('uom')) or it['uom'], s(f.get('description')) or it['description'],
                             s(f.get('width_cm')), s(f.get('length_m')), 'نشط' if f.get('active') else 'موقوف',
                             num(f.get('sub_roll_width_cm')), num(f.get('yield_per_sr')),
                             (s(f.get('raw_item')) or '').upper() or None, s(f.get('machine_code')) or it.get('machine_code'),
                             s(f.get('pack_code')), s(f.get('box_code')), s(f.get('master_box')), code))
                ext = {'name_ar': s(f.get('name_ar')), 'name_en': s(f.get('name_en')), 'barcode': s(f.get('barcode')),
                       'sp_item': (s(f.get('sp_item')) or '').upper() or None, 'ref_width_cm': num(f.get('ref_width_cm')),
                       'waste_limit_pct': num(f.get('waste_limit_pct')), 'notes': s(f.get('notes'))}
                ext = {k: v for k, v in ext.items() if k in f}               # الحقل الغائب عن النموذج لا يُمسح
                if ext:
                    con.execute(f"UPDATE items SET {', '.join(k + '=?' for k in ext)} WHERE item_code=?", (*ext.values(), code))
                con.execute('DELETE FROM pack_config WHERE item_code=?', (code,))
                con.executemany("""INSERT INTO pack_config(item_code,level,unit,unit_ar,per_parent,allow_partial)
                                   VALUES(?,?,?,?,?,?)""", [(code,) + r for r in rows])
            if rc != it.get('route_code'):
                db.log('route_change', 'items', code, f'{it.get("route_code")} -> {rc}')
            db.log('update', 'items', code, f'pack={len(rows)}')
            flash('حُفظت بيانات المنتج وتكوين التعبئة', 'ok')
            return redirect(url_for('master_product', code=code))
        open_orders = db.one("""SELECT COUNT(*) n FROM work_orders WHERE item_code=? AND status IN ('صادر','قيد التنفيذ')""",
                             (code,))['n']
        return render_template('master_product.html', nav='master', it=it, routes=mfg.routes(), cats=CATEGORIES, uoms=UOMS,
                               machines=db.q("SELECT machine_code,name FROM machines WHERE stage='التقطيع والطي' ORDER BY machine_code"),
                               raws=db.q("SELECT item_code, description FROM items WHERE prefix='RR' AND status='نشط' ORDER BY item_code"),
                               units=UNITS, pack={r['level']: r for r in mfg.pack_levels(code)}, open_orders=open_orders)

    @route('/master/routes', 'master_routes')
    @auth.require('reports')
    def master_routes():
        data = []
        for r in mfg.routes(False):
            variants = []
            for v in ('non_sterile', 'sterile'):
                st = mfg.steps_for(r['code'], v)
                if st:
                    variants.append((v, st))
            gates = db.q("""SELECT code, title, kind, dept, is_final FROM qc_templates WHERE active=1
                            AND kind IN ('release','inspection') AND mfg_routes LIKE ? ORDER BY area, code""",
                         (f'%"{r["code"]}"%',))
            data.append(dict(r=r, variants=variants, gates=gates,
                             n_items=db.one('SELECT COUNT(*) n FROM items WHERE route_code=?', (r['code'],))['n']))
        return render_template('master_routes.html', nav='master', data=data,
                               classes=db.q('SELECT * FROM mat_classes ORDER BY class, prefix'))
