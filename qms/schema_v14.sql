-- v14: تبسيط — خطط القص، سجل قرارات الجودة النهائية، الوحدات
CREATE TABLE IF NOT EXISTS units (
  code    TEXT PRIMARY KEY,
  name_ar TEXT NOT NULL,
  sort_no INTEGER DEFAULT 0
);

-- خطة قص واحدة لكل جامبو رول؛ تُولّد السب رول تلقائيًا من بيانات المنتج
CREATE TABLE IF NOT EXISTS cutting_plans (
  plan_no      TEXT PRIMARY KEY,           -- CP-2026-000001
  batch_no     TEXT NOT NULL,
  roll_no      TEXT NOT NULL,
  jumbo_width  REAL,
  usable_width REAL,
  sr_width     REAL,
  n_sub        INTEGER,
  edge_each    REAL,
  status       TEXT NOT NULL DEFAULT 'مخطط',   -- مخطط / منفّذ
  created_by   TEXT,
  created_at   TEXT DEFAULT (datetime('now','localtime')),
  executed_by  TEXT,
  executed_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_cp_batch ON cutting_plans(batch_no);
CREATE INDEX IF NOT EXISTS ix_cp_roll  ON cutting_plans(roll_no);

-- قرار الجودة النهائي على الدفعة (موافقة للتخزين / رفض / تعليق)
CREATE TABLE IF NOT EXISTS approvals (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT NOT NULL,
  decision TEXT NOT NULL,                  -- Approved / Rejected / Hold
  note     TEXT,
  qty      REAL,
  unit     TEXT,
  by_user  TEXT,
  ts       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_appr_batch ON approvals(batch_no);
