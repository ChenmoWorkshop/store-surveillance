import json
import re
import time
from decimal import Decimal, InvalidOperation

from .models import MAX_CATALOG_ITEMS

SERVER_STATE = re.compile(r"window\.__SETUP_SERVER_STATE__\s*=\s*")
PRODUCT_ID = re.compile(r"[a-f0-9]{24}")
SALES_TEXT = re.compile(r"已售\s*[0-9][0-9,.]*(?:万)?\+?(?:件|单)?")
SHOP_PAGE_SIZE = 10
SHOP_COMPONENTS_URL = "https://mall.xiaohongshu.com/api/store/guide/components/general/h5"


def supported(platform, kind):
    return platform == "xiaohongshu" and kind == "shop"


def parse_catalog(html, seller_id):
    state = SERVER_STATE.search(html)
    if not state:
        raise ValueError("公开店铺页没有返回商品清单")
    data, _ = json.JSONDecoder().raw_decode(html, state.end())
    sections = data.get("xhs_shop_home_data") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        raise ValueError("公开店铺页没有返回商品清单")

    goods = goods_component(sections)
    total = goods.get("total")
    if type(total) is not int or not 0 <= total <= 1000000:
        raise ValueError("公开店铺页没有返回可核对的商品总数")
    visible = goods_items(goods, seller_id)
    if total and not visible:
        raise ValueError("公开店铺页没有返回属于该店铺的商品")

    entrance = next((section["componentData"] for section in sections
                     if section.get("componentName") == "DefaultShopEntranceComponent"), {})
    seller = next((card for card in entrance.get("componentVOList", [])
                   if (card.get("baseSellerInfo") or {}).get("sellerId") == seller_id), {})
    tags = (seller.get("tagStrategyMap") or {}).get("productTag")
    total_sales = sold_label(tags)
    return {
        "external_id": seller_id,
        "raw_sales": total_sales,
        "shop_catalog": {"active_count": total, "items": list(visible.values()),
                         "has_more": goods.get("hasMore") is True or len(visible) != total},
    }


def goods_component(sections):
    return next((section["componentData"] for section in sections
                 if section.get("componentName") == "DefaultMixCardComponent"), {})


def goods_items(goods, seller_id):
    items = {}
    for card in goods.get("componentVOList") or []:
        goods_card = card.get("defaultGoodsComponentVO") or {}
        info = goods_card.get("baseInfo") or {}
        item_id = info.get("itemId", "")
        title = info.get("title")
        if info.get("sellerId") != seller_id or not isinstance(item_id, str) or not PRODUCT_ID.fullmatch(item_id):
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        tags = goods_card.get("tagStrategyMap") or {}
        sales = sold_label(tags.get("afterPrice"))
        price_info = (goods_card.get("marketingInfo") or {}).get("priceInfo") or {}
        fen = price_in_fen((price_info.get("expectedPrice") or {}).get("priceStr"))
        items[item_id] = {"id": item_id, "title": title.strip()[:200], "price_fen": fen, "raw_sales": sales}
    return items


def fetch_shop_page(seller_id, page, page_size):
    from .platforms import open_once

    # The mall endpoint honors pagination in the desktop shop scene.
    body = json.dumps({
        "biz_identity": {"template_id": "shopH5Page", "biz_type": "shop", "scene": "shop_pc"},
        "component_param": {"mix_card_component_param": [{
            "sort_type": "sales_qty", "filter_param": "", "page_param": {"page": page, "page_size": page_size},
        }]},
        "common_param": {"seller_id": seller_id},
    }).encode("utf-8")
    with open_once(SHOP_COMPONENTS_URL, {
        "Accept": "application/json", "Content-Type": "application/json",
        "Origin": "https://www.xiaohongshu.com", "Referer": f"https://www.xiaohongshu.com/shop/{seller_id}",
    }, data=body) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("小红书店铺商品响应超出读取上限")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("小红书商城未返回商品清单")
    return camel_case(payload["data"]["components"])


def camel_case(value):
    # The public page state is camel case, while mall responses can use either format.
    if isinstance(value, dict):
        return {re.sub(r"_([a-z]+)", lambda m: "VO" if m[1] == "vo" else m[1].capitalize(), key): camel_case(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [camel_case(item) for item in value]
    return value


def collect_catalog(reading, fetch_page):
    catalog = reading["shop_catalog"]
    if not catalog["has_more"]:
        return reading
    seller_id = reading["external_id"]
    total = catalog["active_count"]
    deadline = time.monotonic() + 35
    items = {}
    at_end = False
    for page in range(MAX_CATALOG_ITEMS // SHOP_PAGE_SIZE + 1):
        if (len(items) >= MAX_CATALOG_ITEMS and total > MAX_CATALOG_ITEMS) or time.monotonic() >= deadline:
            break
        try:
            goods = goods_component(fetch_page(seller_id, page, SHOP_PAGE_SIZE))
            received = goods_items(goods, seller_id)
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            break
        if (goods.get("pageParam") or {}).get("page", page + 1) != page + 1:
            break
        if not goods.get("componentVOList") and goods.get("hasMore") is False:
            # The endpoint can keep hasMore=true on the last nonempty page.
            at_end = True
            break
        if goods.get("total") not in (None, total) or not received.keys() - items.keys():
            break
        for key, item in received.items():
            if len(items) >= MAX_CATALOG_ITEMS:
                break
            items.setdefault(key, item)
        # The service has returned hasMore=true even after all reported items were read.
        if goods.get("hasMore") is False or (goods.get("total") == total == len(items)):
            at_end = True
            break

    if len(items) >= len(catalog["items"]):
        catalog["items"] = list(items.values())
        catalog["has_more"] = not at_end or len(items) != total
    return reading


def price_in_fen(text):
    if not isinstance(text, str) or not re.fullmatch(r"¥\s*[0-9]+(?:\.[0-9]{1,2})?", text):
        return None
    try:
        price = int(Decimal(text[1:].strip()) * 100)
    except (InvalidOperation, ValueError):
        return None
    return price if 0 <= price <= 10**10 else None


def sold_label(tags):
    for tag in tags if isinstance(tags, list) else [tags]:
        value = tag.get("content") if isinstance(tag, dict) else None
        if isinstance(value, str) and SALES_TEXT.fullmatch(value):
            return value
    return None
