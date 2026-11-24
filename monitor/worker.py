import base64
import hashlib
import hmac
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from .models import Reading
from .platforms import NoRedirect, observe, target_configured
from .storage import CST, database, now, record, stamp

logger = logging.getLogger(__name__)
WINDOW = timedelta(minutes=45)


def unavailable_note(platform, kind):
    """Explain why a target has no automatic source yet, in user-facing words."""
    return "尚未接入自动数据源"


def cycle():
    instant = now()
    local = instant.astimezone(CST)
    slot = local.replace(minute=0, second=0, microsecond=0)
    stamp_now = stamp(instant)
    with database() as db:
        db.execute(
            "UPDATE jobs SET state='missed' WHERE state='queued' AND slot<=?",
            (stamp(instant - WINDOW),),
        )
        if instant - slot < WINDOW:
            slot_stamp = stamp(slot)
            for platform in ("xiaohongshu", "xianyu"):
                for kind in ("product", "shop"):
                    available = target_configured(platform, kind)
                    db.execute(
                        """INSERT OR IGNORE INTO jobs(target_id,slot,state,next_at,error)
                           SELECT id,?,?,?,? FROM targets WHERE enabled=1 AND platform=? AND kind=?
                             AND active_since<=? AND deleted_at IS NULL""",
                        (slot_stamp, "queued" if available else "unavailable", stamp_now,
                         None if available else unavailable_note(platform, kind),
                         platform, kind, slot_stamp),
                    )
                    if available:
                        db.execute(
                            """UPDATE jobs SET state='queued',next_at=?,error=NULL
                               WHERE slot=? AND state='unavailable' AND target_id IN (
                                 SELECT id FROM targets WHERE platform=? AND kind=? AND enabled=1
                                   AND deleted_at IS NULL AND active_since<=?)""",
                            (stamp_now, slot_stamp, platform, kind, slot_stamp),
                        )
        jobs = [dict(row) for row in db.execute(
            """SELECT j.id AS job_id,j.slot,j.attempts,t.* FROM jobs j
               JOIN targets t ON t.id=j.target_id WHERE j.state='queued' AND t.enabled=1
               AND t.deleted_at IS NULL AND j.next_at<=? ORDER BY j.slot,j.id LIMIT 24""",
            (stamp_now,),
        )]
    if jobs:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run_job, jobs))
    queue_deliveries()
    send_pending()


def run_job(job):
    slot = datetime.fromisoformat(job["slot"])
    if now() - slot >= WINDOW:
        return
    try:
        reading = Reading.model_validate(observe(job))
    except RuntimeError as error:
        failure(job, str(error))
        return
    except ValueError as error:
        # 保留字段级原因，便于判断是数据源结构变化还是读数缺失。
        failure(job, f"数据源没有返回可核对指标：{error}"[:300])
        return
    instant = now()
    if instant - slot >= WINDOW:
        failure(job, "已超过采样时窗")
        return
    with database() as db:
        claimed = db.execute(
            """UPDATE jobs SET state='success', error=NULL WHERE id=? AND state='queued'
               AND EXISTS(SELECT 1 FROM targets WHERE id=? AND enabled=1 AND active_since<=?)""",
            (job["job_id"], job["id"], job["slot"]),
        ).rowcount
        if claimed:
            record(db, job, reading, instant, "scheduled", slot)


def failure(job, message):
    attempts = job["attempts"] + 1
    restricted = "访问受限" in message
    retry_at = now() + timedelta(seconds=180 if restricted else 25 * 2**attempts)
    terminal = attempts >= 3 or retry_at - datetime.fromisoformat(job["slot"]) >= WINDOW
    with database() as db:
        db.execute(
            """UPDATE jobs SET state=?, attempts=?, next_at=?, error=?
               WHERE id=? AND state='queued' AND attempts=?""",
            ("failed" if terminal else "queued", attempts, stamp(retry_at), message,
             job["job_id"], job["attempts"]),
        )


