"""Read a public Xianyu seller page through the desktop web interface.

The desktop storefront (``www.goofish.com/personal?userId=...``) is readable
without an account and exposes two mtop interfaces that carry everything a
store monitor needs: the header (display name, active listing count) and the
paginated listings. Datacenter IPs are risk-controlled on the plain HTTPS
path, so the calls run inside a browser context that already carries the
fingerprint cookies computed by the page itself.
"""

import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation
import urllib.parse

from . import xy_browser
from .models import MAX_CATALOG_ITEMS

PC_APP_KEY = "34839810"
HEAD_API = "mtop.idle.web.user.page.head"
LIST_API = "mtop.idle.web.xyh.item.list"
SELLER_ID = re.compile(r"[0-9]{5,24}")
ITEM_ID = re.compile(r"[0-9]{5,24}")
PAGE_SIZE = 20


def supported(platform, kind):
    return platform == "xianyu" and kind == "shop"


def _ret(payload):
    ret = payload.get("ret")
    return ret[0] if isinstance(ret, list) and ret else str(ret or "")


def _url(api, token, body):
    instant = str(int(time.time() * 1000))
    sign = hashlib.md5(f"{token}&{instant}&{PC_APP_KEY}&{body}".encode()).hexdigest()
    query = urllib.parse.urlencode({
        "jsv": "2.7.2", "appKey": PC_APP_KEY, "t": instant, "sign": sign,
        "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": api,
        "sessionOption": "AutoLoginOnly",
    })
    return f"https://h5api.m.goofish.com/h5/{api}/1.0/?{query}"


def _token(context):
    for cookie in context.cookies("https://www.goofish.com"):
        if cookie["name"] == "_m_h5_tk":
            return cookie["value"].split("_")[0]
    return ""


def _call(context, api, data):
    body = json.dumps(data, separators=(",", ":"))
    last = ""
    for _ in range(2):
        response = context.request.post(
            _url(api, _token(context), body), form={"data": body}, timeout=15000,
            headers={"Referer": "https://www.goofish.com/", "Origin": "https://www.goofish.com"},
        )
        try:
            payload = response.json()
        except Exception as error:
            raise RuntimeError("闲鱼卖家页接口未返回 JSON") from error
        if "SUCCESS" in _ret(payload):
            return payload
        last = _ret(payload)
        if "TOKEN_EMPTY" not in last:
            break
    raise RuntimeError(f"闲鱼卖家页接口要求登录或风控校验：{last[:100] or '无返回状态'}")


def _fen(text):
    if isinstance(text, bool):
        return None
    if not isinstance(text, str):
        text = str(text or "")
    text = text.strip().lstrip("¥").strip()
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", text):
        return None
    try:
        fen = int(Decimal(text) * 100)
    except (InvalidOperation, ValueError):
        return None
    return fen if 0 <= fen <= 10**10 else None


def parse_head(uid, head):
    base_info = (head.get("data") or {}).get("baseInfo") or {}
    if str(base_info.get("kcUserId")) != uid:
        raise ValueError("闲鱼卖家主页返回的账号不匹配")
    module = (head.get("data") or {}).get("module") or {}
    name = ((module.get("base") or {}).get("displayName") or "").strip()
    number = ((module.get("tabs") or {}).get("item") or {}).get("number")
    if type(number) is not int or not 0 <= number <= 10**6:
        raise ValueError("闲鱼卖家主页未返回在售数量")
    return name, number


def listing_items(page):
    items = {}
    for card in page.get("cardList") or []:
        data = card.get("cardData") or {}
        item_id = str(data.get("id") or "")
        title = data.get("title")
        if not ITEM_ID.fullmatch(item_id) or item_id in items:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        price = _fen((data.get("priceInfo") or {}).get("price"))
        if price is None:
            price = _fen((data.get("detailParams") or {}).get("soldPrice"))
        items[item_id] = {"id": item_id, "title": title.strip()[:200], "price_fen": price}
    return items


