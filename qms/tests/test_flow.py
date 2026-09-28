# -*- coding: utf-8 -*-
"""اختبار تدفق v12 كاملًا عبر الواجهة: مدير ← أسليتر ← طي ← فرز ← تغليف ← تتبع ← جودة ← تقارير.

التشغيل:  cd qms && python -m unittest discover -s tests -v
يعمل على قاعدة مؤقتة (QMS_DB) ولا يمس data/qms.db.
"""
import os, re, sys, tempfile, unittest, json, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
_tmp = tempfile.mkdtemp(prefix='qms-test-')
os.environ['QMS_DB'] = os.path.join(_tmp, 'qms.db')

import seed, migrate_v11, migrate_v12, db, auth        # noqa: E402
seed.main()
migrate_v11.run(); migrate_v12.run(); db.apply_migrations()
auth.ensure_admin()
for un, role in (('mgr', 'manager'), ('op', 'operator'), ('qc', 'qc'), ('qa', 'qa'), ('view', 'viewer')):
    db.run("INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw) VALUES(?,?,?,?,1,0)",
           (un, f'مستخدم {un}', auth.hash_pw('pass123'), role))
from app import app                                     # noqa: E402

TODAY = datetime.date.today().isoformat()


class Client:
    def __init__(self, user):
        self.c = app.test_client()
        r = self.c.get('/login')
        self.tok = re.search(r'name="csrf" content="([^"]+)"', r.get_data(True)).group(1)
        r = self.c.post('/login', data={'username': user, 'password': 'pass123', '_csrf': self.tok})
        assert r.status_code == 302, r.status_code

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data, follow=True):
        d = dict(data); d['_csrf'] = self.tok
        return self.c.post(url, data=d, follow_redirects=follow)


