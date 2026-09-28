# -*- coding: utf-8 -*-
"""قوالب سجلات الجودة المبدئية — خطوط الشاش (الأسليتر/الرولات ومكائن الطي).

12 قالبًا = منطقتان (أسليتر، طي) × إدارتان (QC، QA) × ثلاثة أنواع (إفراج، يومي، دوري).

هذه القوالب **نقطة بداية** بنود عامة متعارف عليها في تصنيع الشاش الطبي، بلا حدود
رقمية مفترضة. تفاصيل نظام الجودة الفعلية (البنود، الحدود، التكرار) تُدخَل لاحقًا
من شاشة «قوالب الجودة» دون أي تعديل في الكود؛ كل سجل يحفظ نسخة من القالب وقت
تعبئته فلا تتأثر السجلات القديمة بالتعديل.

نوع الحقل:  number | text | textarea | check (مطابق/غير مطابق/لا ينطبق) | select
الحدود:     min / max اختياريان للحقول الرقمية — الخروج عنهما = «غير مطابق» آليًا.
"""

CHECK_OPTIONS = ['مطابق', 'غير مطابق', 'لا ينطبق']

AREAS = {
    'slitter': dict(ar='خط الأسليتر (الرولات)', code='SLT', stage='القص الطولي'),
    'folding': dict(ar='مكائن الطي', code='FLD', stage='التقطيع والطي'),
}
DEPTS = {'QC': 'مراقبة الجودة', 'QA': 'ضمان الجودة'}
KINDS = {
    'release':  dict(ar='إفراج', code='REL'),
    'daily':    dict(ar='سجل يومي', code='DLY'),
    'periodic': dict(ar='سجل دوري', code='PRD'),
}
# الفاصل الافتراضي لاستحقاق السجل (ساعات) — يُعدَّل من شاشة القوالب
DEFAULT_FREQ = {
    ('QC', 'daily'): 24, ('QC', 'periodic'): 4,
    ('QA', 'daily'): 24, ('QA', 'periodic'): 168,
    ('QC', 'release'): None, ('QA', 'release'): None,
}


def _c(id_, label, req=True):
    return dict(id=id_, label=label, type='check', required=req)


def _n(id_, label, unit=None, req=False, min=None, max=None):
    f = dict(id=id_, label=label, type='number', required=req)
    if unit:
        f['unit'] = unit
    if min is not None:
        f['min'] = min
    if max is not None:
        f['max'] = max
    return f


def _t(id_, label, req=False):
    return dict(id=id_, label=label, type='text', required=req)


NOTES = dict(id='notes', label='ملاحظات / إجراء تصحيحي', type='textarea', required=False)

