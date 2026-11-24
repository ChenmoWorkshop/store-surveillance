import asyncio
import base64
import json
import os
import re
import secrets
from typing import Annotated
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from decimal import Decimal
from unicodedata import normalize
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import platforms, worker, xhs_product
from .models import ImportLinks, Reading, Rules, SamplingReview, TargetPatch, VisibleReading, xianyu_visible_reading
from .storage import CST, ROOT, catalog_products, catalog_readings, daily_observations, database, dump_database, export_catalog, export_series, init, now, observation_jobs, observation_times, read_rules, recent_catalogs, record, restore_database, series, stamp, three_hour_delta, update_product_metadata

logger = logging.getLogger(__name__)
CLOUD_MODE = os.getenv("MONITOR_CLOUD") == "1"


@asynccontextmanager
async def lifespan(app):
    if CLOUD_MODE:
        username = os.getenv("MONITOR_ACCESS_USERNAME", "monitor")
        password = os.getenv("MONITOR_ACCESS_PASSWORD", "")
        if not username or ":" in username or not username.isascii() or not username.isprintable():
            raise RuntimeError("云托管用户名须为不含冒号的可打印 ASCII 字符")
        if len(password) < 8 or not password.isascii() or not password.isprintable():
            raise RuntimeError("云托管访问密码须至少 8 位，使用可打印 ASCII 字符")
        app.state.authorization = b"Basic " + base64.b64encode(f"{username}:{password}".encode("ascii"))
    init()
    stop = asyncio.Event()
    running = asyncio.create_task(scheduler(stop))
    try:
        yield
    finally:
        stop.set()
        await running