def listing_query(uid, page, group=None, previous=None):
    query = {"pageNumber": page, "userId": uid, "pageSize": PAGE_SIZE}
    if page == 1 and group is None:
        query["needGroupInfo"] = True
    if group:
        query.update({key: group[key] for key in ("groupId", "groupName", "defaultGroup") if key in group})
    if previous:
        query.update({key: previous[key] for key in ("nextPageModel", "nextPageNum")
                      if previous.get(key) is not None})
    return query


def collect_catalog(uid, head, listing, fetch_page):
    name, number = parse_head(uid, head)
    deadline = time.monotonic() + 35
    page_data = listing["data"]
    groups = page_data.get("itemGroupList") or []
    active_group = next((group for group in groups if group.get("groupName") == "在售"), None)
    group = next((group for group in groups if group.get("groupName") == "综合"), None)
    if active_group and group and group.get("itemNumber") != active_group.get("itemNumber"):
        group = active_group
        page_data = fetch_page(listing_query(uid, 1, group))["data"]
    items = listing_items(page_data)
    if number and not items:
        raise ValueError("闲鱼卖家页未返回在售商品清单")

    for page in range(2, MAX_CATALOG_ITEMS // PAGE_SIZE + 1):
        more = page_data.get("nextPage")
        if more is False or (more is not True and len(items) >= number):
            break
        if len(items) >= MAX_CATALOG_ITEMS or time.monotonic() >= deadline:
            break
        try:
            next_data = fetch_page(listing_query(uid, page, group, page_data))["data"]
            received = listing_items(next_data)
        except (AttributeError, KeyError, TypeError, RuntimeError):
            break
        if not received.keys() - items.keys():
            break
        for key, item in received.items():
            if len(items) >= MAX_CATALOG_ITEMS:
                break
            items.setdefault(key, item)
        page_data = next_data
    return {
        "external_id": uid,
        "shop_name": name or None,
        "shop_catalog": {
            "active_count": number,
            "items": list(items.values()),
            "has_more": page_data.get("nextPage") is True or len(items) != number,
        },
    }


def read_shop(target):
    uid = target["external_id"]
    if not SELLER_ID.fullmatch(uid):
        raise RuntimeError("闲鱼卖家编号无效")
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as error:
        raise RuntimeError("闲鱼卖家页读取组件尚未安装") from error
    captured = {}

    try:
        with sync_playwright() as engine:
            browser = xy_browser.launch(engine)
            try:
                context = xy_browser.new_context(browser)
                page = context.new_page()

                def capture(response):
                    if "h5api.m.goofish.com" not in response.url:
                        return
                    api = response.url.split("/h5/")[1].split("/")[0] if "/h5/" in response.url else ""
                    if api not in (HEAD_API, LIST_API) or api in captured:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    if "SUCCESS" in _ret(payload):
                        captured[api] = payload

                page.on("response", capture)
                page.goto(f"https://www.goofish.com/personal?userId={uid}",
                          wait_until="domcontentloaded", timeout=20000)
                for _ in range(16):
                    if HEAD_API in captured and LIST_API in captured:
                        break
                    page.wait_for_timeout(500)
                head = captured.get(HEAD_API) or _call(context, HEAD_API, {"self": False, "userId": uid})
                def fetch_page(query):
                    try:
                        return _call(context, LIST_API, query)
                    except Error as error:
                        raise RuntimeError("闲鱼卖家商品列表请求失败") from error

                listing = captured.get(LIST_API) or fetch_page(listing_query(uid, 1))
                return collect_catalog(uid, head, listing, fetch_page)
            finally:
                browser.close()
    except TimeoutError as error:
        raise RuntimeError("闲鱼卖家页加载超时，未取得本次读数") from error
    except Error as error:
        raise RuntimeError(f"闲鱼卖家页读取失败：{str(error)[:80]}") from error
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"闲鱼卖家页返回的数据不可用：{str(error)[:80]}") from error
