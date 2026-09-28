# -*- coding: utf-8 -*-
"""لوحة مدير المصنع والتقارير.

المدير يُصدر أوامر الإنتاج والتقطيع وأرقام التشغيلات (من شاشة أوامر التشغيل)، ويتابع
التقدّم والجودة والتقارير من هنا. لا يُدخل بيانات الإنتاج ولا يعتمد قرارات الجودة —
فصل الصلاحيات معرَّف في constants.PERMS.
"""
import csv, io, datetime
from flask import render_template, request, Response, abort

import db, auth, forms, quality
from util import s


def _rng():
    a = request.args
    today = datetime.date.today()
    dto = s(a.get('dto')) or today.isoformat()
    dfrom = s(a.get('dfrom')) or (today - datetime.timedelta(days=30)).isoformat()
    return dfrom, dto


# ------------------------------------------------------------------ التقارير
def _r_batches(d1, d2):
    cols = ['رقم التشغيلة', 'أمر الإنتاج', 'الصنف', 'المقاس', 'الطبقات', 'المسار', 'تاريخ البدء',
            'المطلوب', 'الوحدة', 'الطي — سليم', 'الطي — تالف', 'الفاقد %', 'مغلفات', 'بوكسات', 'كراتين',
            'دورات التعقيم', 'مستلم بعد التعقيم (بوكس)', 'المرحلة', 'شهادة الإفراج', 'القرار', 'الحالة']
    rows = []
    for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج'
                     AND batch_start_date BETWEEN ? AND ? ORDER BY batch_start_date, batch_no""", (d1, d2)):
        p = forms.batch_progress(w['batch_no'])
        rows.append([w['batch_no'], w['wo_no'], w['item_code'], w['size'], w['ply'], w['route'],
                     w['batch_start_date'], w['qty_required'], w['uom'], p['fold_good'], p['fold_scrap'],
                     p['scrap_pct'], p['env'], p['boxes'], p['pack_cartons'], p['cycles'], p['received_boxes'],
                     p['stage'], p['release_no'] or '—', p['decision'] or '—', w['status']])
    return 'سجل تشغيلات الإنتاج', cols, rows


def _r_daily(d1, d2):
    cols = ['التاريخ', 'رولات مقصوصة', 'سب رول منشأ', 'سب رول مسحوب للطي', 'طي — سليم', 'طي — تالف',
            'فرز — قطع', 'تغليف — مغلفات', 'تغليف — بوكسات']
    days = {}

    def add(sql, key):
        for r in db.q(sql, (d1, d2)):
            if r['d']:
                days.setdefault(r['d'], {})[key] = r['v'] or 0
    add("SELECT sdate d, COUNT(*) v FROM slitting WHERE sdate BETWEEN ? AND ? GROUP BY sdate", 'rolls')
    add("SELECT sdate d, COUNT(*) v FROM subrolls WHERE sdate BETWEEN ? AND ? GROUP BY sdate", 'subs')
    add("SELECT fdate d, COUNT(*) v FROM folding_in WHERE fdate BETWEEN ? AND ? GROUP BY fdate", 'subin')
    add("SELECT fdate d, SUM(qty_good) v FROM folding_out WHERE fdate BETWEEN ? AND ? GROUP BY fdate", 'good')
    add("SELECT fdate d, SUM(scrap) v FROM folding_out WHERE fdate BETWEEN ? AND ? GROUP BY fdate", 'scrap')
    add("SELECT sdate d, SUM(pieces_used) v FROM sorting WHERE sdate BETWEEN ? AND ? GROUP BY sdate", 'sorted')
    add("SELECT pdate d, SUM(env_good) v FROM packaging WHERE pdate BETWEEN ? AND ? GROUP BY pdate", 'env')
    add("SELECT pdate d, SUM(boxes) v FROM packaging WHERE pdate BETWEEN ? AND ? GROUP BY pdate", 'boxes')
    rows = [[d] + [days[d].get(k, 0) for k in ('rolls', 'subs', 'subin', 'good', 'scrap', 'sorted', 'env', 'boxes')]
            for d in sorted(days)]
    return 'الإنتاج اليومي', cols, rows


def _r_slit(d1, d2):
    cols = ['رقم تشغيلة الأسليتر', 'أمر التقطيع', 'تاريخ البدء', 'المستهدف', 'رولات', 'سب رول منشأ',
            'مسحوب للطي', 'متاح بالمخزون', 'أوامر الإنتاج المستفيدة', 'الحالة']
    rows = []
    for w in db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='تقطيع'
                     AND batch_start_date BETWEEN ? AND ? ORDER BY batch_start_date, batch_no""", (d1, d2)):
        bn = w['batch_no']
        n_roll = db.one('SELECT COUNT(*) n FROM slitting WHERE batch_no=?', (bn,))['n']
        subs = db.q('SELECT stock_status, consumed_by_batch FROM subrolls WHERE batch_no=? OR slit_batch=?', (bn, bn))
        used = [x for x in subs if x['stock_status'] == 'مستهلك']
        rows.append([bn, w['wo_no'], w['batch_start_date'], w['qty_required'], n_roll, len(subs), len(used),
                     len(subs) - len(used), '، '.join(sorted({x['consumed_by_batch'] for x in used
                                                              if x['consumed_by_batch']})) or '—', w['status']])
    return 'أوامر التقطيع (الأسليتر)', cols, rows


