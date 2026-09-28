# -*- coding: utf-8 -*-
"""مفردات الحالات والقرارات — مصدر واحد للحقيقة.

كل نص حالة أو قرار يُقارَن في النظام يجب أن يأتي من هنا، لا أن يُكتب حرفيًا
داخل المنطق. هذا يمنع أخطاء مثل اختلاف شرطة المدّ (—) عن الشرطة العادية (-)
من أن تكسر حكم اجتياز/رفض دورة التعقيم بصمت.
"""
import re

# ----------------------------------------------------------- أدوار المستخدمين
# v12: الصلاحيات صريحة لكل دور (لا ترتيب هرمي) حتى لا يرث مدير المصنع
# صلاحية اعتماد الجودة، ولا يرث مراقب الجودة صلاحية إصدار أوامر الإنتاج.
ROLES = {'viewer': 0, 'operator': 1, 'qc': 2, 'qa': 3, 'manager': 4, 'admin': 5}
ROLE_AR = {
    'viewer':   'عرض فقط',
    'operator': 'إدخال إنتاج',
    'qc':       'مراقب جودة (QC)',
    'qa':       'ضمان جودة (QA)',
    'manager':  'مدير المصنع',
    'admin':    'مدير النظام',
}
PERMS = {
    # view       فتح الشاشات للقراءة
    # enter      إدخال بيانات الإنتاج (استلام، أسليتر، طي، فرز، تغليف، دورات)
    # wo_issue   إصدار أوامر الإنتاج/التقطيع وأرقام التشغيلات
    # wo_close   إغلاق/إعادة فتح التشغيلة
    # reports    التقارير ولوحة المتابعة
    # audit      سجل التدقيق
    # qc_record  تسجيل سجلات الجودة (يومي/دوري/إفراج مرحلة)
    # qc_sign    اعتماد قرارات الجودة: فحص الخام، شهادة الإفراج، سجلات QA
    # qc_template تعديل قوالب الجودة
    # admin      المستخدمون والنسخ الاحتياطي
    'viewer':   {'view'},
    'operator': {'view', 'enter'},
    'qc':       {'view', 'qc_record'},
    'qa':       {'view', 'enter', 'qc_record', 'qc_sign', 'qc_template', 'audit', 'wo_close'},
    'manager':  {'view', 'wo_issue', 'wo_close', 'reports', 'audit'},
    'admin':    {'view', 'enter', 'wo_issue', 'wo_close', 'reports', 'audit',
                 'qc_record', 'qc_sign', 'qc_template', 'admin'},
}

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
