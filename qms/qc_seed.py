# -*- coding: utf-8 -*-
"""قوالب سجلات الجودة — خطوط الشاش (منتج معقم وغير معقم).

القوالب بيانات لا كود: تُعدَّل بنودها وحدودها من شاشة «قوالب الجودة»، وكل سجل يحفظ
نسخة القالب وقت تعبئته. ما وُرد من إدارة المصنع مثبَّت هنا (سماحية الأبعاد ±4 ملم،
تسلسل الشاش من الحواف، الشوائب والشعر، مطابقة بيانات الطباعة لرقم التشغيلة والتاريخ،
فحص الكراتين ثم الإفراج، وللمعقم فحص العدد والشكل وطباعة المغلف والبوكس والكرتون
وبيانات التعقيم). وما أُضيف من عندنا مُعلَّم بـ «مقترح» في ملاحظة القالب، بلا حدود رقمية.

أنواع الحقول:
  check     مطابق / غير مطابق / لا ينطبق
  number    رقم، مع min / max اختياريين
  dim       قياس بُعد: المعياري من مقاس أمر الإنتاج (size_w عرض / size_l طول) ± tol سم
  expect    رقم يجب أن يساوي قيمة من التشغيلة (per_envelope / env_per_box / boxes_per_carton)
  match     نص مطبوع يُقارَن آليًا بـ batch_no / item_code / batch_date / cycle_no
  text | textarea | select

kind: release (إفراج بتوقيع) | inspection (فحص لكل تشغيلة) | daily | periodic
route: all | sterile | non_sterile   (نوع المنتج الذي ينطبق عليه القالب)
requires: رموز قوالب يجب أن يكون لكل منها سجل مطابق للتشغيلة قبل قبول «مفرج»
"""

AREAS = {
    'slitter':       dict(ar='الأسليتر (الرولات)',       stage='القص الطولي',    batch='SL'),
    'folding':       dict(ar='مكائن الطي',               stage='التقطيع والطي',  batch='PROD'),
    'packaging':     dict(ar='التغليف والمنتج النهائي',  stage='التغليف',        batch='PROD'),
    'sterilization': dict(ar='التعقيم',                  stage='التعقيم',        batch='PROD'),
    'general':       dict(ar='عام (مخبر / بيئة / عينات)', stage=None,             batch='PROD'),
    'bandage':       dict(ar='خط الرباط الضاغط',         stage='الأربطة',        batch='PROD'),
}
# مسارات التصنيع التي ينطبق عليها كل نوع منطقة افتراضيًا (تُعدَّل لكل قالب في قاعدة البيانات)
AREA_ROUTES = {
    'slitter': ['FULL_GAUZE'], 'folding': ['FULL_GAUZE'],
    'packaging': ['FULL_GAUZE', 'SP_GAUZE'], 'sterilization': ['FULL_GAUZE', 'SP_GAUZE'],
    'general': ['FULL_GAUZE', 'SP_GAUZE', 'BANDAGE'], 'bandage': ['BANDAGE'],
}
DEPTS = {'QC': 'مراقبة الجودة', 'QA': 'ضمان الجودة'}
KINDS = {
    'release':    dict(ar='إفراج'),
    'inspection': dict(ar='فحص تشغيلة'),
    'daily':      dict(ar='سجل يومي'),
    'periodic':   dict(ar='سجل دوري'),
}
ROUTES = {'all': 'الكل', 'sterile': 'معقم', 'non_sterile': 'غير معقم'}
ROUTE_OF = {'معقم': 'sterile', 'غير معقم': 'non_sterile'}
CHECK_OPTIONS = ['مطابق', 'غير مطابق', 'لا ينطبق']


def _c(id_, label, req=True):
    return dict(id=id_, label=label, type='check', required=req)


def _n(id_, label, unit=None, req=False, **kw):
    f = dict(id=id_, label=label, type='number', required=req, **kw)
    if unit:
        f['unit'] = unit
    return f


def _t(id_, label, req=False):
    return dict(id=id_, label=label, type='text', required=req)


def _dim(id_, label, of, tol=0.4, req=True):
    return dict(id=id_, label=label, type='dim', required=req, nominal_from=of, tol=tol, unit='سم' if of != 'length_m' else 'م')