def _r_quality(d1, d2):
    cols = ['السجل', 'النموذج', 'الإدارة', 'التاريخ', 'الوردية', 'التشغيلة', 'الخط', 'النتيجة',
            'بنود غير مطابقة', 'القرار', 'الفاحص']
    rows = [[r['rec_no'], r['title'], r['dept'], r['rec_date'], r['shift'], r['batch_no'] or '—',
             r['line_code'], r['result'], r['fail_count'], r['decision'] or '—', r['inspector']]
            for r in db.q("""SELECT r.*, t.title, t.dept FROM qc_records r JOIN qc_templates t ON t.code=r.template_code
                             WHERE r.rec_date BETWEEN ? AND ? ORDER BY r.rec_date, r.id""", (d1, d2))]
    return 'سجلات الجودة', cols, rows


def _r_ncr(d1, d2):
    cols = ['الرقم', 'التاريخ', 'التشغيلة', 'المرحلة', 'الوصف', 'الخطورة', 'التصرّف', 'المسؤول', 'الحالة', 'تاريخ الإغلاق']
    rows = [[r['dev_no'], r['ddate'], r['batch_no'] or '—', r['stage'], r['description'], r['severity'],
             r['disposition'] or '—', r['owner'] or '—', r['status'], r['close_date'] or '—']
            for r in db.q("SELECT * FROM deviations WHERE ddate BETWEEN ? AND ? ORDER BY id", (d1, d2))]
    return 'عدم المطابقة', cols, rows


REPORTS = {
    'batches': ('سجل تشغيلات الإنتاج', 'كل أمر إنتاج بتقدّمه ومراحله وقرار إفراجه', _r_batches),
    'daily':   ('الإنتاج اليومي', 'حصيلة كل يوم عبر الأسليتر والطي والفرز والتغليف', _r_daily),
    'slit':    ('أوامر التقطيع', 'إنتاج الأسليتر ومخزون السب رول المتاح والمسحوب', _r_slit),
    'ncr':     ('عدم المطابقة', 'الحالات المفتوحة والمغلقة والتصرّف بالمنتج', _r_ncr),
    'quality': ('سجلات الجودة', 'السجلات اليومية والدورية والإفراج ونتائجها', _r_quality),
}


def register(app):
    def route(path, endpoint, **kw):
        def deco(fn):
            app.add_url_rule(path, endpoint=endpoint, view_func=fn, **kw)
            return fn
        return deco

    @route('/manager', 'manager_home')
    @auth.require('reports')
    def manager_home():
        today = datetime.date.today().isoformat()
        open_orders = db.q("""SELECT * FROM work_orders WHERE COALESCE(order_type,'إنتاج')='إنتاج'
                              AND status IN ('صادر','قيد التنفيذ') ORDER BY due_date IS NULL, due_date,
                              batch_start_date""")
        for w in open_orders:
            w['prog'] = forms.batch_progress(w['batch_no'])
            w['late'] = bool(w.get('due_date') and w['due_date'] < today)
        open_sl = db.q("""SELECT w.*, (SELECT COUNT(*) FROM subrolls x WHERE x.slit_batch=w.batch_no OR x.batch_no=w.batch_no) subs
                          FROM work_orders w WHERE COALESCE(order_type,'إنتاج')='تقطيع'
                          AND status IN ('صادر','قيد التنفيذ') ORDER BY batch_start_date DESC""")
        due = [x for x in quality.due_items() if x['due']]
        week = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        bad = db.q("""SELECT r.*, t.title FROM qc_records r JOIN qc_templates t ON t.code=r.template_code
                      WHERE r.result='غير مطابق' AND r.rec_date>=? ORDER BY r.id DESC LIMIT 8""", (week,))
        k = dict(
            open_orders=len(open_orders),
            late=sum(1 for w in open_orders if w['late']),
            sr_stock=db.one("SELECT COUNT(*) n FROM subrolls WHERE stock_status='متاح'")['n'],
            wait_release=db.one("SELECT COUNT(*) n FROM post_ster_receipts WHERE final_status='بانتظار الإفراج'")['n'],
            wait_bi=db.one("""SELECT COUNT(*) n FROM cycles WHERE IFNULL(bi_result,'')<>'سالب — مطابق'
                              AND status<>'مرفوضة'""")['n'],
            rolls_hold=db.one("SELECT COUNT(*) n FROM rolls WHERE stock_status='حجر'")['n'],
            qc_due=len(due), qc_bad=len(bad),
            ncr_open=db.one("SELECT COUNT(*) n FROM deviations WHERE status<>'مغلق'")['n'])
        return render_template('manager_home.html', nav='mgr', k=k, open_orders=open_orders,
                               open_sl=open_sl, due=due[:8], bad=bad)

    @route('/reports', 'reports')
    @auth.require('reports')
    def reports():
        return render_template('reports.html', nav='reports', reports=REPORTS)

    @route('/reports/<name>', 'report_view')
    @auth.require('reports')
    def report_view(name):
        if name not in REPORTS:
            abort(404)
        d1, d2 = _rng()
        title, cols, rows = REPORTS[name][2](d1, d2)
        if request.args.get('fmt') == 'csv':
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            w.writerows(rows)
            db.log('export', 'reports', name, f'{d1}..{d2}; {len(rows)} rows')
            return Response('﻿' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': f'attachment; filename=report-{name}-{d1}_{d2}.csv'})
        return render_template('report_view.html', nav='reports', name=name, title=title, cols=cols,
                               rows=rows, d1=d1, d2=d2)
