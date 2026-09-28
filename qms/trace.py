# -*- coding: utf-8 -*-
"""محرك التتبع — يجمع سلسلة التشغيلة كاملة من رقم واحد"""
from db import q, one

def batch_chain(batch_no):
    """يرجع قاموسًا فيه كل حلقات السلسلة لرقم تشغيلة"""
    b = batch_no.strip()
    d = {'batch_no': b}

    d['wo'] = one('SELECT * FROM work_orders WHERE batch_no=?', (b,))
    if not d['wo']:
        d['found'] = False
        return d
    d['found'] = True

    d['kind'] = (d['wo'].get('order_type') or 'إنتاج')
    d['slit_orders'] = []
    d['consumers'] = []
    if d['kind'] == 'تقطيع':
        # أمر الأسليتر: رولاته وسب رولاته، وأوامر الإنتاج التي سحبت منه
        d['rolls']    = q('SELECT * FROM slitting WHERE batch_no=? ORDER BY id', (b,))
        d['subrolls'] = q('SELECT * FROM subrolls WHERE batch_no=? OR slit_batch=? ORDER BY tag_no', (b, b))
        d['fold_in'], d['fold_out'] = [], []
        d['consumers'] = q("""SELECT DISTINCT fi.batch_no, w.item_code, w.batch_start_date, w.status,
                                     COUNT(*) subrolls
                              FROM folding_in fi JOIN subrolls s ON s.tag_no=fi.tag_no
                              LEFT JOIN work_orders w ON w.batch_no=fi.batch_no
                              WHERE s.batch_no=? OR s.slit_batch=?
                              GROUP BY fi.batch_no ORDER BY fi.batch_no""", (b, b))
        slit_batches = [b]
    else:
        # أمر الإنتاج: السب رول المسحوب يربطه بأوامر التقطيع ثم بالرولات ثم بالمورّد
        d['fold_in']  = q('SELECT * FROM folding_in WHERE batch_no=? ORDER BY id', (b,))
        d['fold_out'] = q('SELECT * FROM folding_out WHERE batch_no=? ORDER BY id', (b,))
        tags = [r['tag_no'] for r in d['fold_in'] if r.get('tag_no')]
        d['subrolls'] = []
        seen_tags = set()
        for chunk in [tags[i:i+400] for i in range(0, len(tags), 400)] or [[]]:
            ph = ','.join('?' * len(chunk))
            for r in q(f"SELECT * FROM subrolls WHERE batch_no=? OR tag_no IN ({ph or 'NULL'}) ORDER BY tag_no",
                       (b, *chunk)):
                if r['tag_no'] not in seen_tags:
                    seen_tags.add(r['tag_no']); d['subrolls'].append(r)
        d['subrolls'].sort(key=lambda r: r['tag_no'])
        slit_batches = sorted({(r.get('slit_batch') or r.get('batch_no')) for r in d['subrolls']
                               if (r.get('slit_batch') or r.get('batch_no'))})
        d['rolls'] = []
        seen_rolls = set()
        for r in d['subrolls']:
            key = (r.get('roll_no'), r.get('slit_batch') or r.get('batch_no'))
            if key in seen_rolls or not key[0]:
                continue
            seen_rolls.add(key)
            d['rolls'] += q('SELECT * FROM slitting WHERE roll_no=? AND batch_no=? ORDER BY id', key)
    for sb in slit_batches:
        if sb != b:
            w = one('SELECT * FROM work_orders WHERE batch_no=?', (sb,))
            if w:
                d['slit_orders'].append(w)
    d['cartons']  = q('SELECT * FROM cartons WHERE batch_no=? ORDER BY carton_code', (b,))
    d['sorting']  = q('SELECT * FROM sorting WHERE batch_no=? ORDER BY doc_no', (b,))
    d['packaging']= q('SELECT * FROM packaging WHERE batch_no=? ORDER BY doc_no', (b,))
    d['loads']    = q('SELECT * FROM cycle_loads WHERE batch_no=? ORDER BY id', (b,))
    d['psr']      = q('SELECT * FROM post_ster_receipts WHERE batch_no=? ORDER BY doc_no', (b,))
    d['release']  = one('SELECT * FROM releases WHERE batch_no=?', (b,))
    d['shipments']= q('SELECT * FROM shipments WHERE batch_no=? ORDER BY id', (b,))
    d['slit_batches'] = slit_batches

    # سجلات الجودة المرتبطة بالتشغيلة وبأوامر التقطيع التي غذّتها
    qb = [b] + [x for x in slit_batches if x != b]
    ph = ','.join('?' * len(qb))
    d['qc'] = q(f"""SELECT r.id, r.rec_no, r.rec_date, r.shift, r.batch_no, r.line_code, r.result, r.decision,
                           r.inspector, t.title, t.kind, t.dept
                    FROM qc_records r JOIN qc_templates t ON t.code=r.template_code
                    WHERE r.batch_no IN ({ph}) ORDER BY r.rec_date, r.id""", tuple(qb))

    # المورّد — قفزة مزدوجة: التشغيلة ← الرول ← سند الاستلام
    d['supply'] = []
    seen = set()
    for r in d['rolls']:
        rn = r.get('roll_no')
        if not rn or rn in seen: continue
        seen.add(rn)
        row = one("""SELECT rl.roll_no, rl.supplier_lot, rl.xray_grade, rl.mesh,
                            rc.grn_no, rc.receipt_date, rc.po_no, rc.invoice_no,
                            rc.coa_no, rc.mfg_date, rc.expiry_date, rc.country,
                            rc.inspection_no, s.name AS supplier
                     FROM rolls rl
                     LEFT JOIN receipts  rc ON rc.grn_no = rl.grn_no
                     LEFT JOIN suppliers s  ON s.supplier_id = rc.supplier_id
                     WHERE rl.roll_no=?""", (rn,))
        if row:
            ins = one('SELECT decision FROM inspections WHERE inspection_no=?',
                      (row.get('inspection_no'),)) if row.get('inspection_no') else None
            row['qc_decision'] = ins['decision'] if ins else None
            d['supply'].append(row)

    # الدورات والتهوية
    d['cycles'] = []
    for c in {l['cycle_no'] for l in d['loads'] if l.get('cycle_no')}:
        cy = one('SELECT * FROM cycles WHERE cycle_no=?', (c,))
        if cy:
            cy['aeration'] = one('SELECT * FROM aeration WHERE cycle_no=?', (c,))
            d['cycles'].append(cy)
    d['cycles'].sort(key=lambda x: x.get('cycle_no') or '')

    d['verdict'] = verdict(d)
    return d

