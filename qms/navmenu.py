# -*- coding: utf-8 -*-
"""القائمة الجانبية الشجرية مرتبة بتسلسل العمل الفعلي (عناوين قابلة للفتح والإغلاق) + مسار التنقّل التلقائي.

كل عنصر يظهر فقط لمن يملك صلاحيته ويعمل على خطه — ولا تُعرض عناصر غير مصرَّح بها ولو معطَّلة.
الخادم يفرض الصلاحية نفسها في access.py.
"""
from flask import url_for, request, g, current_app

import auth, mfg

# أيقونات SVG (24×24، خطية) — تُلوَّن بلون المجموعة
ICONS = {
    'home': 'M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10',
    'factory': 'M3 21V9l6 4V9l6 4V5h6v16H3zM8 17h1M13 17h1M18 17h1',
    'order': 'M7 3h10l3 3v15H4V3h3zM8 9h8M8 13h8M8 17h5',
    'slit': 'M4 6h16M4 18h16M9 6v12M15 6v12M12 3v3M12 18v3',
    'roll': 'M5 8a7 3 0 1 0 14 0 7 3 0 1 0-14 0zM5 8v8c0 1.7 3.1 3 7 3s7-1.3 7-3V8M12 11v5',
    'fold': 'M4 5h16v14H4zM4 12h16M12 5v14M8 8.5l2 2M14 8.5l2 2',
    'box': 'M3 7l9-4 9 4-9 4-9-4zM3 7v10l9 4 9-4V7M12 11v10',
    'pack': 'M5 8h14l-1.5 12h-11L5 8zM8 8a4 4 0 0 1 8 0',
    'sp': 'M4 9l8-5 8 5v8l-8 5-8-5V9zM12 4v18M4 9l8 5 8-5',
    'sort': 'M4 6h10M4 12h7M4 18h4M17 4v16M14 17l3 3 3-3',
    'steril': 'M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8M9 12a3 3 0 1 0 6 0 3 3 0 1 0-6 0',
    'wind': 'M3 8h11a3 3 0 1 0-3-3M3 12h15a3 3 0 1 1-3 3M3 16h8',
    'line': 'M3 17l4-4 4 3 5-7 5 4M3 21h18',
    'check': 'M5 12l4 4 10-10M3 3h18v18H3z',
    'shield': 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3zM8.5 12l2.5 2.5L16 9.5',
    'clock': 'M12 7v5l3 2M3 12a9 9 0 1 0 18 0 9 9 0 1 0-18 0',
    'form': 'M6 3h9l4 4v14H6V3zM9 11h7M9 15h7M9 19h4M14 3v5h5',
    'ban': 'M5.6 5.6l12.8 12.8M3 12a9 9 0 1 0 18 0 9 9 0 1 0-18 0',
    'log': 'M5 4h14v16H5zM8 8h8M8 12h8M8 16h5',
    'stock': 'M4 20V10l8-6 8 6v10M9 20v-6h6v6',
    'card': 'M3 6h18v12H3zM3 10h18M7 15h4',
    'wip': 'M12 3v6l4 2M4 12a8 8 0 1 0 16 0 8 8 0 1 0-16 0M4 4v4h4',
    'moves': 'M7 4l-4 4 4 4M3 8h14M17 20l4-4-4-4M21 16H7',
    'chart': 'M4 20V6M4 20h16M8 16v-4M12 16V8M16 16v-6',
    'trace': 'M6 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM18 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM6 8v4a4 4 0 0 0 4 4h6M11 4h7v6',
    'gear': 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12l2-1-1-3-2 .5-1.5-1.5.5-2-3-1-1 2h-2l-1-2-3 1 .5 2L6.5 8.5 4.5 8l-1 3 2 1v2l-2 1 1 3 2-.5 1.5 1.5-.5 2 3 1 1-2h2l1 2 3-1-.5-2 1.5-1.5 2 .5 1-3-2-1v-2z',
    'users': 'M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 20a6 6 0 0 1 12 0M17 11a3 3 0 1 0-1-5.8M21 20a6 6 0 0 0-4-5.6',
    'excel': 'M6 3h9l4 4v14H6V3zM9 11l5 6M14 11l-5 6',
    'code': 'M8 8l-4 4 4 4M16 8l4 4-4 4M14 5l-4 14',
    'alert': 'M12 3l10 18H2L12 3zM12 10v5M12 18h.01',
    'door': 'M5 21V3h9l5 3v15M5 21h14M12 12h.01',
}


