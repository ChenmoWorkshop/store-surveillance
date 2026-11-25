CREATE TABLE monitor_rows (
  table_name TEXT NOT NULL,
  id TEXT NOT NULL,
  target_id INTEGER,
  data TEXT NOT NULL,
  PRIMARY KEY (table_name, id)
);
--> statement-breakpoint
CREATE INDEX monitor_rows_target ON monitor_rows (table_name, target_id);
