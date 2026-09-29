# -*- coding: utf-8 -*-
"""مركز استيراد Excel: رفع ← قراءة الأوراق ← معاينة ← ربط الأعمدة ← تحقق ← كشف التكرار ← عرض الأخطاء ← استيراد.
أوضاع: إضافة الجديد فقط / تحديث الموجود / تخطي المكرر. كل عملية تُسجَّل (من، متى، ملف، أعداد)."""
import os, uuid, datetime
import db, inventory, util
from flask import render_template, request, redirect, url_for, flash, g, abort

# الهدف: (جدول، مفتاح، حقول {اسم: (وصف عربي، إلزامي)})
ITEM_FIELDS = {'item_code': ('كود المادة', True), 'description': ('الوصف', False), 'name_ar': ('الاسم بالعربي', False),
               'name_en': ('الاسم بالإنجليزي', False), 'category_ar': ('الفئة', False), 'uom': ('الوحدة', False),
               'size': ('المقاس', False), 'ply': ('الطبقات', False), 'mesh': ('Mesh', False), 'xray': ('X-Ray', False),
               'sterile': ('معقم/غير معقم', False), 'barcode': ('الباركود', False), 'supplier_name': ('المورّد', False),
               'min_stock': ('الحد الأدنى', False), 'location': ('الموقع', False), 'width_cm': ('العرض', False),
               'length_m': ('الطول', False), 'ref_width_cm': ('العرض المرجعي (سم)', False), 'machine_code': ('الماكينة', False),
               'yield_per_sr': ('إنتاجية السب رول', False), 'sp_item': ('كود SP', False), 'pack_code': ('كود العبوة', False),
               'master_box': ('كود الكرتون', False), 'waste_limit_pct': ('حد الهالك %', False), 'notes': ('ملاحظات', False)}
TARGETS = {
    'products': dict(ar='المنتجات (Products)', table='items', key='item_code', fields=ITEM_FIELDS, prefix='GS'),
    'raw': dict(ar='المواد الخام (Raw Materials)', table='items', key='item_code', fields=ITEM_FIELDS, prefix='RR'),
    'subrolls': dict(ar='Sub Roll Master', table='items', key='item_code', fields=ITEM_FIELDS, prefix='SR'),
    'pack_codes': dict(ar='أكواد التعبئة (Packaging Codes)', table='items', key='item_code', fields=ITEM_FIELDS, prefix=None),
    'other': dict(ar='مواد أخرى / Master Data عام', table='items', key='item_code', fields=ITEM_FIELDS, prefix=None),
    'mapping': dict(ar='ربط المنتجات (Product Mapping / BOM)', table='items', key='item_code', fields={
        'item_code': ('كود المنتج', True), 'sp_item': ('كود SP', False), 'machine_code': ('الماكينة', False),
        'pack_code': ('كود العبوة', False), 'master_box': ('كود الكرتون', False)}, prefix=None),
    'yield': dict(ar='بيانات الإنتاجية (Yield)', table='items', key='item_code', fields={
        'item_code': ('كود المنتج', True), 'yield_per_sr': ('إنتاجية السب رول', False), 'waste_limit_pct': ('حد الهالك %', False)}, prefix=None),
    'suppliers': dict(ar='الموردون (Suppliers)', table='suppliers', key='name', fields={
        'name': ('اسم المورّد', True), 'country': ('الدولة', False)}, prefix=None),
    'pack_config': dict(ar='Packaging Configuration', table='pack_spec', key='item_code', fields={
        'item_code': ('كود المنتج', True), 'swabs_per_pack': ('مسحات/باكت', False), 'packs_per_carton': ('باكت/كرتون', False),
        'swabs_per_envelope': ('مسحات/مغلف', False), 'swabs_per_box': ('مسحات/بوكس', False), 'boxes_per_carton': ('بوكس/كرتون', False),
        'pack_code': ('كود الباكت', False), 'env_code': ('كود المغلف', False), 'box_code': ('كود البوكس', False),
        'carton_code': ('كود الكرتون', False)}, prefix=None),
    'inventory': dict(ar='المخزون الافتتاحي (Inventory)', table='stock_tx', key='item_code', fields={
        'item_code': ('كود المادة', True), 'qty': ('الكمية', True), 'unit': ('الوحدة', False), 'location': ('الموقع', False)}, prefix=None),
}
NUMERIC = {'ply', 'min_stock', 'width_cm', 'length_m', 'ref_width_cm', 'yield_per_sr', 'waste_limit_pct', 'qty', 'swabs_per_pack',
           'packs_per_carton', 'swabs_per_envelope', 'swabs_per_box', 'boxes_per_carton'}
