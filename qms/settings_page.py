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
        return render_template('settings.html', nav='settings', fields=EDITABLE, vals=vals)
