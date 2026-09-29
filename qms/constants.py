# -*- coding: utf-8 -*-
"""مفردات الحالات والقرارات — مصدر واحد للحقيقة.

كل نص حالة أو قرار يُقارَن في النظام يجب أن يأتي من هنا، لا أن يُكتب حرفيًا
داخل المنطق. هذا يمنع أخطاء مثل اختلاف شرطة المدّ (—) عن الشرطة العادية (-)
من أن تكسر حكم اجتياز/رفض دورة التعقيم بصمت.
"""
import re

# ----------------------------------------------------------- أدوار المستخدمين
# v14: فصل كامل بين واجهات الإنتاج والجودة — يُفرض في الخادم (access.py) لا بإخفاء الأيقونات فقط.
#   الإنتاج  : prod_view + enter (لا يرى شيئًا من الجودة)
#   الجودة   : qual_view + qc_* (لا يرى ولا يُدخل أي تشغيل إنتاج)
#   الأدمن/المدير: كل الصلاحيات
ROLES = {'viewer': 0, 'operator': 1, 'store': 2, 'qc': 3, 'qa': 4, 'maint': 2, 'manager': 5, 'admin': 6}
ROLE_AR = {
    'viewer':   'عرض فقط',
    'operator': 'مستخدم إنتاج',
    'store':    'أمين مخزن',
    'qc':       'مستخدم جودة',
    'qa':       'مستخدم جودة (QA)',
    'maint':    'فني صيانة',
    'manager':  'مدير المصنع (كل الصلاحيات)',
    'admin':    'مدير النظام',
}
PERMS = {
    # view        الصفحات العامة (الرئيسية، الإشعارات، التتبع)
    # prod_view   شاشات الإنتاج    | enter  إدخال بيانات التشغيل وإنهاء الإنتاج
    # qual_view   شاشات الجودة     | qc_record فحص الخام/سجلات | qc_sign اعتماد المنتج النهائي
    # warehouse   إدخال المنتج التام للمخزن | receive استلام الخام
    # wo_issue    إصدار أوامر الإنتاج | reports التقارير | audit سجل التدقيق | admin المستخدمون والنسخ
    # master      بيانات المنتجات | route_override تغيير المسار استثنائيًا | qc_template/ncr إعدادات الجودة (أدمن)
    'viewer':   {'view', 'prod_view', 'qual_view'},
    'operator': {'view', 'prod_view', 'enter', 'maint_report'},
    'store':    {'view', 'receive', 'warehouse'},
    'qc':       {'view', 'qual_view', 'qc_record', 'qc_sign', 'ncr'},
    'qa':       {'view', 'qual_view', 'qc_record', 'qc_sign', 'ncr'},
    'maint':    {'view', 'maint_view', 'maint', 'maint_report'},
    'manager':  None,
    'admin':    None,
}
ALL_PERMS = {'view', 'prod_view', 'qual_view', 'enter', 'wo_issue', 'wo_close', 'reports', 'audit',
             'qc_record', 'qc_sign', 'qc_template', 'ncr', 'admin', 'receive', 'warehouse',
             'route_override', 'master', 'maint_view', 'maint', 'maint_report', 'maint_verify'}
PERMS['manager'] = set(ALL_PERMS)
PERMS['admin'] = set(ALL_PERMS)

# ----------------------------------------------------------- حالات المخزون
STOCK_HOLD     = 'حجر'
STOCK_RELEASED = 'مفرج'
STOCK_REJECTED = 'مرفوض'

# ----------------------------------------------------------- فحص المواد الخام
QC_ACCEPT, QC_COND, QC_REJECT, QC_HOLD = 'قبول', 'قبول مشروط', 'رفض', 'معلّق'
QC_DECISIONS = (QC_ACCEPT, QC_COND, QC_REJECT, QC_HOLD)
QC_TO_STOCK = {
    QC_ACCEPT: STOCK_RELEASED,
    QC_COND:   STOCK_RELEASED,
    QC_REJECT: STOCK_REJECTED,
    QC_HOLD:   STOCK_HOLD,
}

# ----------------------------------------------------------- شهادة الإفراج النهائي
REL_RELEASE, REL_REJECT, REL_HOLD = 'مفرج عنها', 'مرفوضة', 'معلّقة'
REL_DECISIONS = (REL_RELEASE, REL_REJECT, REL_HOLD)
REL_TO_PSR = {
    REL_RELEASE: STOCK_RELEASED,
    REL_REJECT:  STOCK_REJECTED,
    REL_HOLD:    STOCK_HOLD,
}

# ----------------------------------------------------------- نتائج المؤشرات
CONFORM     = 'مطابق'
NONCONFORM  = 'غير مطابق'
CI_RESULTS  = (CONFORM, NONCONFORM)

BI_NEG      = 'سالب — مطابق'
BI_POS      = 'موجب — غير مطابق'
BI_PENDING  = 'بانتظار'
BI_INCUB    = 'قيد الحضانة'
BI_RESULTS  = (BI_NEG, BI_POS, BI_PENDING, BI_INCUB)

CTRL_POS_OK = 'موجب — مطابق'
CTRL_NEG_OK = 'سالب — مطابق'

# ----------------------------------------------------------- حالات دورة التعقيم
CYC_RUNNING  = 'قيد التشغيل'
CYC_INCUB    = 'قيد الحضانة'
CYC_REVIEW   = 'قيد المراجعة'
CYC_PASSED   = 'اجتازت التعقيم'
CYC_REJECTED = 'مرفوضة'

# ----------------------------------------------------------- حالات التهوية
AER_READY    = 'مكتملة — جاهزة للاستلام'
AER_WAIT_BI  = 'مكتملة زمنيًا — بانتظار BI'
AER_REVIEW   = 'قيد المراجعة'
AER_STOPPED  = 'موقوفة — الدورة مرفوضة'

# ----------------------------------------------------------- حالات أمر التشغيل
WO_ISSUED  = 'صادر'
WO_RUNNING = 'قيد التنفيذ'
WO_DONE    = 'مكتملة'
WO_CLOSED  = 'مغلقة'
WO_OPEN    = (WO_ISSUED, WO_RUNNING)

# ----------------------------------------------------------- معاني التوقيع الإلكتروني
SIG_QC_DECISION = 'اعتماد نتيجة فحص المواد الخام'
SIG_RELEASE     = 'إصدار شهادة الإفراج النهائي'
SIG_VOID        = 'إبطال / تصحيح مستند'

# ----------------------------------------------------------- تطبيع النصوص
_LOOSE = re.compile(r'[\s‐-―−\-_]+')


def canon(value, choices):
    """يعيد الخيار القياسي المطابق للقيمة المدخلة بمقارنة متساهلة.

    يتجاهل فروق المسافات وأنواع الشرطات وحالة الأحرف. إن لم يجد تطابقًا
    يعيد القيمة كما هي (منزوعة الأطراف) حتى لا يفقد إدخال المستخدم.
    """
    if value is None:
        return None
    key = _LOOSE.sub('', str(value)).strip().lower()
    if not key:
        return None
    for c in choices:
        if _LOOSE.sub('', c).strip().lower() == key:
            return c
    return str(value).strip() or None


def is_conform(value):
    return canon(value, CI_RESULTS) == CONFORM


def is_nonconform(value):
    return canon(value, CI_RESULTS) == NONCONFORM