def channels():
    return {
        channel: url for channel, url in {
            "dingtalk": os.getenv("MONITOR_DINGTALK_WEBHOOK"),
            "wecom": os.getenv("MONITOR_WECOM_WEBHOOK"),
        }.items() if url
    }


def queue_deliveries():
    configured = channels()
    if not configured:
        return
    with database() as db:
        groups = db.execute(
            """SELECT DISTINCT s.slot FROM events e JOIN snapshots s ON s.id=e.snapshot_id
               WHERE e.notify=1 AND s.slot>=? AND s.slot<=?
               AND EXISTS(SELECT 1 FROM jobs j WHERE j.slot=s.slot)
               AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.slot=s.slot AND j.state='queued')""",
            (stamp(now() - timedelta(days=1)), stamp(now() - WINDOW)),
        ).fetchall()
        for group in groups:
            slot = group["slot"]
            rows = db.execute(
                """SELECT t.title,t.url,e.summary,e.matched FROM events e JOIN targets t ON t.id=e.target_id
                   JOIN snapshots s ON s.id=e.snapshot_id WHERE s.slot=? AND e.notify=1 ORDER BY e.id""", (slot,),
            ).fetchall()
            local = datetime.fromisoformat(slot).astimezone(CST).strftime("%m-%d %H:%M")
            heading = f"链接监控 {local}"
            for index, body in enumerate(batch_messages(rows, heading)):
                for channel in configured:
                    db.execute(
                        "INSERT OR IGNORE INTO deliveries(channel,group_key,body,state) VALUES (?,?,?,'queued')",
                        (channel, f"{slot}/{index}", body),
                    )


def batch_messages(rows, heading):
    body = heading
    for row in rows:
        names = "、".join(json.loads(row["matched"]))
        entry = f"\n\n{row['title'][:100]}\n{row['summary']}\n规则：{names}\n{row['url'][:500]}"
        if len((body + entry).encode()) > 1800 and body != heading:
            yield body
            body = heading
        body += entry
    if body != heading:
        yield body


def send_pending():
    configured = channels()
    with database() as db:
        pending = [dict(row) for row in db.execute(
            "SELECT * FROM deliveries WHERE state='queued' ORDER BY id LIMIT 8"
        )]
    for delivery in pending:
        channel = delivery["channel"]
        if channel not in configured:
            continue
        with database() as db:
            claimed = db.execute(
                "UPDATE deliveries SET state='unknown' WHERE id=? AND state='queued'", (delivery["id"],)
            ).rowcount
        if not claimed:
            continue
        try:
            send(channel, configured[channel], delivery["body"])
        except Exception as error:
            status, message = "unknown", f"渠道未确认送达（{type(error).__name__}）"
        else:
            status, message = "sent", None
        with database() as db:
            db.execute("UPDATE deliveries SET state=?,error=? WHERE id=?",
                       (status, message, delivery["id"]))


def send(channel, url, body):
    parts = urlsplit(url)
    expected = "oapi.dingtalk.com" if channel == "dingtalk" else "qyapi.weixin.qq.com"
    if parts.scheme != "https" or parts.hostname != expected or parts.username or parts.password:
        raise ValueError("通知地址无效")
    secret = os.getenv("MONITOR_DINGTALK_SECRET") if channel == "dingtalk" else None
    if secret:
        timestamp = str(int(now().timestamp() * 1000))
        digest = hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
        signed = parse_qsl(parts.query) + [("timestamp", timestamp), ("sign", base64.b64encode(digest).decode())]
        url = urlunsplit(parts._replace(query=urlencode(signed)))
    payload = json.dumps({"msgtype": "text", "text": {"content": body}}).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=6) as response:
        result = json.loads(response.read(8192))
    if result.get("errcode") != 0:
        raise RuntimeError("渠道拒绝消息")

