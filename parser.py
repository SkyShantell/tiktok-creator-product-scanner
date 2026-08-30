from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

PROFILE_RE = re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@([^/?#]+)", re.I)
VIDEO_ID_RE = re.compile(r"/video/(\d+)")

# TikTok has used several representations for a Shop attachment over time.
PRODUCT_PATTERNS = [
    re.compile(r"(?:placeholder_product_id|product_id|productId|product_id_str|productIdStr)[=\"':%3D\s]+(\d{10,})", re.I),
    re.compile(r"(?:/product/|/pdp/)(?:[^/?#]*[-/])?(\d{10,})(?:[/?#&]|$)", re.I),
    re.compile(r"(?:product%5[Ff]id|placeholder%5[Ff]product%5[Ff]id)%?3[Dd](\d{10,})", re.I),
]

DIRECT_PRODUCT_KEYS = {
    "placeholder_product_id",
    "product_id",
    "productId",
    "product_id_str",
    "productIdStr",
    "productid",
}
COMMERCE_PATH_WORDS = (
    "product",
    "commerce",
    "ecom",
    "shop",
    "anchor",
    "goods",
    "affiliate",
    "promotion",
    "shopping",
)
GENERIC_ID_KEYS = {"id", "id_str", "idStr", "item_id", "itemId"}


def normalize_creator(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a TikTok creator username or profile URL.")
    match = PROFILE_RE.search(value)
    if match:
        return match.group(1).strip("@ ")
    value = value.split("?")[0].split("#")[0].strip().strip("/")
    value = value.lstrip("@")
    if "/" in value or not value:
        raise ValueError("That doesn't look like a TikTok creator profile or @username.")
    return value


def deep_get(obj: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return default


def walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


def walk_paths(obj: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_paths(value, path + (str(key),))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from walk_paths(value, path + (str(idx),))


def find_first_key(obj: Any, keys: set[str]) -> Any:
    for node in walk(obj):
        if isinstance(node, dict):
            for key in keys:
                if key in node and node[key] not in (None, "", [], {}):
                    return node[key]
    return None


def extract_sec_user_id(payload: dict[str, Any]) -> str | None:
    value = find_first_key(payload.get("data", payload), {"sec_user_id", "secUid", "sec_uid"})
    return str(value) if value else None


def extract_video_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    preferred = deep_get(data, "aweme_list", "item_list", "items", "videos", "video_list", default=None)
    if isinstance(preferred, list):
        return [x for x in preferred if isinstance(x, dict)]

    candidates: list[dict[str, Any]] = []
    for node in walk(data):
        if isinstance(node, dict) and any(k in node for k in ("aweme_id", "item_id", "id")):
            if any(k in node for k in ("video", "statistics", "stats", "desc", "description", "share_info")):
                candidates.append(node)
    return dedupe_video_dicts(candidates)


def dedupe_video_dicts(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for video in videos:
        vid = video_id(video)
        if vid and vid not in seen:
            seen.add(vid)
            out.append(video)
    return out


def video_id(video: dict[str, Any]) -> str | None:
    value = deep_get(video, "aweme_id", "item_id", "id", default=None)
    if value is not None and str(value).isdigit():
        return str(value)
    for node in walk(video):
        if isinstance(node, str):
            match = VIDEO_ID_RE.search(node)
            if match:
                return match.group(1)
    return None


def extract_pagination(payload: dict[str, Any]) -> tuple[bool, int | str | None]:
    data = payload.get("data", payload)
    has_more = deep_get(data, "has_more", "hasMore", default=False)
    cursor = deep_get(data, "max_cursor", "maxCursor", "cursor", default=None)
    return bool(has_more), cursor


def _valid_product_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # Current TikTok Shop IDs are long numeric strings; this also rejects video
    # IDs in most generic contexts unless they live under a commerce path.
    if text.isdigit() and 10 <= len(text) <= 24:
        return text
    return None


def _ids_from_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    variants = [text]
    try:
        decoded = unquote(text)
        if decoded != text:
            variants.append(decoded)
        decoded2 = unquote(decoded)
        if decoded2 not in variants:
            variants.append(decoded2)
    except Exception:
        pass

    out: list[str] = []
    for variant in variants:
        # Query string parsing catches normal share/product URLs.
        try:
            qs = parse_qs(urlparse(variant).query)
            for key in ("placeholder_product_id", "product_id", "productId"):
                for value in qs.get(key, []):
                    pid = _valid_product_id(value)
                    if pid:
                        out.append(pid)
        except Exception:
            pass
        for pattern in PRODUCT_PATTERNS:
            for match in pattern.finditer(variant):
                pid = _valid_product_id(match.group(1))
                if pid:
                    out.append(pid)

        # Commerce metadata is often a JSON string nested inside `extra`,
        # `anchor_info`, etc. Parse it if possible and scan recursively.
        s = variant.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                parsed = json.loads(s)
                for pid in extract_product_ids(parsed):
                    out.append(pid)
            except Exception:
                pass
    return list(dict.fromkeys(out))


def extract_product_ids(obj: Any) -> list[str]:
    """Return plausible TikTok Shop product IDs from any TikTok response.

    This deliberately accepts the *whole* TikHub response, not only the video
    object. TikTok moves commerce fields between app/web response variants.
    Candidates are later validated with TikHub's product-detail endpoint.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        pid = _valid_product_id(value)
        if pid and pid not in seen:
            seen.add(pid)
            found.append(pid)

    for path, node in walk_paths(obj):
        if isinstance(node, dict):
            path_text = ".".join(path).lower()
            commerce_context = any(word in path_text for word in COMMERCE_PATH_WORDS)
            for key, value in node.items():
                key_lower = str(key).lower()
                if key in DIRECT_PRODUCT_KEYS or key_lower in {k.lower() for k in DIRECT_PRODUCT_KEYS}:
                    if isinstance(value, (str, int)):
                        add(value)
                elif key in GENERIC_ID_KEYS and commerce_context:
                    if isinstance(value, (str, int)):
                        add(value)
                if isinstance(value, str):
                    for pid in _ids_from_text(value):
                        add(pid)
        elif isinstance(node, str):
            for pid in _ids_from_text(node):
                add(pid)

    return found


def extract_product_id(obj: Any) -> str | None:
    ids = extract_product_ids(obj)
    return ids[0] if ids else None


def extract_video_objects_from_batch(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, list):
        return dedupe_video_dicts([x for x in data if isinstance(x, dict)])
    return extract_video_list({"data": data})


def first_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, list):
        for item in value:
            url = first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "urls", "url", "uri"):
            if key in value:
                url = first_url(value[key])
                if url:
                    return url
        for item in value.values():
            url = first_url(item)
            if url:
                return url
    return None


def as_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if "." in text else int(text)
    except Exception:
        return None


def unix_to_iso(value: Any) -> str | None:
    n = as_number(value)
    if n is None:
        return None
    try:
        if n > 10_000_000_000:
            n = n / 1000
        return datetime.fromtimestamp(float(n), tz=timezone.utc).isoformat()
    except Exception:
        return None


def normalize_video(video: dict[str, Any], creator: str) -> dict[str, Any]:
    vid = video_id(video) or ""
    stats = deep_get(video, "statistics", "stats", default={}) or {}
    author = deep_get(video, "author", "author_info", default={}) or {}
    create_time = deep_get(video, "create_time", "createTime", "create_timestamp", default=None)
    cover = first_url(deep_get(video, "video.cover", "video.origin_cover", "cover", default=None))
    if not cover:
        cover = first_url(video.get("video"))
    return {
        "video_id": vid,
        "video_url": f"https://www.tiktok.com/@{creator}/video/{vid}" if vid else "",
        "caption": str(deep_get(video, "desc", "description", "title", default="") or ""),
        "posted_at": unix_to_iso(create_time),
        "views": as_number(deep_get(stats, "play_count", "playCount", "view_count", "views", default=None)),
        "likes": as_number(deep_get(stats, "digg_count", "diggCount", "like_count", "likes", default=None)),
        "comments": as_number(deep_get(stats, "comment_count", "commentCount", "comments", default=None)),
        "shares": as_number(deep_get(stats, "share_count", "shareCount", "shares", default=None)),
        "author": str(deep_get(author, "unique_id", "uniqueId", "nickname", default=creator) or creator),
        "cover_url": cover or "",
    }


def _scalar_candidates(obj: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = find_first_key(obj, {key})
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_product(product_id: str, payload: dict[str, Any], region: str) -> dict[str, Any]:
    data = payload.get("data", payload)
    info = deep_get(data, "productInfo", "product_info", default=data) or {}
    shop = deep_get(data, "shopInfo", "shop_info", default={}) or {}

    title = _scalar_candidates(info, ("title", "product_name", "productName", "name"))
    description = _scalar_candidates(info, ("description", "desc", "product_description", "productDescription"))
    price = _scalar_candidates(info, ("sale_price", "salePrice", "price", "min_price", "minPrice"))
    currency = _scalar_candidates(info, ("currency", "currency_code", "currencyCode"))
    sold = _scalar_candidates(info, ("sold_count", "soldCount", "sales", "sale_count"))
    rating = _scalar_candidates(info, ("rating", "rating_score", "ratingScore", "star"))
    review_count = _scalar_candidates(info, ("review_count", "reviewCount", "rating_count", "ratingCount"))

    image_source = _scalar_candidates(info, ("images", "image", "cover", "main_image", "mainImage"))
    image_url = first_url(image_source) or ""

    seller = _scalar_candidates(shop, ("shop_name", "shopName", "seller_name", "sellerName", "name"))
    if not seller:
        seller = _scalar_candidates(info, ("shop_name", "shopName", "seller_name", "sellerName"))

    # Prefer a URL returned by TikTok/TikHub when it is present. V3 product
    # payloads do not always expose the public PDP URL, so always fall back to
    # TikTok Shop's canonical product-detail route. This keeps both the UI link
    # and exported CSV usable for every validated product ID.
    product_url = _scalar_candidates(
        info,
        ("detail_link", "detailLink", "product_link", "productLink", "product_url", "productUrl", "share_url", "shareUrl", "url"),
    )
    if not isinstance(product_url, str) or not product_url.startswith("http"):
        product_url = f"https://shop.tiktok.com/view/product/{product_id}?region={region.upper()}&locale=en"

    return {
        "product_id": str(product_id),
        "product_title": str(title or f"Product {product_id}"),
        "product_description": str(description or ""),
        "product_image": image_url,
        "price": price if isinstance(price, (str, int, float)) else "",
        "currency": str(currency or ""),
        "seller": str(seller or ""),
        "sold": as_number(sold),
        "rating": as_number(rating),
        "review_count": as_number(review_count),
        "product_url": product_url,
        "region": region,
    }
