# -*- coding: utf-8 -*-
"""منطق شاشات الإدخال — التحقق والاشتقاق الآلي"""
import db, datetime, re, mfg

PROD, SLIT = 'إنتاج', 'تقطيع'      # نوعا أمر التشغيل


def batches(open_only=True, kind=None):
    """أوامر التشغيل. kind='إنتاج' لمراحل ما بعد الأسليتر، 'تقطيع' لأوامر الأسليتر."""
    sql = "SELECT batch_no,item_code,size,ply,route,fold_machine,std_width_cm,wo_no,xray,mesh," \
          "order_type,batch_start_date,status,qty_required,uom FROM work_orders WHERE 1=1"
    args = []
    if open_only:
        sql += " AND status IN ('صادر','قيد التنفيذ')"
    if kind:
        sql += " AND COALESCE(order_type,'إنتاج')=?"
        args.append(kind)
    return db.q(sql + " ORDER BY batch_start_date DESC, batch_no DESC", tuple(args))

def batch(bn):
    return db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))

def std_width(machine, ply):
    if not machine or not ply: return None
    r = db.one('SELECT std_width_cm,size FROM slit_matrix WHERE key=?', (f'{machine}-{ply}',))
    return r if r else None

def area(length_m, width_cm):
    """المساحة سم² = الطول(م) × 100 × العرض(سم)"""
    try:
        return round(float(length_m) * 100 * float(width_cm), 2)
    except (TypeError, ValueError):
        return None

def next_seq(table, col, prefix):
    r = db.one(f"SELECT {col} v FROM {table} WHERE {col} LIKE ? ORDER BY {col} DESC LIMIT 1",
               (prefix + '%',))
    if not r or not r['v']: return 1
    try:    return int(str(r['v']).rsplit('-', 1)[-1]) + 1
    except ValueError: return 1