GUESS = {'item_code': ('code', 'كود', 'item', 'رمز'), 'description': ('desc', 'وصف', 'description'), 'qty': ('qty', 'كمية', 'quantity'),
         'uom': ('uom', 'unit', 'وحدة'), 'name': ('name', 'اسم', 'مورد', 'supplier')}


def _dir():
    d = os.path.join(os.path.dirname(db.DB_PATH) if hasattr(db, 'DB_PATH') else 'data', 'imports')
    os.makedirs(d, exist_ok=True)
    return d


def read_sheets(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return {ws.title: [[('' if c is None else c) for c in row] for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def _find_header(rows):
    """أول صف فيه ≥3 خلايا نصية غير فارغة يُعدّ صف العناوين."""
    for i, r in enumerate(rows[:15]):
        if sum(1 for c in r if isinstance(c, str) and c.strip()) >= 3:
            return i
    return 0


def _guess(field, headers):
    keys = (field.lower(),) + GUESS.get(field, ())
    for i, h in enumerate(headers):
        hl = str(h).strip().lower()
        if hl and any(k == hl or k in hl for k in keys):
            return i
    return None


def _num(v):
    if v in ('', None):
        return None
    try:
        return float(str(v).replace(',', '').strip())
    except ValueError:
        raise ValueError(f'قيمة غير رقمية: {v}')


def analyse(target, rows, hdr, mapping, con=None):
    """يعيد قائمة سجلات مصنّفة: new / update / dup_in_file / error."""
    T = TARGETS[target]
    keyf = T['key']
    seen, out = set(), []
    for n, r in enumerate(rows[hdr + 1:], start=hdr + 2):
        if not any(str(c).strip() for c in r):
            continue
        rec, err = {}, None
        for f, idx in mapping.items():
            if idx is None or idx >= len(r):
                continue
            v = r[idx]
            v = v.strip() if isinstance(v, str) else v
            if f in NUMERIC:
                try:
                    v = _num(v)
                except ValueError as e:
                    err = err or str(e)
            elif isinstance(v, float) and v == int(v):
                v = str(int(v))
            rec[f] = v if v != '' else None
        for f, (_, req) in T['fields'].items():
            if req and not rec.get(f):
                err = err or f'الحقل الإلزامي «{T["fields"][f][0]}» فارغ'
        k = rec.get(keyf)
        if T['prefix'] and k and not str(k).upper().startswith(T['prefix']):
            err = err or f'الكود {k} لا يبدأ بالبادئة {T["prefix"]}'
        st = 'error' if err else 'new'
        if not err:
            if k in seen:
                st = 'dup_in_file'
            seen.add(k)
        if st == 'new':
            if T['table'] == 'stock_tx':
                if not db.one('SELECT 1 FROM items WHERE item_code=?', (k,)):
                    st, err = 'error', f'الكود {k} غير موجود في Master Data'
            elif db.one(f'SELECT 1 FROM {T["table"]} WHERE {keyf}=?', (k,)):
                st = 'update' if target not in () else st
                if T['table'] in ('items',) and target in ('mapping', 'yield') or T['table'] == 'pack_spec':
                    st = 'update'
            elif target in ('mapping', 'yield', 'pack_config') and not db.one('SELECT 1 FROM items WHERE item_code=?', (k,)):
                st, err = 'error', f'الكود {k} غير موجود في Master Data'
        out.append(dict(line=n, rec=rec, status=st, err=err))
    return out


def apply_import(target, recs, mode, fname):
    T = TARGETS[target]
    ins = upd = skp = rej = 0
    errs = []
    with db.tx() as con:
        for r in recs:
            rec, st = r['rec'], r['status']
            if st == 'error':
                rej += 1; errs.append(f'سطر {r["line"]}: {r["err"]}'); continue
            if st == 'dup_in_file':
                skp += 1; continue
            if st == 'update' and mode == 'insert':
                skp += 1; continue
            if st == 'new' and mode == 'update' and T['table'] != 'stock_tx':
                skp += 1; continue
            if T['table'] == 'stock_tx':
                it = con.execute('SELECT uom FROM items WHERE item_code=?', (rec['item_code'],)).fetchone()
                inventory.post(con, 'IMPORT', 'RM' if 'RM' in inventory.STAGES else next(iter(inventory.STAGES)), rec['qty'],
                               rec.get('unit') or (it['uom'] if it else None), ref_type='import', ref_doc=fname,
                               location=rec.get('location'), note='رصيد افتتاحي من Excel')
                ins += 1; continue
            cols = {k: v for k, v in rec.items() if v is not None}
            if not cols:
                skp += 1; continue
            key = T['key']
            if st == 'update':
                sets = ','.join(f'{c}=?' for c in cols if c != key)
                if sets:
                    con.execute(f'UPDATE {T["table"]} SET {sets} WHERE {key}=?', [v for c, v in cols.items() if c != key] + [cols[key]])
                upd += 1
            else:
                if T['table'] == 'items' and 'prefix' not in cols:
                    cols['prefix'] = (T['prefix'] or str(cols['item_code'])[:2]).upper()
                if T['table'] == 'items':
                    cols.setdefault('uid', uuid.uuid4().hex)
                con.execute(f'INSERT INTO {T["table"]}({",".join(cols)}) VALUES({",".join("?" * len(cols))})', list(cols.values()))
                ins += 1
        con.execute("""INSERT INTO import_log(by_user,file_name,target,mode,inserted,updated,skipped,rejected,errors)
                       VALUES(?,?,?,?,?,?,?,?,?)""", (g.user['username'], fname, target, mode, ins, upd, skp, rej, '\n'.join(errs[:200])))
    db.log('import', 'import_log', fname, f'{target}: +{ins} ~{upd} skip {skp} rej {rej}')
    return ins, upd, skp, rej, errs


def register(app):
    @app.route('/import', endpoint='import_center', methods=['GET', 'POST'])
    def import_center():
        step = request.values.get('step', '1')
        tok = request.values.get('tok', '')
        path = os.path.join(_dir(), os.path.basename(tok) + '.xlsx') if tok else None
        if request.method == 'POST' and step == '1':
            f = request.files.get('file')
            if not f or not f.filename.lower().endswith(('.xlsx', '.xlsm')):
                flash('اختر ملف Excel بصيغة xlsx أو xlsm', 'bad')
                return redirect(url_for('import_center'))
            tok = uuid.uuid4().hex
            f.save(os.path.join(_dir(), tok + '.xlsx'))
            from flask import session
            session['imp_name'] = f.filename
            return redirect(url_for('import_center', step=2, tok=tok))
        if step == '1' or not path or not os.path.exists(path):
            return render_template('import_center.html', nav='admin', step=1, targets=TARGETS,
                                   log=db.q('SELECT * FROM import_log ORDER BY id DESC LIMIT 30'))
        from flask import session
        fname = session.get('imp_name', 'ملف.xlsx')
        try:
            sheets = read_sheets(path)
        except Exception as e:
            flash(f'تعذّر قراءة الملف: {e}', 'bad')
            return redirect(url_for('import_center'))
        sheet = request.values.get('sheet') or next(iter(sheets))
        target = request.values.get('target') or 'other'
        if target not in TARGETS or sheet not in sheets:
            abort(400)
        rows = sheets[sheet]
        hdr = int(request.values.get('hdr') or _find_header(rows))
        headers = rows[hdr] if rows else []
        T = TARGETS[target]
        if step == '2':
            mapping = {f: _guess(f, headers) for f in T['fields']}
            return render_template('import_center.html', step=2, nav='admin', tok=tok, fname=fname, sheets=list(sheets), sheet=sheet,
                                   target=target, targets=TARGETS, T=T, hdr=hdr, headers=headers, preview=rows[hdr + 1:hdr + 9],
                                   mapping=mapping)
        mapping = {}
        for f in T['fields']:
            v = request.values.get('map_' + f, '')
            mapping[f] = int(v) if v.isdigit() else None
        recs = analyse(target, rows, hdr, mapping)
        cnt = {k: sum(1 for r in recs if r['status'] == k) for k in ('new', 'update', 'dup_in_file', 'error')}
        if step == '3':
            return render_template('import_center.html', step=3, nav='admin', tok=tok, fname=fname, sheet=sheet, target=target,
                                   T=T, hdr=hdr, mapping=mapping, recs=recs[:300], cnt=cnt, total=len(recs), targets=TARGETS)
        if step == '4' and request.method == 'POST':
            mode = request.form.get('mode', 'insert')
            if mode not in ('insert', 'update', 'upsert'):
                abort(400)
            ins, upd, skp, rej, errs = apply_import(target, recs, mode, fname)
            try:
                os.remove(path)
            except OSError:
                pass
            flash(f'اكتمل الاستيراد: أُضيف {ins} · حُدّث {upd} · تُخطّي {skp} · رُفض {rej}', 'ok' if not rej else 'warn')
            return redirect(url_for('import_center'))
        abort(400)