def _match(id_, label, to):
    return dict(id=id_, label=label, type='match', required=True, match_to=to)


def _exp(id_, label, src, unit=None):
    f = dict(id=id_, label=label, type='expect', required=True, expect_from=src)
    if unit:
        f['unit'] = unit
    return f


NOTES = dict(id='notes', label='ملاحظات / إجراء تصحيحي', type='textarea', required=False)

# ------------------------------------------------------------------ بنود مشتركة
GMP_DAILY = [
    _c('hygiene', 'النظافة الشخصية وغطاء الشعر والقفازات'),
    _c('documents', 'اكتمال تعبئة سندات الإنتاج والجودة'),
    _c('calibration', 'حالة معايرة أدوات القياس (مسطرة / ميزان)'),
    _n('temp', 'درجة حرارة الصالة', '°م'),
    _n('humidity', 'الرطوبة النسبية', '%'),
    _c('segregation', 'فصل وتعريف المواد (حجر / مفرج / مرفوض / تالف)'),
    _c('housekeeping', 'النظافة العامة ومكافحة الآفات'),
    NOTES]

GMP_PERIODIC = [
    _c('batch_record', 'مراجعة عينة من سجلات التشغيل'),
    _c('deviations', 'متابعة حالات عدم المطابقة المفتوحة'),
    _c('training', 'سجلات تدريب المشغّلين محدّثة'),
    _c('maintenance', 'حالة الصيانة الوقائية'),
    _c('traceability', 'اختبار التتبع (منتج ← تشغيلة ← خام)'),
    NOTES]

PRINT_BLOCK = lambda what: [                                   # noqa: E731
    _match('printed_batch', f'رقم التشغيلة المطبوع على {what}', 'batch_no'),
    _match('printed_date', f'تاريخ الإنتاج المطبوع على {what}', 'batch_date'),
    _match('printed_item', f'كود/اسم الصنف المطبوع على {what}', 'item_code'),
    _c('print_clear', f'وضوح الطباعة على {what} وعدم التلطخ'),
]