# ------------------------------------------------ الأسليتر
def _fnum(v, default=None):
    try:
        if v in (None, ''):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _width_from_text(v):
    """استخراج العرض الرقمي من قيم مثل 23 أو 23 Cm."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r'-?\d+(?:[.,]\d+)?', str(v))
    return float(m.group(0).replace(',', '.')) if m else None


def slit_specs():
    """المقاسات المعتمدة للقص كما هي معرفة في مصفوفة الأسليتر."""
    return db.q('SELECT * FROM slit_matrix ORDER BY std_width_cm, machine_code, ply')


def _slitter_params():
    """إعدادات تنظيف الأطراف والتحذير في الأسليتر."""
    edge_each = _fnum(db.setting('slitter_edge_trim_each_cm', '1'), 1.0)
    warn_limit = _fnum(db.setting('slitter_warning_limit_cm',
                                  db.setting('slitter_max_trim_cm', '4')), 4.0)
    return max(edge_each or 0, 0), max(warn_limit or 0, 0)


def slit_recon(batch_no):
    """مطابقة خطة القص مع العرض التشغيلي بعد خصم تنظيف طرفي الرول.

    الاختلاف بالزيادة أو النقصان لا يمنع الحفظ؛ يظهر كتحذير رقابي فقط.
    """
    out = []
    edge_each, warn_limit = _slitter_params()
    edge_total = round(edge_each * 2, 2)
    for r in db.q('SELECT * FROM slitting WHERE batch_no=? ORDER BY id', (batch_no,)):
        kids = db.q('SELECT * FROM subrolls WHERE roll_no=? AND batch_no=? ORDER BY tag_no',
                    (r['roll_no'], batch_no))
        sw = sum(_fnum(k.get('width_cm'), 0) or 0 for k in kids)
        sa = sum(_fnum(k.get('area_cm2'), 0) or 0 for k in kids)
        pw = _fnum(r.get('width_cm'), 0) or 0
        pa = _fnum(r.get('area_cm2'), 0) or 0
        usable_w = round(max(pw - edge_total, 0), 2)
        delta = round(sw - usable_w, 2)  # موجب = زيادة، سالب = نقص
        under_w = round(max(-delta, 0), 2)
        over_w = round(max(delta, 0), 2)
        edge_a = area(r.get('length_m'), min(edge_total, pw)) or 0
        usable_a = area(r.get('length_m'), usable_w) or 0
        area_gap = round(usable_a - sa, 0)

        if not kids:
            result = 'بانتظار خطة القص'
        elif abs(delta) <= 0.01:
            result = 'مطابق — مجموع القص يساوي العرض التشغيلي'
        elif delta < 0:
            result = f'تحذير — مجموع القص أقل من العرض التشغيلي بـ {under_w:g} سم'
        else:
            result = f'تحذير — مجموع القص أعلى من العرض التشغيلي بـ {over_w:g} سم'
        if kids and abs(delta) > warn_limit + 0.01:
            result += f' · الفرق أكبر من الحد المرجعي {warn_limit:g} سم'

        out.append(dict(
            roll_no=r['roll_no'], parent_w=pw, parent_a=pa, n=len(kids),
            edge_each=edge_each, edge_total=edge_total, usable_w=usable_w,
            sum_w=round(sw, 2), sum_a=round(sa, 2), deviation=delta,
            under_w=under_w, over_w=over_w, edge_a=edge_a, usable_a=usable_a,
            dw=round(usable_w - sw, 2), da=area_gap,
            waste=round(under_w / pw, 4) if pw else 0,
            warning_limit=warn_limit, max_trim=warn_limit, result=result
        ))
    return out


def _sr_code_for(con, width_cm, xray, mesh):
    """محاولة ربط السب رول بكود SR الحالي عند وجود تطابق تام في العرض/الميش/X-Ray."""
    rows = con.execute("""SELECT item_code,width_cm FROM items
                          WHERE prefix='SR' AND status='نشط'
                            AND COALESCE(xray,'')=COALESCE(?, '')
                            AND COALESCE(mesh,'')=COALESCE(?, '')
                          ORDER BY item_code""", (xray, mesh)).fetchall()
    for r in rows:
        w = _width_from_text(r['width_cm'])
        if w is not None and abs(w - width_cm) < 0.01:
            return r['item_code']
    return None


def _next_subroll_seq(con, batch_no):
    seq = 1
    for r in con.execute('SELECT tag_no FROM subrolls WHERE batch_no=?', (batch_no,)).fetchall():
        m = re.search(r'-(\d+)$', str(r['tag_no'] or ''))
        if m:
            seq = max(seq, int(m.group(1)) + 1)
    return seq


def add_subroll_plan(f):
    """إنشاء عدة سب رولات من رول واحد بعد التحقق من خطة القص كاملة قبل الحفظ."""
    bn = (f.get('batch_no') or '').strip()
    roll_no = (f.get('roll_no') or '').strip()
    src = db.one('SELECT * FROM slitting WHERE batch_no=? AND roll_no=?', (bn, roll_no))
    if not src:
        return [], 'الرول المختار غير مسجل كمدخل للأسليتر في هذه التشغيلة', False

    parent_w = _fnum(src.get('width_cm'))
    parent_l = _fnum(src.get('length_m'))
    length_m = _fnum(f.get('length_m'), parent_l)
    if not parent_w or parent_w <= 0 or not parent_l or parent_l <= 0:
        return [], 'بيانات عرض/طول الرول الأب غير مكتملة', False
    if not length_m or length_m <= 0:
        return [], 'طول السب رول يجب أن يكون أكبر من صفر', False
    if length_m > parent_l + 0.01:
        return [], f'طول السب رول ({length_m:g} م) لا يجوز أن يتجاوز طول الرول الأب ({parent_l:g} م)', False

    if not (f.get('operator') or '').strip():
        return [], 'اسم فني الأسليتر إلزامي', False
    keys = f.getlist('spec_key')
    qtys = f.getlist('qty')
    plan = []
    for i, key in enumerate(keys):
        key = (key or '').strip()
        if not key:
            continue
        try:
            qty = int(float(qtys[i])) if i < len(qtys) and qtys[i] not in (None, '') else 0
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        m = db.one('SELECT * FROM slit_matrix WHERE key=?', (key,))
        if not m:
            return [], f'مقاس القص {key} غير موجود في المصفوفة المعتمدة', False
        plan.append((m, qty))

    if not plan:
        return [], 'أضف مقاس سب رول واحدًا على الأقل وحدد الكمية', False

    existing = db.one("""SELECT COUNT(*) n, COALESCE(SUM(width_cm),0) sw
                         FROM subrolls WHERE batch_no=? AND roll_no=?""", (bn, roll_no)) or {'n':0, 'sw':0}
    existing_w = _fnum(existing.get('sw'), 0) or 0
    new_w = sum(float(m['std_width_cm']) * qty for m, qty in plan)
    total_w = round(existing_w + new_w, 2)
    edge_each, warn_limit = _slitter_params()
    edge_total = round(edge_each * 2, 2)
    usable_w = round(max(parent_w - edge_total, 0), 2)
    deviation = round(total_w - usable_w, 2)

    # الاختلاف بالزيادة أو النقصان تحذير فقط ولا يوقف إنشاء السب رولات.
    if abs(deviation) <= 0.01:
        plan_note = 'الخطة مطابقة للعرض التشغيلي'
    elif deviation < 0:
        plan_note = f'تحذير: مجموع القص أقل من العرض التشغيلي بـ {abs(deviation):g} سم'
    else:
        plan_note = f'تحذير: مجموع القص أعلى من العرض التشغيلي بـ {deviation:g} سم'
    if abs(deviation) > warn_limit + 0.01:
        plan_note += f'، والفرق أكبر من الحد المرجعي {warn_limit:g} سم'
    if total_w > parent_w + 0.01:
        plan_note += f'، كما أن المجموع أعلى من العرض الخام للرول بـ {total_w-parent_w:g} سم'

    b = batch(bn) or {}
    xray = (f.get('xray') or b.get('xray') or '').strip() or None
    mesh = (f.get('mesh') or b.get('mesh') or '').strip() or None
    operator = (f.get('operator') or '').strip() or None
    delivered = (f.get('delivered_date') or '').strip() or None
    notes = (f.get('notes') or '').strip() or None
    xray_grade = 'X-Ray' if xray == 'WITH X-RAY' else ('Plain' if xray == 'WITHOUT X-RAY' else None)

    con = db.get()
    created = []
    try:
        seq = _next_subroll_seq(con, bn)
        for m, qty in plan:
            width = float(m['std_width_cm'])
            for _ in range(qty):
                while True:
                    tag = f"SR-{bn}-{seq:03d}"
                    if not con.execute('SELECT 1 FROM subrolls WHERE tag_no=?', (tag,)).fetchone():
                        break
                    seq += 1
                sr_code = _sr_code_for(con, width, xray, mesh)
                con.execute("""INSERT INTO subrolls(
                              tag_no,roll_no,item_code,doc_no,sdate,shift,wo_no,batch_no,
                              sr_code,xray_grade,xray,mesh,dest_machine,ply,target_size,
                              length_m,width_cm,std_width_cm,width_check,area_cm2,
                              operator,delivered_date,notes,stock_status,slit_batch)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'متاح',?)""",
                            (tag, roll_no, src.get('item_code'), src.get('doc_no'), src.get('sdate'),
                             src.get('shift'), src.get('wo_no'), bn, sr_code, xray_grade, xray, mesh,
                             m['machine_code'], m['ply'], m['size'], length_m, width, width,
                             'مطابق — عرض قياسي من مصفوفة القص', area(length_m, width),
                             operator, delivered, notes, bn))
                created.append(tag)
                seq += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return created, (f'تم إنشاء {len(created)} سب رول من {roll_no}. '
                     f'العرض الخام {parent_w:g} سم، تنظيف الأطراف {edge_each:g}+{edge_each:g} سم، '
                     f'العرض التشغيلي {usable_w:g} سم، مجموع القص {total_w:g} سم. {plan_note}'), True


def add_subroll(f):
    """توافق مع الإدخال القديم: يحول سب رول واحد إلى خطة قص ثم يطبق نفس التحقق."""
    class LegacyForm:
        def __init__(self, base):
            self.base = base
        def get(self, key, default=None):
            if key == 'spec_key':
                mach = self.base.get('dest_machine')
                ply = self.base.get('ply')
                return f'{mach}-{ply}' if mach and ply else default
            return self.base.get(key, default)
        def getlist(self, key):
            if key == 'spec_key':
                v = self.get('spec_key')
                return [v] if v else []
            if key == 'qty':
                return ['1']
            return self.base.getlist(key)
    made, msg, ok = add_subroll_plan(LegacyForm(f))
    return (made[0] if made else f.get('tag_no')), ('مطابق' if ok else msg)

# ------------------------------------------------ الطي
def fold_recon(batch_no):
    """العدد المعياري مقابل الفعلي"""
    ai = db.one('SELECT IFNULL(SUM(area_used_cm2),0) a, COUNT(*) n FROM folding_in WHERE batch_no=?',
                (batch_no,))
    o  = db.one("""SELECT IFNULL(SUM(qty_good),0) g, IFNULL(SUM(scrap),0) s,
                          IFNULL(SUM(cartons),0) c, MAX(piece_area_cm2) pa
                   FROM folding_out WHERE batch_no=?""", (batch_no,))
    pa = o['pa'] or 0
    theo = int(ai['a'] // pa) if pa else 0
    loss = theo - o['g'] - o['s'] if theo else 0
    lim  = float(db.setting('scrap_limit', 0.03))
    if not theo:                res = '—'
    elif loss < 0:              res = 'انحراف — الإنتاج يفوق المعياري'
    elif loss / theo <= lim:    res = 'ضمن الحد'
    else:                       res = 'تجاوز الحد'
    return dict(subrolls=ai['n'], area=ai['a'], piece_area=pa, theo=theo,
                good=o['g'], scrap=o['s'], cartons=o['c'], loss=loss,
                pct=round(loss / theo, 4) if theo else 0, limit=lim, result=res)

def machine_size_check(machine, size):
    ok = {'FD-05': '5x5 cm', 'FD-75': '7.5x7.5 cm', 'FD-10': '10x10 cm'}
    if not machine or not size: return None
    return 'مطابق' if ok.get(machine) == size else 'انحراف — الماكينة لا تطابق المقاس'


# =====================================================================
#  v10 — الحساب الرياضي وأمر الإنتاج ومخزون السب رول وأرقام السندات
# =====================================================================

def cut_length(machine_code):
    """طول القطعة المفرودة على الماكينة — ثابت لكل ماكينة أيًا كان العرض"""
    r = db.one('SELECT cut_length_cm FROM machine_cut WHERE machine_code=?', (machine_code,))
    return float(r['cut_length_cm']) if r else None

def piece_area_cm2(machine_code, sr_width_cm):
    """
    مساحة الشاش المستهلك للمسحة الواحدة (سم²)
      = عرض السب رول × طول القطع على الماكينة
    مثال: 10×10 من سب رول 23 سم على ماكينة 10 → 23 × 40 = 920 سم²
    """
    cl = cut_length(machine_code)
    if not cl or not sr_width_cm:
        return None
    return float(sr_width_cm) * cl

def piece_weight_g(machine_code, sr_width_cm, gsm=None):
    """الوزن النظري للمسحة الواحدة (جم) من المساحة ووزن الشاش"""
    a = piece_area_cm2(machine_code, sr_width_cm)
    if not a:
        return None
    g = _fnum(gsm if gsm is not None else db.setting('gsm', '17'), 17.0)
    return a * g / 10000.0

def sr_yield(spec_key, length_m, size=None):
    """
    عدد المسحات من سب رول واحد — حساب رياضي مؤكد.
      مساحة السب رول = الطول(م) × 100 × العرض(سم)
      مساحة المسحة   = العرض × طول القطع على الماكينة
      العدد = مساحة السب رول ÷ مساحة المسحة
    ملاحظة: العدد يعتمد على الماكينة والطول فقط — العرض يختصر من الطرفين.
    """
    spec = db.one('SELECT std_width_cm, machine_code, size FROM slit_matrix WHERE key=?', (spec_key,))
    if not spec or not length_m:
        return None
    pa = piece_area_cm2(spec['machine_code'], spec['std_width_cm'])
    if not pa:
        return None
    sr_area = float(spec['std_width_cm']) * float(length_m) * 100.0
    theo = sr_area / pa
    loss = _fnum(db.setting('yield_loss_pct', '0'), 0.0) or 0.0
    return int(theo * (1 - loss / 100.0))

def plan_production(item_code, qty_needed, sr_length_m=None):
    """
    من صنف المنتج والكمية المطلوبة → يحسب احتياج السب رول والجامبو.
    يختار مواصفة السب رول من المصفوفة حسب مقاس المنتج وطبقاته.
    """
    it = db.one('SELECT * FROM items WHERE item_code=?', (item_code,))
    if not it or not it.get('machine_code') or not it.get('ply'):
        return None
    spec_key = f"{it['machine_code']}-{it['ply']}"
    spec = db.one('SELECT * FROM slit_matrix WHERE key=?', (spec_key,))
    if not spec:
        return None
    # الطول النظري للتخطيط فقط — الطول الفعلي يُدخل في سند الأسليتر
    L = sr_length_m or _fnum(db.setting('default_sr_length_m', '2000'), 2000.0)
    per = sr_yield(spec_key, L)
    if not per or not qty_needed:
        return dict(spec_key=spec_key, spec=spec, item=it, per_sr=per,
                    sr_needed=None, jumbo=None, sr_length_m=L)
    import math
    sr_needed = math.ceil(float(qty_needed) / per)
    # كم سب رول من كل جامبو؟ العرض التشغيلي ÷ عرض السب رول
    edge_each, _ = _slitter_params()
    jumbo_w = _fnum(db.setting('default_jumbo_width_cm', '90'), 90.0)
    usable = max(jumbo_w - 2 * edge_each, 0)
    sr_per_jumbo = int(usable // spec['std_width_cm']) if spec['std_width_cm'] else 0
    jumbo = math.ceil(sr_needed / sr_per_jumbo) if sr_per_jumbo else None
    return dict(spec_key=spec_key, spec=spec, item=it, per_sr=per,
                sr_needed=sr_needed, sr_per_jumbo=sr_per_jumbo, jumbo=jumbo, sr_length_m=L)

def doc_seq(batch_no):
    """يولّد رقم السند المتسلسل التالي لتشغيلة: SEP-2601-SL-001/03"""
    con = db.get()
    try:
        con.execute('INSERT OR IGNORE INTO doc_seq(batch_no,last_seq) VALUES(?,0)', (batch_no,))
        con.execute('UPDATE doc_seq SET last_seq=last_seq+1 WHERE batch_no=?', (batch_no,))
        n = con.execute('SELECT last_seq FROM doc_seq WHERE batch_no=?', (batch_no,)).fetchone()[0]
        con.commit()
        return f'{batch_no}/{n:02d}'
    finally:
        con.close()

def sr_stock(spec_key=None, status='متاح'):
    """مخزون السب رول المتاح — اختياريًا مصفّى بنوع المواصفة"""
    sql = "SELECT * FROM subrolls WHERE stock_status=?"
    args = [status]
    if spec_key:
        spec = db.one('SELECT machine_code, ply FROM slit_matrix WHERE key=?', (spec_key,))
        if spec:
            sql += " AND dest_machine=? AND ply=?"
            args += [spec['machine_code'], spec['ply']]
    return db.q(sql + " ORDER BY slit_batch, tag_no", tuple(args))

def sr_by_code(tag_no):
    """يقرأ مواصفات سب رول من كوده — لتعبئة الخانات تلقائيًا"""
    return db.one('SELECT * FROM subrolls WHERE tag_no=?', (tag_no,))

def consume_subroll(tag_no, fold_batch, fdate=None):
    """يسحب سب رول من المخزون إلى تشغيلة طي — يحوّل حالته إلى مستهلك"""
    sr = db.one('SELECT * FROM subrolls WHERE tag_no=?', (tag_no,))
    if not sr:
        return False, 'رقم البطاقة غير موجود في مخزون السب رول'
    if sr['stock_status'] == 'مستهلك':
        return False, f'السب رول {tag_no} مستهلك مسبقًا في التشغيلة {sr.get("consumed_by_batch") or "؟"}'
    db.run("""UPDATE subrolls SET stock_status='مستهلك', consumed_by_batch=?, consumed_date=?
              WHERE tag_no=?""", (fold_batch, fdate, tag_no))
    return True, sr


# =====================================================================
#  v12 — رقم التشغيلة لمراحل ما بعد الأسليتر
# =====================================================================
#  • أمر الأسليتر (SL) له رقمه الخاص ويغذّي مخزون السب رول.
#  • أمر الإنتاج (حرف الماكينة T/S/F) هو رقم التشغيلة الذي يُطبع على العبوة
#    ويحكم الطي ← الفرز ← التغليف ← التعقيم ← الإفراج، ويحمل الصنف النهائي.
#  • السب رول المسحوب من المخزون يربط الرقمين، فيظهر التتبع الكامل من أي رقم.

_SEQ_TAIL = re.compile(r'-(\d+)$')


def next_batch_seq(stem):
    """أول تسلسل غير مستخدم لجذع رقم تشغيلة مثل SEP-2610-F-"""
    top = 0
    for r in db.q("SELECT batch_no FROM work_orders WHERE batch_no LIKE ?", (stem + '%',)):
        m = _SEQ_TAIL.search(r['batch_no'] or '')
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def item_fields(item):
    """الحقول المشتقة من الصنف النهائي — تُحفظ في أمر التشغيل فلا تُكتب ثانية."""
    return dict(item_code=item['item_code'], size=item.get('size'), ply=item.get('ply'),
                xray=item.get('xray'), mesh=item.get('mesh'), route=item.get('sterile'),
                machine=item.get('machine_code'), description=item.get('description'))


def fold_spec_key(b):
    """مفتاح مصفوفة القص لأمر إنتاج: ماكينة-طبقات"""
    return f"{b['fold_machine']}-{b['ply']}" if b and b.get('fold_machine') and b.get('ply') else None


def sr_for_batch(b):
    """السب رول المتاح في المخزون والمطابق لأمر الإنتاج (ماكينة، طبقات، x-ray، ميش).

    يشمل أيضًا السب رول القديم المقصوص مباشرة تحت رقم التشغيلة نفسها.
    """
    if not b:
        return []
    rows = db.q("""SELECT * FROM subrolls
                   WHERE stock_status='متاح'
                     AND ((dest_machine=? AND ply=?) OR batch_no=?)
                   ORDER BY COALESCE(slit_batch,batch_no), tag_no""",
                (b.get('fold_machine'), b.get('ply'), b['batch_no']))
    return [r for r in rows if sr_matches(b, r)[0]]


def sr_matches(b, sr):
    """(ok, سبب) — مطابقة سب رول لأمر إنتاج. عدم تطابق الخط الكاشف أو الميش يمنع."""
    if sr.get('batch_no') == b['batch_no']:
        return True, ''
    if sr.get('dest_machine') != b.get('fold_machine') or sr.get('ply') != b.get('ply'):
        return False, (f"السب رول {sr['tag_no']} مخصص لـ {sr.get('dest_machine')} / {sr.get('ply')} طبقة "
                       f"وأمر الإنتاج على {b.get('fold_machine')} / {b.get('ply')} طبقة")
    if sr.get('xray') and b.get('xray') and sr['xray'] != b['xray']:
        return False, f"الخط الكاشف لا يطابق: السب رول {sr['xray']} والمنتج {b['xray']}"
    if sr.get('mesh') and b.get('mesh') and sr['mesh'] != b['mesh']:
        return False, f"نوع الميش لا يطابق: السب رول {sr['mesh']} والمنتج {b['mesh']}"
    return True, ''


def _n(v):
    return float(v or 0)


def batch_progress(bn):
    """ملخص تقدّم تشغيلة إنتاج عبر كل المراحل — للوحة المدير وقائمة الأوامر."""
    w = batch(bn)
    if not w:
        return None
    fi = db.one('SELECT COUNT(*) n, SUM(area_used_cm2) a FROM folding_in WHERE batch_no=?', (bn,))
    fo = db.one('SELECT SUM(qty_good) g, SUM(scrap) s, SUM(cartons) c FROM folding_out WHERE batch_no=?', (bn,))
    so = db.one('SELECT SUM(pieces_used) p, SUM(groups) g FROM sorting WHERE batch_no=?', (bn,))
    pk = db.one("""SELECT SUM(env_good) e, SUM(env_good*per_envelope) pcs, SUM(boxes) b, SUM(cartons) c,
                          SUM(CASE WHEN doc_status<>'مكتمل' THEN 1 ELSE 0 END) open_docs
                   FROM packaging WHERE batch_no=?""", (bn,))
    cy = db.one("""SELECT SUM(boxes_in) i, SUM(boxes_out) o, COUNT(DISTINCT cycle_no) n
                   FROM cycle_loads WHERE batch_no=?""", (bn,))
    ps = db.one('SELECT SUM(boxes) b FROM post_ster_receipts WHERE batch_no=?', (bn,))
    rel = db.one('SELECT release_no, decision FROM releases WHERE batch_no=?', (bn,))
    if not rel:      # الإفراج النهائي بسجل ضمان جودة (منتج غير معقم / رباط)
        fr = mfg.final_release(bn)
        if fr:
            rel = dict(release_no=fr['ref'], decision='مفرج عنها')
    if rel:
        stage = 'الإفراج — ' + (rel['decision'] or '')
    elif ps and ps['b']:
        stage = 'استلام بعد التعقيم — بانتظار الإفراج'
    elif cy and cy['n']:
        stage = 'التعقيم / التهوية'
    elif pk and pk['e']:
        stage = 'التغليف' + (' — اكتمل' if (w.get('route') == 'غير معقم') else '')
    elif so and so['p']:
        stage = 'الفرز'
    elif (fo and fo['g']) or (fi and fi['n']):
        stage = 'الطي'
    else:
        stage = 'لم تبدأ'

    req = _n(w.get('qty_required'))
    pieces = w.get('uom') in (None, 'قطعة')
    good = _n(fo and fo['g'])
    packed = _n(pk and pk['pcs'])

    def pct(x):
        return round(min(x / req * 100, 100)) if (req and pieces) else None

    return dict(
        batch_no=bn, stage=stage, required=req, uom=w.get('uom'),
        subrolls=fi['n'] if fi else 0,
        fold_good=good, fold_scrap=_n(fo and fo['s']), cartons=_n(fo and fo['c']),
        sorted_pieces=_n(so and so['p']), groups=_n(so and so['g']),
        packed_pieces=packed, env=_n(pk and pk['e']), boxes=_n(pk and pk['b']),
        pack_cartons=_n(pk and pk['c']), open_pack_docs=int((pk and pk['open_docs']) or 0),
        ster_in=_n(cy and cy['i']), ster_out=_n(cy and cy['o']), cycles=int((cy and cy['n']) or 0),
        received_boxes=_n(ps and ps['b']),
        release_no=rel['release_no'] if rel else None, decision=rel['decision'] if rel else None,
        pct_fold=pct(good), pct_pack=pct(packed), route_progress=mfg.progress(bn),
        scrap_pct=round(_n(fo and fo['s']) / (good + _n(fo and fo['s'])) * 100, 1)
                  if (good + _n(fo and fo['s'])) else 0)
