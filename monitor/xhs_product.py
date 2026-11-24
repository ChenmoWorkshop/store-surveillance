"""Read the visible metrics of a public Xiaohongshu product page."""

import json
import os
import re
import sys
from decimal import Decimal
from urllib.parse import urlencode, urlsplit, urlunsplit

PRODUCT_ID = re.compile(r"[a-f0-9]{24}")
AMOUNT = re.compile(r"¥\s*([0-9]{1,9})(?:\s*\.\s*([0-9]{1,2}))?(?!\s*\.|[0-9])")
SALES_LABEL = re.compile(r"已售\s*[0-9][0-9,.]*(?:万)?\+?(?:件|单)?")


def supported(platform, kind):
    return platform == "xiaohongshu" and kind == "product"


def parse_product_metric(price_text, sales_text, price_label=""):
    metrics = {}
    price = AMOUNT.search(price_text)
    if price:
        value = Decimal(price[1] + ("." + price[2] if price[2] else ""))
        price_fen = int(value * 100)
        if price_fen <= 10**10:
            metrics["price_fen"] = price_fen
            if price_label.strip() in ("券后价", "到手价"):
                metrics["price_label"] = price_label.strip()
    sales = sales_text.strip()
    if SALES_LABEL.fullmatch(sales):
        metrics["raw_sales"] = sales
    if not metrics:
        raise ValueError("公开商品详情未显示价格或销量")
    return metrics


def read_product(target):
    external_id = target["external_id"]
    if not PRODUCT_ID.fullmatch(external_id):
        raise RuntimeError("商品编号无效")
    try:
        result = read_json_product(external_id)
    except (OSError, ValueError) as error:
        try:
            return read_browser_product(target)
        except (RuntimeError, ValueError) as fallback:
            raise RuntimeError(f"小红书详情接口未取得读数；浏览器备用采样：{fallback}") from error
    if "raw_sales" not in result or "price_fen" not in result:
        try:
            fallback = read_browser_product(target)
        except (RuntimeError, ValueError):
            return result
        if "price_fen" in result:
            fallback.pop("price_label", None)
        return fallback | result
    return result


def read_json_product(external_id):
    from .platforms import open_once

    query = urlencode({"version": "0.0.5", "item_id": external_id})
    url = "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc?" + query
    with open_once(url, {"Accept": "application/json"}) as response:
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("小红书商品响应超出读取上限")
    return parse_json_product(json.loads(payload), external_id)


def parse_json_product(payload, external_id):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("小红书接口未返回商品详情")
    data = payload.get("data")
    blocks = data.get("template_data") if isinstance(data, dict) else None
    if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
        raise ValueError("小红书接口未返回商品详情")
    product = blocks[0]

    def section(key):
        value = product.get(key)
        return value if isinstance(value, dict) else {}

    desc = section("descriptionH5")
    main = section("descriptionMain")
    if external_id not in (desc.get("skuId"), main.get("itemId")):
        raise ValueError("小红书接口返回的商品编号不匹配")
    price = section("priceH5")
    deal = price.get("dealPrice")
    deal = deal if isinstance(deal, dict) else {}
    price_label = deal.get("priceText")
    displayed_price = deal.get("price") if price_label in ("券后价", "到手价") else None
    if type(displayed_price) not in (str, int, float):
        displayed_price = price.get("highlightPrice")
        price_label = ""
    price_text = str(displayed_price).strip()
    price_text = "¥" + price_text if re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", price_text) else ""
    sales = next((value for value in (
        price.get("spuAnalysisDataText"), main.get("spuAnalysisDataText"),
        price.get("itemAnalysisDataText"), desc.get("itemAnalysisDataText"),
    ) if isinstance(value, str) and SALES_LABEL.fullmatch(value.strip())), "")
    result = parse_product_metric(price_text, sales, price_label)
    for key, value, limit in (("official_title", desc.get("name") or main.get("name"), 2048),
                              ("shop_name", section("sellerH5").get("name"), 256)):
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:limit]
    images = section("carouselH5").get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        source = images[0].get("url")
        if isinstance(source, str) and len(source) <= 2048:
            parts = urlsplit(source)
            if parts.scheme == "https" and parts.hostname and parts.hostname.endswith(".xhscdn.com"):
                result["cover_url"] = source
    return result


def read_browser_product(target):
    seller_host = "www.xiaohongshu.com"
    external_id = target["external_id"]
    parts = urlsplit(target["url"])
    query = parts.query if parts.hostname in (seller_host, "xiaohongshu.com") else ""
    path = f"/goods-detail/{external_id}"
    url = urlunsplit(("https", seller_host, path, query, ""))
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as error:
        raise RuntimeError("小红书公开商品读取组件尚未安装") from error

    channel = os.getenv("MONITOR_BROWSER_CHANNEL") or ("msedge" if sys.platform == "win32" else "chromium")
    try:
        with sync_playwright() as engine:
            browser = engine.chromium.launch(channel=channel, timeout=8000)
            try:
                page = browser.new_page(locale="zh-CN")
                page.goto(url, wait_until="domcontentloaded", timeout=8500)
                card = page.locator(".basic-container .price-container").first
                card.wait_for(state="visible", timeout=7000)
                final = urlsplit(page.url)
                if final.hostname != seller_host or final.path.rstrip("/") != path:
                    raise RuntimeError("小红书没有返回目标商品详情")
                prices = card.locator(".price-left .price").all_inner_texts()
                sales = card.locator(".price-right .spu-text").all_inner_texts()
                labels = card.locator(".price-in-hands_text").all_inner_texts()
                result = parse_product_metric(prices[0] if prices else "", sales[0] if sales else "",
                                              labels[0] if labels else "")
                for key, selector in (
                    ("official_title", ".basic-container .goods-name"),
                    ("shop_name", ".seller-container .seller-name"),
                ):
                    values = page.locator(selector).all_text_contents()
                    if values and values[0].strip():
                        result[key] = values[0].strip()
                cover = page.locator("img.carousel-image").first
                if cover.count():
                    source = cover.get_attribute("src")
                    parts = urlsplit(source or "")
                    if parts.scheme == "https" and parts.hostname and parts.hostname.endswith(".xhscdn.com"):
                        result["cover_url"] = source
                return result
            finally:
                browser.close()
    except TimeoutError as error:
        raise RuntimeError("小红书公开商品页加载超时，未取得本次读数") from error
    except Error as error:
        raise RuntimeError("小红书公开商品页读取失败") from error
