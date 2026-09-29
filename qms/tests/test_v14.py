# -*- coding: utf-8 -*-
"""اختبار v14 — سيناريو GS310M كاملًا عبر الواجهة + فصل الإنتاج عن الجودة + قواعد التبسيط.

التشغيل:  cd qms && python -m unittest discover -s tests -v
يعمل على قاعدة مؤقتة (QMS_DB) ولا يمس data/qms.db.
"""
import os, re, sys, json, tempfile, unittest, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_tmp = tempfile.mkdtemp(prefix='qms-test-')
os.environ['QMS_DB'] = os.path.join(_tmp, 'qms.db')

import seed, migrate_v11, migrate_v12, migrate_v13, migrate_v14, db, auth   # noqa: E402
seed.main()
migrate_v11.run(); migrate_v12.run(); migrate_v13.run(); migrate_v14.run(); db.apply_migrations()
auth.ensure_admin()
for un, role in (('mgr', 'manager'), ('op', 'operator'), ('qc', 'qc'), ('qa', 'qa'), ('view', 'viewer'), ('store', 'store')):
    db.run("INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw) VALUES(?,?,?,?,1,0)",
           (un, f'مستخدم {un}', auth.hash_pw('pass123'), role))
from app import app                                     # noqa: E402
import mfg, forms, inventory                             # noqa: E402

TODAY = datetime.date.today().isoformat()
YEAR = TODAY[:4]


class Client:
    def __init__(self, user):
        self.c = app.test_client()
        r = self.c.get('/login')
        self.tok = re.search(r'name="csrf" content="([^"]+)"', r.get_data(True)).group(1)
        r = self.c.post('/login', data={'username': user, 'password': 'pass123', '_csrf': self.tok})
        assert r.status_code == 302, r.status_code

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, follow=True):
        d = dict(data or {}); d['_csrf'] = self.tok
        return self.c.post(url, data=d, follow_redirects=follow)


def body(r):
    return r.get_data(True)


def form_fields(html, action_hint=None):
    """أسماء حقول النماذج المرئية في صفحة (بدون hidden و_csrf)."""
    names = []
    html = html.split('<main')[-1]                       # حقول المحتوى فقط (بدون بحث الترويسة)
    for m in re.finditer(r'<(input|select|textarea)\b([^>]*)>', html):
        attrs = m.group(2)
        n = re.search(r'name="([^"]+)"', attrs)
        if not n or n.group(1).startswith('_') or 'type="hidden"' in attrs or 'type=hidden' in attrs:
            continue
        if n.group(1) not in names:
            names.append(n.group(1))
    return names


def recv_jumbo(store, qc, lot, width=None, length=None, code='RR005', cls=None, rolls=1):
    """يستلم جامبو رول ويفرج عنه — يتم مرة عند وصول الخام لا أثناء الإنتاج.
    العرض والطول لا يُكتبان: يأتيان من بيانات الصنف (RR005 = 120 سم × 2000 م)."""
    d = {'supplier_name': 'مورد تجريبي', 'item_code': code, 'supplier_lot': lot, 'roll_count': str(rolls)}
    if width is not None:
        d['width_cm'] = str(width)
    if length is not None:
        d['length_m'] = str(length)
    if cls:
        d['cls'] = cls
    r = store.post('/receipts/new', d)
    grn = re.search(r'GRN-\d{6}-\d{3}', body(r)).group(0)
    qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123', 'insp_date': TODAY})
    assert db.one('SELECT stock_status s FROM receipts WHERE grn_no=?', (grn,))['s'] == 'مفرج'
    return grn, [x['roll_no'] for x in db.q('SELECT roll_no FROM rolls WHERE grn_no=? ORDER BY roll_no', (grn,))]


