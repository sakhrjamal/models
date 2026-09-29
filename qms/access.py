# -*- coding: utf-8 -*-
"""فصل الصلاحيات في الخادم: كل مسار يتطلب صلاحية قسمه (إنتاج / جودة / إدارة).

يُستدعى من before_request لكل طلب، فلا يكفي أن تختفي الأيقونة: فتح رابط قسم آخر مباشرة = 403.
القيمة: صلاحية واحدة أو مجموعة (يكفي امتلاك إحداها). ما لا يُذكر هنا يتطلب تسجيل الدخول فقط.
"""

PROD = ('prod_view',)
QUAL = ('qual_view',)
ADMIN = ('master',)

ENTER = ('enter',)

EXACT = {
    # ---- الإنتاج (عرض = prod_view، وأي حفظ/تعديل/حذف يتطلب enter داخل المسار وهنا أيضًا)
    'wo_list': PROD, 'order_view': PROD, 'order_finish': ENTER, 'slit': PROD, 'slit_work': PROD,
    'slit_confirm': ENTER, 'slit_plan_delete': ENTER, 'fold': PROD, 'fold_work': PROD, 'fold_edit': ENTER,
    'fold_delete': ENTER, 'slit_start': ENTER, 'ns_pack': PROD, 'ns_pack_edit': ENTER, 'ns_pack_delete': ENTER,
    'sp_supply': PROD, 'sp_supply_delete': ENTER, 'ster_order': PROD, 'ster_new': ENTER, 'ster_home_all': PROD, 'ster_record': PROD, 'ster_record_delete': ENTER,
    'ster_cycles': PROD, 'ster_cycle_new': ENTER, 'ster_cycle': PROD, 'ster_cycle_delete': ENTER,
    'print_doc': ('view',), 'print_label': ('view',), 'proc_delete': ENTER, 'alloc_delete': ENTER, 'completed_list': PROD, 'production_log': PROD, 'tags': PROD, 'wip': PROD,
    'sp_home': PROD, 'sp_alloc': PROD, 'sp_pack': PROD, 'sp_lots': PROD,
    'bandage_home': PROD, 'bandage_alloc': PROD, 'bandage_machine': PROD, 'bandage_wrap': PROD,
    'api_order_preview': ('wo_issue',), 'api_items_search': ('wo_issue',), 'wo_new': ('wo_issue',),
    'wo_close': ('wo_close',),
    # ---- الجودة (قرار الاعتماد يتطلب qc_sign)
    'quality_pending': QUAL, 'quality_review': QUAL, 'quality_decide': ('qc_sign',), 'quality_sp_decide': ('qc_sign',), 'quality_pre_decide': ('qc_sign',),
    'quality_cycle_verify': ('qc_sign',), 'quality_held': QUAL,
    'quality_history': QUAL,
    # ---- صفحات قديمة (فحوص/بوابات/تعقيم/فرز/تغليف/مواد) مخفية من القوائم ومحصورة بالإدارة
    'quality_home': QUAL, 'quality_new': ('qc_record',), 'quality_record': QUAL, 'quality_records': QUAL, 'quality_tasks': QUAL,
    'quality_templates': ('qc_template',), 'quality_template_edit': ('qc_template',),
    'quality_template_new': ('qc_template',), 'sorting': ADMIN, 'packaging': ADMIN, 'cycles': ADMIN,
    'cycle_new': ADMIN, 'cycle_view': ADMIN, 'cycle_results': ADMIN, 'aeration_save': ADMIN,
    'post_ster_receive': ADMIN, 'releases': ADMIN, 'release_new': ADMIN, 'materials': ADMIN,
    'ncr_list': ('ncr',), 'ncr_view': ('ncr',), 'ncr_new': ('ncr',),
    # ---- مشترك حسب الوظيفة
    'receipts': ('prod_view', 'qual_view', 'receive'), 'receipt_view': ('prod_view', 'qual_view', 'receive'),
    'receipt_new': ('receive',), 'sp_receive': ('receive',),
    'warehouse': ('warehouse', 'prod_view', 'qual_view'), 'fg': ('warehouse', 'prod_view', 'qual_view'),
    'shipping': ('warehouse', 'master'), 'shipping_void': ('master',),
    # ---- الإدارة
    'reports': ('reports',), 'report_view': ('reports',),
    'master_products': ADMIN, 'master_product': ADMIN, 'master_product_new': ADMIN, 'master_routes': ADMIN,
    'audit_view': ('audit',), 'users_admin': ('admin',), 'backup_admin': ('admin',), 'settings_admin': ('admin',), 'letterhead_upload': ('admin',), 'letterhead_toggle': ('admin',),
    'forward': ('view',), 'items': ('view',),
    'pack_config_list': ADMIN, 'pack_config_edit': ADMIN, 'master_subrolls': ADMIN, 'operators_admin': ADMIN,
    'roles_admin': ('admin',), 'data_alerts': ADMIN, 'master_rename': ADMIN, 'import_center': ADMIN,
    'inv_cards': ('view',), 'inv_card': ('view',), 'inv_card_new': ADMIN, 'inv_raw': ('warehouse', 'prod_view', 'qual_view'),
    'inv_moves': ('warehouse', 'prod_view', 'qual_view'), 'subroll_list': PROD, 'inter_list': PROD, 'ns_list': PROD,
    'maint_home': ('maint_view',), 'maint_plan': ('maint_view',), 'maint_orders': ('maint_view',), 'maint_order': ('maint_view',),
    'maint_generate': ('maint',), 'maint_report': ('maint_report',), 'maint_equipment': ADMIN, 'maint_checklist': ADMIN,
    'trace_center': ('view',), 'trace_api': ('view',), 'scan': ('view',), 'letterhead': ('view',),
}


def required(endpoint):
    """الصلاحيات المقبولة (أيٌّ منها) للمسار، أو None."""
    ep = endpoint or ''
    if ep in EXACT:
        return EXACT[ep]
    if ep.startswith('quality_'):
        return QUAL
    if ep.startswith('release'):
        return ADMIN
    if ep.startswith(('bandage_', 'sp_', 'slit_', 'fold_', 'order_')):
        return PROD
    return None