# ------------------------------------------------------------------ الحقول حسب التركيبة
FIELDS = {
    # ---------------- الأسليتر / الرولات
    ('slitter', 'QC', 'daily'): [
        _t('roll_no', 'رقم الرول قيد التشغيل', True),
        _n('roll_width', 'عرض الرول الأب', 'سم'),
        _n('weight_gsm', 'وزن المتر المربع', 'جم/م²'),
        _t('mesh_count', 'عدد الخيوط (سداة × لحمة)'),
        _c('visual', 'الفحص البصري: ثقوب / بقع / شوائب / خيوط مقطوعة'),
        _c('edges', 'حالة الحواف والقص'),
        _n('slit_dev', 'انحراف عرض القص عن المعياري', 'سم'),
        _c('knives', 'حالة سكاكين القص'),
        _c('cleanliness', 'نظافة الخط ومنطقة العمل'),
        NOTES],
    ('slitter', 'QC', 'periodic'): [
        _t('roll_no', 'رقم الرول', True),
        _n('w1', 'عرض السب رول — قراءة 1', 'سم'),
        _n('w2', 'عرض السب رول — قراءة 2', 'سم'),
        _n('w3', 'عرض السب رول — قراءة 3', 'سم'),
        _c('winding', 'انتظام اللف وشد الرول'),
        _c('visual', 'الفحص البصري للعيوب'),
        _c('foreign', 'خلو من المواد الغريبة'),
        _c('labels', 'مطابقة بطاقات التعريف للسب رول'),
        NOTES],
    ('slitter', 'QC', 'release'): [
        _c('clearance', 'تفريغ الخط من مواد التشغيلة السابقة (Line Clearance)'),
        _c('roll_status', 'الرولات الداخلة مفرج عنها من فحص الخام'),
        _c('slit_plan', 'مطابقة خطة القص للمقاسات المعتمدة'),
        _c('first_roll', 'اعتماد أول رول (فحص بصري وأبعاد)'),
        _c('labels', 'بطاقات التعريف جاهزة وصحيحة'),
        _c('cleanliness', 'نظافة الخط قبل التشغيل'),
        NOTES],
    ('slitter', 'QA', 'daily'): [
        _c('hygiene', 'النظافة الشخصية والملابس'),
        _c('documents', 'اكتمال تعبئة سندات الإنتاج والجودة'),
        _c('calibration', 'حالة معايرة أدوات القياس'),
        _n('temp', 'درجة حرارة الصالة', '°م'),
        _n('humidity', 'الرطوبة النسبية', '%'),
        _c('segregation', 'فصل وتعريف المواد (حجر / مفرج / مرفوض)'),
        _c('housekeeping', 'النظافة العامة ومكافحة الآفات'),
        NOTES],
    ('slitter', 'QA', 'periodic'): [
        _c('batch_record', 'مراجعة عينة من سجلات التشغيل'),
        _c('deviations', 'متابعة حالات عدم المطابقة المفتوحة'),
        _c('training', 'سجلات تدريب المشغّلين محدّثة'),
        _c('maintenance', 'حالة الصيانة الوقائية للخط'),
        _c('traceability', 'اختبار التتبع (رول ← سب رول)'),
        NOTES],
    ('slitter', 'QA', 'release'): [
        _c('qc_release', 'مراجعة سجل إفراج QC للخط'),
        _c('rm_released', 'مراجعة قرار فحص الخام للرولات المستخدمة'),
        _c('docs', 'مراجعة الوثائق قبل البدء'),
        _c('personnel', 'المشغّلون مؤهلون ومدرّبون'),
        NOTES],
    # ---------------- مكائن الطي
    ('folding', 'QC', 'daily'): [
        _n('piece_size', 'أبعاد المسحة بعد الطي', 'سم'),
        _n('ply_count', 'عدد الطبقات', 'طبقة'),
        _c('fold_align', 'انتظام الطي وتطابق الأطراف'),
        _c('edge_fold', 'الحواف المطوية (Folded Edge)'),
        _n('piece_weight', 'وزن المسحة', 'جم'),
        _c('xray', 'وجود الخيط الكاشف (للأصناف X-Ray)'),
        _c('visual', 'الفحص البصري: بقع / جسيمات / شوائب'),
        _n('counter', 'قراءة العداد', None),
        _c('cleanliness', 'نظافة الماكينة ومنطقة العمل'),
        NOTES],
    ('folding', 'QC', 'periodic'): [
        _n('size_avg', 'متوسط الأبعاد (عينة)', 'سم'),
        _n('weight_avg', 'متوسط الوزن (عينة)', 'جم'),
        _n('ply_count', 'عدد الطبقات', 'طبقة'),
        _c('visual', 'الفحص البصري للعينة'),
        _c('count_carton', 'مطابقة العدد داخل الكرتونة'),
        _c('labels', 'مطابقة بطاقة الكرتونة (تشغيلة/صنف/تاريخ)'),
        NOTES],
    ('folding', 'QC', 'release'): [
        _c('clearance', 'تفريغ الماكينة من مواد التشغيلة السابقة (Line Clearance)'),
        _c('machine_size', 'ضبط الماكينة على مقاس المنتج المطلوب'),
        _c('subroll_tags', 'مطابقة بطاقات السب رول للتشغيلة والصنف'),
        _c('first_piece', 'اعتماد أول قطعة (أبعاد وطبقات ومظهر)'),
        _c('cleanliness', 'نظافة الماكينة قبل التشغيل'),
        NOTES],
    ('folding', 'QA', 'daily'): [
        _c('hygiene', 'النظافة الشخصية والملابس'),
        _c('documents', 'اكتمال تعبئة سندات الطي والجودة'),
        _c('calibration', 'حالة معايرة أدوات القياس'),
        _n('temp', 'درجة حرارة الصالة', '°م'),
        _n('humidity', 'الرطوبة النسبية', '%'),
        _c('segregation', 'فصل وتعريف الكراتين والتالف'),
        _c('housekeeping', 'النظافة العامة ومكافحة الآفات'),
        NOTES],
    ('folding', 'QA', 'periodic'): [
        _c('batch_record', 'مراجعة عينة من سجلات الطي'),
        _c('deviations', 'متابعة حالات عدم المطابقة المفتوحة'),
        _c('training', 'سجلات تدريب المشغّلين محدّثة'),
        _c('maintenance', 'حالة الصيانة الوقائية للماكينة'),
        _c('traceability', 'اختبار التتبع (كرتونة ← سب رول ← رول)'),
        NOTES],
    ('folding', 'QA', 'release'): [
        _c('qc_release', 'مراجعة سجل إفراج QC للماكينة'),
        _c('subroll_ok', 'مراجعة مصدر السب رول (تتبع الخام)'),
        _c('docs', 'مراجعة الوثائق قبل البدء'),
        _c('personnel', 'المشغّلون مؤهلون ومدرّبون'),
        NOTES],
}


def templates():
    """يولّد قائمة قواميس القوالب الـ 12 جاهزة للإدراج."""
    out = []
    for (area, dept, kind), fields in FIELDS.items():
        a, k = AREAS[area], KINDS[kind]
        out.append(dict(
            code=f"{dept}-{a['code']}-{k['code']}",
            title=f"{k['ar']} — {DEPTS[dept]} — {a['ar']}",
            dept=dept, kind=kind, area=area,
            freq_hours=DEFAULT_FREQ[(dept, kind)],
            needs_batch=1 if kind == 'release' else 0,
            fields=fields,
            note='قالب مبدئي — بنوده عامة وبلا حدود رقمية، بانتظار اعتماد تفاصيل نظام الجودة.',
        ))
    return out
