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
    'fold_delete': ENTER, 'proc_delete': ENTER, 'alloc_delete': ENTER, 'completed_list': PROD, 'production_log': PROD, 'tags': PROD, 'wip': PROD,
    'sp_home': PROD, 'sp_alloc': PROD, 'sp_pack': PROD, 'sp_lots': PROD,
    'bandage_home': PROD, 'bandage_alloc': PROD, 'bandage_machine': PROD, 'bandage_wrap': PROD,
    'api_order_preview': ('wo_issue',), 'api_items_search': ('wo_issue',), 'wo_new': ('wo_issue',),
    'wo_close': ('wo_close',),
    # ---- الجودة (قرار الاعتماد يتطلب qc_sign)
    'quality_pending': QUAL, 'quality_review': QUAL, 'quality_decide': ('qc_sign',), 'quality_held': QUAL,
    'quality_history': QUAL,
    # ---- صفحات قديمة (فحوص/بوابات/تعقيم/فرز/تغليف/مواد) مخفية من القوائم ومحصورة بالإدارة
    'quality_home': ADMIN, 'quality_new': ADMIN, 'quality_record': ADMIN, 'quality_records': ADMIN,
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
    'audit_view': ('audit',), 'users_admin': ('admin',), 'backup_admin': ('admin',), 'settings_admin': ('admin',),
    'forward': ('view',), 'items': ('view',),
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
