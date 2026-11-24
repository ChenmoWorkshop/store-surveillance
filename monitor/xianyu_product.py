"""Read the public metrics of a Xianyu listing through the H5 detail interface.

The mobile detail page renders without an account, and the JSON it requests
(`mtop.taobao.idle.awesome.detail`) is reachable over plain HTTPS. The mtop
gateway hands out an anonymous token on the first call and requires it in the
`_m_h5_tk` cookie on the second, so one read costs two requests.
"""

import hashlib
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

DETAIL_API = "mtop.taobao.idle.awesome.detail"
APP_KEY = "12574478"
# 电脑版网页用的 appKey，店铺接口在机房网络下可以正常调用，商品详情也先走它。
PC_APP_KEY = "34839810"
ITEM_ID = re.compile(r"[0-9]{5,24}")
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
STATE_LABELS = {"在线": "active", "已售出": "sold", "已下架": "delisted"}


class TokenMissing(RuntimeError):
    """The gateway answered without a usable anonymous token."""


def supported(platform, kind):
    return platform == "xianyu" and kind == "product"


class Session:
    def __init__(self, timeout=8):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def token(self):
        for cookie in self.jar:
            if cookie.name == "_m_h5_tk":
                return cookie.value.split("_")[0]
        return ""

    def call(self, data):
        body = json.dumps(data, separators=(",", ":"))
        url = detail_url(self.token(), body)
        request = urllib.request.Request(
            url, data=("data=" + urllib.parse.quote(body)).encode(),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://h5.m.goofish.com/",
                "Origin": "https://h5.m.goofish.com",
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read(2_000_001))


def _ret(payload):
    ret = payload.get("ret")
    return ret[0] if isinstance(ret, list) and ret else str(ret or "")


def detail_url(token, body, app_key=APP_KEY):
    """按 mtop 规则拼出一次详情请求的地址（sign = md5(token&t&appKey&data)）。"""
    instant = str(int(time.time() * 1000))
    sign = hashlib.md5(f"{token}&{instant}&{APP_KEY}&{body}".encode()).hexdigest()
    query = urllib.parse.urlencode({
        "jsv": "2.7.2", "appKey": app_key, "t": instant, "sign": sign,
        "v": "1.0", "type": "originaljson", "dataType": "json", "timeout": "20000",
        "api": DETAIL_API, "accountSite": "xianyu",
    })
    return f"https://h5api.m.goofish.com/h5/{DETAIL_API}/1.0/?{query}"


