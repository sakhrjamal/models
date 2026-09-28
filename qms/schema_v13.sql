-- =====================================================================
--  v13 — منصة التصنيع: مسارات، مخزون/WIP، إشعارات، تتبع أبوي/فرعي
--  يُنفَّذ من migrate_v13.py (كل العبارات IF NOT EXISTS — آمن للتكرار)
-- =====================================================================

-- مسارات التصنيع (بيانات لا كود) ------------------------------------
CREATE TABLE IF NOT EXISTS routes (
  code            TEXT PRIMARY KEY,        -- FULL_GAUZE / SP_GAUZE / BANDAGE
  name_ar         TEXT NOT NULL,
  name_en         TEXT,
  category        TEXT,                    -- Gauze / Bandage
  input_kind      TEXT,                    -- ROLL / SP / JUMBO  (فئة الخام — انظر mat_classes)
  batch_letter    TEXT,                    -- حرف رقم التشغيلة؛ NULL = يُشتق من ماكينة الطي
  base_uom        TEXT DEFAULT 'قطعة',     -- وحدة الإنتاج الأساسية للمسار
  fg_unit_sterile TEXT,                    -- وحدة الاستلام في مخزن المنتج التام
  fg_unit_non_sterile TEXT,
  sort_no         INTEGER DEFAULT 0,
  active          INTEGER NOT NULL DEFAULT 1
);

-- خطوات المسار حسب نوع المنتج (معقم / غير معقم / كلاهما) --------------
CREATE TABLE IF NOT EXISTS route_steps (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  route_code TEXT NOT NULL REFERENCES routes(code),
  variant    TEXT NOT NULL DEFAULT 'all' CHECK(variant IN ('all','sterile','non_sterile')),
  seq        INTEGER NOT NULL,
  step_code  TEXT NOT NULL,                -- مفتاح معالج التقدّم في mfg.py
  name_ar    TEXT NOT NULL,
  endpoint   TEXT,                         -- الشاشة المرتبطة بالخطوة
  UNIQUE(route_code, variant, seq)
);

-- فئات الخام: أي بادئة كود = أي فئة ------------------------------------
CREATE TABLE IF NOT EXISTS mat_classes (
  prefix   TEXT PRIMARY KEY,
  class    TEXT NOT NULL,                  -- ROLL / SP / JUMBO
  label_ar TEXT NOT NULL
);

-- بنود فحص الخام لكل فئة (تسع خانات c1..c9) -----------------------------
CREATE TABLE IF NOT EXISTS rm_checks (
  class    TEXT NOT NULL,
  seq      INTEGER NOT NULL,
  label_ar TEXT NOT NULL,
  hint     TEXT,
  PRIMARY KEY(class, seq)
);

-- تكوين التعبئة لكل صنف: قطعة ← باك ← بوكس ← كرتون (لا أرقام في الكود) ---
CREATE TABLE IF NOT EXISTS pack_config (
  item_code     TEXT NOT NULL REFERENCES items(item_code),
  level         INTEGER NOT NULL,          -- 1 = أول تجميع فوق وحدة الصنف الأساسية
  unit          TEXT NOT NULL,             -- pack / box / carton
  unit_ar       TEXT NOT NULL,
  per_parent    REAL NOT NULL,             -- عدد وحدات المستوى الأدنى في هذه الوحدة
  allow_partial INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(item_code, level)
);

