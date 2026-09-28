# -*- coding: utf-8 -*-
"""مسارات التصنيع الثلاثة كاملة: تحديد المسار تلقائيًا، SP (معقم/غير معقم)، الرباط، WIP، المخزن،
الإشعارات، خطوط المستخدمين، والأصل والفرع. يعمل بعد test_flow على القاعدة المؤقتة نفسها."""
import json, unittest, datetime

import test_flow as T
from test_flow import Client, body, db, TODAY
import mfg, inventory, sp as sp_mod


def find(sql, args=()):
    return db.one(sql, args)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mgr, cls.op, cls.qc, cls.qa = Client('mgr'), Client('op'), Client('qc'), Client('qa')
        cls.store = Client('store')
        db.run("INSERT INTO users(username,full_name,pw_hash,role,active,must_change_pw,lines) VALUES(?,?,?,?,1,0,?)",
               ('spop', 'مشغل SP', T.auth.hash_pw('pass123'), 'operator', 'SP_GAUZE'))
        cls.spop = Client('spop')

    # ---------------------------------------------------------------- helpers
    def q(self, who, code, base, **kw):
        d = dict(base); d.update(kw)
        return who.post(f'/quality/new/{code}', d)

    def close_ncrs(self, bn):
        for d in db.q("SELECT dev_no FROM deviations WHERE batch_no=? AND status<>'مغلق'", (bn,)):
            self.qa.post(f"/ncr/{d['dev_no']}", {'act': 'close', 'root_cause': 'س', 'action': 'ج',
                                                  'disposition': 'إعادة تشغيل / إعادة فحص', 'esign_pw': 'pass123'})

    def set_pack(self, code, rows, route='SP_GAUZE'):
        it = db.one('SELECT * FROM items WHERE item_code=?', (code,))
        f = {'route_code': route, 'product_category': it['product_category'] or 'Gauze', 'uom': it['uom'] or 'قطعة',
             'description': it['description'], 'active': 'on'}
        for i, (u, per, part) in enumerate(rows, 1):
            f[f'pc_unit_{i}'], f[f'pc_per_{i}'] = u, str(per)
            if part:
                f[f'pc_part_{i}'] = 'on'
        return self.mgr.post(f'/master/products/{code}', f)

    def wo(self, code, qty, uom='قطعة'):
        self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'batch_date': TODAY, 'item_code': code,
                                  'qty_required': str(qty), 'uom': uom})
        return db.one("SELECT * FROM work_orders WHERE item_code=? ORDER BY rowid DESC", (code,))

    def release_receipt(self, grn):
        self.qc.post(f'/receipt/{grn}', {'act': 'inspect', 'decision': 'قبول', 'esign_pw': 'pass123'})

    def prints(self, bn, code):
        return {'f_printed_batch': bn, 'f_printed_date': TODAY, 'f_printed_item': code, 'f_print_clear': 'مطابق'}

    # ---------------------------------------------------------------- 1. المسار تلقائيًا
    def test_1_route_resolution_and_master(self):
        it = find("""SELECT * FROM items WHERE prefix='GS' AND size='10x10 cm' AND ply=8 AND sterile='غير معقم'
                     AND status='نشط' ORDER BY item_code LIMIT 1""")
        self.assertEqual(it['route_code'], 'FULL_GAUZE')
        self.assertEqual(self.op.get('/master/products').status_code, 403)
        # ربط الصنف بمسار SP في بيانات المنتجات (مع تكوين التعبئة)
        r = self.set_pack(it['item_code'], [('pack', 10, 1), ('box', 5, 1), ('carton', 2, 1)])
        self.assertEqual(db.one('SELECT route_code r FROM items WHERE item_code=?', (it['item_code'],))['r'], 'SP_GAUZE')
        self.assertEqual(db.one('SELECT COUNT(*) n FROM pack_config WHERE item_code=?', (it['item_code'],))['n'], 3)
        pv = json.loads(body(self.mgr.get('/api/route_preview', query_string={'item': it['item_code']})))
        self.assertEqual((pv['route'], pv['variant']), ('SP_GAUZE', 'non_sterile'))
        self.assertEqual(pv['steps'][0], 'تخصيص خام SP')
        self.assertNotIn('الأسليتر', pv['steps'])
        self.assertTrue(any('SP' in x for x in pv['warn']))        # لا خام مفرج بعد
        # أمر الإنتاج: المسار من الصنف — بحرف SP وبلا ماكينة طي
        w = self.wo(it['item_code'], 600)
        self.assertEqual((w['route_code'], w['fold_machine'], w['route_override']), ('SP_GAUZE', None, 0))
        self.assertRegex(w['batch_no'], r'^[A-Z]{3}-\d{4}-SP-001$')
        type(self).sp_item, type(self).sp_wo = it, w
        # تغيير المسار يدويًا: لا للمشغّل، والمدير بسبب فقط، ويُسجَّل في التدقيق
        self.assertEqual(self.op.get('/wo/new').status_code, 403)
        n0 = db.one('SELECT COUNT(*) n FROM work_orders')['n']
        self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'item_code': it['item_code'],
                                  'qty_required': '10', 'route_override': 'BANDAGE'})
        self.assertEqual(db.one('SELECT COUNT(*) n FROM work_orders')['n'], n0)     # بلا سبب: مرفوض
        self.mgr.post('/wo/new', {'order_type': 'إنتاج', 'issue_date': TODAY, 'item_code': it['item_code'],
                                  'qty_required': '10', 'route_override': 'BANDAGE', 'route_reason': 'اختبار استثنائي'})
        ov = db.one("SELECT * FROM work_orders WHERE route_override=1")
        self.assertEqual((ov['route_code'], ov['route_note']), ('BANDAGE', 'اختبار استثنائي'))
        self.assertTrue(db.one("SELECT 1 FROM audit_log WHERE action='route_override' AND record_key=?", (ov['batch_no'],)))
        db.run("UPDATE work_orders SET status='مغلقة' WHERE route_override=1")
        # شاش FULL لا يزال بمساره القديم
        fw = db.one("SELECT route_code FROM work_orders WHERE batch_no LIKE '%-F-001'")
        self.assertEqual(fw['route_code'], 'FULL_GAUZE')

    # ---------------------------------------------------------------- 2. SP غير معقم
    def test_2_sp_nonsterile_flow(self):
        it, w = type(self).sp_item, type(self).sp_wo
        bn = w['batch_no']
        spi = find("SELECT item_code FROM items WHERE prefix='SP' AND size=? AND ply=? AND xray=? AND mesh=?",
                   (it['size'], it['ply'], it['xray'], it['mesh']))
        self.assertIsNotNone(spi)
        rc = {'supplier_name': 'مورد SP', 'item_code': spi['item_code'], 'supplier_lot': 'SPLOT-1', 'qty': '1000',
              'packages': '10', 'received_by': 'مخزن', 'wh_location': 'حجر A1', 'receipt_date': TODAY}
        self.assertIn('أكبر من صفر', body(self.op.post('/sp/receive', dict(rc, packages=''))))
        self.assertIn('إلزامية', body(self.op.post('/sp/receive', dict(rc, supplier_name=''))))
        self.assertEqual(self.store.post('/sp/receive', rc, follow=False).status_code, 302)
        grn = db.one("SELECT grn_no FROM receipts WHERE item_code=?", (spi['item_code'],))['grn_no']
        self.assertTrue(grn.startswith('SPR-'))
        r = db.one('SELECT * FROM receipts WHERE grn_no=?', (grn,))
        self.assertEqual((r['stock_status'], r['packages']), ('حجر', 10))
        self.assertEqual(inventory.balance(grn, 'RM'), 1000)
        # إشعار للجودة فقط
        self.assertIn('SPR-', body(self.qc.get('/notifications')))
        self.assertNotIn(grn, body(self.op.get('/notifications')))
        # لا استخدام قبل الإفراج
        al = {'grn_no': grn, 'qty': '600', 'alloc_date': TODAY, 'operator': 'م'}
        self.assertIn('غير مفرج', body(self.op.post(f'/sp/{bn}/alloc', al)))
        self.release_receipt(grn)
        self.assertEqual(db.one('SELECT stock_status s FROM receipts WHERE grn_no=?', (grn,))['s'], 'مفرج')
        self.assertIn(grn, body(self.op.get('/notifications')))          # إشعار الإفراج يصل للإنتاج
        # تخصيص: لا أكبر من المتاح ولا من الاحتياج، والصحيح يحرّك المخزون
        self.assertIn('تتجاوز المتبقي', body(self.op.post(f'/sp/{bn}/alloc', dict(al, qty='700'))))
        self.op.post(f'/sp/{bn}/alloc', al)
        self.assertEqual(inventory.balance(grn, 'RM'), 400)
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 600)
        self.assertEqual(db.one('SELECT parent p FROM batch_links WHERE child=? AND link_type="SP_LOT"', (bn,))['p'], grn)
        # التعبئة: لا أكثر من المصروف، ولا بلا تكوين، والحساب حسب التكوين
        pk = {'work_date': TODAY, 'shift': 'أ', 'operator': 'معبئ', 'good': '590', 'reject': '5', 'scrap': '5'}
        self.assertIn('أكبر من المصروف', body(self.op.post(f'/sp/{bn}/pack', dict(pk, good='700'))))
        self.assertIn('إلزامية', body(self.op.post(f'/sp/{bn}/pack', dict(pk, operator=''))))
        self.op.post(f'/sp/{bn}/pack', pk)
        pr = db.one("SELECT * FROM proc_batches WHERE stage_code='SPK'")
        x = json.loads(pr['extra_json'])
        self.assertEqual((x['packs'], x['boxes'], x['cartons'], pr['qty_out']), (59, 12, 6, 6))
        self.assertTrue(pr['proc_no'].startswith(bn + '/SPK'))
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 0)
        self.assertEqual(inventory.balance(pr['proc_no'], 'SPK_OUT'), 590)
        self.assertEqual((inventory.total(bn, 'REJECT'), inventory.total(bn, 'SCRAP')), (5, 5))
        self.assertIn('أكبر من المصروف', body(self.op.post(f'/sp/{bn}/pack', dict(pk, good='1', reject='0', scrap='0'))))
        # رصيد اللوط: مستلم/مصروف/سليم/مرفوض/هالك/متبقٍ
        led = sp_mod.lot_ledger(grn)
        self.assertEqual((led['received'], led['issued'], led['good'], led['reject'], led['scrap'], led['remaining']),
                         (1000, 600, 590, 5, 5, 400))
        self.assertIn('SPLOT-1', body(self.mgr.get('/sp/lots')))
        # WIP: يظهر مرحلة «شاش SP معبّأ» ثم يختفي بعد المخزن
        self.assertIn('شاش SP معبّأ', body(self.mgr.get('/wip?route=SP_GAUZE')))
        pg = mfg.progress(bn)
        self.assertEqual([s['code'] for s in pg['steps']], ['ALLOC', 'SPPACK', 'FINALQC', 'WH'])
        self.assertEqual(pg['current'], 'الفحص النهائي والإفراج')
        # الجودة: نماذج الطي لا تنطبق على SP، ونماذج التعبئة والإفراج تنطبق
        base = {'rec_date': TODAY, 'shift': 'أ', 'line_code': 'PK-01', 'batch_no': bn, 'inspector': 'مراقب'}
        self.assertIn('لا ينطبق على مسار', body(self.q(self.qc, 'QC-FLD-PRD', dict(base, line_code='FD-10'))))
        fin = dict(base, f_appearance='مطابق', f_count_pack='مطابق', f_impurities='مطابق', f_seal='مطابق',
                   f_other_print='مطابق', **self.prints(bn, it['item_code']))
        ctn = dict(base, f_carton_cond='مطابق', f_units_carton='100', f_stacking='مطابق', **self.prints(bn, it['item_code']))
        qa = dict(base, f_records='مطابق', f_raw_ok='مطابق', f_nc_closed='مطابق', decision='مفرج', esign_pw='pass123')
        self.assertIn('لا يجوز الإفراج قبل', body(self.q(self.qa, 'QA-PKN-REL', qa)))
        self.q(self.qc, 'QC-PKN-FIN', fin); self.q(self.qc, 'QC-PKN-CTN', ctn)
        self.q(self.qa, 'QA-PKN-REL', qa)                               # لا يشترط فحص الطي لمسار SP
        self.assertEqual(db.one('SELECT status s FROM work_orders WHERE batch_no=?', (bn,))['s'], 'مكتملة')
        self.assertIn(bn, body(self.mgr.get('/notifications')))
        self.assertEqual(mfg.released_qty(bn)[:2], (6.0, 'كرتون'))
        # المخزن: لا قبل الإفراج (سبق)، ولا فوق المفرج، والاستلام يفرّغ WIP
        wh = {'batch_no': bn, 'rdate': TODAY, 'qty': '4', 'location': 'مخزن ب', 'received_by': 'أمين'}
        self.assertIn('الكمية غير صحيحة', body(self.store.post('/warehouse', dict(wh, qty='7'))))
        self.store.post('/warehouse', wh)
        self.assertAlmostEqual(inventory.balance(pr['proc_no'], 'SPK_OUT'), 590 / 3, places=4)
        self.store.post('/warehouse', dict(wh, qty='2'))
        self.assertAlmostEqual(inventory.balance(pr['proc_no'], 'SPK_OUT'), 0, places=6)
        self.assertEqual(inventory.total(bn, 'FG'), 6)
        self.assertEqual(mfg.progress(bn)['pct'], 100)
        # الشحن من رصيد المخزن، والأصل والفرع
        self.op.post('/shipping', {'batch_no': bn, 'customer': 'عميل SP', 'sdate': TODAY, 'shipper': 'ش', 'qty': '3'})
        self.assertEqual(db.one('SELECT SUM(qty) n FROM shipments WHERE batch_no=?', (bn,))['n'], 3)
        g = body(self.mgr.get(f'/genealogy?b={bn}'))
        for needle in (bn, grn, 'SPLOT-1', 'مورد SP', pr['proc_no'], 'QA-PKN-REL', 'عميل SP'):
            self.assertIn(needle, g, needle)
        fw = body(self.mgr.get('/genealogy?ref=SPLOT-1'))
        self.assertIn(bn, fw); self.assertIn('عميل SP', fw)
        self.assertEqual(self.mgr.get(f'/trace?b={bn}', follow_redirects=False).status_code, 302)   # trace → genealogy
        type(self).sp_grn = grn

    # ---------------------------------------------------------------- 3. SP معقم — قيود المخزون
    def test_3_sp_sterile_ledger(self):
        it = find("""SELECT * FROM items WHERE prefix='GS' AND size='10x10 cm' AND ply=12 AND sterile='معقم'
                     AND status='نشط' ORDER BY item_code LIMIT 1""")
        self.set_pack(it['item_code'], [])
        w = self.wo(it['item_code'], 500)
        bn = w['batch_no']
        self.assertEqual(w['route_code'], 'SP_GAUZE')
        self.assertEqual([s['code'] for s in mfg.progress(bn)['steps']], ['ALLOC', 'SORT', 'PACK', 'STER', 'FINALQC', 'WH'])
        spi = find("SELECT item_code FROM items WHERE prefix='SP' AND size=? AND ply=? AND xray=? AND mesh=?",
                   (it['size'], it['ply'], it['xray'], it['mesh']))
        self.op.post('/sp/receive', {'supplier_name': 'مورد SP', 'item_code': spi['item_code'], 'supplier_lot': 'SPLOT-2',
                                     'qty': '800', 'packages': '8', 'received_by': 'م', 'wh_location': 'ح', 'receipt_date': TODAY})
        grn = db.one("SELECT grn_no FROM receipts WHERE supplier_lot='SPLOT-2'")['grn_no']
        self.release_receipt(grn)
        # مواصفات غير مطابقة (لوط SP بطبقات مختلفة) مرفوض
        other = find("SELECT item_code FROM items WHERE prefix='SP' AND size=? AND ply<>? LIMIT 1", (it['size'], it['ply']))
        self.op.post('/sp/receive', {'supplier_name': 'مورد SP', 'item_code': other['item_code'], 'supplier_lot': 'SPLOT-X',
                                     'qty': '100', 'packages': '1', 'received_by': 'م', 'wh_location': 'ح', 'receipt_date': TODAY})
        bad = db.one("SELECT grn_no FROM receipts WHERE supplier_lot='SPLOT-X'")['grn_no']
        self.release_receipt(bad)
        self.assertIn('لا يطابق', body(self.op.post(f'/sp/{bn}/alloc', {'grn_no': bad, 'qty': '10', 'alloc_date': TODAY, 'operator': 'م'})))
        self.op.post(f'/sp/{bn}/alloc', {'grn_no': grn, 'qty': '500', 'alloc_date': TODAY, 'operator': 'م'})
        car = db.one("SELECT * FROM cartons WHERE batch_no=?", (bn,))
        self.assertEqual((car['qty'], car['source']), (500, 'خام SP'))
        # الفرز ثم التغليف على الشاشات المشتركة تُنشئ قيود SP
        self.op.post('/sorting', {'batch_no': bn, 'sdate': TODAY, 'shift': 'أ', 'operator': 'ف', 'carton_code': car['carton_code'],
                                  'qty_in': '500', 'per_group': '10', 'scrap': '10'})
        self.assertEqual(inventory.balance(bn, 'ISSUED'), 0)                  # 500 = 490 مفروز + 10 هالك
        self.assertEqual(inventory.balance(bn, 'SORTED'), 490)
        self.assertEqual(inventory.total(bn, 'SCRAP'), 10)
        so = db.one('SELECT * FROM sorting WHERE batch_no=?', (bn,))
        self.op.post('/packaging', {'batch_no': bn, 'pdate': TODAY, 'shift': 'أ', 'operator': 'غ', 'sort_doc_no': so['doc_no'],
                                    'groups_in': str(so['groups']), 'per_envelope': '10', 'env_good': '48', 'env_scrap': '1',
                                    'seal_check': 'مطابق', 'code_verify': 'مطابق', 'env_per_box': '10', 'boxes_per_carton': '5'})
        self.assertEqual(inventory.balance(bn, 'PACKED'), 480)
        self.assertEqual(inventory.total(bn, 'REJECT'), 10)
        self.assertEqual(mfg.output(bn)['good'], 480)
        self.assertEqual(mfg.output(bn)['reject'], 10)
        # قيد الحماية: فرز يفوق المصروف يفشل ويُلغى كليًا
        self.assertEqual(self.mgr.get(f'/wip?route=SP_GAUZE').status_code, 200)
        # مسار SP لا يفتح شاشات الطي
        self.assertEqual(self.op.get(f'/fold?b={bn}', follow_redirects=False).status_code, 200)  # الطي يعمل بأوامر الإنتاج فقط — يظهر بلا سب رول

    # ---------------------------------------------------------------- 4. الرباط الضاغط
    def test_4_bandage_flow(self):
        w = self.wo('CB100', 58, 'رول')
        bn = w['batch_no']
        self.assertEqual((w['route_code'], w['uom']), ('BANDAGE', 'رول'))
        self.assertRegex(bn, r'^[A-Z]{3}-\d{4}-B-00\d$')
        self.assertEqual([s['code'] for s in mfg.progress(bn)['steps']],
                         ['ALLOC', 'BMACH', 'BWRAP', 'BBOX', 'BCARTON', 'FINALQC', 'WH'])
        # استلام جامبو: الطول إلزامي؛ يدخل الحجر Pending QC
        rc = {'receipt_date': TODAY, 'supplier_name': 'مورد جامبو', 'item_code': 'PBT01', 'supplier_lot': 'JB-9',
              'roll_count': '2', 'width_cm': '60', 'length_m': '250', 'received_by': 'م', 'cls': 'JUMBO'}
        self.op.post('/receipts/new', rc)
        grn = db.one("SELECT grn_no FROM receipts WHERE supplier_lot='JB-9'")['grn_no']
        rolls = db.q('SELECT * FROM rolls WHERE grn_no=? ORDER BY roll_no', (grn,))
        self.assertEqual((len(rolls), rolls[0]['stock_status']), (2, 'حجر'))
        self.assertEqual(inventory.balance(rolls[0]['roll_no'], 'RM'), 250)
        self.assertIn(grn, body(self.qc.get('/notifications')))
        # لا تخصيص لجامبو غير مفرج
        r0 = rolls[0]['roll_no']
        self.assertIn('غير مفرج', body(self.op.post(f'/bandage/{bn}/alloc', {'roll_no': r0, 'alloc_date': TODAY, 'operator': 'م'})))
        self.release_receipt(grn)
        self.op.post(f'/bandage/{bn}/alloc', {'roll_no': r0, 'alloc_date': TODAY, 'operator': 'م'})
        self.assertEqual(db.one('SELECT batch_no b FROM rolls WHERE roll_no=?', (r0,))['b'], bn)
        # ماكينة الأربطة
        m = {'work_date': TODAY, 'shift': 'أ', 'operator': 'ع', 'machine': 'BM-01', 'start_time': '08:00', 'end_time': '12:00',
             'roll_no': r0, 'input_len': '250', 'output_rolls': '60', 'roll_len': '4', 'reject_rolls': '2', 'scrap': '3',
             'scrap_unit': 'كجم'}
        self.assertIn('أكبر من المتاح', body(self.op.post(f'/bandage/{bn}/machine', dict(m, input_len='300'))))
        self.assertIn('أكبر من طول المدخل', body(self.op.post(f'/bandage/{bn}/machine', dict(m, output_rolls='70'))))
        self.assertIn('بعد وقت البدء', body(self.op.post(f'/bandage/{bn}/machine', dict(m, end_time='07:00'))))
        self.assertIn('إلزامية', body(self.op.post(f'/bandage/{bn}/machine', dict(m, machine=''))))
        self.op.post(f'/bandage/{bn}/machine', m)
        bm = db.one("SELECT * FROM proc_batches WHERE stage_code='BM'")
        self.assertEqual((bm['qty_out'], bm['qty_reject'], bm['qty_scrap']), (60, 2, 3))
        self.assertTrue(bm['proc_no'].startswith(bn + '/BM'))
        self.assertEqual(inventory.balance(r0, 'RM'), 0)
        self.assertEqual(inventory.balance(bm['proc_no'], 'BM_OUT'), 60)
        # التغليف: لا أكثر من الناتج، ولا مجموع لا يطابق
        wr = {'work_date': TODAY, 'shift': 'أ', 'operator': 'غ', 'machine': 'BW-01', 'start_time': '13:00', 'end_time': '15:00',
              'src': bm['proc_no'], 'input_qty': '60', 'good': '58', 'reject': '2'}
        self.assertIn('أكبر من المتاح', body(self.op.post(f'/bandage/{bn}/wrap', dict(wr, input_qty='70', good='68'))))
        self.assertIn('يجب أن يساوي', body(self.op.post(f'/bandage/{bn}/wrap', dict(wr, good='50'))))
        self.op.post(f'/bandage/{bn}/wrap', wr)
        bw = db.one("SELECT * FROM proc_batches WHERE stage_code='BW'")
        self.assertEqual((bw['parent_proc'], bw['qty_out']), (bm['proc_no'], 58))
        self.assertEqual(inventory.balance(bm['proc_no'], 'BM_OUT'), 0)
        self.assertIn('أكبر من المتاح', body(self.op.post(f'/bandage/{bn}/wrap', dict(wr, input_qty='1', good='1', reject='0'))))
        # البوكسات من تكوين التعبئة (10 رولات) والكسر الجزئي
        bx = {'work_date': TODAY, 'shift': 'أ', 'operator': 'ب', 'src': bw['proc_no'], 'input_qty': '58', 'good': '58', 'reject': '0'}
        self.assertIn('أكبر من المتاح', body(self.op.post(f'/bandage/{bn}/box', dict(bx, input_qty='60', good='60'))))
        self.op.post(f'/bandage/{bn}/box', bx)
        bxp = db.one("SELECT * FROM proc_batches WHERE stage_code='BX'")
        self.assertEqual((bxp['qty_out'], json.loads(bxp['extra_json'])['partial_rolls']), (6, 8))
        # الكراتين
        ct = {'work_date': TODAY, 'shift': 'أ', 'operator': 'ك', 'src': bxp['proc_no'], 'boxes_in': '7'}
        self.assertIn('أكبر من المتاح', body(self.op.post(f'/bandage/{bn}/carton', ct)))
        self.op.post(f'/bandage/{bn}/carton', dict(ct, boxes_in='6'))
        cp = db.one("SELECT * FROM proc_batches WHERE stage_code='CT'")
        self.assertEqual((cp['qty_out'], json.loads(cp['extra_json'])['rolls']), (1, 58))
        self.assertEqual(inventory.balance(cp['proc_no'], 'CT_OUT'), 1)
        self.assertEqual(mfg.progress(bn)['current'], 'الفحص النهائي والإفراج')
        self.assertIn('جاهزًا للفحص النهائي', body(self.qc.get('/notifications')))
        # الجودة: القوالب الخاصة بالرباط، مع سماحية العرض من بيانات المنتج
        base = {'rec_date': TODAY, 'shift': 'أ', 'line_code': 'BM-01', 'batch_no': bn, 'inspector': 'مراقب'}
        prd = dict(base, f_w1='10.2', f_w2='10', f_w3='9.7', f_tension='مطابق', f_edges='مطابق', f_impurities='مطابق', f_stains='مطابق')
        resp = self.q(self.qc, 'QC-BND-PRD', dict(prd, f_w1='11'))
        r = db.one("SELECT * FROM qc_records WHERE template_code='QC-BND-PRD'")
        import re
        self.assertIsNotNone(r, re.findall(r'class="msg [a-z]+">([^<]+)', body(resp)))
        self.assertEqual(json.loads(r['values_json'])['__exp']['w1'], '10 ± 0.4')
        self.assertEqual(r['result'], 'غير مطابق')
        self.q(self.qc, 'QC-BND-PRD', prd)
        wrp = dict(base, line_code='BW-01', f_wrap_intact='مطابق', f_appearance='مطابق', **self.prints(bn, 'CB100'))
        self.q(self.qc, 'QC-BND-WRP', wrp)
        fin = dict(base, line_code='BW-01', f_roll_box='10', f_box_carton='10', f_box_cond='مطابق', f_carton_batch=bn,
                   f_carton_item='CB100', **self.prints(bn, 'CB100'))
        self.q(self.qc, 'QC-BND-FIN', dict(fin, f_roll_box='9'))                      # عدد خطأ ← فشل
        self.assertEqual(db.one("SELECT result r FROM qc_records WHERE template_code='QC-BND-FIN'")['r'], 'غير مطابق')
        qa = dict(base, line_code='BW-01', f_records='مطابق', f_raw_ok='مطابق', f_nc_closed='مطابق', decision='مفرج', esign_pw='pass123')
        self.assertIn('عدم مطابقة مفتوحة', body(self.q(self.qa, 'QA-BND-REL', qa)))
        self.close_ncrs(bn)
        self.assertIn('لا يجوز الإفراج قبل', body(self.q(self.qa, 'QA-BND-REL', qa)))   # آخر فحص نهائي غير مطابق
        self.q(self.qc, 'QC-BND-FIN', fin)
        self.q(self.qa, 'QA-BND-REL', qa)
        self.assertEqual(db.one('SELECT status s FROM work_orders WHERE batch_no=?', (bn,))['s'], 'مكتملة')
        # المخزن يفرّغ WIP
        self.store.post('/warehouse', {'batch_no': bn, 'rdate': TODAY, 'qty': '1', 'location': 'م', 'received_by': 'أ'})
        self.assertEqual(inventory.balance(cp['proc_no'], 'CT_OUT'), 0)
        self.assertEqual(inventory.total(bn, 'FG'), 1)
        self.assertEqual(mfg.progress(bn)['pct'], 100)
        # الأصل والفرع: جامبو ← GRN ← المورد، وبالعكس من الجامبو إلى المنتجات
        g = body(self.mgr.get(f'/genealogy?b={bn}'))
        for needle in (r0, grn, 'JB-9', 'مورد جامبو', bm['proc_no'], bw['proc_no'], bxp['proc_no'], cp['proc_no']):
            self.assertIn(needle, g, needle)
        self.assertIn(bn, body(self.mgr.get(f'/genealogy?ref={r0}')))
        self.assertIn(bn, body(self.mgr.get('/genealogy?ref=JB-9')))
        out = mfg.output(bn)
        self.assertEqual((out['good'], out['reject'], out['scrap']), (58, 4, 3))

    # ---------------------------------------------------------------- 5. الصلاحيات وخطوط المستخدمين
    def test_5_lines_and_menus(self):
        bn = db.one("SELECT batch_no FROM work_orders WHERE route_code='BANDAGE' AND route_override=0")['batch_no']
        # مشغّل SP فقط: لا يصل لخط الرباط ولا لقائمته
        self.assertEqual(self.spop.get(f'/bandage/{bn}/machine').status_code, 403)
        self.assertEqual(self.spop.post(f'/bandage/{bn}/machine', {}, follow=False).status_code, 403)
        self.assertEqual(self.spop.get('/line/BANDAGE').status_code, 403)
        self.assertEqual(self.spop.get('/line/SP_GAUZE').status_code, 200)
        home = body(self.spop.get('/'))
        self.assertIn('/sp', home); self.assertNotIn('/bandage?go=', home)
        # مدير كل الخطوط، ومشغّل بلا قيود يرى الكل
        mh = body(self.mgr.get('/manager'))
        for name in ('تصنيع الشاش من الرول الخام', 'الشاش نصف المصنع SP', 'الرباط الضاغط'):
            self.assertIn(name, mh)
        self.assertIn('/bandage?go=machine', body(self.op.get('/')))
        # أمين المخزن: المخزن نعم، الإنتاج لا
        self.assertEqual(self.store.get('/warehouse').status_code, 200)
        self.assertEqual(self.store.post('/fold', {'act': 'in', 'batch_no': 'X'}, follow=False).status_code, 403)
        self.assertEqual(self.op.get('/warehouse').status_code, 200)     # العرض متاح، والاستلام مرفوض في test سابق
        # إشعار موجّه لخط: مشغّل SP لا يرى إشعارات الرباط
        n = body(self.spop.get('/notifications'))
        self.assertNotIn('جامبو', n)
        self.assertGreater(db.one('SELECT COUNT(*) n FROM notifications')['n'], 10)
        # القراءة: تعليم الكل يصفّر العدّاد
        u = db.one("SELECT * FROM users WHERE username='qc'")
        import notify
        self.assertGreater(notify.unread_count(u), 0)
        self.qc.post('/notifications', {})
        self.assertEqual(notify.unread_count(u), 0)

    # ---------------------------------------------------------------- 6. الصفحات والتقارير
    def test_6_pages(self):
        for url in ('/manager', '/line/FULL_GAUZE', '/line/SP_GAUZE', '/line/BANDAGE', '/wip', '/fg', '/warehouse',
                    '/sp', '/sp/lots', '/bandage', '/quality/tasks', '/quality/final?route=BANDAGE',
                    '/quality/final?route=SP_GAUZE', '/quality/final?route=FULL_GAUZE', '/master/routes',
                    '/master/products', '/master/products/CB100', '/master/products/new', '/genealogy', '/wo', '/notifications',
                    '/receipts?cls=SP', '/receipts?cls=JUMBO', '/materials', '/shipping'):
            self.assertEqual(self.mgr.get(url).status_code, 200, url)
        self.assertIn('الرباط الضاغط', body(self.mgr.get('/wo')))
        # مسارات FULL القديمة لم تتأثر بمقاييس التقدّم
        fb = db.one("SELECT batch_no FROM work_orders WHERE batch_no LIKE '%-F-001'")['batch_no']
        p = mfg.progress(fb)
        self.assertEqual(p['route'], 'FULL_GAUZE')
        self.assertEqual([s['code'] for s in p['steps']], ['RM', 'SLIT', 'FOLD', 'SORT', 'PACK', 'STER', 'FINALQC', 'WH'])


if __name__ == '__main__':
    unittest.main()
