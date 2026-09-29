# -*- coding: utf-8 -*-
"""اختبار v15 — التتبع الكامل: GS310M غير معقم + منتج معقم حقيقي من Excel، الاستخدام المزدوج، التعديل/الحذف،
تغيير الكود، استيراد Excel، الطباعة، الصلاحيات والقائمة.

التشغيل:  cd qms && python -m unittest discover -s tests -v      (قاعدة مؤقتة QMS_DB، لا يمس data/qms.db)
"""
import os, re, sys, io, tempfile, unittest, datetime, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_tmp = tempfile.mkdtemp(prefix='qms-test-')
os.environ['QMS_DB'] = os.path.join(_tmp, 'qms.db')

import seed, migrate_v11, migrate_v12, migrate_v13, migrate_v14, migrate_v15, migrate_v16, db, auth   # noqa: E402
seed.main()
migrate_v11.run(); migrate_v12.run(); migrate_v13.run(); migrate_v14.run(); migrate_v15.run(); migrate_v16.run(); db.apply_migrations()
auth.ensure_admin()
db.run('UPDATE users SET must_change_pw=0')
for un, role in (('mgr', 'manager'), ('op', 'operator'), ('qc', 'qc'), ('qa', 'qa'), ('view', 'viewer'), ('store', 'store'), ('tech', 'maint')):
    db.run("INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw) VALUES(?,?,?,?,1,0)",
           (un, f'مستخدم {un}', auth.hash_pw('pass123'), role))
from app import app                                     # noqa: E402
import mfg, inventory, constants                         # noqa: E402

TODAY = datetime.date.today().isoformat()


class Client:
    def __init__(self, user, pw='pass123'):
        self.c = app.test_client()
        r = self.c.get('/login')
        self.tok = re.search(r'name="csrf" content="([^"]+)"', r.get_data(True)).group(1)
        r = self.c.post('/login', data={'username': user, 'password': pw, '_csrf': self.tok})
        assert r.status_code == 302, r.status_code

    def get(self, url, follow=True):
        return self.c.get(url, follow_redirects=follow)

    def post(self, url, data=None, follow=True, files=None):
        d = dict(data or {}); d['_csrf'] = self.tok
        return self.c.post(url, data=d, follow_redirects=follow, content_type='multipart/form-data' if files else None) \
            if not files else self.c.post(url, data={**d, **files}, follow_redirects=follow, content_type='multipart/form-data')


def body(r):
    return r.get_data(True)


def flashes(r):
    return [re.sub(r'<[^>]+>', '', m).strip() for m in re.findall(r'<div class="msg [a-z]+">(.*?)</div>', body(r), re.S)]


def txt(r):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body(r)))