def verdict(d):
    if d.get('kind') == 'تقطيع':
        n = len(d.get('subrolls') or [])
        used = sum(1 for r in d.get('subrolls') or [] if r.get('stock_status') == 'مستهلك')
        return f'أمر تقطيع — {n} سب رول، المسحوب للطي {used}'
    if d.get('release'):
        return d['release'].get('decision') or 'معلّقة'
    if not d.get('loads'):
        if d['wo'].get('route') == 'غير معقم':
            return 'مسار غير معقم — لا يمر بالتعقيم'
        return 'لم تدخل التعقيم بعد'
    bio = {c.get('bi_result') for c in d['cycles']}
    if 'موجب — غير مطابق' in bio:
        return 'مرفوضة — مؤشر بيولوجي موجب'
    if bio and bio <= {'سالب — مطابق'}:
        aer_ok = bool(d['cycles']) and all(c.get('aeration') and c['aeration'].get('status') == 'مكتملة — جاهزة للاستلام' for c in d['cycles'])
        if aer_ok and d.get('psr'):
            return 'اجتازت التعقيم والتهوية — بانتظار شهادة الإفراج'
        if aer_ok:
            return 'اجتازت التعقيم والتهوية — بانتظار الاستلام في الحجر'
        return 'اجتازت التعقيم — بانتظار اكتمال التهوية'
    return 'بانتظار نتيجة المؤشر البيولوجي'

def batch_forward(supplier_lot):
    """تتبع أمامي: من لوط المورّد إلى أوامر التقطيع ثم أوامر الإنتاج التي دخل فيها"""
    return q("""SELECT s.batch_no, w.item_code, w.batch_start_date, r.release_no, r.decision,
                       'أسليتر' AS stage
                FROM rolls rl
                JOIN slitting s ON s.roll_no = rl.roll_no
                LEFT JOIN work_orders w ON w.batch_no = s.batch_no
                LEFT JOIN releases   r ON r.batch_no = s.batch_no
                WHERE rl.supplier_lot=?
                UNION
                SELECT sr.consumed_by_batch, w.item_code, w.batch_start_date, r.release_no, r.decision,
                       'إنتاج' AS stage
                FROM rolls rl
                JOIN subrolls sr ON sr.roll_no = rl.roll_no
                JOIN work_orders w ON w.batch_no = sr.consumed_by_batch
                LEFT JOIN releases r ON r.batch_no = sr.consumed_by_batch
                WHERE rl.supplier_lot=? AND sr.consumed_by_batch IS NOT NULL
                ORDER BY 1""", (supplier_lot, supplier_lot))

def cycle_contents(cycle_no):
    return q('SELECT * FROM cycle_loads WHERE cycle_no=? ORDER BY id', (cycle_no,))
