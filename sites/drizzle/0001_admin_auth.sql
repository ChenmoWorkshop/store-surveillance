CREATE TABLE site_admin (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  username TEXT NOT NULL,
  salt TEXT NOT NULL,
  digest TEXT NOT NULL,
  session_key TEXT NOT NULL
);
--> statement-breakpoint
CREATE TABLE login_attempts (
  ip_hash TEXT PRIMARY KEY,
  period INTEGER NOT NULL,
  attempts INTEGER NOT NULL
);