def _t():
    F, S, B = mfg.FULL, mfg.SP, mfg.BANDAGE
    I = lambda label, ep, icon, actives=None, perms=None, rc=None, **kw: dict(          # noqa: E731
        label=label, ep=ep, icon=icon, actives=tuple(actives or (ep,)), perms=perms, rc=rc, kw=kw)
    return [
        dict(key='prod', title='الإنتاج', icon='factory', color='amber', perms=('prod_view',), children=[
            I('أوامر الإنتاج', 'wo_list', 'order', ('wo_list', 'wo_new', 'order_view')),
            dict(sub='الشاش', children=[
                I('السليتر', 'slit', 'slit', ('slit', 'slit_work'), rc=F),
                I('الرولات الفرعية', 'subroll_list', 'roll', rc=F),
                I('ماكينة الطي', 'fold', 'fold', ('fold', 'fold_work', 'fold_edit'), rc=F),
                I('تعبئة غير المعقم', 'ns_list', 'pack', ('ns_list', 'ns_pack', 'ns_pack_edit'), rc=F),
                I('المنتج النصف مصنع SP', 'inter_list', 'sp', rc=F),
                I('الفرز والعد وتغليف المعقم', 'ster_home_all', 'sort', ('ster_home_all', 'ster_order', 'ster_record'), rc=F),
                I('التعقيم', 'ster_cycles', 'steril', ('ster_cycles', 'ster_cycle', 'ster_cycle_new'), rc=F),
                I('التهوية', 'ster_cycles', 'wind', ('ster_cycles',), rc=F, v='aeration'),
            ]),
            dict(sub='خطوط أخرى', children=[
                I('خط SP الخارجي', 'sp_home', 'line', ('sp_home', 'sp_alloc', 'sp_pack', 'sp_lots'), rc=S),
                I('خط الأربطة', 'bandage_home', 'line', ('bandage_home', 'bandage_alloc', 'bandage_machine', 'bandage_wrap'), rc=B),
            ]),
            I('المنتجات المكتملة', 'completed_list', 'check'),
            I('سجل الإنتاج', 'production_log', 'log'),
        ]),
        dict(key='quality', title='الجودة', icon='shield', color='green', perms=('qual_view',), children=[
            I('الإفراجات', 'quality_pending', 'shield', ('quality_pending', 'quality_review', 'quality_sp_review', 'quality_pre_review')),
            I('المهام المنتظرة', 'quality_tasks', 'clock'),
            I('قوالب وسجلات المتابعة', 'quality_home', 'form', ('quality_home', 'quality_new', 'quality_record', 'quality_records', 'quality_templates')),
            I('حالات الرفض والتعليق', 'quality_held', 'ban'),
            I('سجل الجودة', 'quality_history', 'log'),
            I('عدم المطابقة NCR', 'ncr_list', 'alert', ('ncr_list', 'ncr_new', 'ncr_view'), perms=('ncr',)),
        ]),
        dict(key='inv', title='المخزون', icon='stock', color='sky', perms=('warehouse', 'master', 'prod_view', 'qual_view'), children=[
            I('استلام المنتج التام', 'warehouse', 'door', perms=('warehouse',)),
            I('استلام الخام', 'receipts', 'roll', ('receipts', 'receipt_new', 'receipt_view', 'sp_receive'), perms=('receive', 'master')),
            I('بطاقات المواد', 'inv_cards', 'card', ('inv_cards', 'inv_card_new', 'inv_card'), perms=('warehouse', 'master')),
            I('المواد الخام (الرولات)', 'inv_raw', 'roll', perms=('warehouse', 'master', 'prod_view', 'qual_view')),
            I('تحت التشغيل WIP', 'wip', 'wip'),
            I('المنتجات النصف مصنعة', 'inter_list', 'sp', ('inter_list',)),
            I('المنتجات النهائية', 'fg', 'box', ('fg', 'shipping')),
            I('حركات المخزون', 'inv_moves', 'moves', perms=('warehouse', 'master')),
        ]),
        dict(key='rep', title='التقارير', icon='chart', color='violet', perms=('reports',), children=[
            I('تقارير الإنتاج', 'reports', 'chart', ('reports', 'report_view'), group='production'),
            I('تقارير الجودة', 'reports', 'chart', ('reports',), group='quality'),
        ]),
        dict(key='trace', title='التتبع', icon='trace', color='pink', perms=('view',), flat=True, children=[
            I('مركز التتبع', 'trace_center', 'trace', ('trace_center', 'trace_view', 'genealogy', 'forward')),
        ]),
        dict(key='admin', title='الإدارة', icon='gear', color='slate', perms=('master', 'admin'), children=[
            I('الأصناف والأكواد', 'master_products', 'code', ('master_products', 'master_product', 'master_product_new', 'master_routes')),
            I('Sub Roll Master', 'master_subrolls', 'roll'),
            I('Packaging Configuration', 'pack_config_list', 'box', ('pack_config_list', 'pack_config_edit')),
            I('المشغلون', 'operators_admin', 'users'),
            I('المستخدمون', 'users_admin', 'users', perms=('admin',)),
            I('الصلاحيات', 'roles_admin', 'shield', perms=('admin',)),
            I('استيراد Excel', 'import_center', 'excel', ('import_center',)),
            I('تنبيهات البيانات', 'data_alerts', 'alert'),
            I('الإعدادات', 'settings_admin', 'gear', ('settings_admin', 'audit_view', 'backup_admin'), perms=('admin',)),
        ]),
    ]


