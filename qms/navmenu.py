# -*- coding: utf-8 -*-
"""القائمة الجانبية المبسّطة (حسب الصلاحية) ومسار التنقّل (Breadcrumb) التلقائي لكل صفحة.

  الإنتاج : لوحة، أوامر، سليتر، طي، خط SP، خط الرباط، منتجات مكتملة، سجل
  الجودة  : لوحة، بانتظار الاعتماد، مرفوض/معلّق، سجل
  المخزن  : استلام المنتج التام، مخزن المنتج التام، استلام الخام
  الإدارة : لوحة، أصناف، مواد، مخزون، تقارير، مستخدمون، إعدادات

كل عنصر يظهر فقط لمن يملك صلاحيته ويعمل على خطه؛ الخادم يفرض الصلاحية نفسها في access.py.
"""
from flask import url_for, request, g, current_app

import auth, mfg


def _groups():
    F, S, B = mfg.FULL, mfg.SP, mfg.BANDAGE
    # (عنوان, مفتاح, صلاحيات (أيٌّ منها), [ (تسمية, endpoint, kwargs, endpoints نشطة, مسار) ])
    return [
        ('الإنتاج', 'prod', ('prod_view',), [
            ('أوامر الإنتاج', 'wo_list', {}, ('wo_list', 'wo_new', 'order_view'), None),
            ('السليتر', 'slit', {}, ('slit', 'slit_work'), F),
            ('ماكينات الطي', 'fold', {}, ('fold', 'fold_work', 'fold_edit'), F),
            ('خط الشاش SP', 'sp_home', {}, ('sp_home', 'sp_alloc', 'sp_pack', 'sp_lots'), S),
            ('خط الرباط الضاغط', 'bandage_home', {}, ('bandage_home', 'bandage_alloc', 'bandage_machine', 'bandage_wrap'), B),
            ('المنتجات المكتملة', 'completed_list', {}, ('completed_list',), None),
            ('سجل الإنتاج', 'production_log', {}, ('production_log',), None),
        ]),
        ('الجودة', 'quality', ('qual_view',), [
            ('بانتظار الاعتماد', 'quality_pending', {}, ('quality_pending', 'quality_review'), None),
            ('المرفوض والمعلّق', 'quality_held', {}, ('quality_held',), None),
            ('سجل الجودة', 'quality_history', {}, ('quality_history',), None),
        ]),
        ('المخزن', 'wh', ('warehouse',), [
            ('استلام المنتج التام', 'warehouse', {}, ('warehouse',), None),
            ('مخزن المنتج التام', 'fg', {}, ('fg', 'shipping'), None),
            ('استلام الخام', 'receipts', {}, ('receipts', 'receipt_new', 'receipt_view', 'sp_receive'), None),
        ]),
        ('الإدارة', 'admin', ('master', 'reports', 'admin'), [
            ('الأصناف', 'master_products', {}, ('master_products', 'master_product', 'master_product_new', 'master_routes'), None),
            ('المواد الخام', 'receipts', {}, ('receipts', 'receipt_new', 'receipt_view', 'sp_receive'), None),
            ('المخزون', 'fg', {}, ('fg', 'wip', 'shipping'), None),
            ('التقارير', 'reports', {}, ('reports', 'report_view'), None),
            ('المستخدمون', 'users_admin', {}, ('users_admin',), None),
            ('الإعدادات', 'settings_admin', {}, ('settings_admin', 'audit_view', 'backup_admin', 'ncr_list', 'ncr_new',
                                                 'ncr_view', 'quality_templates'), None),
        ]),
    ]


def build():
    """المجموعات المسموحة للمستخدم الحالي، مع تعليم العنصر النشط. لا يتكرر رابط بين مجموعتين."""
    ep = request.endpoint or ''
    groups = [g_ for g_ in _groups() if any(auth.can(p) for p in g_[2])]
    # أولوية الإدارة: عنصر يظهر في «الإدارة» لا يتكرر في «المخزن» لمن يملك الاثنين
    claimed = {endpoint for title, key, perms, items in groups if key == 'admin' for _, endpoint, *_r in items}
    out = []
    for title, key, perms, items in groups:
        shown = []
        for label, endpoint, kw, actives, rc in items:
            if rc and not auth.line_ok(rc):
                continue
            if key == 'wh' and endpoint in claimed:
                continue
            shown.append(dict(label=label, url=url_for(endpoint, **kw), active=ep in actives))
        if shown:
            out.append(dict(title=title, key=key, items=shown))
    return out


