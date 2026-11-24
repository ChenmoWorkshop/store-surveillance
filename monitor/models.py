import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unicodedata import normalize
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator


MAX_CATALOG_ITEMS = 2000

LISTING_LABELS = {"active": "在售", "sold": "已售／售罄", "delisted": "已下架",
                  "removed_violation": "违规删除", "unavailable": "链接不可用"}


def public_date(value):
    """Keep the precision supplied by the source and normalize complete timestamps."""
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = date.fromisoformat(text)
            today = datetime.now(timezone(timedelta(hours=8))).date()
            return text if date(1990, 1, 1) <= day <= today else None
        if re.fullmatch(r"\d{10}|\d{13}", text):
            instant = datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), timezone.utc)
        else:
            instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
        if instant.year >= 1990 and instant <= datetime.now(timezone.utc):
            return instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        pass
    return None


PublicDate = Annotated[str | None, BeforeValidator(public_date)]


def listing_state_from_text(value):
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("。！!")
    text = re.sub(r"^(?:该|此)?(?:商品|宝贝)", "", text).strip()
    return {
        "在线": "active", "在售": "active", "正常售卖": "active",
        "已售": "sold", "已售出": "sold", "已售罄": "sold", "已抢光": "sold", "售罄": "sold",
        "已下架": "delisted", "下架": "delisted",
        "因违规已被删除": "removed_violation", "因违规被删除": "removed_violation",
        "因违规已被移除": "removed_violation", "因违规被移除": "removed_violation",
        "已被平台违规下架": "removed_violation", "因违规已下架": "removed_violation",
        "因违规被下架": "removed_violation", "因违规被平台下架": "removed_violation",
        "不存在": "unavailable", "已删除": "unavailable", "已被删除": "unavailable",
        "已下架或不存在": "unavailable",
    }.get(text)


class CatalogItem(BaseModel):
    id: str = Field(pattern=r"^(?:[0-9]{5,24}|[a-f0-9]{24})$")
    sku_id: str | None = Field(None, pattern=r"^[a-f0-9]{24}$")
    title: str = Field(min_length=1, max_length=200)
    price_fen: int | None = Field(None, ge=0, le=10**10, strict=True)
    raw_sales: str | None = Field(None, max_length=100)
    want_count: int | None = Field(None, ge=0, le=10**12, strict=True)
    listed_at: PublicDate = None


class ShopCatalog(BaseModel):
    active_count: int = Field(ge=0, le=1000000, strict=True)
    items: list[CatalogItem] = Field(max_length=MAX_CATALOG_ITEMS)
    has_more: bool | None = None

    @model_validator(mode="after")
    def unique_items(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("商品 ID 不能重复")
        return self


class Reading(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    raw_sales: str | None = Field(None, max_length=100)
    exact_sales: int | None = Field(None, ge=0, le=10**12)
    want_count: int | None = Field(None, ge=0, le=10**12, strict=True)
    price_fen: int | None = Field(None, ge=0, le=10**10)
    listing_state: Literal["active", "sold", "delisted", "removed_violation", "unavailable"] | None = None
    price_label: Literal["券后价", "到手价", "活动价"] | None = None
    cover_url: str | None = Field(None, max_length=2048)
    official_title: str | None = Field(None, max_length=2048)
    shop_name: str | None = Field(None, max_length=256)
    follower_count: int | None = Field(None, ge=0, le=10**12, strict=True)
    shop_opened_at: PublicDate = None
    listed_at: PublicDate = None
    shop_catalog: ShopCatalog | None = None

    @field_validator("follower_count", mode="before")
    @classmethod
    def normalize_follower_count(cls, value):
        if isinstance(value, str) and re.fullmatch(r"\d{1,13}", value):
            value = int(value)
        return value if type(value) is int and 0 <= value <= 10**12 else None

    @model_validator(mode="after")
    def check_metrics(self):
        if self.raw_sales:
            text = self.raw_sales.replace(",", "").replace("，", "")
            match = re.fullmatch(r"(?:已售\s*)?(\d{1,12})(?:\s*[件单])?", text)
            if not match and self.exact_sales is not None:
                raise ValueError("模糊或未知展示销量不能指定精确销量")
            if match and int(match[1]) != self.exact_sales and self.exact_sales is not None:
                raise ValueError("展示销量与精确销量不一致")
            if match and self.exact_sales is None:
                self.exact_sales = int(match[1])
        if not self.raw_sales and all(value is None for value in (
            self.exact_sales, self.price_fen, self.want_count, self.listing_state, self.shop_catalog,
            self.follower_count, self.shop_opened_at, self.listed_at,
        )):
            raise ValueError("至少填写一个可核对指标")
        return self


class VisibleReading(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


def xianyu_visible_reading(text):
    text = re.split(r"为你推荐|猜你喜欢|相关推荐", normalize("NFKC", text), maxsplit=1)[0]
    amounts = re.findall(
        r"(活动价|到手价|现价|售价|原价|划线价|参考价|行情价)?\s*[:：]?\s*¥\s*"
        r"([0-9]{1,9}(?:,[0-9]{3})*)(\s*\.[0-9]{1,2})?(?![0-9.])",
        text,
    )
    prices = set()
    explicit = set()
    for label, amount, fraction in amounts:
        if label in ("原价", "划线价", "参考价", "行情价"):
            continue
        fen = int(Decimal(amount.replace(",", "") + fraction.strip()) * 100)
        if label:
            explicit.add(fen)
        prices.add(fen)
    prices = explicit or prices
    counts = {
        int(match[1].replace(",", ""))
        for match in re.finditer(r"(?<![0-9.,])([0-9]{1,12}(?:,[0-9]{3})*)\s*人\s*想要", text)
        if not re.search(r"(?:约|超过|至少|近|超)\s*$", text[max(0, match.start() - 8):match.start()])
    }
    if len(prices) > 1 or len(counts) > 1:
        raise ValueError("文字中有多个价格或想要数，请只复制这件商品的价格与想要数")
    if not prices and not counts:
        raise ValueError("未找到本商品的价格或想要数，例如「活动价 ¥2.00 · 78 人想要」")
    return Reading(price_fen=next(iter(prices), None), want_count=next(iter(counts), None))


class ImportLinks(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    kind: Literal["product", "shop"] | None = None


class TargetPatch(BaseModel):
    enabled: bool | None = None
    title: str | None = Field(None, min_length=1, max_length=160)
    category: str | None = Field(None, max_length=40)


class SamplingReview(BaseModel):
    slot: str = Field(max_length=40)


class ShopMonitoringChange(BaseModel):
    enabled: bool
    item_id: str | None = Field(None, pattern=r"^(?:[0-9]{5,24}|[a-f0-9]{24})$")


class Rules(BaseModel):
    hourly_units: int = Field(ge=1, le=1000000)
    premium_price_fen: int = Field(ge=0, le=100000000)
    premium_units: int = Field(ge=1, le=1000000)
    price_drop_fen: int = Field(ge=0, le=100000000)
    state_alerts: bool
