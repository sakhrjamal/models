# -*- coding: utf-8 -*-
"""القائمة الشجرية للنظام — مرتبطة بالصلاحيات وبخطوط الإنتاج المسموحة للمستخدم.

كل عنصر: (تسمية, endpoint, kwargs, صلاحية, مسار تصنيع | None). عنصر بلا endpoint = عنوان قسم فرعي.
لا يظهر للمستخدم إلا ما يملك صلاحيته وما يعمل على خطه.
"""
from flask import url_for

import auth, mfg

SEC = None      # عنوان قسم


def _items():
    F, S, B = mfg.FULL, mfg.SP, mfg.BANDAGE
    return [
        ('الإنتاج', 'prod', [
            ('أوامر الإنتاج', 'wo_list', {}, 'view', None),
            ('أمر إنتاج / تقطيع جديد', 'wo_new', {}, 'wo_issue', None),
            (SEC, 'الشاش — تصنيع كامل من الرول', None, None, F),
            ('استلام رولات الشاش الخام', 'receipt_new', {'cls': 'ROLL'}, 'receive', F),
            ('الأسليتر', 'slit', {}, 'enter', F),
            ('مكائن الطي', 'fold', {}, 'enter', F),
            ('الفرز', 'sorting', {}, 'enter', F),
            ('التغليف (شاش)', 'packaging', {}, 'enter', F),
            ('دورات التعقيم', 'cycles', {}, 'view', F),
            (SEC, 'الشاش نصف المصنع SP', None, None, S),
            ('استلام خام SP', 'sp_receive', {}, 'receive', S),
            ('لوطات SP ورصيدها', 'sp_lots', {}, 'view', S),
            ('أوامر SP — تخصيص وتعبئة', 'sp_home', {}, 'enter', S),
            ('فرز وتغليف SP المعقم', 'sorting', {}, 'enter', S),
            (SEC, 'الرباط الضاغط', None, None, B),
            ('استلام جامبو رول الأربطة', 'receipt_new', {'cls': 'JUMBO'}, 'receive', B),
            ('تشغيل ماكينة الأربطة', 'bandage_home', {'go': 'machine'}, 'enter', B),
            ('تغليف الأربطة', 'bandage_home', {'go': 'wrap'}, 'enter', B),
            ('تعبئة البوكسات', 'bandage_home', {'go': 'box'}, 'enter', B),
            ('تعبئة الكراتين', 'bandage_home', {'go': 'carton'}, 'enter', B),
            (SEC, 'المتابعة', None, None, None),
            ('المنتجات تحت التشغيل (WIP)', 'wip', {}, 'view', None),
            ('المنتجات النهائية', 'fg', {}, 'view', None),
            ('صرف مواد التعبئة', 'materials', {}, 'enter', None),
        ]),
        ('المخزن', 'wh', [
            ('استلام المنتج التام', 'warehouse', {}, 'warehouse', None),
            ('المنتجات النهائية', 'fg', {}, 'view', None),
            ('الشحن', 'shipping', {}, 'view', None),
            ('استلام الخام', 'receipts', {}, 'view', None),
        ]),
        ('الجودة', 'quality', [
            ('المهام المنتظرة', 'quality_tasks', {}, 'view', None),
            (SEC, 'فحص المواد الخام', None, None, None),
            ('رولات الشاش', 'receipts', {'cls': 'ROLL'}, 'view', F),
            ('خام SP', 'receipts', {'cls': 'SP'}, 'view', S),
            ('جامبو رول الأربطة', 'receipts', {'cls': 'JUMBO'}, 'view', B),
            (SEC, 'فحص أثناء الإنتاج', None, None, None),
            ('فحوص وإفراجات التشغيلات', 'quality_home', {}, 'view', None),
            (SEC, 'فحص المنتج النهائي', None, None, None),
            ('منتجات الشاش', 'quality_final', {'route': F}, 'view', F),
            ('منتجات SP', 'quality_final', {'route': S}, 'view', S),
            ('الأربطة', 'quality_final', {'route': B}, 'view', B),
            (SEC, 'الإفراج والمتابعة', None, None, None),
            ('الإفراج النهائي (شهادات المعقم)', 'releases', {}, 'view', None),
            ('عدم المطابقة NCR', 'ncr_list', {}, 'view', None),
            ('سجل الفحوصات', 'quality_records', {}, 'view', None),
            ('قوالب الجودة', 'quality_templates', {}, 'view', None),
        ]),
        ('التتبع', 'trace', [
            ('مستكشف التشغيلة', 'trace_view', {}, 'view', None),
            ('تقرير الأصل والفرع (Genealogy)', 'genealogy', {}, 'view', None),
            ('التتبع الأمامي من لوط المورّد', 'forward', {}, 'view', None),
            ('مستكشف الأصناف', 'items', {}, 'view', None),
        ]),
        ('الإدارة', 'admin', [
            ('لوحة المدير', 'manager_home', {}, 'reports', None),
            ('التقارير', 'reports', {}, 'reports', None),
            ('بيانات المنتجات والمسارات', 'master_products', {}, 'wo_issue', None),
            ('مسارات التصنيع وبواباتها', 'master_routes', {}, 'reports', None),
            ('سجل التدقيق', 'audit_view', {}, 'audit', None),
            ('المستخدمون', 'users_admin', {}, 'admin', None),
            ('النسخ الاحتياطي', 'backup_admin', {}, 'admin', None),
        ]),
    ]


def build():
    """قائمة المجموعات المسموحة للمستخدم الحالي."""
    out = []
    for title, key, items in _items():
        shown, pending_sec = [], None
        for label, ep, kw, perm, rc in items:
            if label is SEC:                        # عنوان قسم: يُعرض فقط إذا ظهر تحته عنصر
                pending_sec = ep
                continue
            if perm and not auth.can(perm):
                continue
            if rc and not auth.line_ok(rc):
                continue
            if pending_sec:
                shown.append(dict(sec=pending_sec)); pending_sec = None
            shown.append(dict(label=label, url=url_for(ep, **kw), sub=bool(any(x.get('sec') for x in shown))))
        if shown:
            out.append(dict(title=title, key=key, items=shown))
    return out
