import json
import re
from decimal import Decimal, InvalidOperation

SERVER_STATE = re.compile(r"window\.__SETUP_SERVER_STATE__\s*=\s*")
PRODUCT_ID = re.compile(r"[a-f0-9]{24}")
SALES_TEXT = re.compile(r"已售\s*[0-9][0-9,.]*(?:万)?\+?(?:件|单)?")


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

    goods = next((section["componentData"] for section in sections
                  if section.get("componentName") == "DefaultMixCardComponent"), {})
    total = goods.get("total")
    if type(total) is not int or not 0 <= total <= 1000000:
        raise ValueError("公开店铺页没有返回可核对的商品总数")
    visible = {}
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
        visible[item_id] = {"id": item_id, "title": title.strip()[:200],
                            "price_fen": fen, "raw_sales": sales}
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
                         "has_more": goods.get("hasMore") if type(goods.get("hasMore")) is bool else None},
    }


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
