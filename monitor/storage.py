import csv
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DB_FILE = Path(os.getenv("MONITOR_DB", ROOT / "data" / "monitor.db"))
BACKUP_VERSION = 1
TABLES = ("targets", "import_readings", "jobs", "snapshots", "shop_catalogs",
          "events", "deliveries", "rules")
RULE_DEFAULTS = {
    "hourly_units": 5, "premium_price_fen": 2900, "premium_units": 2,
    "price_drop_fen": 0, "state_alerts": False,
}


def now():
    return datetime.now(timezone.utc)


def stamp(time):
    return time.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        if os.getenv("MONITOR_CLOUD") == "1":
            db.execute("PRAGMA journal_mode = DELETE")
        else:
            db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY, platform TEXT NOT NULL, kind TEXT NOT NULL,
                external_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, active_since TEXT NOT NULL,
                deleted_at TEXT, cover_url TEXT, official_title TEXT, shop_name TEXT, category TEXT,
                reviewed_sampling_slot TEXT,
                UNIQUE(platform, kind, external_id)
            );
            CREATE TABLE IF NOT EXISTS import_readings (
                target_id INTEGER PRIMARY KEY REFERENCES targets(id),
                observed_at TEXT NOT NULL,
                metrics TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id),
                slot TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL, error TEXT,
                UNIQUE(target_id, slot)
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, next_at);
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id),
                slot TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
                raw_sales TEXT, exact_sales INTEGER, price_fen INTEGER, listing_state TEXT,
                want_count INTEGER,
                hourly_delta INTEGER, daily_delta INTEGER, quality TEXT NOT NULL,
                daily_baseline_at TEXT, price_label TEXT,
                UNIQUE(target_id, slot)
            );
            CREATE TABLE IF NOT EXISTS shop_catalogs (
                snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id),
                target_id INTEGER NOT NULL REFERENCES targets(id),
                active_count INTEGER NOT NULL,
                items TEXT NOT NULL,
                has_more INTEGER
            );
            CREATE INDEX IF NOT EXISTS shop_catalogs_recent ON shop_catalogs(target_id, snapshot_id DESC);
            CREATE INDEX IF NOT EXISTS snapshots_recent ON snapshots(target_id, observed_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL UNIQUE REFERENCES snapshots(id),
                target_id INTEGER NOT NULL REFERENCES targets(id), created_at TEXT NOT NULL,
                summary TEXT NOT NULL, matched TEXT NOT NULL, notify INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, group_key TEXT NOT NULL,
                body TEXT NOT NULL, state TEXT NOT NULL, error TEXT, UNIQUE(channel, group_key)
            );
        """)
        db.execute("BEGIN IMMEDIATE")
        target_columns = {column["name"] for column in db.execute("PRAGMA table_info(targets)")}
        for column in ("deleted_at", "cover_url", "official_title", "shop_name", "category", "reviewed_sampling_slot"):
            if column not in target_columns:
                db.execute(f"ALTER TABLE targets ADD COLUMN {column} TEXT")
        snapshot_columns = {column["name"] for column in db.execute("PRAGMA table_info(snapshots)")}
        if "want_count" not in snapshot_columns:
            db.execute("ALTER TABLE snapshots ADD COLUMN want_count INTEGER")
        for column in ("daily_baseline_at", "price_label"):
            if column not in snapshot_columns:
                db.execute(f"ALTER TABLE snapshots ADD COLUMN {column} TEXT")
        catalog_columns = {column["name"] for column in db.execute("PRAGMA table_info(shop_catalogs)")}
        if "has_more" not in catalog_columns:
            db.execute("ALTER TABLE shop_catalogs ADD COLUMN has_more INTEGER")
        db.execute("INSERT OR IGNORE INTO rules VALUES (1, ?)", (json.dumps(RULE_DEFAULTS),))


def dump_database():
    """Return a consistent copy of every table for backup or migration."""
    with database() as db:
        db.execute("BEGIN")
        tables = {}
        for table in TABLES:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            tables[table] = [dict(row) for row in rows]
    return {"version": BACKUP_VERSION, "exported_at": stamp(now()), "tables": tables,
            "counts": {name: len(rows) for name, rows in tables.items()}}


def restore_database(payload, replace=False):
    """Load a dump produced by dump_database into an empty database.

    Existing data is only removed when replace is true; the caller is expected
    to require an explicit confirmation before asking for that.
    """
    if not isinstance(payload, dict) or payload.get("version") != BACKUP_VERSION:
        raise ValueError("备份文件版本不支持")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("备份文件缺少 tables")

    with database() as db:
        columns = {table: [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
                   for table in TABLES}
        prepared = {}
        for table in TABLES:
            rows = tables.get(table, [])
            if not isinstance(rows, list):
                raise ValueError(f"备份文件 {table} 格式错误")
            for row in rows:
                if not isinstance(row, dict) or not row:
                    raise ValueError(f"备份文件 {table} 存在空记录")
                unknown = set(row) - set(columns[table])
                if unknown:
                    raise ValueError(f"备份文件 {table} 包含未知字段：{'、'.join(sorted(unknown))}")
            prepared[table] = rows
        if len(prepared["rules"]) != 1 or not isinstance(prepared["rules"][0].get("settings"), str):
            raise ValueError("备份文件缺少提醒规则")
        try:
            if not isinstance(json.loads(prepared["rules"][0]["settings"]), dict):
                raise ValueError
        except ValueError as error:
            raise ValueError("备份文件的提醒规则格式错误") from error
        db.execute("BEGIN IMMEDIATE")
        occupied = sum(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                       for table in TABLES if table != "rules")
        if occupied and not replace:
            raise ValueError("数据库已有监控数据，恢复会覆盖现有记录")
        for table in reversed(TABLES):
            db.execute(f"DELETE FROM {table}")
        for table in TABLES:
            for row in prepared[table]:
                names = list(row)
                marks = ",".join("?" * len(names))
                db.execute(f"INSERT INTO {table}({','.join(names)}) VALUES ({marks})",
                           tuple(row[name] for name in names))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("备份文件关联校验失败")
        for table in TABLES:
            if db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != len(prepared[table]):
                raise ValueError(f"备份文件 {table} 行数校验失败")
        recompute_deltas(db)
    return {table: len(prepared[table]) for table in TABLES}



def read_rules(db):
    row = db.execute("SELECT settings FROM rules WHERE id=1").fetchone()
    return RULE_DEFAULTS | json.loads(row["settings"])


def metric_column(target):
    # 店铺按在售数统计；小红书商品用精确展示销量，闲鱼商品用想要数。
    if target["kind"] == "shop":
        return "active_count"
    return "exact_sales" if target["platform"] == "xiaohongshu" else "want_count"


def scheduled_deltas(db, target, slot, instant, reading, quality):
    column = metric_column(target)
    current = reading.shop_catalog.active_count if column == "active_count" and reading.shop_catalog else (
        getattr(reading, column, None)
    )
    return compute_deltas(db, target, slot, instant, current, quality, column)


def compute_deltas(db, target, slot, instant, current, quality, column):
    """Return (hourly, daily, daily_baseline, strict_hourly).

    ``hourly`` 在下 面缺采时会回溯到最近一条有效整点，用于页面展示；
    ``strict_hourly`` 只保留严格相邻整点的差值，供异动规则判断，避免把
    跨了几个小时的累计涨幅当成「近一小时」而误报。
    """
    if current is None:
        return None, None, None, None

    prefix = f"""SELECT s.slot,s.observed_at,s.quality,{column} AS metric
                 FROM snapshots s LEFT JOIN shop_catalogs c ON c.snapshot_id=s.id
                 WHERE s.target_id=? AND s.source='scheduled' AND {column} IS NOT NULL"""
    hourly = strict = None
    # 上一小时缺采时不直接放弃，而是向前找最近一条有效整点（最多回溯 6 小时）。
    # 实际跨度由两条记录的 slot 差值体现，前端据此提示「与几小时前相比」。
    lookback = timedelta(hours=6)
    earliest = max(stamp(slot - lookback), target["active_since"])
    if stamp(slot - timedelta(hours=1)) >= earliest:
        previous = db.execute(
            prefix + " AND s.slot>=? AND s.slot<? ORDER BY s.slot DESC LIMIT 1",
            (target["id"], earliest, stamp(slot)),
        ).fetchone()
        if previous:
            # 用整点差而不是采集时刻差：采样在 45 分钟窗口内随时可能发生，
            # 补采的那一小时采集时刻会明显偏移，但两条记录的整点仍相邻。
            gap = slot - datetime.fromisoformat(previous["slot"]) if previous["slot"] else None
            if gap and timedelta(hours=1) <= gap <= lookback and (
                    column != "exact_sales" or previous["metric"] <= current):
                hourly = current - previous["metric"]
                if gap == timedelta(hours=1):
                    strict = hourly

    day = slot.astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(stamp(day), target["active_since"])
    end = stamp(slot)
    # 计数回退后从回退时的读数重新起算，避免当天剩余整点的今日增量全部为空。
    reset = db.execute(
        """SELECT s.slot FROM snapshots s WHERE s.target_id=? AND s.source='scheduled'
           AND s.slot>=? AND s.slot<? AND s.quality='counter_reset' ORDER BY s.slot DESC LIMIT 1""",
        (target["id"], start, end),
    ).fetchone()
    if reset:
        start = reset["slot"]
    first = db.execute(prefix + " AND s.slot>=? AND s.slot<? ORDER BY s.slot LIMIT 1",
                       (target["id"], start, end)).fetchone()
    if not first:
        # 当天首条有效读数即基准，今日增量从 0 起算。
        return hourly, 0, stamp(instant), strict
    if column == "exact_sales" and first["metric"] > current:
        # 当天读数低于基准（回退未被标记）时按重新起算处理，不再整天留空。
        return hourly, 0, stamp(instant), strict
    return hourly, current - first["metric"], first["observed_at"], strict


def recompute_deltas(db):
    """Recalculate hourly/daily deltas for every scheduled snapshot.

    Restoring a backup written by an older build can carry stale or empty
    deltas; replaying them with the current rules keeps the history readable.
    """
    snapshots = db.execute(
        """SELECT s.id,s.target_id,s.slot,s.observed_at,s.quality,s.raw_sales,s.exact_sales,
                  s.want_count,c.active_count
           FROM snapshots s LEFT JOIN shop_catalogs c ON c.snapshot_id=s.id
           WHERE s.source='scheduled' AND s.slot IS NOT NULL
           ORDER BY s.slot,s.id"""
    ).fetchall()
    targets = {row["id"]: row for row in db.execute("SELECT * FROM targets")}
    for row in snapshots:
        target = targets.get(row["target_id"])
        if target is None:
            continue
        column = metric_column(target)
        hourly, daily, baseline, _ = compute_deltas(
            db, target, datetime.fromisoformat(row["slot"]), datetime.fromisoformat(row["observed_at"]),
            row[column], row["quality"], column,
        )
        db.execute("UPDATE snapshots SET hourly_delta=?,daily_delta=?,daily_baseline_at=? WHERE id=?",
                   (hourly, daily, baseline, row["id"]))
    return len(snapshots)


def update_product_cover(db, target_id, url):
    if not url:
        return
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return
    except ValueError:
        return
    if host.endswith((".xhscdn.com", ".alicdn.com")):
        db.execute("UPDATE targets SET cover_url=? WHERE id=? AND deleted_at IS NULL", (url, target_id))


def update_product_metadata(db, target_id, reading):
    update_product_cover(db, target_id, reading.cover_url)
    if reading.official_title or reading.shop_name:
        db.execute(
            """UPDATE targets SET official_title=COALESCE(?,official_title),
                   shop_name=COALESCE(?,shop_name) WHERE id=? AND deleted_at IS NULL""",
            (reading.official_title or None, reading.shop_name or None, target_id),
        )


def record(db, target, reading, instant, source, slot=None):
    target_id = target["id"]
    if target["kind"] == "product":
        update_product_metadata(db, target_id, reading)
    last = db.execute(
        """SELECT * FROM snapshots WHERE target_id=? AND source=?
           AND (slot IS NULL OR slot < ?) ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (target_id, source, stamp(slot or instant)),
    ).fetchone()
    sales = reading.exact_sales
    hourly = daily = day_baseline = strict_hourly = None
    quality = "exact" if sales is not None else ("display_only" if reading.raw_sales else "unavailable")
    if target["platform"] == "xiaohongshu" and sales is not None:
        previous = db.execute(
            """SELECT exact_sales FROM snapshots WHERE target_id=? AND source=?
               AND exact_sales IS NOT NULL AND (slot IS NULL OR slot<?)
               ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (target_id, source, stamp(slot or instant)),
        ).fetchone()
        if previous and sales < previous["exact_sales"]:
            quality = "counter_reset"
    if source == "scheduled":
        hourly, daily, day_baseline, strict_hourly = scheduled_deltas(
            db, target, slot, instant, reading, quality)
    snapshot_id = db.execute(
        """INSERT INTO snapshots(target_id, slot, observed_at, source, raw_sales, exact_sales,
               price_fen, listing_state, hourly_delta, daily_delta, quality, want_count,
               daily_baseline_at, price_label)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (target_id, stamp(slot) if slot else None, stamp(instant), source, reading.raw_sales,
         sales, reading.price_fen, reading.listing_state, hourly, daily, quality,
         reading.want_count if target["platform"] == "xianyu" and target["kind"] == "product" else None,
         day_baseline, reading.price_label),
    ).lastrowid
    if reading.shop_catalog and target["kind"] == "shop":
        catalog = reading.shop_catalog
        db.execute("INSERT INTO shop_catalogs(snapshot_id,target_id,active_count,items,has_more) VALUES (?,?,?,?,?)",
                   (snapshot_id, target_id, catalog.active_count,
                    json.dumps([item.model_dump() for item in catalog.items], ensure_ascii=False), catalog.has_more))
    rules = read_rules(db)
    changes, matches = [], []
    # 规则只认严格相邻整点的增量；缺采后回溯算出的累计值仅用于页面展示。
    sales_alert = (target["platform"] == "xiaohongshu" and target["kind"] == "product"
                   and strict_hourly is not None)
    if sales_alert and strict_hourly >= rules["hourly_units"]:
        matches.append("小时销量")
    if (sales_alert and reading.price_fen is not None
            and reading.price_fen >= rules["premium_price_fen"] and strict_hourly >= rules["premium_units"]):
        matches.append("高客单价")
    if matches:
        changes.append(f"近一小时 +{strict_hourly} · 累计 {sales} · 今日 {daily if daily is not None else '暂无基准'}")
    if last and reading.price_fen is not None:
        previous_price = latest_field(db, target_id, source, "price_fen", snapshot_id)
        if previous_price is not None and previous_price != reading.price_fen:
            changes.append(f"价格 ¥{previous_price / 100:.2f} → ¥{reading.price_fen / 100:.2f}")
            if (target["platform"] == "xianyu" and target["kind"] == "product"
                    and 0 < rules["price_drop_fen"] <= previous_price - reading.price_fen):
                matches.append("降价")
    if last and reading.listing_state is not None:
        previous_state = latest_field(db, target_id, source, "listing_state", snapshot_id)
        if previous_state and previous_state != reading.listing_state:
            changes.append(f"{STATE_LABELS[previous_state]} → {STATE_LABELS[reading.listing_state]}")
            if rules["state_alerts"] and target["platform"] == "xianyu" and target["kind"] == "product":
                matches.append("商品状态")
    if changes:
        db.execute(
            "INSERT INTO events(snapshot_id,target_id,created_at,summary,matched,notify) VALUES (?,?,?,?,?,?)",
            (snapshot_id, target_id, stamp(instant), "；".join(changes),
             json.dumps(matches, ensure_ascii=False), bool(matches) and source == "scheduled"),
        )
    return snapshot_id