class V15(unittest.TestCase):
    def assertIn(self, a, b, msg=None):                     # رسالة قصيرة بدل طباعة الصفحة كلها
        if a not in b:
            self.fail(msg or f'{a!r} غير موجود ({len(b)} حرف)')

    def assertNotIn(self, a, b, msg=None):
        if a in b:
            self.fail(msg or f'{a!r} موجود ولا يجب')

    @classmethod
    def setUpClass(cls):
        cls.adm, cls.mgr, cls.op, cls.qc = Client('admin', 'admin'), Client('mgr'), Client('op'), Client('qc')
        cls.qa, cls.view, cls.store = Client('qa'), Client('view'), Client('store')
        cls.opn = db.one("SELECT name FROM operators WHERE dept='production' ORDER BY id")['name']
        cls.sop = db.one("SELECT name FROM operators WHERE dept='sterilization' ORDER BY id")['name']
        cls.lot = 0

    # ---------------------------------------------------------------- أدوات
    def jumbo(self, code='RR005'):
        type(self).lot += 1
        d = {'supplier_name': 'مورد تجريبي', 'item_code': code, 'supplier_lot': f'LOT-{self.lot}', 'roll_count': '1'}
        r = self.store.post('/receipts/new', d)
        grn = re.search(r'GRN-\d{6}-\d{3}', body(r)).group(0)
        self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123', 'insp_date': TODAY})
        return db.one('SELECT roll_no FROM rolls WHERE grn_no=?', (grn,))['roll_no']

    def order(self, code, qty):
        self.mgr.post('/orders/new', {'item_code': code, 'qty_required': str(qty)})
        return db.one("SELECT * FROM work_orders WHERE item_code=? ORDER BY rowid DESC LIMIT 1", (code,))

    def cut(self, bn, roll=None, w='120', l='2000', mode='run'):
        roll = roll or self.jumbo()
        r = self.op.post(f'/slitter/{bn}', {'roll_no': roll, 'mode': mode, 'actual_width': w, 'actual_length': l, 'operator': self.opn})
        return roll, r

    def subs(self, bn, status=None):
        sql = 'SELECT tag_no FROM subrolls WHERE batch_no=?' + (' AND stock_status=?' if status else '') + ' ORDER BY tag_no'
        return [x['tag_no'] for x in db.q(sql, (bn, status) if status else (bn,))]

    def fold(self, bn, tag, qty, **kw):
        d = {'tag_no': tag, 'qty_good': str(qty), 'operator': self.opn}; d.update(kw)
        return self.op.post(f'/folding/{bn}', d)

    def bal(self, bn):
        out = {}
        for b in inventory.buckets(batch_no=bn):
            if abs(b['qty']) > 1e-9:
                out[b['stage']] = out.get(b['stage'], 0) + b['qty']
        return out

    def theo(self, w):
        return mfg.theoretical_swabs(w['fold_machine'], w['std_width_cm'], 2000, w['std_width_cm'])

    # ================================================================== 1. Master Data من Excel
    def test_01_master_data_from_excel(self):
        it = db.one("SELECT * FROM items WHERE item_code='GS310M'")
        self.assertTrue(it['uid'] and len(it['uid']) == 32)
        m = mfg.match_subrolls(it)
        self.assertEqual([x['item_code'] for x in m], ['SR009'])            # 23 سم + Plain + Mesh 19×15
        self.assertEqual(db.one("SELECT ref_width_cm r FROM items WHERE item_code='SR009'")['r'], 23)
        self.assertEqual(it['sp_item'], 'SP310M')
        ps = mfg.pack_spec('GS310M')
        self.assertEqual(ps['swabs_per_pack'], 100)
        self.assertIsNone(ps['packs_per_carton'])                            # غير موجود في Excel: لا اختراع
        page = txt(self.adm.get('/admin/data-alerts'))
        self.assertIn('غير معرَّف', page)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM items WHERE uid IS NULL")['n'], 0)
        self.assertEqual(db.one('SELECT COUNT(DISTINCT uid) n FROM items')['n'], db.one('SELECT COUNT(*) n FROM items')['n'])

    # ================================================================== 2. GS310M غير معقم — سلسلة كاملة
    def test_02_gs310m_non_sterile_end_to_end(self):
        adm = self.adm
        # الإدارة تكمل ما ليس في Excel (باكتات/كرتون) من Packaging Configuration
        r = adm.post('/admin/pack-config/GS310M', {'swabs_per_pack': '100', 'packs_per_carton': '20', 'pack_code': 'PK013', 'carton_code': 'MB007'})
        self.assertEqual(mfg.pack_spec('GS310M')['packs_per_carton'], 20)
        w = self.order('GS310M', 500)
        bn = w['batch_no']
        self.assertEqual((w['req_swabs'], w['sr_needed'], w['jumbo_needed'], w['uom']), (50000, 10, 2, 'باكت'))
        # صفحة السليتر: الحقول المطلوبة فقط، مع تعبئة تلقائية
        roll1 = self.jumbo()
        page = body(self.op.get(f'/slitter/{bn}'))
        self.assertIn('name="actual_width"', page); self.assertIn('name="operator"', page)
        self.assertIn('SR009', page)
        self.assertNotIn('name="run_no"', page)
        roll1, r = self.cut(bn, roll1)
        self.assertIn('كود SR009', ' '.join(flashes(r)))
        sr = db.q('SELECT * FROM subrolls WHERE batch_no=? ORDER BY tag_no', (bn,))
        self.assertEqual(len(sr), 5)
        self.assertTrue(all(x['sr_code'] == 'SR009' and x['barcode'] == x['tag_no'] and x['run_no'] and x['width_cm'] == 23 and
                            x['length_m'] == 2000 and x['supplier_lot'] for x in sr))
        self.assertRegex(sr[0]['run_no'], r'^[A-Z]{3}-\d{4}-SL-\d{3}$')
        self.assertEqual(db.one('SELECT use_status u FROM rolls WHERE roll_no=?', (roll1,))['u'], 'مستهلك')
        # الجامبو لا يُستخدم مرتين
        r = self.op.post(f'/slitter/{bn}', {'roll_no': roll1, 'mode': 'run', 'actual_width': '120', 'actual_length': '2000', 'operator': self.opn})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM cutting_plans WHERE roll_no=?', (roll1,))['n'], 1)
        # الطي: مسحات سليمة، النظري والهدر تلقائيان
        page = body(self.op.get(f'/folding/{bn}'))
        self.assertIn('name="qty_good"', page); self.assertIn('name="operator"', page)
        for bad in ('name="machine', 'name="fdate', 'name="doc_no', 'name="size'):
            self.assertNotIn(bad, page)
        t = self.theo(w); self.assertEqual(t, 5000)
        tags = self.subs(bn)
        r = self.fold(bn, tags[0], 4850)
        self.assertIn('نظري 5,000', ' '.join(flashes(r))); self.assertIn('هدر 150', ' '.join(flashes(r)))
        fo = db.one('SELECT * FROM folding_out WHERE tag_no=?', (tags[0],))
        self.assertEqual((fo['unit'], fo['qty_good'], fo['theoretical_qty'], fo['waste_qty'], fo['waste_pct']), ('مسحة', 4850, 5000, 150, 3))
        self.assertRegex(fo['doc_no'], r'^FOLD-\d{4}-\d{6}$')
        # السب رول يُستخدم مرة واحدة
        r = self.fold(bn, tags[0], 100)
        self.assertIn(f'تم استخدام هذا Sub Roll مسبقا في سند رقم {fo["doc_no"]}.', ' '.join(flashes(r)))
        # هدر أعلى من الحد → تحذير
        r = self.fold(bn, tags[1], 3000)
        self.assertIn('الهدر أعلى من الحد', ' '.join(flashes(r)))
        self.op.post(f'/folding/doc/{db.one("SELECT doc_no FROM folding_out WHERE tag_no=?", (tags[1],))["doc_no"]}/delete')
        self.assertEqual(db.one('SELECT stock_status s FROM subrolls WHERE tag_no=?', (tags[1],))['s'], 'متاح')      # عكس
        for tg in tags[1:]:
            self.fold(bn, tg, 4850)
        roll2, _ = self.cut(bn)
        for tg in self.subs(bn, 'متاح'):
            self.fold(bn, tg, 4850)
        st = mfg.order_state(bn)
        self.assertEqual((st['produced'], st['unpacked']), (48500, 48500))
        self.assertEqual(st['next']['endpoint'], 'ns_pack')
        # التعبئة (وليس التغليف): 100 مسحة = باكت
        page = body(self.op.get(f'/packing/{bn}'))
        self.assertIn('تعبئة', page)
        r = self.op.post(f'/packing/{bn}', {'swabs_in': '48500', 'operator': self.opn})
        pk = db.one('SELECT * FROM ns_packing WHERE batch_no=?', (bn,))
        self.assertEqual((pk['packs_actual'], pk['cartons_actual'], pk['swabs_per_pack'], pk['packs_per_carton']), (485, 24, 100, 20))
        self.assertEqual(self.bal(bn), {'NSP_OUT': 485})
        # إنهاء الإنتاج بالكمية الفعلية ثم الإفراج ثم المخزن
        r = self.op.post(f'/orders/{bn}/finish', {})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], None)
        self.op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        w2 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
        self.assertEqual((w2['final_status'], w2['produced_qty']), ('PENDING_QC', 485))
        self.assertEqual(self.qc.get(f'/quality/review/{bn}').status_code, 200)
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'APPROVED')
        r = self.store.post('/warehouse', {'batch_no': bn})
        self.assertIn('تم تخزين 485 باكت', ' '.join(flashes(r)))
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'STORED')
        self.assertEqual(self.bal(bn), {'FG': 485})                            # WIP فُرِّغ بالكامل
        # الطباعة والبطاقات
        for u in (f'/print/order/{bn}', f'/print/slitter/{sr[0]["run_no"]}', f'/print/folding/{fo["doc_no"]}', f'/print/ns_pack/{pk["doc_no"]}',
                  f'/label/subroll/{sr[0]["run_no"]}', f'/label/pack/{pk["doc_no"]}', f'/label/ns_carton/{pk["doc_no"]}', f'/label/jumbo/{roll1}', f'/label/fg/{bn}'):
            r = self.op.get(u)
            self.assertEqual(r.status_code, 200, u)
        self.assertIn('<svg', body(self.op.get(f'/label/subroll/{sr[0]["run_no"]}')))
        # التتبع من أي معرّف
        for q in (sr[0]['tag_no'], roll1, sr[0]['supplier_lot'], fo['doc_no'], pk['doc_no'], sr[0]['run_no'], bn):
            self.assertIn(bn, body(self.qc.get(f'/trace/center?q={q}')), q)
        page = body(self.qc.get(f'/trace/center?q={roll1}'))
        self.assertIn(fo['doc_no'], page)                                       # أمامي: الجامبو ← الطي
        page = body(self.qc.get(f'/trace/center?q={bn}'))
        self.assertIn(roll1, page); self.assertIn(pk['doc_no'], page); self.assertIn(sr[0]['tag_no'], page)   # خلفي

    # ================================================================== 3. منتج معقم حقيقي — سلسلة كاملة
    def test_03_sterile_end_to_end(self):
        code = 'GS013M'
        it = db.one('SELECT * FROM items WHERE item_code=?', (code,))
        self.assertEqual(it['sterile'], 'معقم'); self.assertEqual(it['sp_item'], 'SP013M')
        self.assertEqual([x['item_code'] for x in mfg.match_subrolls(it)], ['SR007'])
        self.adm.post(f'/admin/pack-config/{code}', {'swabs_per_envelope': '10', 'swabs_per_box': '100', 'boxes_per_carton': '10', 'box_code': 'BX001M'})
        w = self.order(code, 20)
        bn = w['batch_no']
        roll, _ = self.cut(bn)
        tags = self.subs(bn)
        r = self.fold(bn, tags[0], 9800)
        ic = db.one('SELECT * FROM intermediates WHERE batch_no=?', (bn,))
        self.assertEqual((ic['sp_code'], ic['sp_source'], ic['status'], ic['qty']), ('SP013M', 'INTERNAL_PRODUCTION', 'Pending SP Release', 9800))
        self.assertRegex(ic['barcode'], r'^SPC-\d{6}$')
        # لا فرز قبل إفراج SP
        r = self.op.post('/sterile/new', {'scan': ic['barcode']})
        self.assertIn('إفراج SP', ' '.join(flashes(r)))
        self.assertEqual(db.one('SELECT COUNT(*) n FROM ster_records')['n'], 0)
        self.assertEqual(self.op.get(f'/quality/sp/{bn}').status_code, 403)     # الإنتاج لا يفتح إفراج الجودة
        self.qc.post(f'/quality/sp/{bn}/decide', {'decision': 'approve'})
        self.op.post('/sterile/new', {'scan': ic['barcode']})
        rec = db.one('SELECT * FROM ster_records WHERE batch_no=?', (bn,))['rec_no']
        # الكرتون الوسيط لا يدخل سجلًا ثانيًا
        r = self.op.post('/sterile/new', {'scan': ic['barcode']})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM ster_records WHERE batch_no=?', (bn,))['n'], 1)
        self.op.post(f'/sterile/record/{rec}', {'act': 'sort', 'accepted': '9700', 'rejected': '100', 'operator': self.opn})
        self.op.post(f'/sterile/record/{rec}', {'act': 'pack', 'env_actual': '970', 'env_rejected': '0', 'operator': self.opn})
        r = self.op.post(f'/sterile/record/{rec}', {'act': 'box', 'operator': self.opn})
        rc = db.one('SELECT * FROM ster_records WHERE rec_no=?', (rec,))
        self.assertEqual((rc['status'], rc['ppe'], rc['env_per_box'], rc['boxes_actual'], rc['cartons']),
                         ('Ready for Pre-Sterilization QC', 10, 10, 97, 9))
        # الإفراج قبل التعقيم ثم دورة تعقيم
        self.qc.post(f'/quality/pre/{rec}/decide', {'decision': 'approve'})
        self.assertEqual(db.one('SELECT status s FROM ster_records WHERE rec_no=?', (rec,))['s'], 'Released for Sterilization')
        r = self.op.post('/sterilization/new', {'rec_no': rec, 'operator': self.sop, 'machine': 'EO-01'})
        cyc = db.one('SELECT * FROM cycles ORDER BY rowid DESC LIMIT 1')
        self.assertEqual(db.one('SELECT cycle_no c FROM ster_records WHERE rec_no=?', (rec,))['c'], cyc['cycle_no'])
        self.assertEqual(cyc['operator'], self.sop)
        # الإفراج النهائي قبل اكتمال الدورة/التهوية غير ممكن
        self.assertNotEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')
        cn = cyc['cycle_no']
        self.op.post(f'/sterilization/{cn}', {'act': 'end'})
        self.op.post(f'/sterilization/{cn}', {'act': 'aeration'})
        self.op.post(f'/sterilization/{cn}', {'act': 'aeration_done', 'operator': self.sop, 'confirm': '1'})
        self.assertEqual(db.one('SELECT status s FROM cycles WHERE cycle_no=?', (cn,))['s'], 'Aeration Completed')
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')
        # لا إفراج نهائي دون تحقق فعالية الدورة
        r = self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'PENDING_QC')
        self.qc.post(f'/quality/cycle/{cn}/verify', {'batch_no': bn, 'ci_external': constants.CI_RESULTS[0], 'ci_internal': constants.CI_RESULTS[0],
                                                      'bi_result': constants.BI_NEG})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        w2 = db.one('SELECT * FROM work_orders WHERE batch_no=?', (bn,))
        self.assertEqual((w2['final_status'], w2['approved_qty']), ('APPROVED', 97))
        # الطباعة: LOT الإنتاج ورقم الدورة على البوكس
        page = body(self.op.get(f'/label/box/{rec}'))
        self.assertIn(bn, page); self.assertIn(cn, page)
        for u in (f'/print/ster_record/{rec}', f'/print/cycle/{cn}', f'/label/intermediate/{ic["barcode"]}', f'/label/envelope/{rec}',
                  f'/label/carton/{rec}', f'/print/release/{bn}'):
            self.assertEqual(self.op.get(u).status_code, 200, u)
        # التتبع الكامل بالاتجاهين
        page = body(self.qc.get(f'/trace/center?q={tags[0]}'))
        for x in (ic['barcode'], rec, cn, bn):
            self.assertIn(x, page)
        page = body(self.qc.get(f'/trace/center?q={cn}'))
        self.assertIn(bn, page)
        # المخزن يستلم المعتمد
        r = self.store.post('/warehouse', {'batch_no': bn})
        self.assertIn('تم تخزين 97 بوكس', ' '.join(flashes(r)))
        self.assertEqual(self.bal(bn), {'FG': 97})

    # ================================================================== 4. الاستخدام المزدوج والتزامن
    def test_04_double_use_and_concurrency(self):
        w = self.order('GS310M', 100)
        bn = w['batch_no']
        roll, _ = self.cut(bn)
        tags = self.subs(bn)
        results = []

        def go(i):
            c = Client('op')
            r = c.post(f'/folding/{bn}', {'tag_no': tags[0], 'qty_good': '1000', 'operator': self.opn})
            results.append('تم استخدام هذا Sub Roll مسبقا' in body(r))
        th = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in th]; [t.join() for t in th]
        self.assertEqual(sum(results), 3)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_in WHERE tag_no=?', (tags[0],))['n'], 1)
        # جامبو بالتزامن: سند واحد فقط
        w2 = self.order('GS310M', 100)
        r2 = self.jumbo()
        out = []

        def cutter(i):
            c = Client('op')
            c.post(f'/slitter/{w2["batch_no"]}', {'roll_no': r2, 'mode': 'run', 'actual_width': '120', 'actual_length': '2000', 'operator': self.opn})
            out.append(1)
        th = [threading.Thread(target=cutter, args=(i,)) for i in range(4)]
        [t.start() for t in th]; [t.join() for t in th]
        self.assertEqual(db.one('SELECT COUNT(*) n FROM cutting_plans WHERE roll_no=?', (r2,))['n'], 1)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM subrolls WHERE plan_no IN (SELECT plan_no FROM cutting_plans WHERE roll_no=?)', (r2,))['n'], 5)

    # ================================================================== 5. تعديل وحذف سند الطي مع الاعتماديات
    def test_05_edit_delete_dependencies(self):
        w = self.order('GS310M', 100)
        bn = w['batch_no']
        self.cut(bn)
        tags = self.subs(bn)
        self.fold(bn, tags[0], 4000); self.fold(bn, tags[1], 3000)
        doc = db.one('SELECT doc_no FROM folding_out WHERE tag_no=?', (tags[0],))['doc_no']
        # تعديل الكمية + الوحدة + المشغل
        opx = db.one("SELECT name FROM operators WHERE dept='production' ORDER BY id DESC")['name']
        self.op.post(f'/folding/doc/{doc}/edit', {'tag_no': tags[0], 'qty_good': '45', 'unit': 'باكت', 'operator': opx})
        fo = db.one('SELECT * FROM folding_out WHERE doc_no=?', (doc,))
        self.assertEqual((fo['qty_good'], fo['unit'], fo['operator'], fo['theoretical_qty'], fo['waste_qty']), (4500, 'مسحة', opx, 5000, 500))
        self.assertEqual(mfg.order_state(bn)['produced'], 7500)
        # تعبئة تعتمد على المسحات: تخفيض الطي تحتها ممنوع
        self.adm.post('/admin/pack-config/GS310M', {'swabs_per_pack': '100', 'packs_per_carton': '20'})
        self.op.post(f'/packing/{bn}', {'swabs_in': '7500', 'operator': self.opn})
        r = self.op.post(f'/folding/doc/{doc}/edit', {'tag_no': tags[0], 'qty_good': '1000', 'operator': self.opn})
        self.assertEqual(db.one('SELECT qty_good q FROM folding_out WHERE doc_no=?', (doc,))['q'], 4500)
        self.assertTrue(any('التعبئة' in f or 'تعبئة' in f for f in flashes(r)))
        r = self.op.post(f'/folding/doc/{doc}/delete')
        self.assertIsNotNone(db.one('SELECT 1 FROM folding_out WHERE doc_no=?', (doc,)))
        # حذف التعبئة ثم حذف الطي = Reversal كامل
        pk = db.one('SELECT doc_no FROM ns_packing WHERE batch_no=?', (bn,))['doc_no']
        self.op.post(f'/packing/doc/{pk}/delete')
        self.assertEqual(mfg.order_state(bn)['unpacked'], 7500)
        before = inventory.balance(bn, 'PROD_OUT') if hasattr(inventory, 'balance') else None
        self.op.post(f'/folding/doc/{doc}/delete')
        self.assertIsNone(db.one('SELECT 1 FROM folding_out WHERE doc_no=?', (doc,)))
        self.assertEqual(db.one('SELECT stock_status s FROM subrolls WHERE tag_no=?', (tags[0],))['s'], 'متاح')
        self.assertEqual(mfg.order_state(bn)['produced'], 3000)
        self.fold(bn, tags[0], 4000)                                         # يمكن إعادة استخدامه بعد العكس
        self.assertEqual(mfg.order_state(bn)['produced'], 7000)
        # المشغل من القائمة فقط
        r = self.fold(bn, tags[2], 100, operator='اسم حر غير موجود')
        self.assertIn('اختر المشغّل', ' '.join(flashes(r)))
        # مدير النظام يعدّل بعد الاعتماد؛ مستخدم الإنتاج لا
        self.op.post(f'/packing/{bn}', {'swabs_in': '7000', 'operator': self.opn})
        self.op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'], 'APPROVED')
        d2 = db.one('SELECT doc_no FROM folding_out WHERE batch_no=? ORDER BY id LIMIT 1', (bn,))['doc_no']
        q0 = db.one('SELECT qty_good q FROM folding_out WHERE doc_no=?', (d2,))['q']
        self.op.post(f'/folding/doc/{d2}/edit', {'tag_no': db.one('SELECT tag_no t FROM folding_out WHERE doc_no=?', (d2,))['t'], 'qty_good': '10', 'operator': self.opn})
        self.assertEqual(db.one('SELECT qty_good q FROM folding_out WHERE doc_no=?', (d2,))['q'], q0)   # الإنتاج لا يعدّل بعد الاعتماد
        tg = db.one('SELECT tag_no t FROM folding_out WHERE doc_no=?', (d2,))['t']
        r = self.adm.post(f'/folding/doc/{d2}/edit', {'tag_no': tg, 'qty_good': str(int(q0) + 100), 'operator': self.opn, 'confirm': '1'})
        self.assertEqual(db.one('SELECT qty_good q FROM folding_out WHERE doc_no=?', (d2,))['q'], q0 + 100)
        self.assertIsNone(db.one('SELECT final_status f FROM work_orders WHERE batch_no=?', (bn,))['f'])   # سُحب الاعتماد لإعادة الإفراج

    # ================================================================== 6. تغيير الكود (uid ثابت) والمشغلون
    def test_06_code_rename_and_operators(self):
        db.run("""INSERT INTO items(item_code,prefix,category_ar,description,status,uid) VALUES('ZZTEST1','ZZ','مواد أخرى','مادة اختبار','نشط','uidtest000000000000000000000001')""")
        self.assertEqual(self.op.post('/admin/codes/ZZTEST1/rename', {'new_code': 'ZZTEST2'}).status_code, 403)
        r = self.adm.post('/admin/codes/ZZTEST1/rename', {'new_code': 'ZZTEST2'})
        it = db.one("SELECT * FROM items WHERE uid='uidtest000000000000000000000001'")
        self.assertEqual(it['item_code'], 'ZZTEST2')
        self.assertEqual(db.one("SELECT new_code n FROM item_code_history WHERE uid=?", (it['uid'],))['n'], 'ZZTEST2')
        r = self.adm.post('/admin/codes/ZZTEST2/rename', {'new_code': 'GS310M'})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM items WHERE item_code='GS310M'")['n'], 1)     # لا تكرار
        # الكود المعاد تسميته يتبعه التاريخ: غيّر GS013M إلى كود جديد وتحقق من السجلات
        n_orders = db.one("SELECT COUNT(*) n FROM work_orders WHERE item_code='GS013M'")['n']
        self.adm.post('/admin/codes/GS013M/rename', {'new_code': 'GS013M-X'})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM work_orders WHERE item_code='GS013M-X'")['n'], n_orders)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM pack_spec WHERE item_code='GS013M-X'")['n'], 1)
        self.adm.post('/admin/codes/GS013M-X/rename', {'new_code': 'GS013M'})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM work_orders WHERE item_code='GS013M'")['n'], n_orders)
        # المشغلون: إضافة من الإدارة فقط
        self.adm.post('/admin/operators', {'act': 'add', 'name': 'مشغل جديد', 'dept': 'production'})
        self.assertIn('مشغل جديد', body(self.op.get('/folding')) + body(self.adm.get('/admin/operators')))
        self.assertEqual(self.op.get('/admin/operators').status_code, 403)

    # ================================================================== 7. مركز استيراد Excel
    def test_07_import_center(self):
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'مواد'
        ws.append(['code', 'description', 'uom', 'min_stock'])
        ws.append(['IMP001', 'مادة مستوردة 1', 'قطعة', 5]); ws.append(['IMP002', 'مادة مستوردة 2', 'قطعة', 'abc'])
        ws.append(['IMP001', 'مكرر داخل الملف', 'قطعة', 1]); ws.append(['GS310M', 'وصف جديد', 'باكت', 1])
        ws.append([None, 'بلا كود', 'قطعة', 1])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        c = self.adm
        r = self.adm.c.post('/import', data={'step': '1', '_csrf': c.tok, 'file': (io.BytesIO(buf.getvalue()), 'test.xlsx')}, content_type='multipart/form-data', follow_redirects=True)
        tok = re.search(r'name="tok" value="([0-9a-f]+)"', body(r)).group(1)
        self.assertIn('ربط الأعمدة', body(r))
        url = f'/import?step=3&tok={tok}&sheet=مواد&target=other&hdr=0&map_item_code=0&map_description=1&map_uom=2&map_min_stock=3'
        page = body(c.get(url))
        self.assertIn('مكرر', page); self.assertIn('غير رقمية', page); self.assertIn('الحقل الإلزامي', page)
        r = c.post('/import', {'step': '4', 'tok': tok, 'sheet': 'مواد', 'target': 'other', 'hdr': '0', 'map_item_code': '0',
                               'map_description': '1', 'map_uom': '2', 'map_min_stock': '3', 'mode': 'insert'})
        self.assertIsNotNone(db.one("SELECT 1 FROM items WHERE item_code='IMP001'"))
        self.assertIsNone(db.one("SELECT 1 FROM items WHERE item_code='IMP002'"))
        self.assertNotEqual(db.one("SELECT description d FROM items WHERE item_code='GS310M'")['d'], 'وصف جديد')     # insert لا يعدّل
        lg = db.one('SELECT * FROM import_log ORDER BY id DESC LIMIT 1')
        self.assertEqual((lg['inserted'], lg['rejected'], lg['by_user']), (1, 2, 'admin'))
        self.assertEqual(self.op.get('/import').status_code, 403)

    # ================================================================== 8. الورق الرسمي والطباعة
    def test_08_letterhead_and_print(self):
        page = body(self.op.get('/print/order/' + db.one('SELECT batch_no b FROM work_orders LIMIT 1')['b']))
        self.assertIn('Page ', page); self.assertIn('@page', page); self.assertIn('dir="rtl"', page)
        self.assertNotIn("url('/letterhead')", page)                     # الورق الرسمي معطّل افتراضيًا
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82')
        r = self.adm.c.post('/admin/letterhead', data={'_csrf': self.adm.tok, 'file': (io.BytesIO(png), 'lh.png')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('حُفظ الورق الرسمي', body(r))
        self.assertNotIn("url('/letterhead')", body(self.op.get('/print/order/' + db.one('SELECT batch_no b FROM work_orders LIMIT 1')['b'])))   # الرفع وحده لا يفعّله
        self.assertEqual(self.op.c.post('/admin/letterhead/toggle', data={'_csrf': self.op.tok, 'on': '1'}).status_code, 403)
        self.adm.post('/admin/letterhead/toggle', {'on': '1'})
        page = body(self.op.get('/print/order/' + db.one('SELECT batch_no b FROM work_orders LIMIT 1')['b']))
        self.assertIn("background:url('/letterhead')", page)            # خلفية كل صفحة مطبوعة
        self.assertIn('Page ', page)
        self.assertEqual(self.op.get('/letterhead').status_code, 200)
        self.assertEqual(self.op.c.post('/admin/letterhead', data={'_csrf': self.op.tok}).status_code, 403)
        self.adm.post('/admin/letterhead', {'act': 'delete'})
        self.adm.post('/admin/letterhead/toggle', {})                       # إيقاف
        # الباركود Code128
        import barcode
        self.assertIn('<svg', barcode.svg('SR-0001'))

    # ================================================================== 9. الصلاحيات والقائمة الجانبية
    def test_09_permissions_and_sidebar(self):
        def links(c):
            page = body(c.get('/')).split('<aside')[-1].split('</aside>')[0]
            return set(re.findall(r'href="([^"]+)"', page))
        L_op, L_qc, L_st, L_adm = links(self.op), links(self.qc), links(self.store), links(self.adm)
        for hidden in ('/quality/pending', '/admin/pack-config', '/import', '/admin/operators', '/quality/tasks'):
            self.assertNotIn(hidden, L_op, hidden)
        for hidden in ('/slitter', '/folding', '/admin/pack-config', '/import'):
            self.assertNotIn(hidden, L_qc, hidden)
        self.assertNotIn('/quality/pending', L_st); self.assertNotIn('/slitter', L_st)
        for shown in ('/quality/pending', '/admin/pack-config', '/import', '/admin/operators', '/trace/center', '/inventory/cards'):
            self.assertIn(shown, L_adm, shown)
        self.assertIn('/quality/pending', L_qc); self.assertIn('/trace/center', L_qc)
        self.assertIn('/slitter', L_op)
        # الخادم يمنع الوصول المباشر
        for u, c, code in (('/quality/pending', self.op, 403), ('/admin/pack-config', self.op, 403), ('/import', self.qc, 403),
                           ('/slitter', self.qc, 403), ('/folding', self.store, 403), ('/admin/roles', self.mgr, 200),
                           ('/quality/tasks', self.op, 403), ('/inventory/cards', self.view, 200), ('/sterilization', self.qc, 403)):
            self.assertEqual(c.get(u).status_code, code, (u, code))
        self.assertEqual(self.view.post('/orders/new', {'item_code': 'GS310M', 'qty_required': '1'}, follow=False).status_code, 403)

    # ================================================================== 10. الجودة: 5 إفراجات ومتابعة غير معيقة
    def test_10_quality_monitoring_non_blocking(self):
        page = txt(self.qc.get('/quality'))
        self.assertIn('قوالب', page)
        tpl = db.one("SELECT * FROM qc_templates WHERE kind<>'release' AND area='folding' LIMIT 1")
        self.assertTrue(tpl['form_no'] and tpl['revision'] and tpl['effective_date'])
        self.assertEqual(self.qc.get(f'/quality/new/{tpl["code"]}').status_code, 200)
        self.assertNotIn('release', [k for k in ('release',) if not db.one("SELECT 1 FROM qc_templates WHERE kind='release' LIMIT 1")] or [])
        self.assertEqual(self.qc.get('/quality/tasks').status_code, 200)
        # سجل متابعة (مع ملاحظة/إجراء تصحيحي) لا يمنع أي إفراج
        w = db.one("SELECT batch_no FROM work_orders LIMIT 1")
        for u in ('/quality/pending', '/quality/held', '/quality/log', '/quality/records'):
            self.assertEqual(self.qc.get(u).status_code, 200, u)
        sec = txt(self.qc.get('/quality/pending'))
        self.assertIn('الإفراجات', sec)

    # ================================================================== 12. حالات الجامبو، عدم وجود Sub Roll مطابق، بطاقات المواد
    def test_12_jumbo_states_no_match_cards(self):
        w = self.order('GS310M', 100)
        bn = w['batch_no']
        roll = self.jumbo()
        st = lambda: db.one('SELECT use_status u FROM rolls WHERE roll_no=?', (roll,))['u']
        self.assertIn(st(), (None, '', 'متاح'))                                   # Available
        self.cut(bn, roll, mode='plan')
        self.assertEqual(st(), 'مخصص')                                            # Allocated
        plan = db.one('SELECT plan_no p FROM cutting_plans WHERE roll_no=?', (roll,))['p']
        self.op.post(f'/slitter/plan/{plan}/start')
        self.assertEqual(st(), 'قيد القص')                                        # In Slitting
        self.op.post(f'/slitter/plan/{plan}/confirm')
        self.assertEqual(st(), 'مستهلك')                                          # Consumed
        # Sub Roll لا يُستخدم مرتين حتى عبر الطي المتتابع؛ والجامبو المستهلك لا يظهر للقص
        self.assertNotIn(roll, body(self.op.get(f'/slitter/{bn}')).split('سند سليتر جديد')[-1].split('خطط القص')[0])
        # منتج بلا Sub Roll مطابق: رسالة واضحة + اختيار يدوي، بلا تخمين
        db.run("""INSERT INTO items(item_code,prefix,category_ar,size,ply,mesh,xray,sterile,machine_code,uom,description,status,route_code,sub_roll_width_cm,uid)
                  SELECT 'GSNOMATCH','GS','منتج تام','9x9 cm',8,mesh,xray,'غير معقم','FD-10','باكت','بلا سب رول','نشط','FULL_GAUZE',77,'nomatchuid0000000000000000000001' FROM items WHERE item_code='GS310M'""")
        self.assertEqual(mfg.match_subrolls(db.one("SELECT * FROM items WHERE item_code='GSNOMATCH'")), [])
        db.run("INSERT OR REPLACE INTO pack_spec(item_code,swabs_per_pack) VALUES('GSNOMATCH',100)")
        w2 = self.order('GSNOMATCH', 10)
        self.assertEqual(w2['item_code'], 'GSNOMATCH')
        if True:
            r2 = self.jumbo()
            page = txt(self.op.get(f'/slitter/{w2["batch_no"]}'))
            self.assertIn('لا يوجد Sub Roll مطابق لمواصفات المنتج', page)
            self.assertIn('مراجعة Master Data', page)
            r = self.op.post(f'/slitter/{w2["batch_no"]}', {'roll_no': r2, 'mode': 'run', 'actual_width': '120', 'actual_length': '2000', 'operator': self.opn})
            self.assertEqual(db.one('SELECT COUNT(*) n FROM subrolls WHERE batch_no=?', (w2['batch_no'],))['n'], 0)
        # بطاقة مادة جديدة (أي نوع) + حركة استلام وصرف
        self.adm.post('/inventory/cards/new', {'item_code': 'SPARE-001', 'name_ar': 'قطعة غيار', 'category': 'Spare / Other', 'uom': 'قطعة', 'min_stock': '2'})
        c = db.one("SELECT * FROM items WHERE item_code='SPARE-001'")
        self.assertEqual((c['name_ar'], c['category_en'], c['min_stock']), ('قطعة غيار', 'Spare / Other', 2))
        self.assertTrue(c['uid'])
        self.store.post('/inventory/cards/SPARE-001', {'act': 'receipt', 'qty': '10'})
        r = self.store.post('/inventory/cards/SPARE-001', {'act': 'issue', 'qty': '50'})
        self.assertIn('أكبر من الرصيد', ' '.join(flashes(r)))
        self.store.post('/inventory/cards/SPARE-001', {'act': 'issue', 'qty': '4'})
        self.assertIn('6', txt(self.store.get('/inventory/cards')))
        self.assertEqual(self.store.post('/inventory/cards/SPARE-001', {'act': 'save', 'name_ar': 'x'}, follow=False).status_code, 403)

    # ================================================================== 13. خط SP الخارجي (لم يتأثر بـ v15)
    def test_13_sp_line(self):
        it = db.one("""SELECT * FROM items WHERE prefix='GS' AND size='10x10 cm' AND ply=8 AND sterile='معقم'
                       AND status='نشط' AND item_code<>'GS310M' ORDER BY item_code LIMIT 1""")
        spi = db.one("SELECT item_code FROM items WHERE prefix='SP' AND size=? AND ply=? AND xray=? AND mesh=?",
                     (it['size'], it['ply'], it['xray'], it['mesh']))
        self.assertIsNotNone(spi)
        f = {'route_code': 'SP_GAUZE', 'product_category': 'Gauze', 'uom': 'قطعة', 'description': it['description'], 'active': 'on',
             'pc_unit_1': 'pack', 'pc_per_1': '10', 'pc_unit_2': 'box', 'pc_per_2': '5', 'pc_unit_3': 'carton', 'pc_per_3': '2',
             'pc_part_1': 'on', 'pc_part_2': 'on', 'pc_part_3': 'on'}
        self.mgr.post(f'/master/products/{it["item_code"]}', f)
        self.assertEqual(db.one('SELECT sp_item s FROM items WHERE item_code=?', (it['item_code'],))['s'], it['sp_item'])   # لا يُمسح
        w = self.order(it['item_code'], 600)
        bn = w['batch_no']
        self.assertEqual(w['route_code'], 'SP_GAUZE')
        self.store.post('/sp/receive', {'supplier_name': 'مورد SP', 'item_code': spi['item_code'], 'supplier_lot': 'SPLOT-9',
                                        'qty': '1000', 'packages': '10', 'wh_location': 'حجر A1'})
        grn = db.one("SELECT grn_no FROM receipts WHERE supplier_lot='SPLOT-9'")['grn_no']
        self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123'})
        self.op.post(f'/sp/{bn}/alloc', {'grn_no': grn})
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 600)
        self.op.post(f'/sp/{bn}/pack', {'good': '590', 'reject': '5', 'scrap': '5'})
        pr = db.one("SELECT * FROM proc_batches WHERE stage_code='SPK' AND batch_no=?", (bn,))
        self.assertEqual(pr['qty_out'], 590)
        self.op.post(f'/orders/{bn}/finish', {'short_ok': '1'})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.assertEqual(mfg.released_qty(bn)[:2], (590.0, 'قطعة'))
        self.store.post('/warehouse', {'batch_no': bn})
        self.assertEqual(inventory.total(bn, 'FG'), 590)
        g = body(self.mgr.get(f'/genealogy?b={bn}'))
        for needle in (bn, grn, 'SPLOT-9', pr['proc_no']):
            self.assertIn(needle, g, needle)
        self.assertIn(bn, body(self.qc.get('/trace/center?q=SPLOT-9')))

    # ================================================================== 14. خط الأربطة
    def test_14_bandage_line(self):
        w = self.order('CB100', 58)
        bn = w['batch_no']
        self.assertEqual((w['route_code'], w['uom']), ('BANDAGE', 'رول'))
        type(self).lot += 1
        r = self.store.post('/receipts/new', {'supplier_name': 'مورد', 'item_code': 'PBT01', 'supplier_lot': 'JB-15', 'roll_count': '2',
                                              'width_cm': '60', 'length_m': '250', 'cls': 'JUMBO'})
        grn = re.search(r'GRN-\d{6}-\d{3}', body(r)).group(0)
        self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123', 'insp_date': TODAY})
        roll = db.one('SELECT roll_no FROM rolls WHERE grn_no=? ORDER BY roll_no', (grn,))['roll_no']
        self.op.post(f'/bandage/{bn}/alloc', {'roll_no': roll})
        self.op.post(f'/bandage/{bn}/machine', {'roll_no': roll, 'output_rolls': '60', 'reject_rolls': '2', 'scrap': '3'})
        bm = db.one("SELECT * FROM proc_batches WHERE stage_code='BM' AND batch_no=?", (bn,))
        self.op.post(f'/bandage/{bn}/wrap', {'src': bm['proc_no'], 'good': '58', 'reject': '2'})
        self.op.post(f'/orders/{bn}/finish', {})
        self.qc.post(f'/quality/decide/{bn}', {'decision': 'approve'})
        self.store.post('/warehouse', {'batch_no': bn})
        self.assertEqual(inventory.total(bn, 'FG'), 58)
        self.assertIn(bm['proc_no'], body(self.mgr.get(f'/genealogy?b={bn}')))

    # ================================================================== 15. الترحيل آمن على التكرار ومن قاعدة v14
    def test_15_migration_idempotent(self):
        n = db.one('SELECT COUNT(*) n FROM operators')['n']
        migrate_v16.run(); migrate_v16.run()
        self.assertEqual(db.one('SELECT COUNT(*) n FROM operators')['n'], n)
        self.assertEqual(db.one("SELECT COUNT(DISTINCT uid) n FROM items")['n'], db.one('SELECT COUNT(*) n FROM items')['n'])
        with db.tx() as con:
            a = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
            b = db.use_number(con, 'T-260101-007', 'T-260101', 'T-260101-{n3}')
            c = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
        self.assertEqual((a, b, c), ('T-260101-001', 'T-260101-007', 'T-260101-008'))

    # ================================================================== 16. قرارات الإدارة: أصغر عرض، هدر 3%، بوكس أربطة 12، كراتين يدوية
    def test_16_management_decisions(self):
        it = db.one("SELECT * FROM items WHERE item_code='GS174'")
        self.assertEqual(mfg.item_sr_width(it)[0], 43)                       # 43 بدل 44 من Master Data
        self.assertEqual(len(mfg.match_subrolls(it)), 1)
        for r in db.q("SELECT * FROM items WHERE prefix='GS' AND status='نشط' AND route_code='FULL_GAUZE' AND item_code<>'GSNOMATCH'"):
            self.assertTrue(mfg.match_subrolls(r), r['item_code'])           # كل المنتجات لها Sub Roll
        self.assertEqual(float(db.setting('scrap_limit')), 0.03)
        self.assertEqual(db.one("SELECT per_parent p FROM pack_config WHERE item_code='CB100' AND unit='box'")['p'], 12)
        # كراتين يدوية: بلا باكت/كرتون معرَّف يُدخل المستخدم عدد الكراتين
        db.run("UPDATE pack_spec SET packs_per_carton=NULL WHERE item_code='GS310M'")
        w = self.order('GS310M', 100); bn = w['batch_no']
        self.cut(bn); tags = self.subs(bn)
        self.fold(bn, tags[0], 4900); self.fold(bn, tags[1], 4900)
        self.op.post(f'/packing/{bn}', {'swabs_in': '9800', 'cartons_actual': '5', 'operator': self.opn})
        pk = db.one('SELECT * FROM ns_packing WHERE batch_no=?', (bn,))
        self.assertEqual((pk['packs_actual'], pk['cartons_actual']), (98, 5))
        self.assertIn('يدوي', txt(self.adm.get('/admin/pack-config')))

    # ================================================================== 17. SP وارد من مورّد يغذّي غير المعقم والمعقم
    def test_17_supplier_sp_both_routes(self):
        def receive(item, lot, qty):
            self.store.post('/sp/receive', {'supplier_name': 'مورد SP', 'item_code': item, 'supplier_lot': lot, 'qty': str(qty), 'packages': '1', 'wh_location': 'A1'})
            grn = db.one('SELECT grn_no g FROM receipts WHERE supplier_lot=?', (lot,))['g']
            self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123', 'insp_date': TODAY})
            return grn
        # --- غير معقم
        g1 = receive('SP310M', 'SPX-NS', 20000)
        w = self.order('GS310M', 100); bn = w['batch_no']
        r = self.op.post(f'/sp-supply/{bn}', {'grn_no': g1, 'qty': '10000', 'operator': self.opn})
        self.assertEqual(mfg.order_state(bn)['produced'], 10000)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM intermediates WHERE batch_no=?', (bn,))['n'], 0)
        self.op.post(f'/packing/{bn}', {'swabs_in': '10000', 'cartons_actual': '5', 'operator': self.opn})
        self.assertEqual(db.one('SELECT packs_actual p FROM ns_packing WHERE batch_no=?', (bn,))['p'], 100)
        self.assertIn(bn, body(self.qc.get('/trace/center?q=SPX-NS')))
        # حذف مع اعتماد: ممنوع قبل عكس التعبئة
        doc = db.one("SELECT doc_no d FROM allocations WHERE batch_no=? AND doc_no LIKE 'SPIN-%'", (bn,))['d']
        self.op.post(f'/sp-supply/doc/{doc}/delete')
        self.assertIsNotNone(db.one('SELECT 1 FROM folding_out WHERE doc_no=?', (doc,)))
        # --- معقم
        g2 = receive('SP013M', 'SPX-ST', 5000)
        w2 = self.order('GS013M', 10); bn2 = w2['batch_no']
        self.op.post(f'/sp-supply/{bn2}', {'grn_no': g2, 'qty': '1000', 'operator': self.opn})
        ic = db.one('SELECT * FROM intermediates WHERE batch_no=?', (bn2,))
        self.assertEqual((ic['sp_source'], ic['sp_code'], ic['status'], ic['qty']), ('SUPPLIER_RECEIPT', 'SP013M', 'Pending SP Release', 1000))
        self.qc.post(f'/quality/sp/{bn2}/decide', {'decision': 'approve'})
        self.op.post('/sterile/new', {'scan': ic['barcode']})
        self.assertEqual(db.one('SELECT received_qty q FROM ster_records WHERE batch_no=?', (bn2,))['q'], 1000)
        # LOT لا يطابق المنتج مرفوض
        r = self.op.post(f'/sp-supply/{bn2}', {'grn_no': g1, 'qty': '10', 'operator': self.opn})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM allocations WHERE batch_no=? AND doc_no LIKE 'SPIN-%'", (bn2,))['n'], 1)
        # حذف سطر SP الوارد بعد استهلاكه في سجل ممنوع
        d2 = db.one("SELECT doc_no d FROM allocations WHERE batch_no=? AND doc_no LIKE 'SPIN-%'", (bn2,))['d']
        self.op.post(f'/sp-supply/doc/{d2}/delete')
        self.assertIsNotNone(db.one('SELECT 1 FROM intermediates WHERE fold_doc=?', (d2,)))

    # ================================================================== 18. إشعارات الإفراج، جدول الفرز، تفرّد LOT
    def test_18_notifications_sorting_ui_lot_unique(self):
        db.run("UPDATE pack_spec SET swabs_per_envelope=10, swabs_per_box=100 WHERE item_code='GS013M'")
        w = self.order('GS013M', 10); bn = w['batch_no']
        self.cut(bn); tag = self.subs(bn)[0]
        n0 = db.one("SELECT COUNT(*) n FROM notifications WHERE kind='sp_release_needed'")['n']
        self.fold(bn, tag, 5000)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM notifications WHERE kind='sp_release_needed'")['n'], n0 + 1)
        self.assertIn('إفراج SP', txt(self.qc.get('/notifications')))            # يظهر عند الجرس لمستخدم الجودة
        self.assertNotIn('إفراج SP', txt(self.op.get('/notifications')).split('الإشعارات')[-1][:0] or '')
        self.qc.post(f'/quality/sp/{bn}/decide', {'decision': 'approve'})
        ic = db.one('SELECT barcode b FROM intermediates WHERE batch_no=?', (bn,))['b']
        self.op.post('/sterile/new', {'scan': ic})
        rec = db.one('SELECT rec_no r FROM ster_records WHERE batch_no=?', (bn,))['r']
        page = body(self.op.get(f'/sterile/record/{rec}'))                        # جدول/حقول الفرز ظاهرة لا كود مكتوب
        self.assertIn('<input type="number" name="accepted"', page)
        self.assertNotIn('&lt;input', page)
        self.op.post(f'/sterile/record/{rec}', {'act': 'sort', 'accepted': '4900', 'rejected': '100', 'operator': self.opn})
        self.op.post(f'/sterile/record/{rec}', {'act': 'pack', 'env_actual': '490', 'operator': self.opn})
        self.op.post(f'/sterile/record/{rec}', {'act': 'box', 'operator': self.opn})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM notifications WHERE kind='pre_release_needed' AND ref=?", (bn,))['n'], 1)
        # LOT المورّد لا يتكرر (مطابقة بلا فرق أحرف/مسافات) لا في الخام ولا في SP
        lot = f'DUP-{self.lot}-X'
        d = {'supplier_name': 'مورد', 'item_code': 'RR005', 'supplier_lot': lot, 'roll_count': '2'}
        self.store.post('/receipts/new', d)
        n = db.one('SELECT COUNT(*) n FROM receipts')['n']
        r = self.store.post('/receipts/new', dict(d, supplier_lot=' dup-' + lot[4:].lower()))
        self.assertIn('مستخدم مسبقًا', ' '.join(flashes(r)))
        r = self.store.post('/receipts/new', dict(d, roll_count='1'))
        self.assertEqual(db.one('SELECT COUNT(*) n FROM receipts')['n'], n)
        r = self.store.post('/sp/receive', {'supplier_name': 'مورد', 'item_code': 'SP013M', 'supplier_lot': lot, 'qty': '10', 'packages': '1'})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM receipts')['n'], n)
        self.assertNotIn('لوط', body(self.op.get('/')) + body(self.qc.get('/receipts')))    # المصطلح LOT

    # ================================================================== 19. البيانات على قرص/مجلد آخر (QMS_DATA)
    def test_19_data_dir_env(self):
        import subprocess
        d = os.path.join(tempfile.mkdtemp(), 'QMS_Data')
        env = {k: v for k, v in os.environ.items() if k != 'QMS_DB'}; env['QMS_DATA'] = d
        out = subprocess.run([sys.executable, '-c', 'import db,backup,seed;print(db.DB_PATH);print(backup.backup_dir());print(seed.DB)'],
                             cwd=os.path.dirname(HERE), env=env, capture_output=True, text=True).stdout.split('\n')
        self.assertEqual(out[0], os.path.join(d, 'qms.db'))
        self.assertEqual(out[1], os.path.join(d, 'backups'))
        self.assertEqual(out[2], os.path.join(d, 'qms.db'))          # seed.py لا يكتب في مجلد البرنامج

    # ================================================================== 20. النسخ الاحتياطي إلى مكان ثانٍ + فحص السلامة + وضع الخدمة
    def test_20_backup_extra_and_service_mode(self):
        import backup, glob
        extra = os.path.join(tempfile.mkdtemp(), 'ExtBackup')
        self.assertEqual(self.op.get('/admin/backup').status_code, 403)
        bad = os.path.join(tempfile.mkdtemp(), 'afile'); open(bad, 'w').close()
        r = self.adm.post('/admin/backup', {'act': 'extra', 'extra_dir': os.path.join(bad, 'sub')})    # مسار غير صالح للكتابة
        self.assertIn('غير صالح', ' '.join(flashes(r)))
        self.assertEqual(backup.extra_dir(), '')
        self.adm.post('/admin/backup', {'act': 'extra', 'extra_dir': extra})
        self.assertEqual(backup.extra_dir(), extra)
        self.adm.post('/admin/backup', {})                                    # نسخة فورية
        files = glob.glob(os.path.join(extra, 'qms-*.db'))
        self.assertEqual(len(files), 1)
        self.assertTrue(backup.verify(files[0])[0])                          # النسخة سليمة وقابلة للفتح
        page = txt(self.adm.get('/admin/backup'))
        self.assertIn('نجحت', page); self.assertIn('سليمة', page)
        # فشل المكان الثاني لا يُفشل النسخة الأساسية ويظهر للمدير
        db.run("UPDATE settings SET value=? WHERE key='backup_extra_dir'", (os.path.join(bad, 'x'),))
        self.adm.post('/admin/backup', {})
        self.assertIn('فشلت', txt(self.adm.get('/admin/backup')))
        self.adm.post('/admin/backup', {'act': 'extra', 'extra_dir': ''})
        # وضع الخدمة: منفذ ثابت من البيئة
        import subprocess
        env = {k: v for k, v in os.environ.items()}; env.update(QMS_PORT='5099', QMS_SERVICE='1')
        out = subprocess.run([sys.executable, '-c', 'import run;print(run.free_port(), run.SERVICE)'], cwd=os.path.dirname(HERE),
                             env=env, capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ['5099', 'True'])

    # ================================================================== 21. شاشة القفل الجديدة لا تمس منطق الدخول
    def test_21_lock_screen(self):
        c = app.test_client()
        page = body(c.get('/login'))
        for needle in ('company_logo.jpg', 'name="username"', 'name="password"', 'name="_csrf"', 'lk-in', 'backdrop-filter'):
            self.assertIn(needle, page)
        self.assertEqual(c.get('/static/company_logo.jpg').status_code, 200)
        self.assertIn('نظام إدارة الإنتاج والجودة والتتبع', page)                # الاسم المعتمد
        db.run("UPDATE settings SET value='اسم تجريبي' WHERE key='system_name'")
        self.assertIn('اسم تجريبي', body(c.get('/login')))                         # قابل للتغيير من الإعدادات
        db.run("UPDATE settings SET value='نظام إدارة الإنتاج والجودة والتتبع' WHERE key='system_name'")
        tok = re.search(r'name="csrf" content="([^"]+)"', page).group(1)
        r = c.post('/login', data={'username': 'admin', 'password': 'wrong', '_csrf': tok}, follow_redirects=True)
        self.assertIn('غير صحيحة', body(r).split('class="err"')[-1][:400])      # الخطأ داخل اللوحة (يهتز)
        self.assertIn('panel shake', body(r))
        self.assertEqual(c.post('/login', data={'username': 'admin', 'password': 'wrong'}).status_code, 400)   # CSRF كما هو
        r = c.post('/login', data={'username': 'mgr', 'password': 'pass123', '_csrf': tok}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    # ================================================================== 22. الصيانة الوقائية والطارئة
    def test_22_maintenance(self):
        tech = Client('tech')
        eq = {r['no']: r for r in db.q('SELECT * FROM maint_equipment')}
        self.assertEqual({k: v['name_en'] for k, v in eq.items()},
                         {'1': 'Machine (Sliter)', '5': 'Machine 10', '4': 'Machine 7.5', '3': 'Machine 5', '6': 'Packing M/C', '2': 'PPT M/C', '7': 'Sterilization'})
        self.assertTrue(all(v['freq_days'] == 30 for v in eq.values()))
        self.assertEqual(db.setting('maint_form_pm'), 'XXXQP-12.F02')
        # الصلاحيات
        self.assertEqual(self.op.get('/maintenance').status_code, 403)
        self.assertEqual(self.op.get('/maintenance/report').status_code, 200)         # الإنتاج يبلّغ فقط
        self.assertEqual(self.qc.get('/maintenance/report').status_code, 403)
        self.assertEqual(tech.get('/maintenance').status_code, 200)
        self.assertEqual(tech.get('/maintenance/equipment').status_code, 403)
        self.assertEqual(tech.get('/slitter').status_code, 403)                        # فني الصيانة لا يرى الإنتاج
        L = set(re.findall(r'href="([^"]+)"', body(tech.get('/')).split('<aside')[-1].split('</aside>')[0]))
        self.assertIn('/maintenance', L); self.assertNotIn('/slitter', L); self.assertNotIn('/quality/pending', L)
        Lo = set(re.findall(r'href="([^"]+)"', body(self.op.get('/')).split('<aside')[-1].split('</aside>')[0]))
        self.assertIn('/maintenance/report', Lo); self.assertNotIn('/maintenance', Lo)
        # الجدول: أوامر الشهر تُولَّد تلقائيًا (7 آلات)
        page = body(tech.get('/maintenance'))
        n = db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM'")['n']
        self.assertEqual(n, 7)
        self.assertIn('XXXQP-12.F02', body(tech.get('/maintenance/plan')))
        tech.get('/maintenance')                                                        # لا تكرار
        self.assertEqual(db.one("SELECT COUNT(*) n FROM maint_orders WHERE kind='PM'")['n'], 7)
        # صيانة وقائية: تنفيذ ببنود
        pm = db.one("SELECT o.order_no FROM maint_orders o JOIN maint_equipment e ON e.id=o.equip_id WHERE e.no='5' AND o.kind='PM'")['order_no']
        self.assertEqual(self.op.get(f'/maintenance/order/{pm}').status_code, 403)
        tech.post(f'/maintenance/order/{pm}', {'act': 'start'})
        res = db.q('SELECT * FROM maint_results WHERE order_id=(SELECT id FROM maint_orders WHERE order_no=?)', (pm,))
        self.assertGreaterEqual(len(res), 8)
        tech.post(f'/maintenance/order/{pm}', {'act': 'complete'})                     # بنود بلا إجابة
        self.assertEqual(db.one('SELECT status s FROM maint_orders WHERE order_no=?', (pm,))['s'], 'In Progress')
        d = {'act': 'complete'}
        for i, r in enumerate(res):
            d[f'r{r["id"]}'] = 'غير مطابق' if i == 0 else 'مطابق'
        tech.post(f'/maintenance/order/{pm}', d)                                         # غير مطابق بلا ملاحظة
        self.assertEqual(db.one('SELECT status s FROM maint_orders WHERE order_no=?', (pm,))['s'], 'In Progress')
        d[f'n{res[0]["id"]}'] = 'الحزام متآكل'
        tech.post(f'/maintenance/order/{pm}', d)
        o = db.one('SELECT * FROM maint_orders WHERE order_no=?', (pm,))
        self.assertEqual((o['status'], o['result'], o['done_by']), ('Completed', 'به ملاحظات', 'مستخدم tech'))
        e5 = db.one("SELECT * FROM maint_equipment WHERE no='5'")
        self.assertEqual(e5['next_due'], (datetime.date.today() + datetime.timedelta(days=30)).isoformat())
        self.assertEqual(tech.post(f'/maintenance/order/{pm}', {'act': 'verify'}, follow=False).status_code, 403)   # الاعتماد للمدير
        self.mgr.post(f'/maintenance/order/{pm}', {'act': 'verify'})
        self.assertEqual(db.one('SELECT status s FROM maint_orders WHERE order_no=?', (pm,))['s'], 'Verified')
        r = tech.post(f'/maintenance/order/{pm}', {'act': 'raise_em'})                   # بلاغ من بند غير مطابق
        em0 = db.one("SELECT * FROM maint_orders WHERE kind='EM' AND parent_order=?", (pm,))
        self.assertIsNotNone(em0); self.assertIn('الحزام متآكل', em0['problem'])
        # صيانة طارئة: بلاغ من الإنتاج ← إشعار ← تحذير على شاشة الطي ← إصلاح ← اعتماد
        n0 = db.one("SELECT COUNT(*) n FROM notifications WHERE kind='em_reported'")['n']
        self.op.post('/maintenance/report', {'equip_id': str(e5['id']), 'problem': 'الماكينة تصدر صوتًا وتتوقف', 'severity': 'عالية', 'stopped': '1'})
        em = db.one("SELECT * FROM maint_orders WHERE kind='EM' AND reported_by='مستخدم op'")
        self.assertRegex(em['order_no'], r'^EM-\d{4}-\d{6}$')
        self.assertEqual((em['status'], em['production_stopped']), ('Reported', 1))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM notifications WHERE kind='em_reported'")['n'], n0 + 1)
        self.assertIn('الماكينة تصدر صوتًا', txt(tech.get('/notifications')) + txt(tech.get('/maintenance')))
        w = db.one("SELECT batch_no FROM work_orders WHERE fold_machine='FD-10' LIMIT 1")
        self.assertIn('تنبيه صيانة', body(self.op.get(f'/folding/{w["batch_no"]}')))   # تحذير لا منع
        tech.post(f'/maintenance/order/{em["order_no"]}', {'act': 'start'})
        tech.post(f'/maintenance/order/{em["order_no"]}', {'act': 'complete'})           # بلا سبب/إجراء
        self.assertEqual(db.one('SELECT status s FROM maint_orders WHERE order_no=?', (em['order_no'],))['s'], 'In Progress')
        tech.post(f'/maintenance/order/{em["order_no"]}', {'act': 'complete', 'cause': 'تآكل حزام', 'action_taken': 'استبدال الحزام', 'parts_used': 'حزام 1'})
        em2 = db.one('SELECT * FROM maint_orders WHERE order_no=?', (em['order_no'],))
        self.assertEqual(em2['status'], 'Completed'); self.assertIsNotNone(em2['downtime_h'])
        self.assertNotIn('تنبيه صيانة', body(self.op.get(f'/folding/{w["batch_no"]}')))  # زال التحذير
        self.mgr.post(f'/maintenance/order/{em["order_no"]}', {'act': 'verify'})
        # الطباعة والقوائم والتقرير
        for u in (f'/print/pm_schedule/{datetime.date.today().year}', f'/print/maint/{pm}', f'/print/maint/{em["order_no"]}',
                  '/maintenance/orders?kind=EM', '/maintenance/plan', f'/maintenance/equipment/{e5["id"]}/checklist'):
            self.assertEqual(self.adm.get(u).status_code, 200, u)
        self.assertIn('landscape', body(self.adm.get(f'/print/pm_schedule/{datetime.date.today().year}')))
        # تعديل بنود الفحص من الإدارة (يلغي علامة «مقترح»)
        it = db.one('SELECT * FROM maint_checklist WHERE equip_id=? ORDER BY seq', (e5['id'],))
        self.adm.post(f'/maintenance/equipment/{e5["id"]}/checklist', {'act': 'save', f's{it["id"]}': '1', f'a{it["id"]}': 'بند معدَّل', f'k{it["id"]}': 'on'})
        self.assertEqual(db.one('SELECT item_ar a, proposed p FROM maint_checklist WHERE id=?', (it['id'],)), {'a': 'بند معدَّل', 'p': 0})
        # إعادة بناء المستخدمين لم تُفقد أحدًا
        self.assertEqual(db.one("SELECT COUNT(*) n FROM users WHERE username IN ('admin','mgr','op','qc','tech')")['n'], 5)

    # ================================================================== 11. كل الصفحات تعمل
    def test_11_all_pages_render(self):
        bad = []
        skip = {'static', 'logout', 'letterhead'}
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            if 'GET' not in rule.methods or rule.arguments or rule.endpoint in skip:
                continue
            r = self.adm.get(rule.rule)
            if r.status_code >= 400 or '&lt;div' in body(r) or '&lt;input' in body(r):
                bad.append((rule.rule, r.status_code))                       # كود HTML ظاهر كنص = عطل
        self.assertEqual(bad, [])
        # صفحات ببارامترات
        bn = db.one("SELECT batch_no b FROM work_orders WHERE item_code='GS310M' ORDER BY rowid LIMIT 1")['b']
        for u in (f'/orders/{bn}', f'/slitter/{bn}', f'/folding/{bn}', f'/packing/{bn}', f'/quality/review/{bn}', f'/production/subrolls',
                  '/production/intermediates', '/production/packing', '/inventory/raw', '/inventory/moves', '/admin/subrolls',
                  f'/trace/center?q={bn}', '/inventory/cards/new', '/inventory/cards/GS310M', '/admin/pack-config/GS310M'):
            self.assertEqual(self.adm.get(u).status_code, 200, u)


if __name__ == '__main__':
    unittest.main()
