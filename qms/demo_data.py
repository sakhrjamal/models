# -*- coding: utf-8 -*-
"""بيانات تجريبية — سلسلة إنتاج كاملة لتجربة النظام.

تُدرج بأسماء الأعمدة صراحةً حتى لا تنكسر عند إضافة أعمدة جديدة للجداول.
لحذفها: احذف data/qms.db وشغّل seed.py من جديد.
"""
import db


def _ins(con, table, cols, rows):
    ph = ','.join(['?'] * len(cols))
    con.executemany(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({ph})", rows)


def load():
    con = db.get()
    try:
        _ins(con, 'suppliers', ['supplier_id', 'name', 'country'],
             [(1, 'شركة النسيج المتحدة', 'الصين')])

        _ins(con, 'receipts',
             ['grn_no', 'receipt_date', 'kind', 'supplier_id', 'country', 'po_no', 'invoice_no',
              'bl_no', 'item_code', 'supplier_lot', 'mfg_date', 'expiry_date', 'qty', 'uom', 'coa',
              'coa_no', 'pack_cond', 'qc_location', 'stock_status', 'inspection_no', 'received_by', 'notes'],
             [('GRN-2608-041', '2026-08-25', 'مادة خام — رولات', 1, 'الصين', 'PO-2607-012', 'INV-88421',
               'BL-2607-9931', 'RR007', 'SUP-2508-77', '2026-07-10', '2031-07-10', 12, 'رول', 'متوفر',
               'COA-2508-77', 'سليمة', 'منطقة الحجر — خام', 'مفرج', 'QC-RM-260826-001', 'سعود ر.', None)])

        _ins(con, 'inspections',
             ['inspection_no', 'insp_date', 'grn_no', 'item_code', 'supplier_lot', 'sample_size',
              'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8', 'c9', 'decision', 'justification',
              'inspector', 'qa_officer', 'notes'],
             [('QC-RM-260826-001', '2026-08-26', 'GRN-2608-041', 'RR007', 'SUP-2508-77', '3 رولات',
               'مطابق', 'مطابق', 'مطابق', 'مطابق', 'مطابق', 'مطابق', 'مطابق', 'مطابق', 'مطابق',
               'قبول', None, 'ف. الجودة', 'م. الجودة', None)])

        _ins(con, 'rolls',
             ['roll_no', 'grn_no', 'item_code', 'supplier_lot', 'shipment_seq', 'roll_seq', 'width_cm',
              'length_m', 'weight_kg', 'area_cm2', 'xray_grade', 'mesh', 'stock_status', 'inspection_no',
              'issue_date', 'batch_no', 'location', 'notes'],
             [(f'RR007-26-014/0{i}', 'GRN-2608-041', 'RR007', 'SUP-2508-77', 14, i, 90, 1999, 168.0,
               17991000, 'X-Ray-3', '18x11', 'مفرج', 'QC-RM-260826-001', '2026-09-01',
               'SEP-2601-SL-001' if i == 3 else None, 'مستودع الخام', None) for i in (1, 2, 3)])

        wo_cols = ['wo_no', 'issue_date', 'issued_by', 'prod_manager', 'batch_kind', 'item_code', 'size',
                   'ply', 'xray', 'mesh', 'route', 'qty_required', 'uom', 'due_date', 'month_code', 'yy',
                   'dd', 'seq', 'stage_code', 'batch_no', 'machine_letter', 'machine_batch_no',
                   'batch_start_date', 'fold_machine', 'std_width_cm', 'status', 'notes', 'order_type']
        _ins(con, 'work_orders', wo_cols, [
            ('WO-2609-001', '2026-09-01', 'م. عبدالله', 'م. فهد', 'تصنيع من الرولات', 'GS013', '5x5 cm',
             8, 'WITHOUT X-RAY', '18x11', 'معقم', 480000, 'قطعة', '2026-09-20', 'SEP', 26, 1, 1, 'SL',
             'SEP-2601-SL-001', 'F', 'SEP-2601-F-001', '2026-09-01', 'FD-05', 13, 'قيد التنفيذ', None, 'إنتاج'),
            ('WO-2609-002', '2026-09-03', 'م. عبدالله', 'م. فهد', 'تصنيع من الرولات', 'GS099', '7.5x7.5 cm',
             12, 'WITHOUT X-RAY', '18x11', 'معقم', 200000, 'قطعة', '2026-09-25', 'SEP', 26, 3, 1, 'SL',
             'SEP-2603-SL-001', 'S', 'SEP-2603-S-001', '2026-09-03', 'FD-75', 26.5, 'قيد التنفيذ', None, 'إنتاج'),
        ])

        con.execute("""INSERT INTO slitting(doc_no,sdate,shift,wo_no,batch_no,roll_no,item_code,
             supplier_lot,internal_no,length_m,width_cm,area_cm2,operator) VALUES
             ('SEP-2601-SL-001/01','2026-09-01','أ','WO-2609-001','SEP-2601-SL-001','RR007-26-014/03',
              'RR007','SUP-2508-77','D-01-nhf',1999,90,17991000,'سالم ح.')""")

        _ins(con, 'subrolls',
             ['tag_no', 'roll_no', 'item_code', 'doc_no', 'sdate', 'shift', 'wo_no', 'batch_no', 'sr_code',
              'xray_grade', 'xray', 'mesh', 'dest_machine', 'ply', 'target_size', 'length_m', 'width_cm',
              'std_width_cm', 'width_check', 'area_cm2', 'operator', 'delivered_date', 'stock_status', 'slit_batch'],
             [(tag, 'RR007-26-014/03', 'RR007', 'SEP-2601-SL-001/01', '2026-09-01', 'أ', 'WO-2609-001',
               'SEP-2601-SL-001', 'SR015', 'X-Ray-3', 'WITHOUT X-RAY', '18x11', 'FD-05', 8, '5x5 cm',
               1999, wd, 13 if wd == 13 else 23, 'مطابق', 1999 * 100 * wd, 'سالم ح.', '2026-09-02',
               'مستهلك' if tag == 'S1-26-0187' else 'متاح', 'SEP-2601-SL-001')
              for tag, wd in [('S1-26-0187', 23), ('S1-26-0188', 23), ('S1-26-0189', 23), ('S1-26-0190', 13)]])

        con.execute("""INSERT INTO folding_in(doc_no,fdate,shift,machine_code,size,operator,emp_id,wo_no,
             batch_no,tag_no,roll_no,sr_code,xray_grade,length_used_m,width_cm,area_used_cm2,fully_used,returned_m)
             VALUES('SEP-2601-F-001/01','2026-09-02','أ','FD-05','5x5 cm','خالد م.','1102','WO-2609-001',
             'SEP-2601-SL-001','S1-26-0187','RR007-26-014/03','SR015','X-Ray-3',1999,23,4597700,'نعم',0)""")

        con.execute("""INSERT INTO folding_out(doc_no,fdate,shift,machine_code,batch_no,route,item_code,ply,
             size,piece_area_cm2,carton_code,qty_good,qty_per_carton,cartons,scrap,machine_size_check)
             VALUES('SEP-2601-F-001/02','2026-09-02','أ','FD-05','SEP-2601-SL-001','معقم','SP013',8,'5x5 cm',
             100,'CT-2609-0055',45000,2000,23,900,'مطابق')""")

        _ins(con, 'cartons',
             ['carton_code', 'cdate', 'shift', 'source', 'source_ref', 'batch_no', 'item_code', 'size',
              'ply', 'xray', 'mesh', 'qty', 'operator', 'location', 'status', 'consumed_date'],
             [('CT-2609-0055', '2026-09-02', 'أ', 'ماكينة طي', 'FD-05', 'SEP-2601-SL-001', 'SP013', '5x5 cm',
               8, 'WITHOUT X-RAY', '18x11', 45000, 'خالد م.', 'رف A-1', 'مستهلكة بالفرز', '2026-09-03')])

        _ins(con, 'sorting',
             ['doc_no', 'sdate', 'shift', 'batch_no', 'carton_code', 'item_code', 'qty_in', 'per_group',
              'groups', 'pieces_used', 'scrap', 'variance', 'operator', 'emp_id', 'count_review', 'notes'],
             [('SORT-260903-001', '2026-09-03', 'أ', 'SEP-2601-SL-001', 'CT-2609-0055', 'SP013', 45000, 5,
               8970, 44850, 150, 0, 'ناصر ع.', '1150', 'تمت', None)])

        con.execute("""INSERT INTO packaging(doc_no,pdate,shift,batch_no,machine_batch_no,item_code,size,ply,
             per_envelope,sort_doc_no,groups_in,film_code,film_lot,box_code,box_lot,mb_code,mb_lot,operator,
             emp_id,env_good,env_scrap,film_issued,film_used,film_waste,film_returned,film_recon,seal_check,
             code_verify,filled_date,env_packed,env_per_box,boxes,boxes_per_carton,cartons,env_recon,box_recon,doc_status)
             VALUES('PKG-260903-001','2026-09-03','أ','SEP-2601-SL-001','SEP-2601-F-001','GS013','5x5 cm',8,
             5,'SORT-260903-001',8970,'PK019','FLM-2608-04','BX001P','BXL-2608-11','MB001','MBL-2607-02',
             'نايف س.','1205',8900,70,1250,1215,25,10,'مطابق','مطابق','مطابق','2026-09-04',8900,20,445,
             20,23,'مطابق','مطابق','مكتمل')""")

        cyc_cols = ['cycle_no', 'cdate', 'month_code', 'yy', 'dd', 'seq', 'cycle_date', 'machine_code',
                    'operator', 'emp_id', 'start_time', 'end_time', 'duration_h', 'machine_report_no',
                    'gas_lot', 'load_pattern', 'ci_external', 'ci_internal', 'bi_count', 'bi_start',
                    'bi_end', 'bi_result', 'ctrl_pos', 'ctrl_neg', 'status', 'notes']
        _ins(con, 'cycles', cyc_cols, [
            ('SEP-2605-EO-001', '2026-09-05', 'SEP', 26, 5, 1, '2026-09-05', 'EO-01', 'ماجد ف.', '1301',
             '08:00', '20:00', 12, 'RPT-2609-021', 'GAS-2608-03', 'نمط A', 'مطابق', 'مطابق', 6,
             '2026-09-05', '2026-09-07', 'سالب — مطابق', 'موجب — مطابق', 'سالب — مطابق', 'اجتازت التعقيم', None),
            ('SEP-2609-EO-002', '2026-09-09', 'SEP', 26, 9, 2, '2026-09-09', 'EO-01', 'ماجد ف.', '1301',
             '08:00', '21:30', 13.5, 'RPT-2609-030', 'GAS-2608-03', 'نمط A', 'مطابق', 'مطابق', 6,
             '2026-09-09', None, None, 'موجب — مطابق', 'سالب — مطابق', 'قيد الحضانة', None),
        ])

        con.execute("""INSERT INTO cycle_loads(cycle_no,pack_doc_no,batch_no,item_code,size,ply,boxes_in,
             cartons_in,position,boxes_out,variance,ci_load,pack_status_at_load)
             VALUES('SEP-2605-EO-001','PKG-260903-001','SEP-2601-SL-001','GS013','5x5 cm',8,445,23,
             'رف علوي',445,0,'مطابق','مكتمل')""")

        _ins(con, 'aeration',
             ['cycle_no', 'ster_date', 'out_machine_time', 'in_date', 'in_time', 'out_date', 'out_time',
              'duration_h', 'min_required_h', 'delta_h', 'duration_eval', 'temp_in', 'temp_out',
              'temp_eval', 'forced', 'cartons', 'supervisor', 'emp_id', 'status', 'notes'],
             [('SEP-2605-EO-001', '2026-09-05', '20:00', '2026-09-05', '20:30', '2026-09-07', '21:00',
               48.5, 24, 24.5, 'مطابق', 24, 25, 'ضمن المدى', 'نعم', 23, 'ماجد ف.', '1301',
               'مكتملة — جاهزة للاستلام', None)])

        _ins(con, 'post_ster_receipts',
             ['doc_no', 'rdate', 'cycle_no', 'batch_no', 'item_code', 'boxes', 'cartons', 'qc_location',
              'stock_status', 'aeration_status', 'handed_by', 'received_by', 'quarantine_card',
              'final_status', 'notes'],
             [('PSR-260907-001', '2026-09-07', 'SEP-2605-EO-001', 'SEP-2601-SL-001', 'GS013', 445, 23,
               'منطقة الحجر A', 'حجر', 'مكتملة — جاهزة للاستلام', 'ماجد ف.', 'سعود ر.', 'QRT-260907-001',
               'بانتظار الإفراج', None)])

        con.execute("""INSERT INTO cycle_loads(cycle_no,pack_doc_no,batch_no,item_code,size,ply,boxes_in,
             cartons_in,position,boxes_out,variance,ci_load,pack_status_at_load)
             VALUES('SEP-2609-EO-002','PKG-260909-004','SEP-2603-SL-001','GS099','7.5x7.5 cm',12,300,12,
             'رف أوسط',295,-5,'مطابق','مكتمل')""")

        con.commit()
    finally:
        con.close()
    print('  حُمّلت السلسلة التجريبية.')
    print('  التشغيلة SEP-2601-SL-001 جاهزة لإصدار شهادة الإفراج (جرّبها من شاشة «شهادات الإفراج»).')
    print('  التشغيلة SEP-2603-SL-001 بانتظار المؤشر البيولوجي.')


if __name__ == '__main__':
    load()
