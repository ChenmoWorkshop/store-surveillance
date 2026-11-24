import json
import os
import re
from dataclasses import dataclass
from html import unescape
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import xianyu_product, xianyu_shop, xhs_product, xhs_public

LONG_HOSTS = {
    "www.goofish.com": "xianyu", "goofish.com": "xianyu", "2.taobao.com": "xianyu",
    "h5.m.goofish.com": "xianyu", "pages.goofish.com": "xianyu",
    "www.xiaohongshu.com": "xiaohongshu", "xiaohongshu.com": "xiaohongshu",
    "www.xiaohongshu.com.cn": "xiaohongshu",
}
SHORT_HOSTS = {"xhslink.com", "www.xhslink.com", "m.tb.cn"}
ALLOWED_HOSTS = LONG_HOSTS.keys() | SHORT_HOSTS
URL_PATTERN = re.compile(r"https?://[^\s<>\x22\x27，。；！？、【】《》（）]+", re.I)

TAOBAO_LANDING_URL = re.compile(
    r"""(?m)^\s*(?:var|let|const)\s+url\s*=\s*(["'])(https://[^\s"'<>]+)\1\s*;"""
)


@dataclass(frozen=True)
class Target:
    platform: str
    kind: str
    external_id: str
    url: str


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_once(url, headers=None, *, data=None):
    opener = build_opener(NoRedirect())
    return opener.open(Request(url, data=data, headers=headers or {}), timeout=6)


def checked_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
        or parts.username or parts.password or parts.port not in (None, 443)
    ):
        raise ValueError("请使用识别范围内的 HTTPS 平台链接")
    return parts


def resolve(url):
    for _ in range(5):
        parts = checked_url(url)
        if parts.hostname not in SHORT_HOSTS:
            return parse_target(parts)
        try:
            with open_once(url) as response:
                if parts.hostname == "m.tb.cn":
                    return xianyu_landing_target(response)
                raise ValueError("短链未返回商品或店铺跳转，请粘贴展开后的链接")
        except HTTPError as error:
            with error:
                location = error.headers.get("Location")
                if error.code not in (301, 302, 303, 307, 308) or not location:
                    raise ValueError("短链暂时无法展开，请粘贴展开后的链接") from error
            url = urljoin(url, location)
    raise ValueError("短链跳转次数超过限制")


def xianyu_landing_target(response):
    # Read the destination declared by the share page without running its scripts.
    body = response.read(65537)
    if len(body) <= 65536:
        match = TAOBAO_LANDING_URL.search(body.decode("utf-8", errors="replace"))
        if match:
            parts = checked_url(unescape(match[2]))
            if LONG_HOSTS.get(parts.hostname) == "xianyu":
                return parse_target(parts)
    raise ValueError("短链网页中没有可识别的闲鱼链接，请粘贴展开后的链接")


def parse_target(parts):
    platform = LONG_HOSTS[parts.hostname]
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)
    kind = "product"
    external_id = None
    if platform == "xianyu":
        if parts.hostname == "pages.goofish.com":
            accounts = query.get("userid", [])
            if path != "/sharexy" or len(accounts) != 1 or not re.fullmatch(r"[0-9]{5,24}", accounts[0]):
                raise ValueError("未识别到闲鱼分享页的卖家 ID")
            return Target(platform, "shop", accounts[0], f"https://www.goofish.com/personal?userId={accounts[0]}")
        if path == "/item":
            ids = set(query.get("id", []) + query.get("itemId", []))
            if len(ids) == 1:
                external_id = ids.pop()
        elif path in ("/personal", "/seller"):
            kind = "shop"
            external_id = next((query[key][0] for key in ("userId", "sellerId") if key in query), None)
        pattern = r"[A-Za-z0-9_-]{2,64}" if kind == "shop" else r"\d{5,24}"
    else:
        product = re.fullmatch(r"/(?:goods-detail|item|goods)/([\w-]+)", path)
        shop = re.fullmatch(r"/(?:shop|store)/([\w-]+)", path)
        if product:
            external_id = product[1]
        elif shop:
            kind, external_id = "shop", shop[1]
        elif path in ("/goods-detail", "/goods", "/shop", "/store"):
            kind = "shop" if path in ("/shop", "/store") else "product"
            external_id = query.get("sellerId" if kind == "shop" else "itemId", [None])[0]
        pattern = r"[A-Za-z0-9_-]{6,64}"
    if not external_id or not re.fullmatch(pattern, external_id):
        raise ValueError("未识别到商品或店铺 ID，请检查链接类型或使用完整链接")
    if platform == "xianyu" and kind == "product":
        return Target(platform, kind, external_id, f"https://www.goofish.com/item?id={external_id}")
    # Keep platform-specific query values that may be needed to open the link.
    safe_url = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    return Target(platform, kind, external_id, safe_url)


