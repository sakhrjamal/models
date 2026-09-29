# -*- coding: utf-8 -*-
"""دفتر حركات المخزون و WIP.

كل انتقال كمية بين مرحلتين = قيدان في `stock_tx` بالمعرّف نفسه (خروج من المصدر + دخول للوجهة)
داخل تعامل واحد، ولا يُقبل خروج أكبر من رصيد المصدر. بذلك لا توجد الكمية نفسها كمتاحة في
مرحلتين معًا، ويُعاد بناء أي رصيد من الدفتر.

الرصيد يُحدَّد بالمفتاح (owner, stage):
  owner  لوط خام / رقم رول جامبو / سند مرحلة / رقم تشغيلة
  stage  مرحلة من STAGES أدناه
"""
import uuid
from flask import g, has_request_context

import db

# stage: (تسمية عربية, نوع: RM خام | WIP تحت التشغيل | FG منتج تام | LOSS مرفوض/هالك)
STAGES = {
    'RM':      ('خام متاح',                                   'RM'),
    'ISSUED':  ('خام مصروف للإنتاج — بانتظار التشغيل/التعبئة', 'WIP'),
    'BM_OUT':  ('رولات رباط ناتجة — بانتظار التغليف',          'WIP'),
    'BW_OUT':  ('رباط مغلَّف — بانتظار تعبئة البوكسات',        'WIP'),
    'BX_OUT':  ('بوكسات — بانتظار تعبئة الكراتين',             'WIP'),
    'CT_OUT':  ('منتج نهائي (كراتين) — بانتظار الفحص النهائي', 'WIP'),
    'SPK_OUT': ('شاش SP معبّأ — بانتظار الفحص النهائي',        'WIP'),
    'PROD_OUT': ('منتج تام من الإنتاج — بانتظار موافقة الجودة/التخزين', 'WIP'),
    'NSP_OUT': ('باكتات معبأة (غير معقم) — بانتظار الإفراج/التخزين', 'WIP'),
    'STR_OUT': ('بوكسات معقمة — بانتظار التعقيم/الإفراج النهائي', 'WIP'),
    'SORTED':  ('مفروز — بانتظار التغليف',                     'WIP'),
    'PACKED':  ('مغلَّف (معقم) — بانتظار التعقيم والفحص',        'WIP'),
    'USED':    ('خام مستهلك في التصنيع',                       'USED'),
    'FG':      ('منتج تام بمخزن المنتجات',                     'FG'),
    'REJECT':  ('مرفوض',                                       'LOSS'),
    'SCRAP':   ('هالك',                                        'LOSS'),
}
EPS = 1e-9


class InsufficientStock(Exception):
    """المطلوب أكبر من الرصيد المتاح."""


def _actor():
    if has_request_context():
        u = getattr(g, 'user', None)
        if u:
            return u['username']
    return 'system'


def balance(owner, stage, con=None):
    sql = 'SELECT IFNULL(SUM(qty),0) FROM stock_tx WHERE owner=? AND stage=?'
    if con is not None:
        return float(con.execute(sql, (owner, stage)).fetchone()[0])
    return float(db.one(sql.replace('SELECT IFNULL(SUM(qty),0)', 'SELECT IFNULL(SUM(qty),0) n'), (owner, stage))['n'])


def post(con, owner, stage, qty, unit, batch_no=None, route=None, ref_type=None, ref_doc=None,
         location=None, note=None, move_id=None):
    """قيد مفرد (استلام أولي مثلًا). للانتقال بين مرحلتين استعمل move()."""
    if stage not in STAGES:
        raise ValueError(f'مرحلة مخزون غير معروفة: {stage}')
    con.execute("""INSERT INTO stock_tx(owner,stage,qty,unit,batch_no,route_code,location,ref_type,ref_doc,
                   move_id,note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, stage, qty, unit, batch_no, route, location, ref_type, ref_doc, move_id, note, _actor()))


def move(con, from_owner, from_stage, to_owner, to_stage, qty, unit, batch_no=None, route=None,
         ref_type=None, ref_doc=None, location=None, note=None):
    """ينقل qty من (from_owner, from_stage) إلى (to_owner, to_stage) بقيدين متلازمين."""
    if qty is None or qty <= 0:
        raise ValueError('الكمية يجب أن تكون أكبر من صفر')
    have = balance(from_owner, from_stage, con)
    if qty - have > 1e-6:
        raise InsufficientStock(f'الكمية {qty:g} أكبر من المتاح {max(have, 0):g} ({STAGES[from_stage][0]})')
    mid = uuid.uuid4().hex[:12]
    post(con, from_owner, from_stage, -qty, unit, batch_no, route, ref_type, ref_doc, None, note, mid)
    post(con, to_owner, to_stage, qty, unit, batch_no, route, ref_type, ref_doc, location, note, mid)
    return mid


def buckets(batch_no=None, kind=None, route=None):
    """أرصدة موجبة مجمّعة: [{owner, stage, unit, batch_no, qty, label, kind, location}]"""
    where, args = ['1=1'], []
    if batch_no:
        where.append('batch_no=?'); args.append(batch_no)
    if route:
        where.append('route_code=?'); args.append(route)
    rows = db.q(f"""SELECT owner, stage, unit, MAX(batch_no) batch_no, MAX(route_code) route_code,
                           SUM(qty) qty, MAX(location) location, MAX(ts) last_ts
                    FROM stock_tx WHERE {' AND '.join(where)}
                    GROUP BY owner, stage, unit HAVING SUM(qty) > 0.000001
                    ORDER BY MAX(id) DESC""", tuple(args))
    out = []
    for r in rows:
        label, k = STAGES.get(r['stage'], (r['stage'], 'WIP'))
        if kind and k != kind:
            continue
        r.update(label=label, kind=k)
        out.append(r)
    return out


def total(batch_no, stage, unit=None):
    sql = 'SELECT IFNULL(SUM(qty),0) n FROM stock_tx WHERE batch_no=? AND stage=?'
    args = [batch_no, stage]
    if unit:
        sql += ' AND unit=?'; args.append(unit)
    return float(db.one(sql, tuple(args))['n'])


def reverse(con, ref_doc, why):
    """يعكس قيود سند بقيود سالبة (لا حذف من الدفتر) — ولا يعكس القيد الواحد مرتين. يعيد عدد القيود المعكوسة."""
    done = set()
    for r in con.execute("SELECT move_id FROM stock_tx WHERE ref_type='reverse' AND move_id LIKE 'rev-%'").fetchall():
        mv = str(r['move_id'])[4:]
        if mv.isdigit():
            done.add(int(mv))
    n = 0
    for r in con.execute("SELECT * FROM stock_tx WHERE ref_doc=? AND ref_type<>'reverse' ORDER BY id", (ref_doc,)).fetchall():
        if r['id'] in done:
            continue
        con.execute("""INSERT INTO stock_tx(owner,stage,qty,unit,batch_no,route_code,location,ref_type,ref_doc,move_id,note,created_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r['owner'], r['stage'], -r['qty'], r['unit'], r['batch_no'], r['route_code'], r['location'],
                     'reverse', ref_doc, f"rev-{r['id']}", why, _actor()))
        n += 1
    return n