def parse_detail(payload, item_id):
    ret = _ret(payload)
    # 网关异常的状态文案顺序不固定（RGV587 / FAIL_SYS_USER_VALIDATE / SESSION_EXPIRED 等），
    # 这里只认明确的成功，其余统一交给浏览器通道重试。
    if "SUCCESS" not in ret:
        raise RuntimeError(f"闲鱼详情接口要求登录或风控校验：{ret[:100] or '无返回状态'}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("闲鱼详情接口未返回商品数据")
    item = data.get("itemDO")
    if not isinstance(item, dict):
        raise ValueError("闲鱼详情接口未返回商品详情")
    if str(item.get("itemId")) != str(item_id):
        raise ValueError("闲鱼详情接口返回的商品编号不匹配")

    metrics = {}
    want = item.get("wantCnt")
    if isinstance(want, int) and not isinstance(want, bool) and 0 <= want <= 10**12:
        metrics["want_count"] = want

    price = _price_fen(item)
    if price is not None:
        metrics["price_fen"] = price
        promotion = item.get("promotionPriceDO")
        label = promotion.get("promotionName") if isinstance(promotion, dict) else None
        if isinstance(label, str) and label.strip() in ("活动价", "券后价", "到手价"):
            metrics["price_label"] = label.strip()

    title = item.get("title")
    if isinstance(title, str) and title.strip():
        metrics["official_title"] = title.strip()[:2048]

    state = item.get("itemStatusStr")
    if isinstance(state, str) and state.strip() in STATE_LABELS:
        metrics["listing_state"] = STATE_LABELS[state.strip()]

    cover = _cover_url(item)
    if cover:
        metrics["cover_url"] = cover
    if not metrics.get("price_fen") and "want_count" not in metrics:
        raise ValueError("闲鱼详情未显示价格或想要数")
    return metrics


def _price_fen(item):
    candidates = []
    promotion = item.get("promotionPriceDO")
    if isinstance(promotion, dict):
        candidates.append(promotion.get("promotionPriceMin"))
    candidates.append(item.get("soldPrice"))
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", text):
            continue
        try:
            amount = Decimal(text)
        except InvalidOperation:
            continue
        fen = int(amount * 100)
        if 0 <= fen <= 10**10:
            return fen
    return None


def _cover_url(item):
    images = item.get("imageInfos")
    if not isinstance(images, list):
        return ""
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            continue
        # 闲鱼给的是 http 绝对地址，站内统一按 https 存。
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not url.startswith("https://"):
            continue
        parts = urllib.parse.urlsplit(url)
        if parts.hostname and parts.hostname.endswith(".alicdn.com"):
            return url
    return ""


def read_browser_product(target):
    """Open the mobile detail page and read the same interface from the browser.

    Datacenter IPs are often answered with ``RGV587_ERROR`` by the mtop gateway;
    a real browser context carries the fingerprint cookies (``isg``/``bx-ua``/
    ``tfstk``) that the plain HTTPS path cannot produce.
    """
    item_id = target["external_id"]
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as error:
        raise RuntimeError("闲鱼公开商品读取组件尚未安装") from error
    from . import xy_browser

    url = f"https://h5.m.goofish.com/item?id={item_id}"
    found = []

    def capture(response):
        if "awesome.detail" not in response.url:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if "SUCCESS" in _ret(payload):
            if not found:
                found.append(payload)

    try:
        with sync_playwright() as engine:
            browser = xy_browser.launch(engine)
            try:
                context = xy_browser.new_context(browser, mobile=True)
                page = context.new_page()
                page.on("response", capture)
                # 先过一遍首页，让 JS 把 isg / tfstk 这些指纹 cookie 算出来；
                # 机房里直接访问详情页时，网关常因缺少指纹直接返回风控页。
                try:
                    page.goto("https://h5.m.goofish.com/", wait_until="domcontentloaded", timeout=12000)
                    page.wait_for_timeout(700)
                except (TimeoutError, Error):
                    pass
                page.goto(url, wait_until="domcontentloaded", timeout=18000)
                for _ in range(14):
                    if found:
                        break
                    page.wait_for_timeout(500)
                if found:
                    return parse_detail(found[0], item_id)
                # 页面没触发详情请求（风控页 / 脚本未执行）时，用页面算好的 cookie 指纹自己发一次。
                # 先试电脑版的 appKey（店铺接口用的就是它，机房下更宽松），再回落到 H5 版。
                statuses = {}
                for app_key in (PC_APP_KEY, APP_KEY):
                    payload = _context_call(context, item_id, app_key)
                    if isinstance(payload, dict):
                        return parse_detail(payload, item_id)
                    statuses[app_key] = payload
                raise RuntimeError(_risk_message(statuses))
            finally:
                browser.close()
    except TimeoutError as error:
        raise RuntimeError("闲鱼公开商品页加载超时，未取得本次读数") from error
    except Error as error:
        raise RuntimeError(f"闲鱼公开商品页读取失败：{str(error)[:80]}") from error
    except ValueError as error:
        raise RuntimeError(f"闲鱼公开商品返回的数据不可用：{str(error)[:80]}") from error


def _context_call(context, item_id, app_key):
    """在浏览器上下文里直接请求详情接口，复用页面算好的 isg/tfstk 指纹。

    成功返回接口 JSON，失败返回网关的状态文案，交给调用方汇总。
    """
    body = json.dumps({"itemId": item_id}, separators=(",", ":"))
    last = ""
    for _ in range(2):
        token = ""
        for cookie in context.cookies("https://h5.m.goofish.com"):
            if cookie["name"] == "_m_h5_tk":
                token = cookie["value"].split("_")[0]
                break
        response = context.request.post(
            detail_url(token, body, app_key), form={"data": body}, timeout=15000,
            headers={"Referer": f"https://h5.m.goofish.com/item?id={item_id}",
                     "Origin": "https://h5.m.goofish.com"},
        )
        try:
            payload = response.json()
        except Exception:
            return "接口未返回 JSON"
        status = _ret(payload)
        if "SUCCESS" in status:
            return payload
        last = status
        if "TOKEN_EMPTY" not in status:
            break
    return last or "无返回状态"


def _risk_message(statuses):
    blob = " ".join(statuses.values())
    if "RGV587" in blob or "USER_VALIDATE" in blob or "punish" in blob:
        return "闲鱼在机房网络被风控拦截（RGV587）；如需云端采集，请为服务配置 MONITOR_XIANYU_PROXY 出口代理"
    detail = "；".join(f"{key}: {value[:60]}" for key, value in statuses.items())
    return f"闲鱼详情接口要求登录或风控校验：{detail}"


def read_product(target):
    item_id = target["external_id"]
    if not ITEM_ID.fullmatch(item_id):
        raise RuntimeError("闲鱼商品编号无效")
    session = Session()
    payload = None
    try:
        payload = session.call({"itemId": item_id})
        if "TOKEN_EMPTY" in _ret(payload):
            payload = session.call({"itemId": item_id})
        return parse_detail(payload, item_id)
    except ValueError as error:
        status = _ret(payload)[:80] if isinstance(payload, dict) else ""
        raise RuntimeError(f"闲鱼详情读取失败：{status or '无返回状态'}") from error
    except RuntimeError:
        # 风控拦截（RGV587）、需要登录态或页面结构变化时，改用浏览器上下文再试一次。
        return read_browser_product(target)
