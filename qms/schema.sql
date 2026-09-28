-- =====================================================================
--  نظام إدارة الإنتاج والتتبع — مصنع الشاش والأربطة الطبية
--  قاعدة البيانات: SQLite   |  المرجع: QMS-MST-003 › DB_SCHEMA
-- =====================================================================
PRAGMA foreign_keys = ON;

-- ---------- الجداول المرجعية ----------
CREATE TABLE IF NOT EXISTS items (
  item_code     TEXT PRIMARY KEY,
  prefix        TEXT NOT NULL,
  category_ar   TEXT,
  category_en   TEXT,
  family        TEXT,
  size          TEXT,
  ply           INTEGER,
  xray          TEXT,
  mesh          TEXT,
  sterile       TEXT,
  edge          TEXT,
  machine_code  TEXT,
  uom           TEXT,
  width_cm      REAL,
  length_m      REAL,
  xray_grade    TEXT,
  pack_code     TEXT,
  master_box    TEXT,
  description   TEXT,
  search_key    TEXT,
  status        TEXT DEFAULT 'نشط'
);
CREATE INDEX IF NOT EXISTS ix_items_key    ON items(search_key);
CREATE INDEX IF NOT EXISTS ix_items_prefix ON items(prefix);

CREATE TABLE IF NOT EXISTS machines (
  machine_code TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  stage        TEXT,
  letter       TEXT,          -- حرف الترقيم: F / S / T / SL / C / L / R
  size_locked  TEXT,          -- المقاس المخصص للماكينة إن وُجد
  active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS slit_matrix (
  key           TEXT PRIMARY KEY,   -- FD-05-8
  machine_code  TEXT NOT NULL,
  ply           INTEGER NOT NULL,
  std_width_cm  REAL NOT NULL,
  size          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_cut (
  machine_code TEXT PRIMARY KEY, cut_length_cm REAL, note TEXT
);

CREATE TABLE IF NOT EXISTS doc_seq (
  batch_no TEXT PRIMARY KEY, last_seq INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS months (
  code TEXT PRIMARY KEY, name_ar TEXT, num INTEGER
);

CREATE TABLE IF NOT EXISTS suppliers (
  supplier_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  country     TEXT,
  active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS employees (
  emp_id   TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  role     TEXT,
  active   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT, note TEXT
);

-- ---------- الوارد والفحص ----------
CREATE TABLE IF NOT EXISTS receipts (           -- WH-FRM-001/2/3
  grn_no        TEXT PRIMARY KEY,
  receipt_date  TEXT NOT NULL,
  kind          TEXT,
  supplier_id   INTEGER REFERENCES suppliers(supplier_id),
  country       TEXT,
  po_no         TEXT, invoice_no TEXT, bl_no TEXT,
  item_code     TEXT REFERENCES items(item_code),
  supplier_lot  TEXT NOT NULL,
  mfg_date      TEXT, expiry_date TEXT,
  qty           REAL, uom TEXT,
  coa           TEXT, coa_no TEXT,
  pack_cond     TEXT,
  qc_location   TEXT,
  stock_status  TEXT DEFAULT 'حجر',
  inspection_no TEXT,
  received_by   TEXT,
  notes         TEXT
);
CREATE INDEX IF NOT EXISTS ix_receipts_lot ON receipts(supplier_lot);

CREATE TABLE IF NOT EXISTS inspections (        -- QC-FRM-001
  inspection_no TEXT PRIMARY KEY,
  insp_date     TEXT NOT NULL,
  grn_no        TEXT REFERENCES receipts(grn_no),
  item_code     TEXT, supplier_lot TEXT,
  sample_size   TEXT,
  c1 TEXT, c2 TEXT, c3 TEXT, c4 TEXT, c5 TEXT,
  c6 TEXT, c7 TEXT, c8 TEXT, c9 TEXT,
  decision      TEXT,          -- قبول / قبول مشروط / رفض / معلّق
  justification TEXT,
  inspector     TEXT, qa_officer TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS rolls (              -- الرولات الفعلية
  roll_no       TEXT PRIMARY KEY,
  grn_no        TEXT REFERENCES receipts(grn_no),
  item_code     TEXT REFERENCES items(item_code),
  supplier_lot  TEXT,
  shipment_seq  INTEGER, roll_seq INTEGER,
  width_cm      REAL, length_m REAL, weight_kg REAL,
  area_cm2      REAL,
  xray_grade    TEXT, mesh TEXT,
  stock_status  TEXT DEFAULT 'حجر',
  inspection_no TEXT,
  issue_date    TEXT, batch_no TEXT,
  location      TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_rolls_batch ON rolls(batch_no);

-- ---------- أوامر التشغيل والتشغيلات ----------
CREATE TABLE IF NOT EXISTS work_orders (        -- PRD-FRM-007
  wo_no        TEXT PRIMARY KEY,
  issue_date   TEXT NOT NULL,
  issued_by    TEXT, prod_manager TEXT,
  batch_kind   TEXT,          -- تصنيع من الرولات / شاش مقطع / لاب سبونج / رول صغير
  item_code    TEXT REFERENCES items(item_code),
  size TEXT, ply INTEGER, xray TEXT, mesh TEXT, route TEXT,
  qty_required REAL, uom TEXT, due_date TEXT,
  month_code   TEXT, yy INTEGER, dd INTEGER, seq INTEGER,
  stage_code   TEXT,          -- SL / C / L / R
  batch_no     TEXT UNIQUE NOT NULL,
  machine_letter TEXT,
  machine_batch_no TEXT,
  batch_start_date TEXT,      -- مشتق للفرز
  fold_machine TEXT, std_width_cm REAL,
  status       TEXT DEFAULT 'صادر',
  notes        TEXT,
  order_type   TEXT DEFAULT 'إنتاج',
  sr_spec_key  TEXT, sr_needed INTEGER, pieces_per_sr REAL, jumbo_needed REAL
);
CREATE INDEX IF NOT EXISTS ix_wo_batch ON work_orders(batch_no);

CREATE TABLE IF NOT EXISTS bom (                -- احتياج المواد
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wo_no TEXT REFERENCES work_orders(wo_no),
  batch_no TEXT,
  mat_kind TEXT, item_code TEXT, description TEXT,
  qty_required REAL, uom TEXT, qty_issued REAL,
  saptco_ref TEXT, issue_date TEXT, storekeeper TEXT, notes TEXT,
  lot TEXT, created_by TEXT, voided INTEGER DEFAULT 0, void_reason TEXT
);

-- ---------- الأسليتر ----------
CREATE TABLE IF NOT EXISTS slitting (           -- PRD-FRM-001 › الرول
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no TEXT, sdate TEXT, shift TEXT,
  wo_no TEXT, batch_no TEXT,
  roll_no TEXT REFERENCES rolls(roll_no),
  item_code TEXT, supplier_lot TEXT, internal_no TEXT,
  length_m REAL, width_cm REAL, area_cm2 REAL,
  operator TEXT, purpose TEXT, notes TEXT, doc_seq TEXT
);
CREATE INDEX IF NOT EXISTS ix_slit_batch ON slitting(batch_no);

CREATE TABLE IF NOT EXISTS subrolls (           -- PRD-FRM-001 › السب رول
  tag_no TEXT PRIMARY KEY,
  roll_no TEXT, item_code TEXT, doc_no TEXT,
  sdate TEXT, shift TEXT, wo_no TEXT, batch_no TEXT,
  sr_code TEXT, xray_grade TEXT, xray TEXT, mesh TEXT,
  dest_machine TEXT, ply INTEGER, target_size TEXT,
  length_m REAL, width_cm REAL, std_width_cm REAL,
  width_check TEXT, area_cm2 REAL,
  operator TEXT, delivered_date TEXT, notes TEXT,
  stock_status TEXT DEFAULT 'متاح', consumed_by_batch TEXT,
  consumed_date TEXT, slit_batch TEXT
);
CREATE INDEX IF NOT EXISTS ix_sub_batch ON subrolls(batch_no);
CREATE INDEX IF NOT EXISTS ix_sub_roll  ON subrolls(roll_no);

-- ---------- الطي ----------
CREATE TABLE IF NOT EXISTS folding_in (         -- PRD-FRM-002 › المدخل
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no TEXT, fdate TEXT, shift TEXT,
  machine_code TEXT, size TEXT,
  operator TEXT, emp_id TEXT,
  wo_no TEXT, batch_no TEXT,
  tag_no TEXT, roll_no TEXT, sr_code TEXT, xray_grade TEXT,
  length_used_m REAL, width_cm REAL, area_used_cm2 REAL,
  fully_used TEXT, returned_m REAL, notes TEXT, sr_source_batch TEXT
);
CREATE INDEX IF NOT EXISTS ix_fin_batch ON folding_in(batch_no);

CREATE TABLE IF NOT EXISTS folding_out (        -- PRD-FRM-002 › المخرج
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no TEXT, fdate TEXT, shift TEXT,
  machine_code TEXT, batch_no TEXT, route TEXT,
  item_code TEXT, ply INTEGER, size TEXT, piece_area_cm2 REAL,
  carton_code TEXT, qty_good REAL, qty_per_carton REAL,
  cartons REAL, scrap REAL, scrap_weight REAL, piece_weight REAL,
  counter_before REAL, counter_after REAL, cycles REAL,
  machine_size_check TEXT, notes TEXT, doc_seq TEXT
);
CREATE INDEX IF NOT EXISTS ix_fout_batch ON folding_out(batch_no);

-- ---------- الكراتين والفرز ----------
CREATE TABLE IF NOT EXISTS cartons (            -- PRD-FRM-003 › الكراتين
  carton_code TEXT PRIMARY KEY,
  cdate TEXT, shift TEXT,
  source TEXT, source_ref TEXT,
  batch_no TEXT, item_code TEXT,
  size TEXT, ply INTEGER, xray TEXT, mesh TEXT,
  qty REAL, operator TEXT, location TEXT,
  status TEXT DEFAULT 'متاحة',
  consumed_date TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_cart_batch ON cartons(batch_no);

CREATE TABLE IF NOT EXISTS sorting (            -- PRD-FRM-003 › الفرز
  doc_no TEXT PRIMARY KEY,
  sdate TEXT, shift TEXT, batch_no TEXT,
  carton_code TEXT, item_code TEXT,
  qty_in REAL, per_group INTEGER, groups REAL, pieces_used REAL,
  scrap REAL, variance REAL,
  operator TEXT, emp_id TEXT, count_review TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_sort_batch ON sorting(batch_no);

-- ---------- التغليف ----------
CREATE TABLE IF NOT EXISTS packaging (          -- PRD-FRM-004
  doc_no TEXT PRIMARY KEY,
  pdate TEXT, shift TEXT,
  batch_no TEXT, machine_batch_no TEXT,
  item_code TEXT, size TEXT, ply INTEGER, per_envelope INTEGER,
  sort_doc_no TEXT, groups_in REAL,
  film_code TEXT, film_lot TEXT,
  box_code TEXT, box_lot TEXT,
  mb_code TEXT, mb_lot TEXT,
  operator TEXT, emp_id TEXT,
  env_good REAL, env_scrap REAL,
  film_issued REAL, film_used REAL, film_waste REAL, film_returned REAL,
  film_recon TEXT, seal_check TEXT, code_verify TEXT,
  filled_date TEXT, env_packed REAL, env_per_box INTEGER,
  boxes REAL, boxes_per_carton INTEGER, cartons REAL,
  boxes_issued REAL, boxes_scrap REAL,
  cartons_issued REAL, cartons_scrap REAL,
  env_recon TEXT, box_recon TEXT,
  doc_status TEXT DEFAULT 'مفتوح',
  notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_pack_batch ON packaging(batch_no);

-- ---------- التعقيم ----------
CREATE TABLE IF NOT EXISTS cycles (             -- ST-FRM-001 › الدورات
  cycle_no TEXT PRIMARY KEY,
  cdate TEXT, month_code TEXT, yy INTEGER, dd INTEGER, seq INTEGER,
  cycle_date TEXT, machine_code TEXT,
  operator TEXT, emp_id TEXT,
  start_time TEXT, end_time TEXT, duration_h REAL,
  machine_report_no TEXT, gas_lot TEXT, load_pattern TEXT,
  ci_external TEXT, ci_internal TEXT,
  bi_count INTEGER, bi_start TEXT, bi_end TEXT, bi_result TEXT,
  ctrl_pos TEXT, ctrl_neg TEXT,
  status TEXT DEFAULT 'قيد التشغيل', notes TEXT
);

CREATE TABLE IF NOT EXISTS cycle_loads (        -- جدول الربط المتعدد
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_no TEXT REFERENCES cycles(cycle_no),
  pack_doc_no TEXT, batch_no TEXT,
  item_code TEXT, size TEXT, ply INTEGER,
  boxes_in REAL, cartons_in REAL, position TEXT,
  boxes_out REAL, variance REAL,
  ci_load TEXT, pack_status_at_load TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_load_cycle ON cycle_loads(cycle_no);
CREATE INDEX IF NOT EXISTS ix_load_batch ON cycle_loads(batch_no);

CREATE TABLE IF NOT EXISTS aeration (           -- ST-FRM-005
  cycle_no TEXT PRIMARY KEY REFERENCES cycles(cycle_no),
  ster_date TEXT, out_machine_time TEXT,
  in_date TEXT, in_time TEXT, out_date TEXT, out_time TEXT,
  duration_h REAL, min_required_h REAL, delta_h REAL,
  duration_eval TEXT,
  temp_in REAL, temp_out REAL, temp_eval TEXT,
  forced TEXT, cartons REAL,
  supervisor TEXT, emp_id TEXT,
  status TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS post_ster_receipts (  -- ST-FRM-007
  doc_no TEXT PRIMARY KEY,
  rdate TEXT, cycle_no TEXT, batch_no TEXT, item_code TEXT,
  boxes REAL, cartons REAL,
  qc_location TEXT, stock_status TEXT DEFAULT 'حجر',
  aeration_status TEXT,
  handed_by TEXT, received_by TEXT,
  quarantine_card TEXT, release_no TEXT, release_date TEXT,
  final_status TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_psr_batch ON post_ster_receipts(batch_no);

-- ---------- الإفراج ----------
CREATE TABLE IF NOT EXISTS releases (           -- QC-FRM-005
  release_no TEXT PRIMARY KEY,
  issue_date TEXT, batch_no TEXT UNIQUE, item_code TEXT,
  size TEXT, ply INTEGER, xray TEXT, mesh TEXT,
  wo_no TEXT, cycle_no TEXT, ster_date TEXT,
  shelf_life_m INTEGER, expiry_date TEXT,
  qty_boxes REAL, cartons REAL,
  r1 TEXT, r2 TEXT, r3 TEXT, r4 TEXT, r5 TEXT,
  r6 TEXT, r7 TEXT, r8 TEXT, r9 TEXT,
  residue_report_no TEXT,
  decision TEXT, qa_officer TEXT, sign_date TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_rel_batch ON releases(batch_no);

-- ---------- الشحن وعدم المطابقة ----------
CREATE TABLE IF NOT EXISTS shipments (          -- WH-FRM-007
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no TEXT, sdate TEXT, batch_no TEXT, item_code TEXT,
  customer TEXT, qty REAL, uom TEXT,
  release_no TEXT, notes TEXT,
  unit TEXT, shipper TEXT, created_by TEXT, voided INTEGER DEFAULT 0, void_reason TEXT, void_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_ship_batch ON shipments(batch_no);

CREATE TABLE IF NOT EXISTS deviations (         -- QC-FRM-006 عدم المطابقة NCR
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dev_no TEXT, ddate TEXT, batch_no TEXT, stage TEXT,
  description TEXT, root_cause TEXT, action TEXT,
  status TEXT DEFAULT 'مفتوح', owner TEXT, close_date TEXT,
  source_rec TEXT, severity TEXT, disposition TEXT, qty_affected REAL,
  created_by TEXT, closed_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_dev_batch ON deviations(batch_no);

-- ---------- سجل التدقيق ----------
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  username TEXT, action TEXT, table_name TEXT, record_key TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_rec ON audit_log(table_name, record_key);
CREATE INDEX IF NOT EXISTS ix_audit_ts  ON audit_log(ts);

-- ---------- v11: المستخدمون والتواقيع وعدّادات السندات ----------
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT NOT NULL UNIQUE,
  full_name      TEXT NOT NULL,
  pw_hash        TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'viewer'
                 CHECK(role IN ('viewer','operator','store','qc','qa','manager','admin')),
  active         INTEGER NOT NULL DEFAULT 1,
  must_change_pw INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT DEFAULT (datetime('now','localtime')),
  last_login     TEXT
);

-- توقيع إلكتروني: إعادة إدخال كلمة المرور تُثبت هوية الموقّع على كل قرار جودة/إفراج
CREATE TABLE IF NOT EXISTS signatures (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT DEFAULT (datetime('now','localtime')),
  username    TEXT, full_name TEXT, role TEXT,
  meaning     TEXT,           -- معنى التوقيع: اعتماد فحص / إصدار إفراج / إبطال
  table_name  TEXT, record_key TEXT,
  signed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_rec ON signatures(table_name, record_key);

-- عدّاد مركزي لأرقام السندات — يمنع تصادم رقمين من محطتين في نفس اللحظة
CREATE TABLE IF NOT EXISTS counters (
  scope TEXT PRIMARY KEY,
  n     INTEGER NOT NULL DEFAULT 0
);

-- ---------- v12: قوالب وسجلات الجودة ----------
-- القالب: تعريف نموذج (بنود + حدود) — يُعدَّل من الواجهة دون تعديل الكود
CREATE TABLE IF NOT EXISTS qc_templates (
  code        TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  dept        TEXT NOT NULL CHECK(dept IN ('QC','QA')),
  kind        TEXT NOT NULL CHECK(kind IN ('release','inspection','daily','periodic')),
  area        TEXT NOT NULL,
  route       TEXT NOT NULL DEFAULT 'all' CHECK(route IN ('all','sterile','non_sterile')),
  freq_hours  REAL,
  needs_batch INTEGER NOT NULL DEFAULT 0,
  fields_json TEXT NOT NULL,
  requires    TEXT,
  active      INTEGER NOT NULL DEFAULT 1,
  version     INTEGER NOT NULL DEFAULT 1,
  note        TEXT,
  updated_by  TEXT,
  updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- السجل: تعبئة قالب في تاريخ ووردية ورقم تشغيلة وخط
CREATE TABLE IF NOT EXISTS qc_records (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  rec_no           TEXT NOT NULL UNIQUE,
  template_code    TEXT NOT NULL REFERENCES qc_templates(code),
  template_version INTEGER,
  spec_json        TEXT,                 -- نسخة القالب وقت التعبئة (لا تتغير بتعديله لاحقًا)
  rec_date         TEXT NOT NULL,
  rec_time         TEXT,
  shift            TEXT,
  batch_no         TEXT,
  line_code        TEXT,
  item_code        TEXT,
  values_json      TEXT NOT NULL,
  result           TEXT,                 -- مطابق / غير مطابق
  fail_count       INTEGER DEFAULT 0,
  decision         TEXT,                 -- للإفراج فقط: مفرج / غير مفرج
  inspector        TEXT,
  notes            TEXT,
  created_by       TEXT,
  created_at       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_qcr_tpl   ON qc_records(template_code, rec_date);
CREATE INDEX IF NOT EXISTS ix_qcr_batch ON qc_records(batch_no);
CREATE INDEX IF NOT EXISTS ix_qcr_line  ON qc_records(line_code, rec_date);