def _ok(perms):
    return not perms or any(auth.can(p) for p in perms)


def build():
    """الشجرة المسموحة للمستخدم الحالي؛ كل عنصر: label/url/active/icon، وكل مجموعة: title/icon/color/open."""
    ep = request.endpoint or ''
    out = []
    for grp in _t():
        if not _ok(grp['perms']):
            continue

        def mk(it):
            if not _ok(it['perms']) or (it['rc'] and not auth.line_ok(it['rc'])):
                return None
            kw = dict(it['kw'])
            act = ep in it['actives']
            if act and kw:
                act = all(request.args.get(k) == v for k, v in kw.items())
            elif act and (request.args.get('v') or request.args.get('group')):
                act = False
            return dict(label=it['label'], url=url_for(it['ep'], **kw), active=act, icon=it['icon'])
        items, opened = [], False
        for ch in grp['children']:
            if 'sub' in ch:
                sub = [x for x in (mk(i) for i in ch['children']) if x]
                if sub:
                    items.append(dict(sub=ch['sub'], items=sub))
                    opened = opened or any(x['active'] for x in sub)
            else:
                x = mk(ch)
                if x:
                    items.append(x)
                    opened = opened or x['active']
        if items:
            out.append(dict(key=grp['key'], title=grp['title'], icon=grp['icon'], color=grp['color'], items=items, open=opened,
                            flat=grp.get('flat', False)))
    return out


def icon(name):
    return ICONS.get(name, ICONS['form'])


