# -*- coding: utf-8 -*-
"""الإعدادات: ما يلزم لحساب الاحتياج التلقائي ومواقع التخزين — وروابط الإدارة المتقدمة."""
from flask import render_template, request, redirect, url_for, flash

import db, auth
from util import s, num

EDITABLE = [
    ('default_jumbo_width_cm', 'عرض الجامبو الافتراضي (سم)', 'يُستخدم لحساب احتياج الخامة قبل اختيار جامبو فعلي', 'num'),
    ('slitter_edge_trim_each_cm', 'تنظيف كل طرف من الجامبو (سم)', 'العرض المفيد = عرض الجامبو − هذا × 2', 'num'),
    ('fg_location', 'موقع مخزن المنتج التام الافتراضي', 'يُقترح عند تخزين الدفعة المعتمدة', 'text'),
    ('session_hours', 'مدة الجلسة (ساعات)', '', 'num'),
    ('factory_name', 'اسم المصنع (يظهر في تذييل المطبوعات)', '', 'text'),
    ('print_margin_top_mm', 'هامش الطباعة العلوي (مم) — حسب ترويسة الورق الرسمي', 'يُضبط ليتجاوز شعار/ترويسة الورق الرسمي', 'num'),
    ('print_margin_bottom_mm', 'هامش الطباعة السفلي (مم)', 'يتجاوز تذييل الورق الرسمي', 'num'),
    ('print_margin_side_mm', 'هامش الطباعة الجانبي (مم)', '', 'num'),
    ('aeration_min_h', 'أقل مدة للتهوية (ساعة)', 'تحذير عند إنهاء التهوية قبلها', 'num'),
]


def register(app):
    @app.route('/admin/settings', endpoint='settings_admin', methods=['GET', 'POST'])
    @auth.require('admin')
    def settings_admin():
        if request.method == 'POST':
            changed = []
            for key, _, _, kind in EDITABLE:
                v = s(request.form.get(key))
                if v is None:
                    continue
                if kind == 'num' and (num(v) is None or num(v) < 0):
                    flash(f'قيمة غير صحيحة: {key}', 'bad')
                    return redirect(url_for('settings_admin'))
                old = db.setting(key)
                if old != v:
                    db.run("INSERT INTO settings(key,value,note) VALUES(?,?,'') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, v))
                    changed.append(f'{key}: {old} → {v}')
            if changed:
                db.log('update', 'settings', 'settings', '; '.join(changed))
            flash('حُفظت الإعدادات', 'ok')
            return redirect(url_for('settings_admin'))
        vals = {k: db.setting(k, '') for k, *_ in EDITABLE}
        import printing
        return render_template('settings.html', nav='settings', fields=EDITABLE, vals=vals, lh=printing.letterhead_path())

    @app.route('/admin/letterhead', endpoint='letterhead_upload', methods=['POST'])
    @auth.require('admin')
    def letterhead_upload():
        import os, printing
        d = os.path.dirname(db.DB_PATH)
        if request.form.get('act') == 'delete':
            for ext in printing.LH_EXT:
                p = os.path.join(d, f'letterhead.{ext}')
                if os.path.exists(p):
                    os.remove(p)
            db.log('delete', 'letterhead', 'letterhead', '')
            flash('حُذف الورق الرسمي', 'ok')
            return redirect(url_for('settings_admin'))
        f = request.files.get('file')
        ext = (f.filename.rsplit('.', 1)[-1].lower() if f and '.' in f.filename else '')
        if not f or ext not in printing.LH_EXT:
            flash('ارفع الورق الرسمي كصورة PNG أو JPG أو SVG أو WEBP بحجم A4', 'bad')
            return redirect(url_for('settings_admin'))
        data = f.read()
        if len(data) > 8 * 1024 * 1024:
            flash('حجم الملف أكبر من 8 ميغابايت', 'bad')
            return redirect(url_for('settings_admin'))
        for e in printing.LH_EXT:
            p = os.path.join(d, f'letterhead.{e}')
            if os.path.exists(p):
                os.remove(p)
        with open(os.path.join(d, f'letterhead.{ext}'), 'wb') as fh:
            fh.write(data)
        db.log('update', 'letterhead', 'letterhead', f'{ext}; {len(data)} bytes')
        flash('حُفظ الورق الرسمي وسيُستخدم في كل المطبوعات', 'ok')
        return redirect(url_for('settings_admin'))