async def scheduler(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(worker.cycle)
        except Exception:
            logger.exception("调度执行失败")
        try:
            await asyncio.wait_for(stop.wait(), 8)
        except TimeoutError:
            continue


app = FastAPI(title="闲鱼与小红书链接监控", lifespan=lifespan)
if not CLOUD_MODE:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if CLOUD_MODE and request.url.path != "/healthz":
        provided = request.headers.get("authorization", "").encode("latin-1")
        if not secrets.compare_digest(provided, request.app.state.authorization):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Store Monitor"'})
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        scheme = "https" if CLOUD_MODE else request.url.scheme
        if origin != f"{scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "仅接受同源操作"}, status_code=403)
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/healthz", include_in_schema=False)
def health_probe():
    return {"status": "ok"}


@app.get("/api/v1/health")
def health():
    kinds = {
        platform: [kind for kind in ("product", "shop") if platforms.target_configured(platform, kind)]
        for platform in ("xianyu", "xiaohongshu")
    }
    return {"status": "ok",
            "sources": {platform: bool(value) for platform, value in kinds.items()},
            "source_kinds": kinds,
            "public_shop": "xiaohongshu", "channels": list(worker.channels())}


@app.get("/api/v1/admin/export")
def export_backup():
    payload = dump_database()
    name = f"monitor-backup-{now().astimezone(CST):%Y%m%d-%H%M%S}.json"
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/v1/admin/probe")
def probe_source(id: str = Query(..., min_length=5, max_length=32)):
    """按需回放一次采集通道，返回页面与接口的真实返回，用于排查风控。"""
    from . import probe

    try:
        return probe.trace(id)
    except Exception as error:
        return JSONResponse({"error": f"{type(error).__name__}: {error}"[:400]}, status_code=502)


@app.post("/api/v1/admin/restore")
async def restore_backup(request: Request):
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > 64_000_000:
        raise HTTPException(413, "备份文件超过 64 MB")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(422, "备份文件不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(422, "备份文件格式错误")
    mode = payload.get("mode", "empty")
    if mode not in ("empty", "replace"):
        raise HTTPException(422, "恢复模式无效")
    if mode == "replace" and payload.get("confirm") != "replace":
        raise HTTPException(422, "覆盖恢复需要 confirm=replace")
    try:
        counts = restore_database(payload, replace=mode == "replace")
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    logger.info("数据已恢复：%s", counts)
    return {"restored": counts}



@app.post("/api/v1/targets/batch")
def import_links(body: ImportLinks):
    urls = platforms.links(body.text)
    if not urls or len(urls) > 200:
        raise HTTPException(422, "请输入 1–200 条 HTTPS 分享链接")
    output = []
    share_names = platforms.shared_titles(body.text)
    short_count = 0
    pending_previews = {}
    for url in urls:
        try:
            if urlsplit(url).hostname in platforms.SHORT_HOSTS:
                short_count += 1
                if short_count > 10:
                    raise ValueError("单批最多展开 10 条短链，请分批导入")
            # Resolve network redirects outside the database transaction.
            target = platforms.resolve(url)
        except ValueError as error:
            output.append({"url": url, "status": "rejected", "reason": str(error)})
            continue
        except OSError:
            output.append({"url": url, "status": "rejected", "reason": "链接无法访问，请使用展开后的链接"})
            continue
        item = save_import_target(url, target, share_names.get(url))
        output.append(item)
        if target.kind == "product" and platforms.target_configured(target.platform, target.kind):
            pending_previews.setdefault(item["id"], []).append(item)
    if pending_previews:
        with ThreadPoolExecutor(max_workers=min(4, len(pending_previews))) as pool:
            list(pool.map(load_import_reading, pending_previews.values()))
    return {"items": output}


def load_import_reading(items):
    item = items[0]
    target = {"id": item["id"], "platform": item["platform"], "kind": item["kind"],
              "external_id": item["external_id"], "url": item["resolved_url"]}
    try:
        payload = (platforms.observe(target)
                   if platforms.target_configured(target["platform"], target["kind"])
                   else xhs_product.read_product(target))
        reading = Reading.model_validate(payload)
    except RuntimeError as error:
        for item in items:
            item["preview_error"] = str(error)
        return
    except ValueError:
        for item in items:
            item["preview_error"] = "数据源未返回可核对的商品字段"
        return
    wanted = ("price_fen", "want_count") if target["platform"] == "xianyu" else (
        "price_fen", "raw_sales", "exact_sales",
    )
    metrics = {name: getattr(reading, name) for name in wanted if getattr(reading, name) is not None}
    if not metrics:
        for item in items:
            item["preview_error"] = "数据源未返回价格或商品指标"
        return
    if metrics.get("price_fen") is not None and reading.price_label:
        metrics["price_label"] = reading.price_label
    observed_at = stamp(now())
    with database() as db:
        update_product_metadata(db, target["id"], reading)
        stored = db.execute(
            """INSERT INTO import_readings(target_id,observed_at,metrics)
               SELECT id,?,? FROM targets WHERE id=? AND deleted_at IS NULL
               ON CONFLICT(target_id) DO UPDATE
               SET observed_at=excluded.observed_at,metrics=excluded.metrics
               WHERE excluded.observed_at>=import_readings.observed_at""",
            (observed_at, json.dumps(metrics, ensure_ascii=False), target["id"]),
        ).rowcount
    if stored:
        for item in items:
            item["source_preview"] = metrics | {"observed_at": observed_at}


def save_import_target(url, target, shared_name):
    placeholder = ("商品 " if target.kind == "product" else "店铺 ") + target.external_id
    identity = (target.platform, target.kind, target.external_id)
    title, title_source = placeholder, "unavailable"
    if shared_name and shared_name[:2] == identity[:2] and shared_name[2]:
        title, title_source = shared_name[2], "share"
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        catalog_row = None
        if target.kind == "product":
            catalog_row = db.execute(
                """SELECT item.value, s.observed_at
                   FROM shop_catalogs c JOIN targets t ON t.id=c.target_id
                   JOIN snapshots s ON s.id=c.snapshot_id, json_each(c.items) item
                   WHERE t.platform=? AND json_extract(item.value,'$.id')=?
                   ORDER BY s.observed_at DESC,s.id DESC LIMIT 1""",
                (target.platform, target.external_id),
            ).fetchone()
            if catalog_row:
                item = json.loads(catalog_row["value"])
                if item.get("title"):
                    title, title_source = item["title"][:160], "catalog"
        created = db.execute(
            """INSERT OR IGNORE INTO targets(platform,kind,external_id,url,title,active_since)
               VALUES (?,?,?,?,?,?)""",
            (*identity, target.url, title, stamp(now())),
        ).rowcount
        row = db.execute(
            "SELECT id,title,deleted_at FROM targets WHERE platform=? AND kind=? AND external_id=?",
            identity,
        ).fetchone()
        status = "created" if created else "existing"
        if row["deleted_at"] is not None:
            db.execute(
                "UPDATE targets SET deleted_at=NULL,enabled=1,active_since=? WHERE id=?",
                (stamp(now()), row["id"]),
            )
            status = "restored"
        if not created and row["title"] != placeholder:
            title, title_source = row["title"], "saved"
        elif not created and title != placeholder:
            db.execute("UPDATE targets SET title=? WHERE id=?", (title, row["id"]))
        reading = db.execute(
            """SELECT raw_sales,exact_sales,want_count,price_fen,listing_state,observed_at,source
               FROM snapshots WHERE target_id=? ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        preview = None
        if reading is None and catalog_row is not None:
            preview = {key: item.get(key) for key in ("price_fen", "raw_sales", "want_count")}
            if all(value is None for value in preview.values()):
                preview = None
            else:
                preview["observed_at"] = catalog_row["observed_at"]
    return {"url": url, "status": status, "id": row["id"],
            "platform": target.platform, "kind": target.kind, "external_id": target.external_id,
            "resolved_url": target.url, "title": title, "title_source": title_source,
            "reading": dict(reading) if reading else None, "catalog_preview": preview}


def get_target(db, target_id):
    target = db.execute("SELECT * FROM targets WHERE id=? AND deleted_at IS NULL", (target_id,)).fetchone()
    if target is None:
        raise HTTPException(404, "目标不存在")
    return target


@app.get("/api/v1/targets")
def targets(platform: str = "", kind: str = "", q: str = Query("", max_length=160),
            sort: str = "", offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    sort_field = sort.removesuffix("_asc")
    ascending = sort.endswith("_asc")
    sort_displayed = sort_field in ("price", "metric")
    query_limit, query_offset = (-1, 0) if sort_displayed else (limit, offset)
    with database() as db:
        params = (platform, platform, kind, kind, q, q)
        where = """WHERE t.deleted_at IS NULL AND (?='' OR t.platform=?) AND (?='' OR t.kind=?)
                   AND (?='' OR instr(lower(t.title || ' ' || t.external_id),lower(?))>0)"""
        total = db.execute("SELECT count(*) FROM targets t " + where, params).fetchone()[0]
        column = {"daily": "p.daily_delta", "hourly": "p.hourly_delta"}.get(sort_field)
        order = "t.id DESC"
        if column:
            direction = "ASC" if ascending else "DESC"
            order = f"{column} IS NULL,{column} {direction},t.id DESC"
        if not kind:
            order = "t.kind <> 'product'," + order
        local = now().astimezone(CST)
        day_start = stamp(local.replace(hour=0, minute=0, second=0, microsecond=0))
        rows = db.execute(
            """SELECT t.*,s.observed_at,v.raw_sales,v.exact_sales,pr.price_fen,pr.price_label,
                      (SELECT min(observed_at) FROM snapshots WHERE target_id=t.id AND source<>'catalog')
                       AS first_observed_at,
                      (SELECT max(observed_at) FROM snapshots WHERE target_id=t.id AND source<>'catalog')
                       AS last_observed_at,
                      (SELECT want_count FROM snapshots WHERE target_id=t.id AND want_count IS NOT NULL
                       ORDER BY observed_at DESC,id DESC LIMIT 1) AS want_count,
                      (SELECT listing_state FROM snapshots WHERE target_id=t.id AND listing_state IS NOT NULL
                       ORDER BY observed_at DESC,id DESC LIMIT 1) AS listing_state,
                      (SELECT active_count FROM shop_catalogs WHERE target_id=t.id
                       ORDER BY snapshot_id DESC LIMIT 1) AS shop_active_count,
                      s.quality,p.hourly_delta,p.daily_delta,j.state AS job_state,j.error AS job_error,j.slot AS job_slot,
                      CASE WHEN j.state IN ('failed','unavailable') THEN EXISTS (
                          SELECT 1 FROM snapshots WHERE target_id=t.id AND source IN ('scheduled','refresh')
                            AND observed_at>=j.slot
                      ) ELSE 0 END AS sampling_recovered,
                      i.observed_at AS imported_at,i.metrics AS imported_metrics
               FROM targets t LEFT JOIN snapshots s ON s.id=(SELECT id FROM snapshots
                 WHERE target_id=t.id ORDER BY observed_at DESC,id DESC LIMIT 1)
               LEFT JOIN snapshots v ON v.id=(SELECT id FROM snapshots
                 WHERE target_id=t.id AND (raw_sales IS NOT NULL OR exact_sales IS NOT NULL)
                 ORDER BY observed_at DESC,id DESC LIMIT 1)
               LEFT JOIN snapshots pr ON pr.id=(SELECT id FROM snapshots
                 WHERE target_id=t.id AND price_fen IS NOT NULL ORDER BY observed_at DESC,id DESC LIMIT 1)
               LEFT JOIN import_readings i ON i.target_id=t.id
               LEFT JOIN snapshots p ON p.id=(SELECT id FROM snapshots
                 WHERE target_id=t.id AND source='scheduled' AND slot>=?
                 ORDER BY slot DESC LIMIT 1)
               LEFT JOIN jobs j ON j.id=(SELECT MAX(id) FROM jobs WHERE target_id=t.id) """
            + where + " ORDER BY " + order + " LIMIT ? OFFSET ?", (day_start, *params, query_limit, query_offset),
        ).fetchall()
        eligible = [row for row in rows if row["kind"] == "product" and (
            row["price_fen"] is None or (
                row["want_count"] is None if row["platform"] == "xianyu"
                else row["raw_sales"] is None and row["exact_sales"] is None
            )
        )]
        previews = catalog_readings(db, eligible)
    items = []
    for row in rows:
        item = dict(row) | {"configured": platforms.target_configured(row["platform"], row["kind"])}
        imported_metrics = item.pop("imported_metrics")
        imported_at = item.pop("imported_at")
        if imported_metrics:
            values = json.loads(imported_metrics)
            price_label = values.pop("price_label", None)
            fresh = not row["observed_at"] or imported_at >= row["observed_at"]
            displayed = {
                field: value for field, value in values.items() if fresh or (
                    item[field] is None and (field not in ("raw_sales", "exact_sales")
                                            or (item["raw_sales"] is None and item["exact_sales"] is None))
                )
            }
            if displayed:
                if "raw_sales" in displayed or "exact_sales" in displayed:
                    item.update(raw_sales=None, exact_sales=None)
                item.update(displayed)
                item["import_observed_at"] = imported_at
                if "price_fen" in displayed:
                    item["price_label"] = price_label
        preview = previews.get((row["platform"], row["external_id"]))
        if preview:
            fields = ("price_fen", "want_count") if row["platform"] == "xianyu" else ("price_fen", "raw_sales")
            displayed = {field: preview[field] for field in fields
                         if item[field] is None and preview[field] is not None
                         and (field != "raw_sales" or item["exact_sales"] is None)}
            if displayed:
                item.update(displayed)
                item["catalog_observed_at"] = preview["observed_at"]
        items.append(item)
    if sort_displayed:
        def sort_key(item):
            value = item["price_fen"] if sort_field == "price" else displayed_metric_value(item)
            return item["kind"] != "product", value is None, (value or 0) * (1 if ascending else -1)

        items.sort(key=sort_key)
        items = items[offset:offset + limit]
    return {"total": total, "items": items}


def displayed_metric_value(item):
    if item["platform"] == "xianyu":
        return item["want_count"] if item["kind"] == "product" else item["shop_active_count"]
    if item["raw_sales"] is None:
        return item["exact_sales"]
    text = normalize("NFKC", item["raw_sales"]).replace(",", "").strip()
    match = re.fullmatch(r"(?:已售\s*)?([0-9]+(?:\.[0-9]+)?)\s*(万?)\s*\+?\s*[件单]?", text)
    if match:
        return Decimal(match[1]) * (10000 if match[2] else 1)
    return None


@app.post("/api/v1/targets/{target_id}/review-sampling")
def review_sampling(target_id: int, body: SamplingReview):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = get_target(db, target_id)
        job = db.execute("SELECT slot,state FROM jobs WHERE target_id=? ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
        if (target["kind"] != "product" or not target["enabled"] or not job or job["slot"] != body.slot
                or job["state"] not in ("failed", "unavailable")):
            return {"reviewed": False}
        db.execute("UPDATE targets SET reviewed_sampling_slot=? WHERE id=?", (body.slot, target_id))
    return {"reviewed": True}


@app.patch("/api/v1/targets/{target_id}")
def update_target(target_id: int, body: TargetPatch):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = get_target(db, target_id)
        title = body.title.strip() if body.title is not None else target["title"]
        if not title:
            raise HTTPException(422, "名称不能为空")
        enabled = target["enabled"] if body.enabled is None else body.enabled
        active_since = stamp(now()) if enabled and not target["enabled"] else target["active_since"]
        category = target["category"]
        if "category" in body.model_fields_set:
            category = (body.category or "").strip() or None
        db.execute("UPDATE targets SET title=?,enabled=?,active_since=?,category=? WHERE id=?",
                   (title, enabled, active_since, category, target_id))
        if not enabled:
            db.execute("UPDATE jobs SET state='canceled' WHERE target_id=? AND state='queued'", (target_id,))
        return dict(get_target(db, target_id))


@app.delete("/api/v1/targets/{target_id}")
def delete_target(target_id: int):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = db.execute("SELECT deleted_at FROM targets WHERE id=?", (target_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "目标不存在")
        if target["deleted_at"] is None:
            db.execute("UPDATE targets SET deleted_at=?, enabled=0 WHERE id=?", (stamp(now()), target_id))
            db.execute("UPDATE jobs SET state='canceled' WHERE target_id=? AND state='queued'", (target_id,))
    return {"id": target_id, "deleted": True}


@app.post("/api/v1/targets/{target_id}/readings", status_code=201)
def add_reading(target_id: int, body: Reading):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = get_target(db, target_id)
        if body.want_count is not None and (target["platform"] != "xianyu" or target["kind"] != "product"):
            raise HTTPException(422, "想要人数只适用于闲鱼商品")
        if body.shop_catalog is not None and target["kind"] != "shop":
            raise HTTPException(422, "商品清单只适用于店铺")
        return {"id": record(db, target, body, now(), "manual")}


@app.post("/api/v1/targets/{target_id}/readings/visible-text", status_code=201)
def add_visible_reading(target_id: int, body: VisibleReading):
    try:
        observed = xianyu_visible_reading(body.text)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        target = get_target(db, target_id)
        if target["platform"] != "xianyu" or target["kind"] != "product":
            raise HTTPException(422, "此入口只用于闲鱼商品详情文字")
        snapshot_id = record(db, target, observed, now(), "manual")
    return {"id": snapshot_id, "price_fen": observed.price_fen, "want_count": observed.want_count}


@app.post("/api/v1/targets/{target_id}/refresh")
def refresh_target(target_id: int):
    with database() as db:
        target = dict(get_target(db, target_id))
    if not platforms.target_configured(target["platform"], target["kind"]):
        raise HTTPException(409, "该平台尚未配置自动数据源")
    try:
        reading = Reading.model_validate(platforms.observe(target))
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error
    except ValueError as error:
        raise HTTPException(502, f"数据源没有返回可核对指标：{error}"[:300]) from error
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        live_target = get_target(db, target_id)
        if live_target["active_since"] != target["active_since"]:
            raise HTTPException(409, "监控状态已变化，请重新采样")
        snapshot_id = record(db, live_target, reading, now(), "refresh")
        return {"id": snapshot_id}


@app.get("/api/v1/targets/{target_id}/series")
def target_series(target_id: int, limit: int = Query(100, ge=1, le=1000)):
    with database() as db:
        target = dict(get_target(db, target_id))
        catalogs = recent_catalogs(db, target_id) if target["kind"] == "shop" else []
        preview = db.execute(
            "SELECT metrics,observed_at FROM import_readings WHERE target_id=?", (target_id,),
        ).fetchone()
        if target["kind"] == "product" and not target["shop_name"]:
            store = db.execute(
                """SELECT t.title FROM shop_catalogs c JOIN targets t ON t.id=c.target_id,
                          json_each(c.items) p
                   WHERE t.platform=? AND json_extract(p.value,'$.id')=?
                   ORDER BY c.snapshot_id DESC LIMIT 1""",
                (target["platform"], target["external_id"]),
            ).fetchone()
            if store:
                target["shop_name"] = store["title"]
        observation = observation_times(db, target_id)
        if preview and (not observation["last_fetched_at"] or preview["observed_at"] > observation["last_fetched_at"]):
            observation["last_fetched_at"] = preview["observed_at"]
        hours = observation_jobs(db, target_id, min(max(limit, 3), 168))
        categories = [row["category"] for row in db.execute(
            "SELECT DISTINCT category FROM targets WHERE deleted_at IS NULL AND category IS NOT NULL ORDER BY category",
        )]
        return {"target": target, "items": series(db, target_id, limit), "catalogs": catalogs,
                "preview": (json.loads(preview["metrics"]) | {"observed_at": preview["observed_at"]})
                if preview else None, "observation": observation, "hours": hours[:limit],
                "three_hour_delta": three_hour_delta(hours), "categories": categories,
                "days": daily_observations(db, target)}


@app.get("/api/v1/targets/{target_id}/products")
def shop_products(
    target_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    with database() as db:
        target = get_target(db, target_id)
        if target["kind"] != "shop":
            raise HTTPException(404, "该目标暂无店铺商品子页")
        catalogs = recent_catalogs(db, target_id)
        latest = catalogs[0] if catalogs else None
        total = len(latest["items"]) if latest else 0
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        offset = (page - 1) * page_size
        products = []
        summary = None
        if latest:
            items = latest["items"][offset:offset + page_size]
            products = catalog_products(db, latest | {"items": items}, target["platform"])
            summary = {key: latest[key] for key in ("active_count", "observed_at", "source")}
            summary["has_more"] = bool(latest["has_more"])
            summary["complete"] = catalog_complete(latest)
        observation = observation_times(db, target_id)
    return {
        "target": dict(target),
        "latest": summary,
        "observation": observation,
        "comparison": catalog_comparison(catalogs),
        "products": products,
        "pagination": {"page": page, "page_size": page_size, "page_count": page_count,
                       "total": total, "offset": offset},
    }


def catalog_complete(catalog):
    return not catalog["has_more"] and len(catalog["items"]) == catalog["active_count"]


def catalog_comparison(catalogs):
    if len(catalogs) < 2:
        return None
    latest, previous = catalogs
    if latest["source"] != previous["source"]:
        return None
    result = {"active_change": latest["active_count"] - previous["active_count"]}
    if all(catalog_complete(catalog) for catalog in (latest, previous)):
        latest_ids = {item["id"] for item in latest["items"]}
        previous_ids = {item["id"] for item in previous["items"]}
        result.update(added=len(latest_ids - previous_ids), removed=len(previous_ids - latest_ids))
    return result


@app.get("/api/v1/targets/{target_id}/catalog.csv")
def target_catalog_csv(target_id: int):
    with database() as db:
        target = get_target(db, target_id)
        catalogs = recent_catalogs(db, target["id"])
    if not catalogs:
        raise HTTPException(404, "暂无商品链接")
    return Response(export_catalog(catalogs[0], target["platform"]), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=shop-{target_id}-products.csv"})


@app.get("/api/v1/targets/{target_id}/series.csv")
def target_csv(target_id: int):
    with database() as db:
        get_target(db, target_id)
        return Response(export_series(db, target_id, 10000), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=target-{target_id}.csv"})


@app.get("/api/v1/events")
def events():
    with database() as db:
        rows = db.execute(
            """SELECT e.*,t.platform,t.title FROM events e JOIN targets t ON t.id=e.target_id
               WHERE t.deleted_at IS NULL ORDER BY e.id DESC LIMIT 100"""
        )
        return {"items": [dict(row) | {"matched": json.loads(row["matched"])} for row in rows]}


@app.get("/api/v1/rules")
def rules():
    with database() as db:
        return read_rules(db)


@app.put("/api/v1/rules")
def save_rules(body: Rules):
    with database() as db:
        db.execute("UPDATE rules SET settings=? WHERE id=1", (body.model_dump_json(),))
    return body


@app.get("/api/v1/ops/runs")
def runs():
    with database() as db:
        jobs = db.execute(
            """SELECT j.*,t.platform,t.title FROM jobs j JOIN targets t ON t.id=j.target_id
               ORDER BY j.id DESC LIMIT 100"""
        )
        deliveries = db.execute("SELECT id,channel,state,error FROM deliveries ORDER BY id DESC LIMIT 100")
        return {"jobs": [dict(row) for row in jobs], "deliveries": [dict(row) for row in deliveries]}