# endpoint → (عنوان الصفحة في المسار، endpoint الأب). {bn} و{doc_no} … تؤخذ من عنوان الصفحة/السياق.
PAGES = {
    'wo_list': ('أوامر الإنتاج', None), 'wo_new': ('أمر إنتاج جديد', 'wo_list'), 'order_view': ('الأمر {bn}', 'wo_list'),
    'slit': ('السليتر', None), 'slit_work': ('السليتر', 'order_view'),
    'fold': ('ماكينة الطي', None), 'fold_work': ('الطي', 'order_view'), 'fold_edit': ('تعديل السند {doc_no}', 'fold_work'),
    'ns_list': ('تعبئة غير المعقم', None), 'ns_pack': ('التعبئة', 'order_view'), 'ns_pack_edit': ('تعديل سند {doc_no}', 'ns_pack'),
    'subroll_list': ('الرولات الفرعية', None), 'inter_list': ('المنتج النصف مصنع SP', None),
    'ster_home_all': ('الفرز والعد وتغليف المعقم', None), 'ster_order': ('المعالجة', 'order_view'),
    'ster_record': ('السجل {rec_no}', 'ster_order'), 'ster_cycles': ('التعقيم والتهوية', None),
    'ster_cycle_new': ('دورة تعقيم جديدة', 'ster_cycles'), 'ster_cycle': ('الدورة {cycle_no}', 'ster_cycles'),
    'completed_list': ('المنتجات المكتملة', None), 'production_log': ('سجل الإنتاج', None),
    'tags': ('بطاقات Sub Roll', 'order_view'), 'print_doc': ('طباعة', None), 'print_label': ('طباعة بطاقات', None),
    'sp_supply': ('SP وارد', 'order_view'), 'sp_home': ('خط SP الخارجي', None), 'sp_alloc': ('تخصيص الخام', 'sp_home'), 'sp_pack': ('التعبئة', 'sp_home'),
    'sp_lots': ('LOTs SP', 'sp_home'), 'sp_receive': ('استلام خام SP', 'receipts'),
    'bandage_home': ('خط الأربطة', None), 'bandage_alloc': ('تخصيص الجامبو', 'bandage_home'),
    'bandage_machine': ('ماكينة الأربطة', 'bandage_home'), 'bandage_wrap': ('التغليف', 'bandage_home'),
    'quality_pending': ('الإفراجات', None), 'quality_review': ('مراجعة الدفعة {bn}', 'quality_pending'),
    'quality_sp_review': ('إفراج SP — {bn}', 'quality_pending'), 'quality_pre_review': ('إفراج قبل التعقيم — {rec_no}', 'quality_pending'),
    'quality_tasks': ('المهام المنتظرة', None), 'quality_home': ('قوالب وسجلات المتابعة', None),
    'quality_new': ('سجل متابعة جديد', 'quality_home'), 'quality_records': ('سجلات المتابعة', 'quality_home'),
    'quality_record': ('سجل متابعة', 'quality_records'), 'quality_templates': ('إدارة القوالب', 'quality_home'),
    'quality_held': ('حالات الرفض والتعليق', None), 'quality_history': ('سجل الجودة', None),
    'warehouse': ('استلام المنتج التام', None), 'fg': ('المنتجات النهائية', None), 'wip': ('تحت التشغيل WIP', None),
    'shipping': ('الشحن', 'fg'), 'receipts': ('استلام الخام', None), 'receipt_new': ('استلام جديد', 'receipts'),
    'receipt_view': ('سند الاستلام {grn_no}', 'receipts'),
    'inv_cards': ('بطاقات المواد', None), 'inv_card_new': ('بطاقة مادة جديدة', 'inv_cards'), 'inv_card': ('بطاقة {code}', 'inv_cards'),
    'inv_raw': ('المواد الخام (الرولات)', None), 'inv_moves': ('حركات المخزون', None),
    'master_products': ('الأصناف والأكواد', None), 'master_product': ('{code}', 'master_products'),
    'master_product_new': ('صنف جديد', 'master_products'), 'master_routes': ('مسارات التصنيع', 'master_products'),
    'master_subrolls': ('Sub Roll Master', None), 'pack_config_list': ('Packaging Configuration', None),
    'pack_config_edit': ('{code}', 'pack_config_list'), 'operators_admin': ('المشغلون', None), 'roles_admin': ('الصلاحيات', None),
    'import_center': ('استيراد Excel', None), 'data_alerts': ('تنبيهات البيانات', None),
    'reports': ('التقارير', None), 'report_view': ('تقرير', 'reports'),
    'users_admin': ('المستخدمون', None), 'settings_admin': ('الإعدادات', None),
    'audit_view': ('سجل التدقيق', 'settings_admin'), 'backup_admin': ('النسخ الاحتياطي', 'settings_admin'),
    'ncr_list': ('عدم المطابقة', None), 'ncr_new': ('عدم مطابقة جديدة', 'ncr_list'), 'ncr_view': ('عدم المطابقة {dev_no}', 'ncr_list'),
    'notifications': ('الإشعارات', None), 'account': ('حسابي', None),
    'trace_center': ('مركز التتبع', None), 'trace_view': ('تتبع التشغيلة', 'trace_center'), 'genealogy': ('الأصل والفرع', 'trace_center'),
    'forward': ('التتبع الأمامي', 'trace_center'), 'items': ('مستكشف الأصناف', None),
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
    if ep in ('index', 'login', 'static', 'error', 'print_doc', 'print_label') or ep not in PAGES:
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