def templates():
    T = []

    def add(code, title, dept, kind, area, fields, route='all', freq=None, batch=None, requires=None, note='',
            final=False):
        T.append(dict(code=code, title=title, dept=dept, kind=kind, area=area, route=route,
                      mfg_routes=list(AREA_ROUTES[area]), is_final=int(final),
                      freq_hours=freq,
                      needs_batch=int(batch if batch is not None else kind in ('release', 'inspection')),
                      fields=fields, requires=requires or [],
                      note=note or 'بنود مقترحة بانتظار مراجعة الجودة — الحدود الرقمية تُضاف من القالب.'))

    # ======================= الأسليتر (خط واحد SL-01)
    add('QC-SLT-REL', 'إفراج بدء القص — مراقبة الجودة — الأسليتر', 'QC', 'release', 'slitter', [
        _c('clearance', 'تفريغ الخط من مواد التشغيلة السابقة (Line Clearance)'),
        _c('roll_status', 'الرولات الداخلة مفرج عنها من فحص الخام'),
        _c('slit_plan', 'مطابقة خطة القص للمقاسات المعتمدة'),
        _c('first_roll', 'اعتماد أول رول (فحص بصري وأبعاد)'),
        _c('labels', 'بطاقات التعريف جاهزة وصحيحة'),
        _c('cleanliness', 'نظافة الخط قبل التشغيل'), NOTES])
    add('QC-SLT-DLY', 'سجل يومي — مراقبة الجودة — الأسليتر', 'QC', 'daily', 'slitter', [
        _c('knives', 'حالة سكاكين القص'), _c('cleanliness', 'نظافة الخط ومنطقة العمل'),
        _c('calibration', 'معايرة أدوات القياس'), _c('hygiene', 'النظافة الشخصية وغطاء الشعر'),
        _c('labels', 'مطابقة بطاقات التعريف للسب رول'), NOTES], freq=24)
    add('QC-SLT-PRD', 'فحص دوري أثناء القص — مراقبة الجودة — الأسليتر', 'QC', 'periodic', 'slitter', [
        _t('roll_no', 'رقم الرول', True),
        _n('w1', 'عرض السب رول — قراءة 1', 'سم'), _n('w2', 'عرض السب رول — قراءة 2', 'سم'),
        _n('w3', 'عرض السب رول — قراءة 3', 'سم'),
        _c('winding', 'انتظام اللف وشد الرول'),
        _c('visual', 'الفحص البصري: ثقوب / بقع / خيوط مقطوعة'),
        _c('foreign', 'خلو من الشوائب والشعر والمواد الغريبة'), NOTES], freq=4)
    add('QA-SLT-REL', 'إفراج بدء القص — ضمان الجودة — الأسليتر', 'QA', 'release', 'slitter', [
        _c('qc_release', 'مراجعة سجل إفراج QC للخط'),
        _c('rm_released', 'مراجعة قرار فحص الخام للرولات المستخدمة'),
        _c('docs', 'مراجعة الوثائق قبل البدء'), _c('personnel', 'المشغّلون مؤهلون ومدرّبون'), NOTES],
        requires=['QC-SLT-REL'])
    add('QA-SLT-DLY', 'سجل يومي — ضمان الجودة — الأسليتر', 'QA', 'daily', 'slitter', GMP_DAILY, freq=24)
    add('QA-SLT-PRD', 'مراجعة دورية — ضمان الجودة — الأسليتر', 'QA', 'periodic', 'slitter', GMP_PERIODIC, freq=168)

    # ======================= مكائن الطي (ماكينة لكل مقاس) — المعقم وغير المعقم
    add('QC-FLD-REL', 'إفراج بدء الطي — مراقبة الجودة — مكائن الطي', 'QC', 'release', 'folding', [
        _c('clearance', 'تفريغ الماكينة من مواد التشغيلة السابقة (Line Clearance)'),
        _c('machine_size', 'ضبط الماكينة على مقاس المنتج المطلوب'),
        _c('subroll_tags', 'مطابقة بطاقات السب رول للتشغيلة والصنف'),
        _c('first_piece', 'اعتماد أول قطعة (أبعاد وطبقات وتسلسل الحواف ومظهر)'),
        _c('cleanliness', 'نظافة الماكينة قبل التشغيل'), NOTES])
    add('QC-FLD-DLY', 'سجل يومي — مراقبة الجودة — مكائن الطي', 'QC', 'daily', 'folding', [
        _c('cleanliness', 'نظافة الماكينة ومنطقة العمل'),
        _c('calibration', 'معايرة أدوات القياس (مسطرة / ميزان)'),
        _c('hygiene', 'النظافة الشخصية وغطاء الشعر والقفازات'),
        _c('no_jewelry', 'خلو منطقة العمل من الأغراض الشخصية'),
        _c('scrap_seg', 'فصل وتعريف التالف'), NOTES], freq=24)
    add('QC-FLD-PRD', 'فحص المنتج أثناء الطي — مراقبة الجودة — مكائن الطي', 'QC', 'periodic', 'folding', [
        _dim('len1', 'طول المسحة — عينة 1', 'size_l'), _dim('len2', 'طول المسحة — عينة 2', 'size_l'),
        _dim('len3', 'طول المسحة — عينة 3', 'size_l'),
        _dim('wid1', 'عرض المسحة — عينة 1', 'size_w'), _dim('wid2', 'عرض المسحة — عينة 2', 'size_w'),
        _dim('wid3', 'عرض المسحة — عينة 3', 'size_w'),
        _c('edge_seq', 'تسلسل الشاش من الحواف (ترتيب الطبقات وانتظام الطي)'),
        _n('ply_count', 'عدد الطبقات', 'طبقة'),
        _c('impurities', 'خلو من الشوائب والجسيمات'),
        _c('hair', 'خلو من الشعر'),
        _c('stains', 'خلو من البقع والزيوت'),
        _c('holes', 'خلو من الثقوب والخيوط السائبة'),
        _c('xray', 'وجود الخيط الكاشف (للأصناف X-Ray)'),
        _n('piece_weight', 'وزن المسحة', 'جم'),
        _n('counter', 'قراءة العداد'), NOTES], freq=4, batch=True,
        note='سماحية الأبعاد ±4 ملم (0.4 سم) عن مقاس أمر الإنتاج — وباقي البنود من إدارة المصنع. التكرار الافتراضي 4 ساعات قابل للتعديل.')
    add('QA-FLD-REL', 'إفراج بدء الطي — ضمان الجودة — مكائن الطي', 'QA', 'release', 'folding', [
        _c('qc_release', 'مراجعة سجل إفراج QC للماكينة'),
        _c('subroll_ok', 'مراجعة مصدر السب رول (تتبع الخام)'),
        _c('docs', 'مراجعة الوثائق قبل البدء'), _c('personnel', 'المشغّلون مؤهلون ومدرّبون'), NOTES],
        requires=['QC-FLD-REL'])
    add('QA-FLD-DLY', 'سجل يومي — ضمان الجودة — مكائن الطي', 'QA', 'daily', 'folding', GMP_DAILY, freq=24)
    add('QA-FLD-PRD', 'مراجعة دورية — ضمان الجودة — مكائن الطي', 'QA', 'periodic', 'folding', GMP_PERIODIC, freq=168)

    # ======================= غير المعقم: المنتج النهائي ← الكراتين ← الإفراج
    add('QC-PKN-FIN', 'فحص المنتج النهائي وبيانات الطباعة — غير معقم', 'QC', 'inspection', 'packaging',
        [_c('appearance', 'شكل المنتج النهائي والعبوة'),
         _c('count_pack', 'مطابقة العدد داخل العبوة'),
         _c('impurities', 'خلو من الشوائب والشعر'),
         _c('seal', 'إحكام إغلاق العبوة')] + PRINT_BLOCK('العبوة') +
        [_c('other_print', 'باقي بيانات الطباعة (اسم المصنع، الشركة، التحذيرات، الرموز)'), NOTES],
        route='non_sterile',
        note='مطابقة رقم التشغيلة والتاريخ والصنف المطبوعة تُقارن آليًا بأمر الإنتاج.')
    add('QC-PKN-CTN', 'فحص الكراتين — غير معقم', 'QC', 'inspection', 'packaging',
        [_c('carton_cond', 'سلامة الكرتونة وإغلاقها'),
         _n('units_carton', 'عدد الوحدات داخل الكرتونة', req=True)] + PRINT_BLOCK('الكرتونة') +
        [_c('stacking', 'التعبئة على الطبلية والتعريف'), NOTES], route='non_sterile')
    add('QA-PKN-REL', 'إفراج المنتج غير المعقم — ضمان الجودة', 'QA', 'release', 'packaging', [
        _c('records', 'مراجعة سندات الطي والفرز والتغليف للتشغيلة'),
        _c('raw_ok', 'مراجعة فحص الخام المستخدم'),
        _c('nc_closed', 'إغلاق أي عدم مطابقة مرتبطة بالتشغيلة'), NOTES],
        route='non_sterile', requires=['QC-FLD-PRD', 'QC-PKN-FIN', 'QC-PKN-CTN'], final=True,
        note='لا يُقبل «مفرج» قبل وجود سجلات مطابقة لفحص المنتج أثناء الطي والمنتج النهائي والكراتين.')

    # ======================= المعقم: المغلفات والبوكسات والكراتين ← التعقيم
    add('QC-PKS-ENV', 'فحص المغلفات (العدد والشكل والطباعة) — معقم', 'QC', 'inspection', 'packaging',
        [_exp('count_env', 'عدد المسحات داخل المغلف', 'per_envelope', 'مسحة'),
         _c('appearance', 'شكل المنتج داخل المغلف'),
         _c('impurities', 'خلو من الشوائب والشعر'),
         _c('seal', 'سلامة اللحام والإحكام')] + PRINT_BLOCK('المغلف') +
        [_c('sterile_symbol', 'رمز التعقيم وبيانات الصلاحية على المغلف'), NOTES], route='sterile')
    add('QC-PKS-BOX', 'فحص البوكسات والكراتين (العدد والطباعة) — معقم', 'QC', 'inspection', 'packaging',
        [_exp('env_box', 'عدد المغلفات داخل البوكس', 'env_per_box', 'مغلف'),
         _exp('box_carton', 'عدد البوكسات داخل الكرتونة', 'boxes_per_carton', 'بوكس'),
         _c('box_cond', 'سلامة البوكس والكرتونة')] +
        PRINT_BLOCK('البوكس') +
        [_match('carton_batch', 'رقم التشغيلة المطبوع على الكرتونة', 'batch_no'),
         _match('carton_item', 'كود/اسم الصنف المطبوع على الكرتونة', 'item_code'),
         _c('ci_label', 'ملصق/مؤشر التعقيم الخارجي على الكرتونة'), NOTES], route='sterile')
    add('QA-PKS-REL', 'إفراج للتعقيم — ضمان الجودة — معقم', 'QA', 'release', 'packaging', [
        _c('records', 'مراجعة سندات الطي والفرز والتغليف'),
        _c('materials', 'مراجعة مواد التعبئة (فيلم، بوكس، كرتون) ولوطاتها'),
        _c('nc_closed', 'إغلاق أي عدم مطابقة مرتبطة'), NOTES],
        route='sterile', requires=['QC-FLD-PRD', 'QC-PKS-ENV', 'QC-PKS-BOX'],
        note='يسمح بتحميل التشغيلة في دورة التعقيم. يتطلب فحص الطي والمغلفات والبوكسات مطابقة.')
    add('QC-STR-CYC', 'متابعة التعقيم — بيانات الدورة والمؤشرات — معقم', 'QC', 'inspection', 'sterilization', [
        _match('cycle_no', 'رقم دورة التعقيم', 'cycle_no'),
        _c('load_pattern', 'نمط التحميل حسب المعتمد'),
        _c('ci_cartons', 'المؤشر الكيميائي الخارجي على كل الكراتين'),
        _c('ci_change', 'تغيّر لون المؤشرات بعد الدورة'),
        _c('bi_placed', 'وضع المؤشرات البيولوجية في مواقعها المعتمدة'),
        _c('report_attached', 'تقرير ماكينة التعقيم مرفق ومطابق لرقم الدورة'),
        _c('gas_lot', 'تسجيل لوط الغاز'),
        _c('aeration', 'تسجيل التهوية ومدتها'),
        NOTES], route='sterile')
    add('QA-STR-PRD', 'مراجعة دورية — ضمان الجودة — التعقيم', 'QA', 'periodic', 'sterilization', [
        _c('calib', 'معايرة ماكينة التعقيم وأجهزتها سارية'),
        _c('bi_stock', 'صلاحية لوط المؤشرات البيولوجية والكيميائية'),
        _c('residue', 'تقارير متبقيات EO / ECH محدّثة'),
        _c('validation', 'التأهيل الدوري للماكينة'),
        _c('cycles_review', 'مراجعة عينة من تقارير الدورات'), NOTES], route='sterile', freq=168)

    # ======================= الرباط الضاغط (غير معقم دائمًا)
    add('QC-BND-REL', 'إفراج بدء تشغيل ماكينة الأربطة — مراقبة الجودة', 'QC', 'release', 'bandage', [
        _c('clearance', 'تفريغ الماكينة من مواد التشغيلة السابقة (Line Clearance)'),
        _c('jumbo_ok', 'الجامبو رول المخصص مفرج عنه ومطابق للمنتج (العرض والنوع)'),
        _c('settings', 'ضبط الماكينة على عرض وطول الرول المطلوب'),
        _c('first_roll', 'اعتماد أول رول (العرض، الطول، الشد، الحواف)'),
        _c('cleanliness', 'نظافة الماكينة قبل التشغيل'), NOTES], route='non_sterile')
    add('QC-BND-PRD', 'فحص أثناء تصنيع الرباط — مراقبة الجودة', 'QC', 'periodic', 'bandage', [
        _dim('w1', 'عرض الرباط — عينة 1', 'width_cm'), _dim('w2', 'عرض الرباط — عينة 2', 'width_cm'),
        _dim('w3', 'عرض الرباط — عينة 3', 'width_cm'),
        _dim('l1', 'طول الرول — عينة 1', 'length_m', tol=0.1, req=False), _dim('l2', 'طول الرول — عينة 2', 'length_m', tol=0.1, req=False),
        _c('tension', 'انتظام الشد واللف'), _c('edges', 'سلامة الحواف وعدم التقطع'),
        _c('impurities', 'خلو من الشوائب والشعر'), _c('stains', 'خلو من البقع'), NOTES],
        route='non_sterile', freq=4, batch=True,
        note='سماحيات العرض والطول مبدئية (0.4 سم / 0.1 م) — عدّلوها من القالب حسب مواصفة المنتج.')
    add('QC-BND-WRP', 'فحص تغليف الرباط (المظهر والطباعة)', 'QC', 'inspection', 'bandage',
        [_c('wrap_intact', 'سلامة الغلاف وإحكامه'), _c('appearance', 'شكل الرول داخل الغلاف')] +
        PRINT_BLOCK('غلاف الرول') + [NOTES], route='non_sterile')
    add('QC-BND-FIN', 'فحص المنتج النهائي: البوكسات والكراتين (العدد والطباعة)', 'QC', 'inspection', 'bandage',
        [_exp('roll_box', 'عدد الرولات داخل البوكس', 'per_box', 'رول'),
         _exp('box_carton', 'عدد البوكسات داخل الكرتونة', 'per_carton', 'بوكس'),
         _c('box_cond', 'سلامة البوكس والكرتونة')] + PRINT_BLOCK('البوكس') +
        [_match('carton_batch', 'رقم التشغيلة المطبوع على الكرتونة', 'batch_no'),
         _match('carton_item', 'كود/اسم الصنف المطبوع على الكرتونة', 'item_code'), NOTES], route='non_sterile')
    add('QA-BND-REL', 'الإفراج النهائي للرباط الضاغط — ضمان الجودة', 'QA', 'release', 'bandage', [
        _c('records', 'مراجعة سندات الماكينة والتغليف والبوكسات والكراتين'),
        _c('raw_ok', 'مراجعة فحص الجامبو رول المستخدم'),
        _c('nc_closed', 'إغلاق أي عدم مطابقة مرتبطة'), NOTES],
        route='non_sterile', requires=['QC-BND-PRD', 'QC-BND-WRP', 'QC-BND-FIN'], final=True,
        note='لا يُقبل «مفرج» قبل فحص التصنيع والتغليف والمنتج النهائي مطابقة. ينقل الدفعة إلى «جاهزة للمخزن».')
    add('QA-BND-DLY', 'سجل يومي — ضمان الجودة — خط الأربطة', 'QA', 'daily', 'bandage', GMP_DAILY, freq=24)

    # ======================= مقترحات إضافية (عامة)
    add('QC-LAB-INS', 'اختبارات مخبرية على عينة التشغيلة (مقترح)', 'QC', 'inspection', 'general', [
        _n('absorb', 'زمن الامتصاص / الغمر', 'ثانية'), _n('sink', 'زمن الغرق', 'ثانية'),
        _c('fluor', 'الفلورة تحت الأشعة فوق البنفسجية'),
        _n('ph', 'pH المستخلص المائي'),
        _n('gsm', 'وزن المتر المربع', 'جم/م²'),
        _t('threads', 'عدد الخيوط (سداة × لحمة)'), NOTES],
        note='مقترح: اختبارات قياسية للشاش الطبي — أضيفوا الحدود المعتمدة عندكم.')
    add('QA-GEN-ENV', 'المراقبة البيئية (مقترح)', 'QA', 'periodic', 'general', [
        _n('temp', 'درجة الحرارة', '°م'), _n('humidity', 'الرطوبة النسبية', '%'),
        _n('settle', 'عدد المستعمرات — أطباق الترسيب', 'مستعمرة'),
        _c('cleaning', 'تنفيذ جدول النظافة والتطهير'), NOTES], freq=24, batch=False)
    add('QA-GEN-RET', 'عينات الاحتفاظ (مقترح)', 'QA', 'inspection', 'general', [
        _n('qty', 'عدد العينات المحتفَظ بها', 'وحدة', req=True),
        _c('location', 'حفظها في الموقع المخصص'),
        _match('label_batch', 'رقم التشغيلة على ملصق العينة', 'batch_no'), NOTES])
    return T