def links(text):
    normalized = text.replace(r"https\://", "https://").replace(r"http\://", "http://")
    return list(dict.fromkeys(url.rstrip(").]\\") for url in URL_PATTERN.findall(normalized)))


def shared_titles(text):
    """Extract candidate names only from unambiguous platform share blocks."""
    titles = {}
    blocks = re.split(r"(【闲鱼】|【小红书】|推荐小红书好店)", text)
    for stamp, block in zip(blocks[1::2], blocks[2::2]):
        urls = links(block)
        if len(urls) != 1:
            continue
        if stamp == "【闲鱼】":
            listing = re.search(r"「(?:快来捡漏|我在闲鱼发布了)【([^】]+)】」", block)
            shop = re.search(r"「([^」]+)的闲鱼号[，,]", block)
            if listing:
                titles[urls[0]] = ("xianyu", "product", listing[1].strip()[:160])
            elif shop:
                titles[urls[0]] = ("xianyu", "shop", shop[1].strip()[:160])
        elif stamp == "【小红书】":
            listing = re.match(r"\s*([^\r\n😆]+?)\s*😆\s*[A-Za-z0-9]+\s*😆", block)
            if listing:
                titles[urls[0]] = ("xiaohongshu", "product", listing[1].strip()[:160])
        else:
            shop = re.match(r"\s*([^，,\r\n]+)[，,]\s*\d+件商品", block)
            if shop:
                titles[urls[0]] = ("xiaohongshu", "shop", shop[1].strip()[:160])
    return titles


def provider_configured(platform):
    return bool(os.getenv(f"MONITOR_{platform.upper()}_ENDPOINT"))


def target_configured(platform, kind):
    return provider_configured(platform) or (
        xhs_public.supported(platform, kind) or xhs_product.supported(platform, kind)
        or xianyu_product.supported(platform, kind) or xianyu_shop.supported(platform, kind)
    )


def observe_public_shop(target):
    seller_id = target["external_id"]
    if not xhs_public.PRODUCT_ID.fullmatch(seller_id):
        raise RuntimeError("公开采样需要小红书店铺 ID")
    canonical = f"https://www.xiaohongshu.com/shop/{seller_id}"
    parts = urlsplit(target["url"])
    saved = target["url"] if (
        parts.scheme == "https" and parts.netloc == "www.xiaohongshu.com"
        and parts.path == f"/shop/{seller_id}"
    ) else canonical
    message = "公开店铺页暂未返回商品清单"
    for url in (saved, canonical):
        try:
            with open_once(url, {"Accept": "text/html"}) as response:
                body = response.read(1000001)
            if len(body) > 1000000:
                raise ValueError("公开店铺页超出读取上限")
            reading = xhs_public.parse_catalog(body.decode("utf-8"), seller_id)
            return xhs_public.collect_catalog(reading, xhs_public.fetch_shop_page)
        except HTTPError as error:
            with error:
                if error.code in (401, 403, 429):
                    raise RuntimeError(f"公开店铺页访问受限（HTTP {error.code}）") from error
                message = f"公开店铺页返回 HTTP {error.code}"
        except OSError:
            message = "公开店铺页连接失败或超时"
        except (KeyError, TypeError, AttributeError, ValueError):
            message = "公开店铺页暂未返回可核对的商品清单"
    raise RuntimeError(message)


def observe(target):
    platform = target["platform"]
    prefix = f"MONITOR_{platform.upper()}"
    endpoint = os.getenv(f"{prefix}_ENDPOINT")
    if not endpoint:
        if xhs_public.supported(platform, target["kind"]):
            return observe_public_shop(target)
        if xhs_product.supported(platform, target["kind"]):
            return xhs_product.read_product(target)
        if xianyu_product.supported(platform, target["kind"]):
            return xianyu_product.read_product(target)
        if xianyu_shop.supported(platform, target["kind"]):
            return xianyu_shop.read_shop(target)
        raise RuntimeError("待配置数据源")
    # The endpoint is configured by the local operator. User-submitted URLs are data only.
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.fragment:
        raise RuntimeError("数据源地址配置无效")
    params = urlencode({k: target[k] for k in ("platform", "kind", "external_id", "url")})
    token = os.getenv(f"{prefix}_TOKEN")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = urlunsplit(parts._replace(query="&".join(filter(None, (parts.query, params)))))
    try:
        with open_once(url, headers) as response:
            payload = response.read(65537)
            if len(payload) > 65536:
                raise ValueError("response too large")
            value = json.loads(payload)
    except HTTPError as error:
        with error:
            if error.code in (401, 403, 429):
                raise RuntimeError(f"数据源访问受限（HTTP {error.code}）") from error
            raise RuntimeError(f"数据源返回 HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise RuntimeError("数据源无法访问或返回格式错误") from error
    if not isinstance(value, dict) or str(value.get("external_id")) != target["external_id"]:
        raise RuntimeError("数据源返回的目标 ID 不匹配")
    return value