STATE_LABELS = {"active": "在售", "sold": "已售", "delisted": "下架"}


def latest_field(db, target_id, source, column, before):
    # Column names are provided only by the two internal callers.
    row = db.execute(
        f"""SELECT {column} FROM snapshots WHERE target_id=? AND source=? AND id<?
            AND {column} IS NOT NULL ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (target_id, source, before),
    ).fetchone()
    return row[0] if row else None


def series(db, target_id, limit):
    return [dict(row) for row in db.execute(
        "SELECT * FROM snapshots WHERE target_id=? ORDER BY observed_at DESC,id DESC LIMIT ?",
        (target_id, limit),
    )]


def daily_observations(db, target, limit=366):
    column = metric_column(target)
    raw = "s.raw_sales" if column == "exact_sales" else "NULL"
    rows = db.execute(
        f"""WITH ranked AS (
            SELECT date(s.observed_at,'+8 hours') AS day,s.observed_at,s.source,s.quality,
                   {column} AS value,{raw} AS display,s.price_fen,s.price_label,
                   row_number() OVER (
                       PARTITION BY date(s.observed_at,'+8 hours')
                       ORDER BY s.observed_at DESC,s.id DESC
                   ) AS rank
            FROM snapshots s LEFT JOIN shop_catalogs c ON c.snapshot_id=s.id
            WHERE s.target_id=? AND s.source<>'catalog'
              AND ({column} IS NOT NULL OR {raw} IS NOT NULL)
        )
        SELECT day,observed_at,source,quality,value,display,price_fen,price_label
        FROM ranked WHERE rank=1 AND day IS NOT NULL ORDER BY day DESC LIMIT ?""",
        (target["id"], limit + 1),
    ).fetchall()
    days = [dict(row) for row in rows]
    for index, current in enumerate(days):
        current["delta"] = None
        if index + 1 == len(days):
            break
        previous = days[index + 1]
        if (current["value"] is None or previous["value"] is None
                or (date.fromisoformat(current["day"]) - date.fromisoformat(previous["day"])).days != 1):
            continue
        change = current["value"] - previous["value"]
        if column != "exact_sales" or (change >= 0
                and current["quality"] != "counter_reset" and previous["quality"] != "counter_reset"):
            current["delta"] = change
    return days[:limit]


def observation_times(db, target_id):
    row = db.execute(
        """SELECT min(observed_at) AS first_observed_at,max(observed_at) AS last_observed_at,
           max(CASE WHEN source IN ('scheduled','refresh') THEN observed_at END) AS last_fetched_at
           FROM snapshots WHERE target_id=? AND source<>'catalog'""",
        (target_id,),
    ).fetchone()
    return dict(row)


def three_hour_delta(hours):
    slots = hours[:3]
    if len(slots) != 3 or any(row["state"] != "success" or row["hourly_delta"] is None for row in slots):
        return None
    instants = [datetime.fromisoformat(row["slot"]) for row in slots]
    if any(newer - older != timedelta(hours=1) for newer, older in zip(instants, instants[1:])):
        return None
    return sum(row["hourly_delta"] for row in slots)


def observation_jobs(db, target_id, limit):
    rows = db.execute(
        """SELECT j.slot,j.state,j.error,s.observed_at,s.price_fen,s.price_label,
                  s.raw_sales,s.exact_sales,s.want_count,c.active_count AS shop_active_count,
                  s.hourly_delta,s.daily_delta,s.daily_baseline_at
           FROM jobs j LEFT JOIN snapshots s ON s.target_id=j.target_id AND s.slot=j.slot
           LEFT JOIN shop_catalogs c ON c.snapshot_id=s.id
           WHERE j.target_id=? ORDER BY j.slot DESC LIMIT ?""",
        (target_id, limit),
    )
    return [dict(row) for row in rows]


def recent_catalogs(db, target_id):
    rows = db.execute(
        """SELECT c.active_count,c.items,c.has_more,s.observed_at,s.source
           FROM shop_catalogs c JOIN snapshots s ON c.snapshot_id=s.id
           WHERE c.target_id=? ORDER BY c.snapshot_id DESC LIMIT 2""",
        (target_id,),
    )
    return [dict(row) | {"items": json.loads(row["items"])} for row in rows]


def catalog_readings(db, products):
    keys = list({(row["platform"], row["external_id"]) for row in products})
    if not keys:
        return {}
    matches = db.execute(
        """WITH requested AS (
               SELECT json_extract(value,'$[0]') AS platform,json_extract(value,'$[1]') AS external_id
               FROM json_each(?)
           ), matching AS (
               SELECT r.platform,r.external_id,i.value AS item,s.observed_at,
                      row_number() OVER (
                          PARTITION BY r.platform,r.external_id ORDER BY s.observed_at DESC,s.id DESC
                      ) AS rank
               FROM requested r JOIN targets t ON t.platform=r.platform AND t.kind='shop'
               JOIN shop_catalogs c ON c.target_id=t.id JOIN snapshots s ON s.id=c.snapshot_id
               JOIN json_each(c.items) i ON json_extract(i.value,'$.id')=r.external_id
           )
           SELECT platform,external_id,item,observed_at FROM matching WHERE rank=1""",
        (json.dumps(keys),),
    )
    result = {}
    for row in matches:
        item = json.loads(row["item"])
        result[(row["platform"], row["external_id"])] = {
            "price_fen": item.get("price_fen"),
            "raw_sales": item.get("raw_sales") if row["platform"] == "xiaohongshu" else None,
            "want_count": item.get("want_count") if row["platform"] == "xianyu" else None,
            "observed_at": row["observed_at"],
        }
    return result


def catalog_product_url(platform, item_id):
    if platform == "xianyu":
        return f"https://www.goofish.com/item?id={item_id}"
    return f"https://www.xiaohongshu.com/goods-detail/{item_id}"


def catalog_products(db, catalog, platform):
    rows = db.execute(
        """SELECT t.external_id,t.id AS target_id,
                  p.price_fen,p.observed_at AS price_observed_at,
                  w.want_count,w.observed_at AS want_observed_at,
                  coalesce(v.raw_sales, CAST(v.exact_sales AS TEXT)) AS raw_sales,
                  v.observed_at AS sales_observed_at
           FROM json_each(?) item
           JOIN targets t ON t.platform=? AND t.kind='product' AND t.deleted_at IS NULL
                           AND t.external_id=json_extract(item.value,'$.id')
           LEFT JOIN snapshots p ON p.id=(
               SELECT id FROM snapshots WHERE target_id=t.id AND price_fen IS NOT NULL
               ORDER BY observed_at DESC,id DESC LIMIT 1
           )
           LEFT JOIN snapshots w ON w.id=(
               SELECT id FROM snapshots WHERE target_id=t.id AND want_count IS NOT NULL
               ORDER BY observed_at DESC,id DESC LIMIT 1
           )
           LEFT JOIN snapshots v ON v.id=(
               SELECT id FROM snapshots WHERE target_id=t.id AND (raw_sales IS NOT NULL OR exact_sales IS NOT NULL)
               ORDER BY observed_at DESC,id DESC LIMIT 1
           )""",
        (json.dumps(catalog["items"]), platform),
    )
    readings = {row["external_id"]: row for row in rows}
    products = []
    for item in catalog["items"]:
        row = readings.get(item["id"])
        listing_price = item.get("price_fen")
        listing_time = catalog["observed_at"] if listing_price is not None else None
        price_at = row["price_observed_at"] if row else None
        if price_at and (listing_time is None or price_at >= listing_time):
            price_fen, price_source = row["price_fen"], "product"
        else:
            price_fen, price_at, price_source = listing_price, listing_time, "catalog" if listing_time else None
        catalog_sales = item.get("raw_sales")
        sales_at = row["sales_observed_at"] if row else None
        if sales_at and (catalog_sales is None or sales_at >= catalog["observed_at"]):
            raw_sales = row["raw_sales"]
        else:
            raw_sales = catalog_sales
            sales_at = catalog["observed_at"] if catalog_sales is not None else None
        products.append({
            "id": item["id"],
            "raw_sales": raw_sales,
            "sales_observed_at": sales_at,
            "title": item["title"],
            "url": catalog_product_url(platform, item["id"]),
            "target_id": row["target_id"] if row else None,
            "price_fen": price_fen,
            "price_observed_at": price_at,
            "price_source": price_source,
            "want_count": row["want_count"] if row else None,
            "want_observed_at": row["want_observed_at"] if row else None,
        })
    return products


def export_catalog(catalog, platform):
    result = io.StringIO()
    writer = csv.writer(result)
    writer.writerow(["商品ID", "商品名称", "商品链接"])
    for item in catalog["items"]:
        writer.writerow([item["id"], csv_cell(item["title"]), catalog_product_url(platform, item["id"])])
    return "\ufeff" + result.getvalue()


def export_series(db, target_id, limit):
    rows = series(db, target_id, limit)
    fields = ["observed_at", "slot", "source", "raw_sales", "exact_sales", "price_fen",
              "listing_state", "hourly_delta", "daily_delta", "quality", "want_count",
              "daily_baseline_at", "price_label"]
    result = io.StringIO()
    writer = csv.writer(result)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([csv_cell(row[field]) for field in fields])
    return "\ufeff" + result.getvalue()


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