def body(r):
    return r.get_data(True)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mgr, cls.op, cls.qc, cls.qa = Client('mgr'), Client('op'), Client('qc'), Client('qa')
        cls.view = Client('view')
        it = db.one("""SELECT * FROM items WHERE prefix IN ('GS','GB') AND machine_code='FD-05' AND ply=8
                       AND sterile='معقم' AND status='نشط' ORDER BY item_code LIMIT 1""")
        cls.item = it
        cls.rr = db.one("SELECT item_code FROM items WHERE prefix='RR' AND status='نشط' LIMIT 1")['item_code']

    # ------------------------------------------------------------ 1. الصلاحيات
    def test_1_permissions(self):
        self.assertEqual(self.op.get('/wo/new').status_code, 403)          # المشغّل لا يصدر أوامر
        self.assertEqual(self.mgr.get('/wo/new').status_code, 200)
        r = self.qa.post('/fold', {'act': 'in', 'batch_no': 'X'}, follow=False)
        self.assertEqual(r.status_code, 403)                                # ضمان الجودة لا يُدخل إنتاجًا
        self.assertEqual(self.qc.post('/fold', {'act': 'in', 'batch_no': 'X'}, follow=False).status_code, 403)
        self.assertEqual(self.op.get('/quality/new/QC-FLD-PRD').status_code, 403)   # الإنتاج لا يسجل جودة
        self.assertEqual(self.mgr.post('/fold', {'act': 'in', 'batch_no': 'X'}, follow=False).status_code, 302)  # المدير كل الصلاحيات
        self.assertEqual(self.mgr.get('/quality/new/QA-FLD-REL').status_code, 200)
        self.assertEqual(self.mgr.get('/quality/templates/QC-FLD-PRD').status_code, 200)
        self.assertEqual(self.view.post('/receipts/new', {}, follow=False).status_code, 403)
        self.assertEqual(self.qc.get('/quality/new/QA-FLD-DLY').status_code, 403)   # QC لا يملأ نموذج QA
        self.assertEqual(self.qc.get('/quality/new/QC-FLD-DLY').status_code, 200)
        self.assertEqual(self.qc.get('/quality/templates/QC-FLD-DLY').status_code, 403)
        self.assertEqual(self.qa.get('/quality/templates/QC-FLD-DLY').status_code, 200)
        self.assertEqual(self.op.get('/manager').status_code, 403)
        self.assertEqual(self.mgr.get('/manager').status_code, 200)
        self.assertEqual(self.mgr.get('/admin/users').status_code, 200)
        self.assertEqual(self.qa.get('/admin/users').status_code, 403)

    # ------------------------------------------------------------ 2. التدفق الكامل
    def test_2_full_chain(self):
        it = self.item
        self.assertIsNotNone(it)
        # مدير: أمر تقطيع + أمر إنتاج
        r = self.mgr.post('/wo/new', {'order_type': 'تقطيع', 'issue_date': TODAY, 'batch_date': TODAY,
                                      'sr_needed': '10'})
        sl = db.one("SELECT * FROM work_orders WHERE order_type='تقطيع'")
        self.assertIsNotNone(sl, body(r)[:500])
        self.assertRegex(sl['batch_no'], r'^[A-Z]{3}-\d{4}-SL-001$')
        r = self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'batch_date': TODAY,
                                      'item_code': it['item_code'], 'qty_required': '5000', 'uom': 'قطعة'})
        wo = db.one("SELECT * FROM work_orders WHERE order_type='إنتاج'")
        self.assertIsNotNone(wo, body(r)[:500])
        pb = wo['batch_no']
        self.assertRegex(pb, r'^[A-Z]{3}-\d{4}-F-001$')
        self.assertEqual(wo['item_code'], it['item_code'])
        self.assertEqual(wo['fold_machine'], 'FD-05')
        self.assertEqual(wo['issued_by'], 'مستخدم mgr')
        api = json.loads(body(self.mgr.get('/api/next_batch', query_string={
            't': 'إنتاج', 'item': it['item_code'], 'date': TODAY})))
        self.assertTrue(api['ok'] and api['batch'] == pb[:-3] + '002', api)
        self.assertEqual(json.loads(body(self.mgr.get('/api/next_batch', query_string={'t': 'تقطيع', 'date': TODAY})))['seq'], 2)
        self.assertTrue(json.loads(body(self.mgr.get('/api/items/search?q=GS0')))['items'])
        # رقم أمر مكرر يدويًا يُرفض برسالة لا بـ 500
        r = self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'item_code': it['item_code'],
                                      'qty_required': '10', 'wo_no': wo['wo_no']})
        self.assertIn('مستخدم مسبقًا', body(r))
        # تسلسل تلقائي ثانٍ
        self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'item_code': it['item_code'],
                                  'qty_required': '100', 'uom': 'قطعة'})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM work_orders WHERE batch_no LIKE '%-F-002'")['n'], 1)

        # استلام خام وإفراجه
        r = self.op.post('/receipts/new', {'receipt_date': TODAY, 'supplier_name': 'مورد تجريبي', 'item_code': self.rr,
                                           'supplier_lot': 'LOT-77', 'roll_count': '1', 'width_cm': '90',
                                           'length_m': '2000', 'received_by': 'مخزن', 'country': 'الصين'})
        grn = db.one('SELECT grn_no FROM receipts')['grn_no']
        r = self.qa.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123'})
        roll = db.one('SELECT * FROM rolls')
        self.assertEqual(roll['stock_status'], 'مفرج')

        # أسليتر تحت أمر التقطيع — بلا اسم فني يُرفض
        sb = sl['batch_no']
        r = self.op.post(f'/slit/{sb}/roll', {'roll_no': roll['roll_no'], 'sdate': TODAY, 'shift': 'أ',
                                              'length_m': '2000', 'width_cm': '90'})
        self.assertIn('إلزامية', body(r))
        self.op.post(f'/slit/{sb}/roll', {'roll_no': roll['roll_no'], 'sdate': TODAY, 'shift': 'أ',
                                          'operator': 'فني 1', 'length_m': '2000', 'width_cm': '90'})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM slitting')['n'], 1)
        spec = f"{it['machine_code']}-{it['ply']}"
        self.op.post(f'/slit/{sb}/plan', {'act': 'sub_plan', 'batch_no': sb, 'roll_no': roll['roll_no'],
                                          'length_m': '2000', 'xray': it['xray'], 'mesh': it['mesh'],
                                          'operator': 'فني 1', 'spec_key': [spec], 'qty': ['6']})
        subs = db.q('SELECT * FROM subrolls')
        self.assertEqual(len(subs), 6)
        self.assertTrue(all(x['batch_no'] == sb and x['stock_status'] == 'متاح' for x in subs))

        # الطي: أمر التقطيع لا يُقبل، وأمر الإنتاج يسحب من مخزون SL
        self.assertIn('أمر تقطيع', body(self.op.post('/fold', {'act': 'in', 'batch_no': sb}, follow=True)))
        page = body(self.op.get(f'/fold?b={pb}'))
        self.assertIn(subs[0]['tag_no'], page)                     # يظهر السب رول المطابق
        self.assertIn('مستخدم op', page)                           # المشغّل معبّأ مسبقًا
        tags = [x['tag_no'] for x in subs[:4]]
        r = self.op.post('/fold', {'act': 'in', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ', 'tag_no': tags})
        self.assertIn('إلزامية', body(r))                          # المشغّل ناقص
        r = self.op.post('/fold', {'act': 'in', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ',
                                   'operator': 'مشغل طي', 'tag_no': tags})
        fin = db.q('SELECT * FROM folding_in')
        self.assertEqual(len(fin), 4, body(r)[:400])
        self.assertTrue(all(x['sr_source_batch'] == sb and x['batch_no'] == pb for x in fin))
        self.assertEqual(len({x['doc_no'] for x in fin}), 1)       # ورقة واحدة برقم واحد
        consumed = db.q("SELECT * FROM subrolls WHERE stock_status='مستهلك'")
        self.assertTrue(all(x['consumed_by_batch'] == pb for x in consumed))
        # نفس البطاقة مرتين
        r = self.op.post('/fold', {'act': 'in', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ',
                                   'operator': 'مشغل طي', 'tag_no': tags[:1]})
        self.assertIn('مستهلكة', body(r))
        # ناتج الطي: الصنف والماكينة من الأمر
        r = self.op.post('/fold', {'act': 'out', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ',
                                   'operator': 'مشغل طي', 'qty_good': '4000', 'qty_per_carton': '1000',
                                   'scrap': '50'})
        fo = db.one('SELECT * FROM folding_out')
        self.assertEqual((fo['item_code'], fo['machine_code'], fo['size'], fo['ply']),
                         (it['item_code'], 'FD-05', it['size'], it['ply']))
        self.assertTrue(fo['carton_code'].startswith('CT-'))
        self.assertEqual(fo['cartons'], 4)
        self.assertEqual(db.one('SELECT batch_no FROM cartons')['batch_no'], pb)
        # كود كرتونة قائمة من تشغيلة أخرى مرفوض
        other = db.one("SELECT batch_no FROM work_orders WHERE batch_no LIKE '%-F-002'")['batch_no']
        r = self.op.post('/fold', {'act': 'out', 'batch_no': other, 'fdate': TODAY, 'shift': 'أ',
                                   'operator': 'x', 'qty_good': '10', 'carton_code': fo['carton_code']})
        self.assertIn('تشغيلة أخرى', body(r))

        # الفرز
        carton = db.one("SELECT * FROM cartons WHERE batch_no=? ORDER BY carton_code LIMIT 1", (pb,))
        r = self.op.post('/sorting', {'batch_no': pb, 'sdate': TODAY, 'shift': 'أ', 'operator': 'فارز',
                                      'carton_code': carton['carton_code'], 'qty_in': str(carton['qty']),
                                      'per_group': '10', 'scrap': '0'})
        so = db.one('SELECT * FROM sorting')
        self.assertIsNotNone(so, body(r)[:400])
        self.assertEqual(db.one("SELECT status s FROM cartons WHERE carton_code=?", (carton['carton_code'],))['s'],
                         'مستهلكة بالفرز')

        # التغليف
        r = self.op.post('/packaging', {'batch_no': pb, 'pdate': TODAY, 'shift': 'أ', 'operator': 'مغلف',
                                        'sort_doc_no': so['doc_no'], 'groups_in': str(so['groups']),
                                        'per_envelope': '10', 'env_good': str(so['groups']),
                                        'seal_check': 'مطابق', 'code_verify': 'مطابق',
                                        'env_per_box': '50', 'boxes_per_carton': '10'})
        pk = db.one('SELECT * FROM packaging')
        self.assertIsNotNone(pk, body(r)[:400])
        self.assertEqual(pk['item_code'], it['item_code'])

        # التتبع من رقم الإنتاج: السلسلة كاملة حتى المورّد عبر أمر التقطيع
        page = body(self.mgr.get(f'/trace?b={pb}'))
        for needle in (pb, sb, roll['roll_no'], 'LOT-77', grn, subs[0]['tag_no'], so['doc_no'], pk['doc_no']):
            self.assertIn(needle, page, needle)
        # التتبع من رقم الأسليتر يُظهر أوامر الإنتاج المستفيدة
        page = body(self.mgr.get(f'/trace?b={sb}'))
        self.assertIn(pb, page)
        # التتبع الأمامي من لوط المورّد يصل لأمر الإنتاج
        self.assertIn(pb, body(self.mgr.get('/forward?lot=LOT-77')))
        # نسخة الطباعة
        self.assertIn(sb, body(self.mgr.get(f'/trace/print?b={pb}')))

        # تقدّم التشغيلة
        from forms import batch_progress
        p = batch_progress(pb)
        self.assertEqual(p['fold_good'], 4000)
        self.assertEqual(p['stage'], 'التغليف')
        self.assertEqual(p['pct_fold'], 80)

        # القوائم والتقارير
        self.assertIn(pb, body(self.mgr.get('/wo')))
        self.assertIn(pb, body(self.mgr.get('/manager')))
        rep = self.mgr.get(f'/reports/batches?dfrom={TODAY}&dto={TODAY}')
        self.assertIn(pb, body(rep))
        csv = self.mgr.get(f'/reports/batches?dfrom={TODAY}&dto={TODAY}&fmt=csv')
        self.assertTrue(body(csv).lstrip('﻿').startswith('رقم التشغيلة'))
        self.assertEqual(self.op.get('/reports/batches').status_code, 403)
        for name in ('daily', 'slit', 'quality'):
            self.assertEqual(self.mgr.get(f'/reports/{name}').status_code, 200)
        type(self).pb, type(self).sb = pb, sb

    # ------------------------------------------------------------ 3. عدم اختلاط الأصناف
    def test_3_subroll_mismatch_blocked(self):
        # سب رول بخط كاشف مختلف لا يُسحب لأمر إنتاج
        if not getattr(type(self), 'pb', None):
            self.skipTest('يتطلب test_2')
        pb = type(self).pb
        rest = db.q("SELECT * FROM subrolls WHERE stock_status='متاح'")
        self.assertTrue(rest)
        other = 'WITH X-RAY' if rest[0]['xray'] != 'WITH X-RAY' else 'WITHOUT X-RAY'
        db.run('UPDATE subrolls SET xray=? WHERE tag_no=?', (other, rest[0]['tag_no']))
        r = self.op.post('/fold', {'act': 'in', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ',
                                   'operator': 'م', 'tag_no': [rest[0]['tag_no']]})
        self.assertIn('الخط الكاشف', body(r))
        self.assertEqual(db.one("SELECT stock_status s FROM subrolls WHERE tag_no=?",
                                (rest[0]['tag_no'],))['s'], 'متاح')

    # ------------------------------------------------------------ 4. الجودة
    def _qpost(self, who, code, base, **kw):
        d = dict(base); d.update(kw)
        return who.post(f'/quality/new/{code}', d)

    def test_4_quality(self):
        if not getattr(type(self), 'pb', None):
            self.skipTest('يتطلب test_2')
        pb = type(self).pb
        n_tpl = db.one('SELECT COUNT(*) n FROM qc_templates')['n']
        self.assertEqual(n_tpl, 23)
        base = {'rec_date': TODAY, 'shift': 'أ', 'line_code': 'FD-05', 'batch_no': pb, 'inspector': 'مراقب'}
        chk = {k: 'مطابق' for k in ('edge_seq', 'impurities', 'hair', 'stains', 'holes', 'xray')}
        dims = {'f_len1': '5.2', 'f_len2': '5.0', 'f_len3': '5.4', 'f_wid1': '5.0', 'f_wid2': '4.7', 'f_wid3': '5.3',
                'f_ply_count': '8'}
        prd = dict(base, **{f'f_{k}': v for k, v in chk.items()}, **dims)
        # عرض 5.5 خارج 5 ± 0.4 ؛ الطول 5.4 على الحد مقبول
        bad = dict(prd, f_wid1='5.5')
        self._qpost(self.qc, 'QC-FLD-PRD', bad)
        rec = db.one("SELECT * FROM qc_records WHERE template_code='QC-FLD-PRD'")
        self.assertIsNotNone(rec)
        self.assertEqual((rec['result'], rec['fail_count']), ('غير مطابق', 1))
        vals = json.loads(rec['values_json'])
        self.assertEqual(vals['__exp']['wid1'], '5 ± 0.4')
        self.assertEqual(vals['__fails'][0]['id'], 'wid1')
        page = body(self.qc.get(f"/quality/record/{rec['id']}"))
        self.assertIn('5 ± 0.4', page)
        self.assertEqual(rec['item_code'], self.item['item_code'])
        # بند إلزامي ناقص / ماكينة خطأ / تشغيلة أسليتر
        f2 = dict(prd); f2.pop('f_hair')
        self.assertIn('ناقصة', body(self._qpost(self.qc, 'QC-FLD-PRD', f2)))
        self.assertIn('مخصصة لماكينة', body(self._qpost(self.qc, 'QC-FLD-PRD', prd, line_code='FD-10')))
        self.assertIn('لا يناسب هذا النموذج', body(self._qpost(self.qc, 'QC-FLD-PRD', prd, batch_no=type(self).sb)))
        # تسجيل مطابق (يصبح آخر سجل للتشغيلة)
        self._qpost(self.qc, 'QC-FLD-PRD', prd)
        last = db.one("SELECT * FROM qc_records WHERE template_code='QC-FLD-PRD' ORDER BY id DESC")
        self.assertEqual((last['result'], last['fail_count']), ('مطابق', 0))

        # إفراج بدء الطي: توقيع + لا مع بند غير مطابق
        rel = dict(base, f_clearance='مطابق', f_machine_size='مطابق', f_subroll_tags='مطابق',
                   f_first_piece='غير مطابق', f_cleanliness='مطابق', decision='مفرج', esign_pw='pass123')
        self.assertIn('لا يجوز الإفراج', body(self._qpost(self.qc, 'QC-FLD-REL', rel)))
        rel.update(f_first_piece='مطابق', esign_pw='wrong')
        self.assertIn('التوقيع الإلكتروني غير صحيح', body(self._qpost(self.qc, 'QC-FLD-REL', rel)))
        rel['esign_pw'] = 'pass123'
        self._qpost(self.qc, 'QC-FLD-REL', rel)
        self.assertEqual(db.one("SELECT decision d FROM qc_records WHERE template_code='QC-FLD-REL'")['d'], 'مفرج')
        # QA لا يفرج قبل سجل QC مطابق للقالب المطلوب... هنا QC-FLD-REL موجود فيمر
        qa_rel = dict(base, f_qc_release='مطابق', f_subroll_ok='مطابق', f_docs='مطابق', f_personnel='مطابق',
                      decision='مفرج', esign_pw='pass123')
        self._qpost(self.qa, 'QA-FLD-REL', qa_rel)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM qc_records WHERE template_code='QA-FLD-REL'")['n'], 1)

        # ---- المعقم: العدد والطباعة تُقارَن آليًا بالتشغيلة
        d_fmt = datetime.date.today().strftime('%d/%m/%Y')
        pk = db.one('SELECT * FROM packaging')
        prints = lambda p: {f'f_printed_batch': f'LOT {pb}', 'f_printed_date': d_fmt,       # noqa: E731
                            'f_printed_item': self.item['item_code'], 'f_print_clear': 'مطابق'}
        env = dict(base, line_code='PK-01', f_count_env=str(pk['per_envelope']), f_appearance='مطابق',
                   f_impurities='مطابق', f_seal='مطابق', f_sterile_symbol='مطابق', **prints(0))
        self._qpost(self.qc, 'QC-PKS-ENV', env, f_count_env='9', f_printed_batch='SEP-2628-F-002',
                    f_printed_date='29/09/2026')
        r = db.one("SELECT * FROM qc_records WHERE template_code='QC-PKS-ENV'")
        ids = {x['id'] for x in json.loads(r['values_json'])['__fails']}
        self.assertEqual(ids, {'count_env', 'printed_batch', 'printed_date'})
        self._qpost(self.qc, 'QC-PKS-ENV', env)
        box = dict(base, line_code='PK-01', f_env_box=str(pk['env_per_box']), f_box_carton=str(pk['boxes_per_carton']),
                   f_box_cond='مطابق', f_ci_label='مطابق', f_carton_batch=pb,
                   f_carton_item=self.item['item_code'], **prints(0))
        self._qpost(self.qc, 'QC-PKS-BOX', box)
        self.assertEqual(db.one("SELECT result r FROM qc_records WHERE template_code='QC-PKS-BOX'")['r'], 'مطابق')
        # قالب غير معقم على تشغيلة معقمة مرفوض
        self.assertIn('خاص بالمنتج غير معقم', body(self._qpost(self.qc, 'QC-PKN-FIN', dict(base, line_code='PK-01'))))
        # إفراج للتعقيم: ضمان الجودة فقط، بتوقيع، وبعد سجلات مطابقة
        qs = dict(base, line_code='PK-01', f_records='مطابق', f_materials='مطابق', f_nc_closed='مطابق',
                  decision='مفرج', esign_pw='pass123')
        # آخر سجل للمغلفات مطابق ← يمر؛ نُفشل شرطًا بإضافة سجل غير مطابق أحدث
        self._qpost(self.qc, 'QC-PKS-ENV', env, f_count_env='9')
        self.assertIn('لا يجوز الإفراج قبل', body(self._qpost(self.qa, 'QA-PKS-REL', qs)))
        self._qpost(self.qc, 'QC-PKS-ENV', env)
        self._qpost(self.qa, 'QA-PKS-REL', qs)
        import quality
        self.assertTrue(quality.pack_release_ok(pb))

        # ---- غير المعقم: منتج نهائي ← كراتين ← إفراج (يُغلق التشغيلة)
        it2 = db.one("""SELECT * FROM items WHERE prefix IN ('GS','GB') AND machine_code='FD-05' AND ply=8
                        AND sterile='غير معقم' AND status='نشط' ORDER BY item_code LIMIT 1""")
        self.assertIsNotNone(it2)
        self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'item_code': it2['item_code'],
                                  'qty_required': '1000', 'uom': 'قطعة', 'batch_date': TODAY})
        nb = db.one("SELECT batch_no FROM work_orders WHERE item_code=? AND route='غير معقم'", (it2['item_code'],))['batch_no']
        b2 = dict(base, batch_no=nb, line_code='PK-01')
        pr2 = {'f_printed_batch': nb, 'f_printed_date': TODAY, 'f_printed_item': it2['item_code'], 'f_print_clear': 'مطابق'}
        fin = dict(b2, f_appearance='مطابق', f_count_pack='مطابق', f_impurities='مطابق', f_seal='مطابق',
                   f_other_print='مطابق', **pr2)
        ctn = dict(b2, f_carton_cond='مطابق', f_units_carton='100', f_stacking='مطابق', **pr2)
        qa = dict(b2, f_records='مطابق', f_raw_ok='مطابق', f_nc_closed='مطابق', decision='مفرج', esign_pw='pass123')
        self.assertIn('خاص بالمنتج معقم', body(self._qpost(self.qc, 'QC-PKS-ENV', b2)))
        self.assertIn('لا يجوز الإفراج قبل', body(self._qpost(self.qa, 'QA-PKN-REL', qa)))
        self._qpost(self.qc, 'QC-PKN-FIN', dict(fin, f_printed_date='27/09/2026'))         # تاريخ خطأ
        self.assertEqual(db.one("SELECT result r FROM qc_records WHERE template_code='QC-PKN-FIN'")['r'], 'غير مطابق')
        self._qpost(self.qc, 'QC-PKN-FIN', fin)
        self._qpost(self.qc, 'QC-PKN-CTN', ctn)
        fp = dict(prd, batch_no=nb)
        self._qpost(self.qc, 'QC-FLD-PRD', fp)
        self._qpost(self.qa, 'QA-PKN-REL', qa)
        self.assertEqual(db.one('SELECT status s FROM work_orders WHERE batch_no=?', (nb,))['s'], 'مكتملة')
        tp = body(self.mgr.get(f'/trace?b={nb}'))
        self.assertIn('مفرج عنها', tp)
        self.assertIn('QA-PKN-REL', tp)
        from forms import batch_progress
        self.assertEqual(batch_progress(nb)['decision'], 'مفرج عنها')

        # ---- تعديل القالب يرفع الإصدار ولا يغيّر نسخة السجل القديم
        t = db.one("SELECT * FROM qc_templates WHERE code='QC-FLD-PRD'")
        fields = json.loads(t['fields_json'])
        for x in fields:
            if x['id'] == 'piece_weight':
                x['min'], x['max'] = 0.5, 1.0
        self.qa.post('/quality/templates/QC-FLD-PRD', {'title': t['title'], 'freq_hours': '4', 'active': 'on',
                                                       'route': 'all', 'fields_json': json.dumps(fields)})
        t2 = db.one("SELECT * FROM qc_templates WHERE code='QC-FLD-PRD'")
        self.assertEqual(t2['version'], t['version'] + 1)
        self.assertEqual(db.one("SELECT spec_json s FROM qc_records WHERE id=?", (rec['id'],))['s'], t['fields_json'])
        # صفحات
        for url in ('/quality', '/quality/records', '/quality/templates', f'/trace?b={pb}', '/manager'):
            self.assertEqual(self.mgr.get(url).status_code, 200, url)
        self.assertIn(rec['rec_no'], body(self.mgr.get(f'/trace?b={pb}')))
        self.assertEqual(self.qa.get('/quality/templates/QC-PKS-ENV').status_code, 200)
        self.qa.post('/quality/templates/new', {'code': 'qa-x1', 'title': 'تجربة', 'dept': 'QA', 'kind': 'daily',
                                                'area': 'folding', 'freq_hours': '12', 'route': 'all'})
        self.assertIsNotNone(db.one("SELECT 1 FROM qc_templates WHERE code='QA-X1'"))

    # ------------------------------------------------------------ 5. ربط الإنتاج بالإفراج
    def test_5_gate_modes(self):
        if not getattr(type(self), 'pb', None):
            self.skipTest('يتطلب test_2')
        other = db.one("SELECT batch_no FROM work_orders WHERE batch_no LIKE '%-F-002'")['batch_no']
        db.run("UPDATE settings SET value='block' WHERE key='qc_gate_mode'")
        try:
            r = self.op.post('/fold', {'act': 'out', 'batch_no': other, 'fdate': TODAY, 'shift': 'أ',
                                       'operator': 'x', 'qty_good': '5'})
            self.assertIn('لا يوجد إفراج جودة', body(r))
            self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE batch_no=?', (other,))['n'], 0)
            # التشغيلة الأولى لها إفراج مسجّل ← تمر
            pb = type(self).pb
            r = self.op.post('/fold', {'act': 'out', 'batch_no': pb, 'fdate': TODAY, 'shift': 'أ',
                                       'operator': 'x', 'qty_good': '5'})
            self.assertEqual(db.one('SELECT COUNT(*) n FROM folding_out WHERE batch_no=?', (pb,))['n'], 2)
        finally:
            db.run("UPDATE settings SET value='warn' WHERE key='qc_gate_mode'")

    # ------------------------------------------------------------ 6. أرقام السندات
    def test_6_doc_numbers(self):
        with db.tx() as con:
            a = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
            b = db.use_number(con, 'T-260101-007', 'T-260101', 'T-260101-{n3}')   # يدوي بنفس الصيغة
            c = db.use_number(con, None, 'T-260101', 'T-260101-{n3}')
            d = db.use_number(con, 'FREE-TEXT', 'T-260101', 'T-260101-{n3}')
        self.assertEqual((a, b, c, d), ('T-260101-001', 'T-260101-007', 'T-260101-008', 'FREE-TEXT'))


if __name__ == '__main__':
    unittest.main()