-- دفتر حركات المخزون / WIP: كل انتقال قيدان (خروج من مرحلة + دخول لأخرى) ---
CREATE TABLE IF NOT EXISTS stock_tx (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT DEFAULT (datetime('now','localtime')),
  owner     TEXT NOT NULL,                 -- مالك الرصيد: لوط خام / رقم رول / سند مرحلة / رقم تشغيلة
  stage     TEXT NOT NULL,                 -- RM / ISSUED / BM_OUT / BW_OUT / BX_OUT / CT_OUT / SPK_OUT / FG …
  qty       REAL NOT NULL,                 -- موجب دخول، سالب خروج
  unit      TEXT NOT NULL,
  batch_no  TEXT,                          -- أمر الإنتاج الذي ينتمي إليه القيد
  route_code TEXT,
  location  TEXT,
  ref_type  TEXT,                          -- receipt / alloc / proc / warehouse / reject / scrap
  ref_doc   TEXT,
  move_id   TEXT,                          -- يربط قيدَي الانتقال الواحد
  note      TEXT,
  created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_stx_bucket ON stock_tx(owner, stage);
CREATE INDEX IF NOT EXISTS ix_stx_batch  ON stock_tx(batch_no);

-- سندات المراحل (دفعات فرعية) — تحمل مرجع أبيها -----------------------
CREATE TABLE IF NOT EXISTS proc_batches (
  proc_no    TEXT PRIMARY KEY,             -- SEP-2628-B-001/BM01
  batch_no   TEXT NOT NULL,                -- أمر الإنتاج (الأب)
  route_code TEXT,
  stage_code TEXT NOT NULL,                -- BM / BW / BX / CT / SPK
  parent_proc TEXT,                        -- سند المرحلة السابقة
  source_ref TEXT,                         -- لوط الخام / رقم الجامبو
  item_code  TEXT,
  machine    TEXT,
  operator   TEXT,
  shift      TEXT,
  work_date  TEXT,
  start_time TEXT,
  end_time   TEXT,
  qty_in     REAL,
  qty_out    REAL,                         -- الناتج السليم بوحدة المرحلة
  qty_reject REAL DEFAULT 0,
  qty_scrap  REAL DEFAULT 0,
  scrap_unit TEXT,
  unit       TEXT,
  status     TEXT DEFAULT 'مكتمل',
  extra_json TEXT,
  notes      TEXT,
  created_by TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_proc_batch ON proc_batches(batch_no);
CREATE INDEX IF NOT EXISTS ix_proc_parent ON proc_batches(parent_proc);

-- تخصيص الخام لأمر إنتاج (لوط SP / جامبو رول) ---------------------------
CREATE TABLE IF NOT EXISTS allocations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no     TEXT UNIQUE,
  batch_no   TEXT NOT NULL,
  route_code TEXT,
  source_type TEXT NOT NULL,               -- SP / JUMBO
  source_ref TEXT NOT NULL,                -- grn_no (لوط SP) أو roll_no (جامبو)
  qty        REAL NOT NULL,
  unit       TEXT NOT NULL,
  alloc_date TEXT,
  operator   TEXT,
  created_by TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  voided     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alloc_batch ON allocations(batch_no);
CREATE INDEX IF NOT EXISTS ix_alloc_src ON allocations(source_ref);

-- روابط الأصل والفرع لأي دفعة/لوط ---------------------------------------
CREATE TABLE IF NOT EXISTS batch_links (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  child     TEXT NOT NULL,
  parent    TEXT NOT NULL,
  link_type TEXT NOT NULL,                 -- SP_LOT / JUMBO / SUB_ROLL / PROC
  qty       REAL,
  unit      TEXT,
  ref_doc   TEXT,
  UNIQUE(child, parent, link_type)
);
CREATE INDEX IF NOT EXISTS ix_bl_parent ON batch_links(parent);

-- استلام المنتج التام في المخزن ------------------------------------------
CREATE TABLE IF NOT EXISTS fg_receipts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_no     TEXT UNIQUE,
  rdate      TEXT,
  batch_no   TEXT NOT NULL,
  qty        REAL NOT NULL,
  unit       TEXT NOT NULL,
  location   TEXT,
  received_by TEXT,
  release_ref TEXT,
  notes      TEXT,
  created_by TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_fg_batch ON fg_receipts(batch_no);

-- الإشعارات الديناميكية ---------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT DEFAULT (datetime('now','localtime')),
  kind        TEXT,                        -- qc_pending / released / order / ready_qc / ready_wh …
  title       TEXT NOT NULL,
  body        TEXT,
  link        TEXT,
  ref         TEXT,                        -- دفعة/سند مرتبط
  route_code  TEXT,                        -- خط الإنتاج المعني (لتصفية حسب خطوط المستخدم)
  target_perms TEXT,                       -- JSON: من يملك أيًّا منها يرى الإشعار
  created_by  TEXT
);
CREATE INDEX IF NOT EXISTS ix_notif_ts ON notifications(ts);

CREATE TABLE IF NOT EXISTS notif_reads (
  notif_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  ts       TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(notif_id, username)
);