# endpoint → (عنوان الصفحة في المسار، endpoint الأب). {bn} و{doc_no} … تؤخذ من عنوان الصفحة/السياق.
PAGES = {
    'wo_list': ('أوامر الإنتاج', None),
    'wo_new': ('أمر إنتاج جديد', 'wo_list'),
    'order_view': ('الأمر {bn}', 'wo_list'),
    'slit': ('السليتر', None),
    'slit_work': ('السليتر', 'order_view'),
    'fold': ('ماكينات الطي', None),
    'fold_work': ('الطي', 'order_view'),
    'fold_edit': ('تعديل السند {doc_no}', 'fold_work'),
    'completed_list': ('المنتجات المكتملة', None),
    'production_log': ('سجل الإنتاج', None),
    'tags': ('بطاقات السب رول', 'order_view'),
    'sp_home': ('خط الشاش SP', None), 'sp_alloc': ('تخصيص الخام', 'sp_home'), 'sp_pack': ('التعبئة', 'sp_home'),
    'sp_lots': ('لوطات SP', 'sp_home'), 'sp_receive': ('استلام خام SP', 'receipts'),
    'bandage_home': ('خط الرباط الضاغط', None), 'bandage_alloc': ('تخصيص الجامبو', 'bandage_home'),
    'bandage_machine': ('ماكينة الأربطة', 'bandage_home'), 'bandage_wrap': ('التغليف', 'bandage_home'),
    'quality_pending': ('بانتظار الاعتماد', None),
    'quality_review': ('مراجعة الدفعة {bn}', 'quality_pending'),
    'quality_held': ('المرفوض والمعلّق', None),
    'quality_history': ('سجل الجودة', None),
    'warehouse': ('استلام المنتج التام', None),
    'fg': ('مخزن المنتج التام', None),
    'wip': ('المنتجات تحت التشغيل', 'fg'),
    'shipping': ('الشحن', 'fg'),
    'receipts': ('استلام الخام', None),
    'receipt_new': ('استلام جديد', 'receipts'),
    'receipt_view': ('سند الاستلام {grn_no}', 'receipts'),
    'master_products': ('الأصناف', None),
    'master_product': ('{code}', 'master_products'),
    'master_product_new': ('صنف جديد', 'master_products'),
    'master_routes': ('مسارات التصنيع', 'master_products'),
    'reports': ('التقارير', None),
    'report_view': ('تقرير', 'reports'),
    'users_admin': ('المستخدمون', None),
    'settings_admin': ('الإعدادات', None),
    'audit_view': ('سجل التدقيق', 'settings_admin'),
    'backup_admin': ('النسخ الاحتياطي', 'settings_admin'),
    'ncr_list': ('عدم المطابقة', 'settings_admin'),
    'ncr_new': ('عدم مطابقة جديدة', 'ncr_list'),
    'ncr_view': ('عدم المطابقة {dev_no}', 'ncr_list'),
    'quality_templates': ('قوالب الجودة', 'settings_admin'),
    'notifications': ('الإشعارات', None),
    'account': ('حسابي', None),
    'trace_view': ('تتبع التشغيلة', None),
    'genealogy': ('الأصل والفرع', None),
    'forward': ('التتبع الأمامي', None),
    'items': ('مستكشف الأصناف', None),
}


def _url(ep, args):
    """رابط endpoint بالوسائط المتاحة؛ None إن نقصت وسيطة إلزامية."""
    try:
        rules = list(current_app.url_map.iter_rules(ep))
        if not rules:
            return None
        rule = rules[0]
        need = {a for a in rule.arguments if a not in (rule.defaults or {})}
        if need - set(args):
            return None
        return url_for(ep, **{k: v for k, v in args.items() if k in rule.arguments})
    except Exception:
        return None


def crumbs():
    """[(عنوان, رابط|None)] من الرئيسية حتى الصفحة الحالية — تُبنى تلقائيًا لكل صفحة."""
    ep = request.endpoint or ''
    if ep in ('index', 'login', 'static', 'error') or ep not in PAGES:
        return []
    args = dict(request.view_args or {})
    args.update(getattr(g, 'crumb_args', {}) or {})
    chain, cur, guard = [], ep, 0
    while cur and cur in PAGES and guard < 6:
        title, parent = PAGES[cur]
        try:
            title = title.format(**args)
        except (KeyError, IndexError):
            title = title.split('{')[0].strip() or title
        chain.append((title, None if cur == ep else _url(cur, args)))
        cur, guard = parent, guard + 1
    chain.append(('الرئيسية', url_for('index')))
    chain.reverse()
    return chain