class V14(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mgr, cls.op, cls.qc, cls.qa = Client('mgr'), Client('op'), Client('qc'), Client('qa')
        cls.view, cls.store = Client('view'), Client('store')
        cls.n = 0

    # ------------------------------------------------------------------ أدوات
    def order(self, code='GS310M', qty=25000):
        self.mgr.post('/orders/new', {'item_code': code, 'qty_required': str(qty)})
        return db.one("SELECT * FROM work_orders WHERE item_code=? ORDER BY rowid DESC LIMIT 1", (code,))

    def jumbo(self):
        type(self).n += 1
        return recv_jumbo(self.store, self.qc, f'LOT-{self.n}')[1][0]

    def cut(self, bn, mode='run'):
        roll = self.jumbo()
        self.op.post(f'/slitter/{bn}', {'roll_no': roll, 'mode': mode})
        return roll, [s['tag_no'] for s in db.q('SELECT tag_no FROM subrolls WHERE batch_no=? AND roll_no=? ORDER BY tag_no', (bn, roll))]

    def fold(self, bn, tag, qty, who=None):
        return (who or self.op).post(f'/folding/{bn}', {'tag_no': tag, 'qty_good': str(qty)})

    def state(self, bn):
        return mfg.order_state(bn)

    def notes(self):
        return db.one('SELECT COUNT(*) n FROM notifications')['n']

    # ================================================================== 1. بيانات المنتج تقود كل شيء
    def test_01_product_master_and_requirements(self):
        it = db.one("SELECT * FROM items WHERE item_code='GS310M'")
        self.assertEqual((it['uom'], it['sub_roll_width_cm'], it['machine_code'], it['route_code'], it['raw_item']),
                         ('باكت', 23, 'FD-10', 'FULL_GAUZE', 'RR005'))
        self.assertEqual((it['pack_code'], it['master_box']), ('PK013', 'MB007'))
        req = mfg.requirements(it, 25000)
        self.assertTrue(req['ok'])
        self.assertEqual((req['usable_width'], req['per_jumbo'], req['sr_needed'], req['jumbo_needed'], req['yield_per_sr']),
                         (118, 5, 5, 1, 5000))
        # ceil عند عدم القسمة التامة: 25,001 باكت → 6 سب رول → 2 جامبو
        r2 = mfg.requirements(it, 25001)
        self.assertEqual((r2['sr_needed'], r2['jumbo_needed']), (6, 2))
        eq = {e['unit']: e['qty'] for e in mfg.pack_equiv('GS310M', 24850, 'باكت')}
        self.assertAlmostEqual(eq['بوكس'], 1242.5)
        self.assertAlmostEqual(eq['كرتون'], 124.25)
        self.assertEqual(db.one("SELECT name_ar FROM units WHERE code='pack'")['name_ar'], 'باكت')
        # الصنف بلا بيانات (عرض/إنتاجية غير معروفة) يُوقَف بوضوح ولا يخمّن من الكود
        db.run("""INSERT INTO items(item_code,prefix,category_ar,size,ply,sterile,machine_code,uom,description,status,route_code)
                  VALUES('GSXTEST','GS','منتج تام','9x9 cm',99,'غير معقم','FD-10','باكت','بلا بيانات','نشط','FULL_GAUZE')""")
        r = self.mgr.post('/orders/new', {'item_code': 'GSXTEST', 'qty_required': '100'})
        self.assertIn('بيانات المنتج ناقصة', body(r))
        self.assertIsNone(db.one("SELECT 1 FROM work_orders WHERE item_code='GSXTEST'"))
        # الكود لا يُحلَّل: التغيير في بيانات المنتج هو ما يغيّر الاحتياج
        db.run("UPDATE items SET sub_roll_width_cm=20, yield_per_sr=1000 WHERE item_code='GSXTEST'")
        self.assertEqual(mfg.requirements(db.one("SELECT * FROM items WHERE item_code='GSXTEST'"), 5000)['per_jumbo'], 5)

    # ================================================================== 2. السيناريو الكامل
    def test_02_full_gs310m_scenario(self):
        mgr, op, qc, store = self.mgr, self.op, self.qc, self.store
        steps = {}                                                # الحقول المطلوبة من المستخدم في كل خطوة
        # --- أمر إنتاج = المنتج + الكمية فقط
        page = body(mgr.get('/orders/new'))
        steps['أمر الإنتاج'] = form_fields(page)
        self.assertEqual(steps['أمر الإنتاج'], ['qty_required', 'due_date', 'priority'])
        mgr.post('/orders/new', {'item_code': 'GS310M', 'qty_required': '25000'})
        w = db.one("SELECT * FROM work_orders WHERE item_code='GS310M' ORDER BY rowid DESC LIMIT 1")
        bn = w['batch_no']
        self.assertRegex(bn, r'^[A-Z]{3}-\d{4}-T-\d{3}$')
        self.assertEqual((w['fold_machine'], w['std_width_cm'], w['sr_needed'], w['jumbo_needed'], w['uom'],
                          w['pieces_per_sr'], w['raw_item'], w['route_code']),
                         ('FD-10', 23, 5, 1, 'باكت', 5000, 'RR005', 'FULL_GAUZE'))
        n_after_order = self.notes()
        st = self.state(bn)
        self.assertEqual((st['stage'], st['next']['endpoint'], st['required']), ('new', 'slit_work', 25000))
        # --- الجامبو (مرة واحدة عند وصول الخام)
        grn, (roll,) = recv_jumbo(store, qc, 'LOT-E2E')
        n_after_raw = self.notes()
        # --- السليتر: خطة تلقائية من اختيار الجامبو فقط
        page = body(op.get(f'/slitter/{bn}'))
        steps['السليتر'] = form_fields(page)
        self.assertEqual(steps['السليتر'], [])                     # لا حقول نصية: أزرار «تنفيذ القص» فقط
        self.assertIn(roll, page)
        r = op.post(f'/slitter/{bn}', {'roll_no': roll, 'mode': 'run'})
        self.assertIn('استخدام السب رول في ماكينة الطي', body(r))    # Smart Next Action
        self.assertIn('عرض الرولات الفرعية', body(r))
        subs = db.q("SELECT * FROM subrolls WHERE batch_no=? ORDER BY tag_no", (bn,))
        self.assertEqual(len(subs), 5)
        self.assertTrue(all(s_['width_cm'] == 23 and s_['stock_status'] == 'متاح' for s_ in subs))
        self.assertRegex(subs[0]['tag_no'], r'^SR-\d{4}$')
        self.assertEqual([s_['tag_no'] for s_ in subs], sorted(s_['tag_no'] for s_ in subs))
        plan = db.one('SELECT * FROM cutting_plans WHERE batch_no=?', (bn,))
        self.assertRegex(plan['plan_no'], r'^CP-\d{4}-\d{6}$')
        self.assertEqual((plan['n_sub'], plan['usable_width'], plan['status'], plan['roll_no'], plan['jumbo_width']),
                         (5, 118, 'منفّذ', roll, 120))
        self.assertEqual(db.one('SELECT use_status FROM rolls WHERE roll_no=?', (roll,))['use_status'], 'مستهلك')
        self.assertEqual(db.one('SELECT COUNT(*) n FROM slitting WHERE roll_no=?', (roll,))['n'], 1)   # استهلاك تلقائي
        self.assertEqual(self.state(bn)['stage'], 'ready_fold')
        # الجامبو يدخل خطة واحدة فقط
        op.post(f'/slitter/{bn}', {'roll_no': roll, 'mode': 'run'})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM cutting_plans WHERE roll_no=?', (roll,))['n'], 1)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM subrolls WHERE batch_no=?', (bn,))['n'], 5)
        # --- الطي: سب رول + كمية فعلية فقط
        page = body(op.get(f'/folding/{bn}'))
        steps['الطي'] = form_fields(page)
        self.assertEqual(steps['الطي'], ['tag_no', 'qty_good', 'notes'])
        for bad in ('operator', 'shift', 'machine', 'fdate', 'doc_no', 'size', 'item_code', 'ply', 'width', 'carton'):
            self.assertNotIn(f'name="{bad}', page)
        self.assertIn('ماكينة الطي 10', page); self.assertIn('23 سم', page); self.assertIn(f'FOLD-{YEAR}-000001', page.replace('‏', ''))
        tags = [s_['tag_no'] for s_ in subs]
        docs = []
        for i, (tag, q) in enumerate(zip(tags, [5000, 5000, 5000, 5000, 4850])):
            r = self.fold(bn, tag, q)
            self.assertIn('تم حفظ سند الإنتاج', body(r))
            if i == 0:                                                   # Smart Next Action بعد الحفظ
                self.assertIn('استخدام السب رول التالي', body(r))
                self.assertIn('العودة لأمر الإنتاج', body(r))
            docs.append(db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tag,))['doc_no'])
        nums = [int(d[-6:]) for d in docs]
        self.assertEqual(nums, list(range(nums[0], nums[0] + 5)))
        self.assertTrue(all(re.match(rf'^FOLD-{YEAR}-\d{{6}}$', d) for d in docs))
        st = self.state(bn)
        self.assertEqual((st['produced'], st['remaining'], st['stage']), (24850, 150, 'production'))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM subrolls WHERE batch_no=? AND stock_status='مستهلك'", (bn,))['n'], 5)
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='PROD_OUT'", (bn,))['q'], 24850)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_in WHERE batch_no=?', (bn,))['n'], 5)     # استهلاك مُولَّد تلقائيًا
        # استخدام مزدوج ممنوع (من صفحة ثانية مفتوحة قديمًا)
        r = op.post(f'/folding/{bn}', {'tag_no': tags[0], 'qty_good': '10'})
        self.assertIn(f'هذا الرول الفرعي تم استخدامه مسبقا في سند الإنتاج رقم {docs[0]}.', body(r))
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE batch_no=?', (bn,))['n'], 5)
        # لا إشعارات أثناء القص والطي (لا لكل سب رول ولا لكل حركة)
        self.assertEqual(self.notes(), n_after_raw)
        # --- إنهاء الإنتاج: إشعار واحد للجودة
        steps['إنهاء الإنتاج'] = ['short_ok']
        op.post(f'/orders/{bn}/finish', {})                                     # أقل من المطلوب بلا تأكيد → مرفوض
        self.assertIsNone(db.one('SELECT final_status FROM work_orders WHERE batch_no=?', (bn,))['final_status'])
        op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        self.assertEqual(db.one('SELECT final_status FROM work_orders WHERE batch_no=?', (bn,))['final_status'], 'PENDING_QC')
        self.assertEqual(self.notes(), n_after_raw + 1)
        note = db.one('SELECT * FROM notifications ORDER BY id DESC LIMIT 1')
        self.assertEqual(note['title'], f'تم الانتهاء من إنتاج الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، والكمية النهائية 24,850 باكت، '
                                        f'وهي بانتظار موافقة الجودة للتخزين.')
        self.assertEqual(self.state(bn)['stage'], 'pending_quality')
        # الإنتاج لا يستطيع الاعتماد ولا رؤية صفحات الجودة
        self.assertEqual(op.post(f'/quality/decide/{bn}', {'decision': 'approve'}, follow=False).status_code, 403)
        self.assertEqual(op.get('/quality/pending').status_code, 403)
        # لا سندات جديدة بعد الإنهاء
        r = op.post(f'/folding/{bn}', {'tag_no': tags[0], 'qty_good': '1'})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE batch_no=?', (bn,))['n'], 5)
        # --- الجودة: قائمة واحدة ثم قرار واحد
        self.assertIn(w['wo_no'], body(qc.get('/quality/pending')))
        page = body(qc.get(f'/quality/review/{bn}'))
        self.assertIn('24,850', page); self.assertIn('25,000', page); self.assertIn('اعتماد للتخزين', page)
        r = qc.post(f'/quality/decide/{bn}', {'decision': 'reject'})              # الرفض يتطلب سببًا
        self.assertEqual(db.one('SELECT final_status FROM work_orders WHERE batch_no=?', (bn,))['final_status'], 'PENDING_QC')
        qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        w2 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
        self.assertEqual((w2['final_status'], w2['approved_qty']), ('APPROVED', 24850))
        self.assertEqual(self.notes(), n_after_raw + 2)                           # إشعار الاعتماد فقط
        appr = db.one('SELECT * FROM notifications ORDER BY id DESC LIMIT 1')
        self.assertEqual(appr['title'], f'تم اعتماد الدفعة {bn} لأمر الإنتاج {w["wo_no"]}، الكمية المعتمدة 24,850 باكت، وهي جاهزة للتخزين.')
        self.assertIn('جاهزة للتخزين', body(op.get('/notifications')))            # يصل للإنتاج
        self.assertIn('جاهزة للتخزين', body(store.get('/notifications')))         # ولأمين المخزن
        self.assertEqual(self.state(bn)['stage'], 'approved')
        # لا تعديل/حذف بعد الاعتماد
        r = op.post(f'/folding/doc/{docs[0]}/delete', {})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE batch_no=?', (bn,))['n'], 5)
        # --- المخزن: بضغطة واحدة
        page = body(store.get('/warehouse'))
        self.assertIn(bn, page)
        steps['المخزن'] = ['location']
        self.assertEqual(op.post('/warehouse', {'batch_no': bn}, follow=False).status_code, 403)
        r = store.post('/warehouse', {'batch_no': bn})
        self.assertIn('تم تخزين', body(r))
        w3 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
        self.assertEqual((w3['final_status'], w3['status']), ('STORED', 'مكتملة'))
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='FG'", (bn,))['q'], 24850)
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='PROD_OUT'", (bn,))['q'], 0)
        self.assertEqual(self.state(bn)['stage'], 'stored')
        self.assertEqual(mfg.progress(bn)['pct'], 100)
        self.assertEqual([s_['code'] for s_ in mfg.progress(bn)['steps']], ['SLIT', 'FOLD', 'FINALQC', 'WH'])   # لا مراحل افتراضية
        # --- التتبع الكامل: جامبو ← سب رول ← سند طي ← منتج ← موافقة ← مخزن، والعكسي
        g = body(mgr.get(f'/trace?b={bn}', follow_redirects=True))
        for needle in (bn, roll, tags[0], docs[0], plan['plan_no'], 'LOT-E2E', 'مورد تجريبي', 'اعتماد للتخزين', 'FGR-'):
            self.assertIn(needle, g, needle)
        fw = body(mgr.get('/genealogy?ref=LOT-E2E'))
        self.assertIn(bn, fw)
        self.assertIn(bn, body(mgr.get('/forward?lot=LOT-E2E')))
        self.assertIn('APR-', body(mgr.get('/forward?lot=LOT-E2E')))
        # سجلات التدقيق
        for act in ('create', 'finish_production', 'qc_decision'):
            self.assertTrue(db.one('SELECT 1 FROM audit_log WHERE action=? AND record_key=?', (act, bn)), act)
        type(self).e2e = dict(bn=bn, steps=steps)

    # ================================================================== 3. خطة القص: حالات الجامبو والحذف
    def test_03_cutting_plan_states(self):
        w = self.order('GS310M', 25000)
        bn = w['batch_no']
        roll, tags = self.cut(bn, mode='plan')
        self.assertEqual(len(tags), 5)
        self.assertEqual(db.one('SELECT use_status FROM rolls WHERE roll_no=?', (roll,))['use_status'], 'مخصص')     # Allocated
        self.assertEqual(db.one('SELECT status FROM cutting_plans WHERE roll_no=?', (roll,))['status'], 'مخطط')
        self.assertEqual({r['stock_status'] for r in db.q('SELECT stock_status FROM subrolls WHERE batch_no=?', (bn,))}, {'مخطط'})
        self.assertEqual(self.state(bn)['stage'], 'slitting')
        # السب رول المخطط لا يُطوى قبل تنفيذ القص
        r = self.fold(bn, tags[0], 100)
        self.assertIn('لم تُنفّذ', body(r))
        # الجامبو المخصص لا يظهر في قائمة المتاح لأمر آخر
        w2 = self.order('GS310M', 5000)
        self.assertNotIn(roll, body(self.op.get(f'/slitter/{w2["batch_no"]}')))
        # حذف الخطة قبل التنفيذ يعيد الجامبو متاحًا ويحذف السب رول
        plan_no = db.one('SELECT plan_no FROM cutting_plans WHERE roll_no=?', (roll,))['plan_no']
        self.op.post(f'/slitter/plan/{plan_no}/delete', {})
        self.assertEqual(db.one('SELECT use_status, batch_no, plan_no FROM rolls WHERE roll_no=?', (roll,)),
                         {'use_status': 'متاح', 'batch_no': None, 'plan_no': None})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM subrolls WHERE batch_no=?', (bn,))['n'], 0)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM slitting WHERE roll_no=?', (roll,))['n'], 0)
        self.assertIn(roll, body(self.op.get(f'/slitter/{bn}')))                        # عاد إلى القائمة
        # خطة جديدة بالجامبو نفسه ثم تأكيد التنفيذ
        self.op.post(f'/slitter/{bn}', {'roll_no': roll, 'mode': 'plan'})
        plan_no = db.one('SELECT plan_no FROM cutting_plans WHERE roll_no=?', (roll,))['plan_no']
        r = self.op.post(f'/slitter/plan/{plan_no}/confirm', {})
        self.assertIn('تم تنفيذ القص', body(r))
        self.assertEqual(db.one('SELECT use_status FROM rolls WHERE roll_no=?', (roll,))['use_status'], 'مستهلك')
        self.assertEqual({r['stock_status'] for r in db.q('SELECT stock_status FROM subrolls WHERE batch_no=?', (bn,))}, {'متاح'})
        self.assertEqual(self.state(bn)['stage'], 'ready_fold')
        # الخطة المنفّذة: لا حذف للمشغّل (الجامبو قُصّ فعليًا)، والمدير يحذفها إن لم يُستخدم شيء
        self.op.post(f'/slitter/plan/{plan_no}/delete', {})
        self.assertIsNotNone(db.one('SELECT 1 FROM cutting_plans WHERE plan_no=?', (plan_no,)))
        tag = db.one('SELECT tag_no FROM subrolls WHERE plan_no=? ORDER BY tag_no', (plan_no,))['tag_no']
        self.fold(bn, tag, 5000)
        self.mgr.post(f'/slitter/plan/{plan_no}/delete', {})                            # استُخدم سب رول ← ممنوع
        self.assertIsNotNone(db.one('SELECT 1 FROM cutting_plans WHERE plan_no=?', (plan_no,)))
        # لا جامبو إضافي مع وجود سب رول يكفي المتبقي
        roll2 = self.jumbo()
        self.op.post(f'/slitter/{bn}', {'roll_no': roll2, 'mode': 'run'})
        self.assertIsNone(db.one('SELECT 1 FROM cutting_plans WHERE roll_no=?', (roll2,)))
        # جامبو أضيق من السب رول لا يُقص (حماية خادم لا واجهة فقط)
        _, (narrow,) = recv_jumbo(self.store, self.qc, 'NARROW-1', width=20)
        self.op.post(f'/slitter/{w2["batch_no"]}', {'roll_no': narrow, 'mode': 'run'})
        self.assertIsNone(db.one('SELECT 1 FROM cutting_plans WHERE roll_no=?', (narrow,)))
        self.assertEqual(db.one('SELECT COUNT(*) n FROM subrolls WHERE roll_no=?', (narrow,))['n'], 0)
        # رول غير مفرج لا يُقص
        self.store.post('/receipts/new', {'supplier_name': 'م', 'item_code': 'RR005', 'supplier_lot': 'UNREL', 'roll_count': '1'})
        held = db.one("SELECT roll_no FROM rolls WHERE supplier_lot='UNREL'")['roll_no']
        self.op.post(f'/slitter/{w2["batch_no"]}', {'roll_no': held, 'mode': 'run'})
        self.assertIsNone(db.one('SELECT 1 FROM cutting_plans WHERE roll_no=?', (held,)))

    # ================================================================== 4. تعديل وحذف سند الطي
    def test_04_edit_delete_fold_doc(self):
        w = self.order('GS310M', 25000)
        bn = w['batch_no']
        roll, tags = self.cut(bn)
        for t_, q in zip(tags[:3], (5000, 5000, 5000)):
            self.fold(bn, t_, q)
        docs = [db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (t_,))['doc_no'] for t_ in tags[:3]]
        self.assertEqual(self.state(bn)['produced'], 15000)
        # تعديل الكمية يعيد احتساب الإنتاج والمخزون
        page = body(self.op.get(f'/folding/doc/{docs[1]}/edit'))
        self.assertIn('5000', page)
        r = self.op.post(f'/folding/doc/{docs[1]}/edit', {'tag_no': tags[1], 'qty_good': '4900'})
        self.assertIn('تم تعديل السند', body(r))
        self.assertEqual(self.state(bn)['produced'], 14900)
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='PROD_OUT'", (bn,))['q'], 14900)
        # تبديل السب رول: القديم يعود متاحًا والجديد مستهلك
        self.op.post(f'/folding/doc/{docs[1]}/edit', {'tag_no': tags[3], 'qty_good': '4900'})
        st = {r['tag_no']: r for r in db.q('SELECT tag_no, stock_status, used_doc FROM subrolls WHERE batch_no=?', (bn,))}
        self.assertEqual((st[tags[1]]['stock_status'], st[tags[1]]['used_doc']), ('متاح', None))
        self.assertEqual((st[tags[3]]['stock_status'], st[tags[3]]['used_doc']), ('مستهلك', docs[1]))
        self.assertEqual(db.one('SELECT tag_no FROM folding_in WHERE doc_no=?', (docs[1],))['tag_no'], tags[3])
        # لا تبديل إلى سب رول مستخدم
        r = self.op.post(f'/folding/doc/{docs[1]}/edit', {'tag_no': tags[0], 'qty_good': '4900'})
        self.assertIn(f'سند الإنتاج رقم {docs[0]}', body(r))
        # حذف سند: تنفيذ عكس المعاملة
        r = self.op.post(f'/folding/doc/{docs[2]}/delete', {})
        self.assertIn(f'حُذف السند {docs[2]}', body(r))
        self.assertEqual(self.state(bn)['produced'], 9900)
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='PROD_OUT'", (bn,))['q'], 9900)
        self.assertEqual(db.one('SELECT stock_status s FROM subrolls WHERE tag_no=?', (tags[2],))['s'], 'متاح')
        self.assertIsNone(db.one('SELECT 1 FROM folding_out WHERE doc_no=?', (docs[2],)))
        self.assertIsNone(db.one('SELECT 1 FROM folding_in WHERE doc_no=?', (docs[2],)))
        self.assertTrue(db.one("SELECT 1 FROM stock_tx WHERE ref_doc=? AND qty<0 AND note LIKE '%عكس%'", (docs[2],)))
        # ويمكن إعادة استخدام السب رول المفرج في سند جديد
        self.assertIn('تم حفظ سند الإنتاج', body(self.fold(bn, tags[2], 5000)))
        # التدقيق يحفظ اللقطة الكاملة للمحذوف
        d = db.one("SELECT details FROM audit_log WHERE action='delete' AND record_key=?", (docs[2],))
        self.assertIn(tags[2], d['details'])
        self.assertTrue(db.one("SELECT 1 FROM audit_log WHERE action='edit' AND record_key=?", (docs[1],)))
        # المشاهد لا يعدّل، والجودة لا تعدّل
        self.assertEqual(self.view.post(f'/folding/doc/{docs[0]}/delete', {}, follow=False).status_code, 403)
        self.assertEqual(self.qc.post(f'/folding/doc/{docs[0]}/edit', {'qty_good': '1'}, follow=False).status_code, 403)
        # التعديل بعد إرسال الدفعة للجودة يسحب طلب الاعتماد
        self.op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')
        self.op.post(f'/folding/doc/{docs[0]}/edit', {'tag_no': tags[0], 'qty_good': '5100'})
        self.assertIsNone(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'])
        self.assertEqual(self.state(bn)['produced'], 15000 - 5000 + 5100 - 100)

    # ================================================================== 5. حماية الاستخدام المزدوج في قاعدة البيانات
    def test_05_double_use_db_guard(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        roll, tags = self.cut(bn)
        self.fold(bn, tags[0], 1000)
        import sqlite3
        con = db.get()
        with self.assertRaises(sqlite3.IntegrityError):                      # حتى بتجاوز المنطق: الفهرس الفريد يمنع
            con.execute("INSERT INTO folding_out(doc_no,batch_no,tag_no,qty_good) VALUES('X-DUP',?,?,1)", (bn, tags[0]))
            con.commit()
        con.close()
        # جلستان مفتوحتان على الصفحة نفسها ثم حفظ بالتتالي: الثانية تُرفض بالرسالة المطلوبة
        a, b = Client('op'), Client('op')
        a.get(f'/folding/{bn}'); b.get(f'/folding/{bn}')
        self.assertIn('تم حفظ', body(a.post(f'/folding/{bn}', {'tag_no': tags[1], 'qty_good': '1000'})))
        doc = db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tags[1],))['doc_no']
        self.assertIn(f'هذا الرول الفرعي تم استخدامه مسبقا في سند الإنتاج رقم {doc}.',
                      body(b.post(f'/folding/{bn}', {'tag_no': tags[1], 'qty_good': '1000'})))
        # القائمة المتاحة لا تعرض المستخدَم لكن السجل يحتفظ به بحالة مستهلك
        page = body(self.op.get(f'/folding/{bn}'))
        self.assertNotIn(f'value="{tags[1]}"', page)
        self.assertIn(tags[1], body(self.op.get(f'/orders/{bn}')))

    def test_05b_concurrent_double_use(self):
        import threading
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        roll, tags = self.cut(bn)
        res = []

        def go():
            res.append(body(Client('op').post(f'/folding/{bn}', {'tag_no': tags[0], 'qty_good': '1000'})))
        ths = [threading.Thread(target=go) for _ in range(6)]
        [x.start() for x in ths]; [x.join() for x in ths]
        self.assertEqual(sum('تم حفظ سند الإنتاج' in r for r in res), 1)          # نجاح واحد فقط
        self.assertEqual(sum('تم استخدامه مسبقا' in r for r in res), 5)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE tag_no=?', (tags[0],))['n'], 1)
        self.assertEqual(db.one("SELECT SUM(qty) q FROM stock_tx WHERE batch_no=? AND stage='PROD_OUT'", (bn,))['q'], 1000)

    # ================================================================== 6. فصل الإنتاج عن الجودة في الواجهة والخادم
    def test_06_production_vs_quality_separation(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        PROD_GET = ['/orders', '/slitter', '/folding', f'/orders/{bn}', f'/slitter/{bn}', f'/folding/{bn}', '/completed',
                    '/production/log', '/sp', '/bandage']
        QUAL_GET = ['/quality/pending', '/quality/held', '/quality/log', f'/quality/review/{bn}']
        ADMIN_GET = ['/master/products', '/admin/users', '/admin/settings', '/reports', '/quality', '/quality/templates',
                     '/cycles', '/sorting', '/packaging', '/releases', '/materials']
        for u, c in (('مستخدم الإنتاج', self.op), ('المدير', self.mgr)):
            for url in PROD_GET:
                self.assertEqual(c.get(url).status_code, 200, (u, url))
        for u, c in (('الجودة', self.qc), ('QA', self.qa)):
            for url in PROD_GET:
                self.assertEqual(c.get(url).status_code, 403, (u, url))
        for u, c in (('الجودة', self.qc), ('المدير', self.mgr)):
            for url in QUAL_GET:
                self.assertEqual(c.get(url).status_code, 200, (u, url))
        for url in QUAL_GET + ADMIN_GET:
            self.assertEqual(self.op.get(url).status_code, 403, ('إنتاج', url))
        # الإدارة: كل شيء
        for url in PROD_GET + QUAL_GET + ADMIN_GET:
            self.assertEqual(self.mgr.get(url).status_code, 200, ('مدير', url))
        # الجودة لا تنفّذ أي إجراء إنتاج (POST)
        for url, data in ((f'/slitter/{bn}', {'roll_no': 'x'}), (f'/folding/{bn}', {'tag_no': 'x', 'qty_good': '1'}),
                          (f'/orders/{bn}/finish', {}), ('/orders/new', {'item_code': 'GS310M', 'qty_required': '1'}),
                          ('/folding/doc/FOLD-1/delete', {}), ('/slitter/plan/CP-1/confirm', {})):
            self.assertEqual(self.qc.post(url, data, follow=False).status_code, 403, url)
        # الإنتاج لا ينفّذ أي إجراء جودة ولا يصدر أوامر
        for url, data in ((f'/quality/decide/{bn}', {'decision': 'approve'}), ('/orders/new', {'item_code': 'GS310M', 'qty_required': '1'}),
                          ('/warehouse', {'batch_no': bn}), ('/ncr/new', {'description': 'x', 'stage': 'ا'}),
                          ('/admin/users', {'act': 'add'})):
            self.assertEqual(self.op.post(url, data, follow=False).status_code, 403, url)
        # الواجهة: القوائم الجانبية والصفحات
        om, qm = body(self.op.get('/')), body(self.qc.get('/'))
        for word in ('أوامر الإنتاج', 'السليتر', 'ماكينات الطي', 'المنتجات المكتملة', 'سجل الإنتاج'):
            self.assertIn(word, om)
        for word in ('بانتظار الاعتماد', 'المرفوض والمعلّق'):
            self.assertNotIn(word, om.split('<main')[0])
            self.assertIn(word, qm)
        for word in ('السليتر', 'ماكينات الطي', 'أوامر الإنتاج', 'سجل الإنتاج'):
            self.assertNotIn(word, qm.split('<main')[0])
        mm = body(self.mgr.get('/')).split('<main')[0]
        for word in ('أوامر الإنتاج', 'بانتظار الاعتماد', 'الأصناف', 'المواد الخام', 'المخزون', 'التقارير', 'المستخدمون', 'الإعدادات'):
            self.assertIn(word, mm)
        # صفحة أمر الإنتاج لمستخدم الإنتاج لا تحوي أي عنصر جودة/فحص/NCR
        page = body(self.op.get(f'/orders/{bn}'))
        for word in ('عدم مطابقة', 'قوالب الجودة', 'QC', 'إفراج', 'بوابة'):
            self.assertNotIn(word, page)
        # المخزن: لا يرى إنتاجًا ولا جودة
        for url in ('/orders', '/slitter', '/quality/pending'):
            self.assertEqual(self.store.get(url).status_code, 403, url)
        self.assertEqual(self.store.get('/warehouse').status_code, 200)
        # المشاهد: يقرأ فقط
        self.assertEqual(self.view.get('/orders').status_code, 200)
        self.assertEqual(self.view.post(f'/folding/{bn}', {'tag_no': 'x', 'qty_good': '1'}, follow=False).status_code, 403)

    # ================================================================== 7. التنقل: رجوع + مسار + الخطوة التالية
    def test_07_navigation(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        for url in ('/orders', f'/orders/{bn}', f'/slitter/{bn}', f'/folding/{bn}', '/orders/new', '/completed', '/slitter', '/folding'):
            page = body(self.mgr.get(url))
            self.assertIn('class="crumb', page, url)
            self.assertIn('رجوع', page, url)
            self.assertIn('الرئيسية', page, url)
        self.assertIn('الأمر ' + bn, body(self.mgr.get(f'/folding/{bn}')))
        for url in ('/quality/pending', '/quality/held', '/quality/log'):
            self.assertIn('class="crumb', body(self.qc.get(url)), url)
        # الخطوة التالية بعد إنشاء الأمر ثم بعد القص ثم بعد الطي
        r = self.mgr.post('/orders/new', {'item_code': 'GS310M', 'qty_required': '5000'})
        self.assertIn('ابدأ خطة القص', body(r))
        bn2 = db.one('SELECT batch_no FROM work_orders ORDER BY rowid DESC LIMIT 1')['batch_no']
        roll, tags = self.cut(bn2)
        r = self.fold(bn2, tags[0], 5000)
        self.assertIn('العودة لأمر الإنتاج', body(r))
        self.assertIn('إنهاء الإنتاج', body(r))                       # اكتملت الكمية → الإنهاء هو الخطوة التالية
        # الصفحة الرئيسية للإنتاج: أعداد المراحل + استكمال العمل
        home = body(self.op.get('/'))
        for word in ('أوامر جديدة', 'في السليتر', 'جاهزة للطي', 'قيد الإنتاج', 'بانتظار الجودة', 'مكتملة', 'استكمال العمل'):
            self.assertIn(word, home)
        # صفحة الأمر: زر «استكمال العمل» يقود للمرحلة الصحيحة
        self.assertIn(f'href="/slitter/{bn}"', body(self.op.get(f'/orders/{bn}')))       # لا خطة قص بعد ← السليتر
        self.assertIn('id="finish"', body(self.op.get(f'/orders/{bn2}')))              # اكتملت الكمية ← الإنهاء
        st = self.state(bn)
        self.assertEqual(st['next']['endpoint'], 'slit_work')
        st2 = self.state(bn2)
        self.assertEqual((st2['stage'], st2['next']['kind']), ('production', 'finish'))

    # ================================================================== 8. الجودة: رفض وتعليق ثم اعتماد
    def test_08_quality_decisions(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        roll, tags = self.cut(bn)
        self.fold(bn, tags[0], 5000)
        self.op.post(f'/orders/{bn}/finish', {})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')   # 5000 = المطلوب
        before = self.notes()
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'hold', 'note': ''})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'hold', 'note': 'عينة غير مكتملة'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'HOLD')
        self.assertIn(bn, body(self.qc.get('/quality/held')))
        self.assertEqual(self.state(bn)['stage'], 'hold')
        self.assertIn('عينة غير مكتملة', body(self.op.get(f'/orders/{bn}')))            # الإنتاج يرى السبب
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'APPROVED')
        self.assertEqual([r['decision'] for r in db.q('SELECT decision FROM approvals WHERE batch_no=? ORDER BY id', (bn,))],
                         ['Hold', 'Approved'])
        self.assertEqual(self.notes(), before + 2)                                    # تعليق + اعتماد فقط
        # لا اعتماد ثانٍ
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'reject', 'note': 'x'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'APPROVED')
        # سجل الجودة
        log = body(self.qc.get('/quality/log'))
        self.assertIn(bn, log); self.assertIn('تعليق', log); self.assertIn('اعتماد للتخزين', log)
        # الرفض النهائي
        w2 = self.order('GS310M', 5000)
        bn2 = w2['batch_no']
        _, t2 = self.cut(bn2)
        self.fold(bn2, t2[0], 5000)
        self.op.post(f'/orders/{bn2}/finish', {})
        self.qa.post(f'/quality/decide/{bn2}', {'decision': 'reject', 'note': 'شوائب'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn2,))['f'], 'REJECTED')
        self.assertEqual(db.one('SELECT approved_qty a FROM work_orders WHERE batch_no=?', (bn2,))['a'], None)
        self.assertEqual(self.store.post('/warehouse', {'batch_no': bn2}, follow=False).status_code, 302)
        self.assertIsNone(db.one('SELECT 1 FROM fg_receipts WHERE batch_no=?', (bn2,)))                # المرفوض لا يُخزَّن

    # ================================================================== 9. SP: مسار مبسّط بلا مراحل افتراضية
    def test_09_sp_route(self):
        it = db.one("""SELECT * FROM items WHERE prefix='GS' AND size='10x10 cm' AND ply=8 AND sterile='معقم'
                       AND status='نشط' AND item_code<>'GS310M' ORDER BY item_code LIMIT 1""")
        spi = db.one("SELECT item_code FROM items WHERE prefix='SP' AND size=? AND ply=? AND xray=? AND mesh=?",
                     (it['size'], it['ply'], it['xray'], it['mesh']))
        self.assertIsNotNone(spi)
        f = {'route_code': 'SP_GAUZE', 'product_category': 'Gauze', 'uom': 'قطعة', 'description': it['description'], 'active': 'on',
             'pc_unit_1': 'pack', 'pc_per_1': '10', 'pc_unit_2': 'box', 'pc_per_2': '5', 'pc_unit_3': 'carton', 'pc_per_3': '2',
             'pc_part_1': 'on', 'pc_part_2': 'on', 'pc_part_3': 'on'}
        self.mgr.post(f'/master/products/{it["item_code"]}', f)
        w = self.order(it['item_code'], 600)
        bn = w['batch_no']
        self.assertEqual((w['route_code'], w['fold_machine']), ('SP_GAUZE', None))
        self.assertRegex(bn, r'-SP-\d{3}$')
        # المعقم لا يحصل على فرز/تغليف أولي/تعقيم/تهوية افتراضية — المسار وحده يحدد
        self.assertEqual([s_['code'] for s_ in mfg.progress(bn)['steps']], ['ALLOC', 'SPPACK', 'FINALQC', 'WH'])
        self.assertEqual(self.state(bn)['next']['endpoint'], 'sp_alloc')
        pg = body(self.op.get(f'/orders/{bn}'))
        self.assertIn('استكمال العمل', pg); self.assertIn(f'href="/sp/{bn}/alloc"', pg)
        # استلام خام SP وإفراجه
        self.store.post('/sp/receive', {'supplier_name': 'مورد SP', 'item_code': spi['item_code'], 'supplier_lot': 'SPLOT-9',
                                        'qty': '1000', 'packages': '10', 'wh_location': 'حجر A1'})
        grn = db.one("SELECT grn_no FROM receipts WHERE supplier_lot='SPLOT-9'")['grn_no']
        self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123'})
        # التخصيص: الكمية المقترحة جاهزة، بلا تاريخ ولا مشغّل
        self.assertEqual(form_fields(body(self.op.get(f'/sp/{bn}/alloc'))), ['qty'])
        n0 = self.notes()
        self.op.post(f'/sp/{bn}/alloc', {'grn_no': grn})
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 600)
        self.assertEqual(inventory.balance(grn, 'RM'), 400)
        # التعبئة: الكمية فقط، والمكافئات تلقائية
        fields = form_fields(body(self.op.get(f'/sp/{bn}/pack')))
        self.assertEqual(fields, ['good', 'reject', 'scrap', 'notes'])
        self.op.post(f'/sp/{bn}/pack', {'good': '590', 'reject': '5', 'scrap': '5'})
        pr = db.one("SELECT * FROM proc_batches WHERE stage_code='SPK'")
        self.assertEqual((pr['qty_out'], pr['operator']), (590, 'مستخدم op'))
        # حذف سند التعبئة = عكس المعاملة (المصروف يعود، والناتج يُخصم)، ثم إعادة الإدخال (لا رقم مكرر في الدفتر)
        self.op.post(f'/production/proc/{pr["proc_no"]}/delete', {})
        self.assertIsNone(db.one("SELECT 1 FROM proc_batches WHERE stage_code='SPK' AND batch_no=?", (bn,)))
        self.assertEqual((inventory.balance(bn, 'ISSUED'), inventory.balance(pr['proc_no'], 'SPK_OUT'),
                          inventory.total(bn, 'REJECT'), inventory.total(bn, 'SCRAP')), (600, 0, 0, 0))
        self.assertEqual(self.state(bn)['produced'], 0)
        self.op.post(f'/sp/{bn}/pack', {'good': '590', 'reject': '5', 'scrap': '5'})
        pr2 = db.one("SELECT * FROM proc_batches WHERE stage_code='SPK' AND batch_no=?", (bn,))
        self.assertNotEqual(pr2['proc_no'], pr['proc_no'])                      # الرقم لا يُعاد استعماله
        pr = pr2
        self.assertEqual(json.loads(pr['extra_json'])['equiv']['بوكس'], 11.8)
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 0)
        self.assertEqual(inventory.balance(pr['proc_no'], 'SPK_OUT'), 590)
        self.assertEqual(self.notes(), n0)                                         # لا إشعارات للتخصيص/التعبئة
        st = self.state(bn)
        self.assertEqual((st['produced'], st['next']['kind']), (590, 'finish'))
        self.op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(mfg.released_qty(bn)[:2], (590.0, 'قطعة'))
        self.store.post('/warehouse', {'batch_no': bn})
        self.assertEqual(inventory.balance(pr['proc_no'], 'SPK_OUT'), 0)
        self.assertEqual(inventory.total(bn, 'FG'), 590)
        self.assertEqual(self.state(bn)['stage'], 'stored')
        g = body(self.mgr.get(f'/genealogy?b={bn}'))
        for needle in (bn, grn, 'SPLOT-9', 'مورد SP', pr['proc_no']):
            self.assertIn(needle, g, needle)

    # ================================================================== 10. الرباط: بلا شاشات بوكس/كرتون
    def test_10_bandage_route(self):
        w = self.order('CB100', 58)
        bn = w['batch_no']
        self.assertEqual((w['route_code'], w['uom']), ('BANDAGE', 'رول'))
        self.assertEqual([s_['code'] for s_ in mfg.progress(bn)['steps']], ['ALLOC', 'BMACH', 'BWRAP', 'FINALQC', 'WH'])
        self.assertIn(f'href="/bandage/{bn}/alloc"', body(self.op.get(f'/orders/{bn}')))
        self.assertNotIn('bandage_box', {r.endpoint for r in app.url_map.iter_rules()})
        self.assertNotIn('bandage_carton', {r.endpoint for r in app.url_map.iter_rules()})
        grn, (roll, _r2) = recv_jumbo(self.store, self.qc, 'JB-10', width=60, length=250, code='PBT01', cls='JUMBO', rolls=2)
        self.assertEqual(form_fields(body(self.op.get(f'/bandage/{bn}/alloc'))), [])           # نقرة تخصيص فقط
        n0 = self.notes()
        self.op.post(f'/bandage/{bn}/alloc', {'roll_no': roll})
        self.assertEqual(db.one('SELECT batch_no b FROM rolls WHERE roll_no=?', (roll,))['b'], bn)
        page = body(self.op.get(f'/bandage/{bn}/machine'))
        self.assertEqual(form_fields(page), ['roll_no', 'output_rolls', 'input_len', 'reject_rolls', 'scrap', 'scrap_unit'])
        self.op.post(f'/bandage/{bn}/machine', {'roll_no': roll, 'output_rolls': '60', 'reject_rolls': '2', 'scrap': '3'})
        bm = db.one("SELECT * FROM proc_batches WHERE stage_code='BM'")
        self.assertEqual((bm['qty_in'], bm['qty_out'], bm['qty_reject'], bm['machine']), (250, 60, 2, 'BM-01'))
        self.assertEqual(inventory.balance(roll, 'RM'), 0)
        self.assertEqual(inventory.balance(bm['proc_no'], 'BM_OUT'), 60)
        self.assertEqual(form_fields(body(self.op.get(f'/bandage/{bn}/wrap'))), ['src', 'good', 'reject'])
        r = self.op.post(f'/bandage/{bn}/wrap', {'src': bm['proc_no'], 'good': '61', 'reject': '0'})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM proc_batches WHERE stage_code='BW'")['n'], 0)        # أكثر مما خرج
        self.op.post(f'/bandage/{bn}/wrap', {'src': bm['proc_no'], 'good': '58', 'reject': '2'})
        bw = db.one("SELECT * FROM proc_batches WHERE stage_code='BW'")
        self.assertEqual((bw['parent_proc'], bw['qty_out']), (bm['proc_no'], 58))
        self.assertEqual(self.notes(), n0)
        # السلسلة تُحذف من الآخر: لا حذف لماكينة الأربطة قبل التغليف، ولا إلغاء تخصيص قبل حذف التشغيل
        self.op.post(f'/production/proc/{bm["proc_no"]}/delete', {})
        self.assertIsNotNone(db.one('SELECT 1 FROM proc_batches WHERE proc_no=?', (bm['proc_no'],)))
        adoc = db.one('SELECT doc_no FROM allocations WHERE batch_no=? AND voided=0', (bn,))['doc_no']
        self.op.post(f'/production/alloc/{adoc}/delete', {})
        self.assertEqual(db.one('SELECT voided v FROM allocations WHERE doc_no=?', (adoc,))['v'], 0)
        self.op.post(f'/production/proc/{bw["proc_no"]}/delete', {})
        self.assertEqual((inventory.balance(bm['proc_no'], 'BM_OUT'), inventory.balance(bw['proc_no'], 'BW_OUT'),
                          self.state(bn)['produced']), (60, 0, 0))
        self.op.post(f'/production/proc/{bm["proc_no"]}/delete', {})
        self.assertEqual((inventory.balance(roll, 'RM'), inventory.balance(bm['proc_no'], 'BM_OUT'),
                          inventory.balance(bm['proc_no'], 'USED')), (250, 0, 0))
        self.op.post(f'/production/alloc/{adoc}/delete', {})
        self.assertEqual(db.one('SELECT batch_no b FROM rolls WHERE roll_no=?', (roll,))['b'], None)     # الجامبو حرّ من جديد
        self.assertIn(roll, body(self.op.get(f'/bandage/{bn}/alloc')))
        # إعادة التشغيل من البداية
        self.op.post(f'/bandage/{bn}/alloc', {'roll_no': roll})
        self.op.post(f'/bandage/{bn}/machine', {'roll_no': roll, 'output_rolls': '60', 'reject_rolls': '2', 'scrap': '3'})
        bm = db.one("SELECT * FROM proc_batches WHERE stage_code='BM' AND batch_no=?", (bn,))
        self.op.post(f'/bandage/{bn}/wrap', {'src': bm['proc_no'], 'good': '58', 'reject': '2'})
        bw = db.one("SELECT * FROM proc_batches WHERE stage_code='BW' AND batch_no=?", (bn,))
        self.assertEqual((bw['parent_proc'], bw['qty_out']), (bm['proc_no'], 58))
        st = self.state(bn)
        self.assertEqual((st['produced'], st['remaining'], st['next']['kind']), (58, 0, 'finish'))
        self.op.post(f'/orders/{bn}/finish', {})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.store.post('/warehouse', {'batch_no': bn})
        self.assertEqual(inventory.total(bn, 'FG'), 58)
        self.assertEqual(inventory.balance(bw['proc_no'], 'BW_OUT'), 0)
        eq = {e['unit']: e['qty'] for e in mfg.pack_equiv('CB100', 58, 'رول')}
        self.assertEqual(eq, {'بوكس': 5.8, 'كرتون': 0.58})
        g = body(self.mgr.get(f'/genealogy?b={bn}'))
        for needle in (roll, grn, 'JB-10', bm['proc_no'], bw['proc_no']):
            self.assertIn(needle, g, needle)
        out = mfg.output(bn)
        self.assertEqual((out['good'], out['reject'], out['scrap']), (58, 4, 3))

    # ================================================================== 11. لا شاشات «تسجيل الاستهلاك» ولا بوابات
    def test_11_no_consumption_screen_no_gates(self):
        eps = {r.endpoint for r in app.url_map.iter_rules()}
        for gone in ('slit_roll', 'slit_plan', 'slit_review', 'quality_tasks', 'quality_final', 'line_view', 'manager_home',
                     'api_plan', 'api_route_preview', 'bandage_box', 'bandage_carton'):
            self.assertNotIn(gone, eps, gone)
        self.assertNotIn('تسجيل الاستهلاك', body(self.op.get('/folding')) + body(self.op.get('/orders')))
        self.assertEqual(db.setting('qc_gate_mode'), 'off')
        # الفحوصات القديمة مخفية عن الإنتاج وعن قائمة الجودة
        self.assertNotIn('قوالب الجودة', body(self.qc.get('/')).split('<main')[0])

    # ================================================================== 12. ترقيم وترحيل
    def test_12_numbers_and_migration(self):
        with db.tx() as con:
            a = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
            b = db.use_number(con, 'T-260101-007', 'T-260101', 'T-260101-{n3}')
            c = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
            d = db.use_number(con, 'FREE-TEXT', 'T-260101', 'T-260101-{n3}')
            e = db.alloc(con, 'SRX', 'SR-{n4}')
        self.assertEqual((a, b, c, d, e), ('T-260101-001', 'T-260101-007', 'T-260101-008', 'FREE-TEXT', 'SR-0001'))
        migrate_v14.run(); migrate_v14.run()                                    # آمن على التكرار
        self.assertEqual(db.one("SELECT COUNT(*) n FROM route_steps WHERE route_code='FULL_GAUZE'")['n'], 4)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM units")['n'], 5)

    # ================================================================== 14. الشحن من المخزن (أمين المخزن فقط)
    def test_14_shipping_after_storage(self):
        e = getattr(type(self), 'e2e', None)
        if not e:
            self.skipTest('يتطلب test_02')
        bn = e['bn']
        self.assertEqual(self.op.post('/shipping', {'batch_no': bn, 'customer': 'ع', 'qty': '1'}, follow=False).status_code, 403)
        self.assertEqual(self.op.get('/shipping').status_code, 403)
        self.assertIn('الكمية غير صحيحة', body(self.store.post('/shipping', {'batch_no': bn, 'customer': 'مستشفى الأحساء', 'qty': '99999'})))
        self.store.post('/shipping', {'batch_no': bn, 'customer': 'مستشفى الأحساء', 'qty': '20000'})
        sh = db.one('SELECT * FROM shipments WHERE batch_no=?', (bn,))
        self.assertEqual((sh['qty'], sh['customer'], sh['shipper']), (20000, 'مستشفى الأحساء', 'مستخدم store'))
        self.assertTrue(sh['doc_no'].startswith('SHP-'))
        self.assertIn('مستشفى الأحساء', body(self.mgr.get(f'/genealogy?b={bn}')))
        # التتبع الأمامي من لوط الخام إلى العميل
        self.assertIn('مستشفى الأحساء', body(self.mgr.get('/genealogy?ref=LOT-E2E')))

    # ================================================================== 15. سب رول قديم (قبل v14) يبقى صالحًا للطي
    def test_15_legacy_subroll_usable(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        db.run("""INSERT INTO subrolls(tag_no,roll_no,item_code,batch_no,dest_machine,ply,target_size,length_m,width_cm,std_width_cm,
                  xray,mesh,stock_status,slit_batch) VALUES('SR-LEGACY-1','RR005-OLD','RR005','SEP-OLD-SL-001','FD-10',8,'10x10 cm',
                  2000,23,23,'WITHOUT X-RAY','19x15','متاح','SEP-OLD-SL-001')""")
        self.assertIn('value="SR-LEGACY-1"', body(self.op.get(f'/folding/{bn}')))
        self.assertIn('تم حفظ سند الإنتاج', body(self.fold(bn, 'SR-LEGACY-1', 5000)))
        self.assertEqual(db.one("SELECT stock_status s FROM subrolls WHERE tag_no='SR-LEGACY-1'")['s'], 'مستهلك')
        self.assertEqual(self.state(bn)['produced'], 5000)
        # سب رول بعرض مختلف (منتج آخر) لا يظهر ولا يُقبل
        db.run("""INSERT INTO subrolls(tag_no,roll_no,item_code,batch_no,dest_machine,ply,width_cm,std_width_cm,stock_status)
                  VALUES('SR-LEGACY-2','RR005-OLD','RR005','SEP-OLD-SL-001','FD-10',8,33,33,'متاح')""")
        w2 = self.order('GS310M', 5000)
        self.assertNotIn('value="SR-LEGACY-2"', body(self.op.get(f'/folding/{w2["batch_no"]}')))
        r = self.fold(w2['batch_no'], 'SR-LEGACY-2', 100)
        self.assertIsNone(db.one("SELECT 1 FROM folding_out WHERE tag_no='SR-LEGACY-2'"))

    # ================================================================== 13. كل الصفحات تعمل لكل دور
    def test_13_all_pages_render(self):
        w = self.order('GS310M', 5000)
        bn = w['batch_no']
        roll, tags = self.cut(bn)
        self.fold(bn, tags[0], 5000)
        doc = db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tags[0],))['doc_no']
        urls = [f'/folding/doc/{doc}/edit', f'/tags?b={bn}', f'/quality/review/{bn}', f'/genealogy?b={bn}', f'/trace?b={bn}',
                '/wip', '/fg', '/warehouse', '/notifications', '/receipts', '/receipts/new', '/master/products',
                '/master/products/GS310M', '/master/routes', '/admin/audit', '/admin/backup', '/shipping', '/ncr']
        for c, name in ((self.mgr, 'mgr'), ):
            for url in urls:
                r = c.get(url, follow_redirects=True)
                self.assertLess(r.status_code, 400, (name, url))
        for name, c in (('mgr', self.mgr), ('op', self.op), ('qc', self.qc), ('store', self.store), ('view', self.view)):
            for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
                if 'GET' not in rule.methods or rule.arguments or rule.endpoint in ('static', 'logout', 'login'):
                    continue
                r = c.get(rule.rule, follow_redirects=False)
                self.assertLess(r.status_code, 500, (name, rule.rule))
        # صفحة الصنف تعرض الحقول التي تقود الأمر
        page = body(self.mgr.get('/master/products/GS310M'))
        for name in ('sub_roll_width_cm', 'yield_per_sr', 'raw_item', 'machine_code', 'pack_code', 'box_code', 'master_box'):
            self.assertIn(f'name="{name}"', page)
        self.assertIn('value="23"', page.replace('value="23.0"', 'value="23"'))
        # تعديل بيانات المنتج يغيّر الاحتياج لاحقًا ولا يمس الأوامر القائمة
        f = {'route_code': 'FULL_GAUZE', 'product_category': 'Gauze', 'uom': 'باكت', 'description': 'x', 'active': 'on',
             'sub_roll_width_cm': '23', 'yield_per_sr': '4000', 'raw_item': 'RR005', 'machine_code': 'FD-10',
             'pack_code': 'PK013', 'box_code': 'BX001', 'master_box': 'MB007',
             'pc_unit_1': 'pack', 'pc_per_1': '100', 'pc_unit_2': 'box', 'pc_per_2': '20', 'pc_unit_3': 'carton', 'pc_per_3': '10'}
        self.mgr.post('/master/products/GS310M', f)
        w2 = self.order('GS310M', 8000)
        self.assertEqual((w2['sr_needed'], w2['pieces_per_sr']), (2, 4000))
        self.assertEqual(db.one('SELECT sr_needed FROM work_orders WHERE batch_no=?', (bn,))['sr_needed'], 1)
        f['yield_per_sr'] = '5000'
        self.mgr.post('/master/products/GS310M', f)


if __name__ == '__main__':
    unittest.main()
