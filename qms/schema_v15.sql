-- v15: التتبع الكامل — المشغلون، مواصفات التعبئة، المنتج الوسيط (SP الداخلي)، تعبئة غير المعقم،
--      سجل الفرز والتغليف للمعقم، سجل استيراد Excel، تاريخ تغيير الأكواد
CREATE TABLE IF NOT EXISTS operators (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  name   TEXT NOT NULL UNIQUE,
  dept   TEXT NOT NULL DEFAULT 'production',       -- production / sterilization / quality / warehouse
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS item_code_history (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  uid      TEXT,
  old_code TEXT NOT NULL,
  new_code TEXT NOT NULL,
  by_user  TEXT,
  ts       TEXT DEFAULT (datetime('now','localtime'))
);

-- Packaging Configuration لكل صنف (مصدر واحد لكل العلاقات؛ لا أرقام في الصفحات)
CREATE TABLE IF NOT EXISTS pack_spec (
  item_code          TEXT PRIMARY KEY,
  swabs_per_pack     REAL,     -- غير معقم: 100
  packs_per_carton   REAL,     -- غير معقم: يختلف بالصنف
  swabs_per_envelope REAL,     -- معقم: 5 أو 10
  swabs_per_box      REAL,     -- معقم: 100
  boxes_per_carton   REAL,     -- معقم: يختلف بالصنف
  pack_code          TEXT,
  env_code           TEXT,
  box_code           TEXT,
  carton_code        TEXT,
  updated_by         TEXT,
  updated_at         TEXT
);

-- كرتون وسيط بعد الطي للمنتج المعقم (SP داخلي). الباركود مدخل للفرز والعد
CREATE TABLE IF NOT EXISTS intermediates (
  barcode     TEXT PRIMARY KEY,                     -- SPC-000001
  batch_no    TEXT NOT NULL,
  item_code   TEXT,                                 -- المنتج النهائي المقصود
  sp_code     TEXT,                                 -- كود SP من Master Data
  sp_source   TEXT NOT NULL DEFAULT 'INTERNAL_PRODUCTION',   -- أو SUPPLIER_RECEIPT
  fold_doc    TEXT UNIQUE,
  tag_no      TEXT,
  qty         REAL NOT NULL,                        -- مسحات سليمة
  status      TEXT NOT NULL DEFAULT 'Pending SP Release',
  prod_date   TEXT,
  operator    TEXT,
  rec_no      TEXT,
  created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_inter_batch ON intermediates(batch_no);

-- تعبئة غير المعقم (مسحات ← باكت ← كرتون)
CREATE TABLE IF NOT EXISTS ns_packing (
  doc_no             TEXT PRIMARY KEY,              -- PACK-2026-000001
  batch_no           TEXT NOT NULL,
  swabs_in           REAL NOT NULL,
  swabs_per_pack     REAL,
  packs_per_carton   REAL,
  packs_expected     REAL,
  packs_actual       REAL,
  cartons_expected   REAL,
  cartons_actual     REAL,
  operator           TEXT,
  notes              TEXT,
  created_by         TEXT,
  created_at         TEXT DEFAULT (datetime('now','localtime')),
  updated_by         TEXT,
  updated_at         TEXT
);
CREATE INDEX IF NOT EXISTS ix_nsp_batch ON ns_packing(batch_no);
CREATE TABLE IF NOT EXISTS ns_pack_src (
  doc_no   TEXT NOT NULL,
  fold_doc TEXT NOT NULL,
  swabs    REAL NOT NULL,
  PRIMARY KEY(doc_no, fold_doc)
);

-- سجل معالجة المنتج المعقم: فرز وعد ← تغليف ← بوكسات (سند واحد يُحدَّث)
CREATE TABLE IF NOT EXISTS ster_records (
  rec_no           TEXT PRIMARY KEY,               -- STR-2026-000001
  batch_no         TEXT NOT NULL,
  item_code        TEXT,
  sp_code          TEXT,
  status           TEXT NOT NULL DEFAULT 'Sorting',
  received_qty     REAL,
  sort_accepted    REAL,
  sort_rejected    REAL,
  sort_operator    TEXT,  sort_at TEXT,
  ppe              REAL,                            -- مسحات لكل مغلف (لقطة من Packaging Configuration)
  env_expected     REAL,
  env_actual       REAL,
  env_rejected     REAL,
  pack_operator    TEXT,  pack_at TEXT,
  env_per_box      REAL,
  boxes_expected   REAL,
  boxes_actual     REAL,
  box_diff         REAL,
  box_operator     TEXT,  box_at TEXT,
  boxes_per_carton REAL,
  cartons          REAL,
  cycle_no         TEXT,
  pre_release_by   TEXT,  pre_release_at TEXT,
  notes            TEXT,
  created_by       TEXT,
  created_at       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_str_batch ON ster_records(batch_no);
CREATE TABLE IF NOT EXISTS ster_inputs (
  rec_no  TEXT NOT NULL,
  barcode TEXT NOT NULL UNIQUE,                     -- كرتون وسيط يدخل سجلًا واحدًا فقط
  qty     REAL,
  PRIMARY KEY(rec_no, barcode)
);

CREATE TABLE IF NOT EXISTS import_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT DEFAULT (datetime('now','localtime')),
  by_user   TEXT,
  file_name TEXT,
  target    TEXT,
  mode      TEXT,
  inserted  INTEGER DEFAULT 0,
  updated   INTEGER DEFAULT 0,
  skipped   INTEGER DEFAULT 0,
  rejected  INTEGER DEFAULT 0,
  errors    TEXT
);
