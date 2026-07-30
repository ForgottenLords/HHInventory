"""Fetch and parse product data from barcode lookup pages.

Primary target is barcodelookup.com URLs such as:
  https://www.barcodelookup.com/030111179302

That site often blocks automated clients, so this module also falls back to
go-upc.com HTML and the upcitemdb trial API. Results are normalized to fields
compatible with ``core.models.inventory.Product`` / ``Kibble`` / ``Canned``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

BARCODELOOKUP_HOST = "www.barcodelookup.com"
GO_UPC_SEARCH_URL = "https://go-upc.com/search"
UPCITEMDB_LOOKUP_URL = "https://api.upcitemdb.com/prod/trial/lookup"
BARCODELOOKUP_API_URL = "https://api.barcodelookup.com/v3/products"

BARCODE_RE = re.compile(r"\b(\d{8,14})\b")
WEIGHT_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>lb|lbs|pound|pounds|oz|ounce|ounces|kg|g)\b",
    re.IGNORECASE,
)
CAN_SIZE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:oz|ounce|ounces)\b",
    re.IGNORECASE,
)


@dataclass
class ProductData:
    """Normalized product payload for HHInventory models."""

    barcode: str
    name: str = ""
    brand: str = ""
    description: str = ""
    category: str = ""
    country_of_origin: str = ""
    estimated_price: Decimal | None = None
    image_urls: list[str] = field(default_factory=list)
    weight: float | None = None
    can_size: float | None = None
    life_stages: str | None = None
    proteins: list[str] = field(default_factory=list)
    grain_free: bool = False
    limited_ingredient: bool = False
    special_diet: str | None = None
    product_type: str = "unknown"  # kibble | canned | unknown
    source: str = ""
    source_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["estimated_price"] is not None:
            data["estimated_price"] = str(data["estimated_price"])
        return data

    def kibble_defaults(self) -> dict[str, Any]:
        """Keyword args suitable for ``Kibble(...)`` (excluding photo)."""
        if not self.name or not self.barcode or not self.brand:
            raise ValueError("name, barcode, and brand are required for Kibble")
        if self.weight is None:
            raise ValueError("weight could not be inferred; set it manually")
        defaults: dict[str, Any] = {
            "name": self.name,
            "barcode": self.barcode,
            "brand": self.brand,
            "country_of_origin": self.country_of_origin,
            "estimated_price": self.estimated_price,
            "notes": self.description,
            "weight": self.weight,
            "grain_free": self.grain_free,
            "limited_ingrediant": self.limited_ingredient,
        }
        if self.life_stages:
            defaults["life_stages"] = self.life_stages
        if self.proteins:
            defaults["proteins"] = self.proteins
        if self.special_diet:
            defaults["special_diet"] = self.special_diet
        return defaults


class ProductLookupError(RuntimeError):
    """Raised when no source can return product data for a barcode."""


def extract_barcode(url_or_barcode: str) -> str:
    """Extract an 8–14 digit barcode from a URL or raw barcode string."""
    value = (url_or_barcode or "").strip()
    if not value:
        raise ValueError("url_or_barcode is required")

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        candidate = path.rsplit("/", 1)[-1]
        match = BARCODE_RE.search(candidate) or BARCODE_RE.search(value)
    else:
        match = BARCODE_RE.search(value)

    if not match:
        raise ValueError(f"Could not find a barcode in {url_or_barcode!r}")
    return match.group(1)


def lookup_product(
    url_or_barcode: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    api_key: str | None = None,
) -> ProductData:
    """Look up product data for a barcodelookup URL or barcode.

    Tries, in order:
      1. Barcode Lookup official API (when ``api_key`` / env is set)
      2. barcodelookup.com HTML
      3. go-upc.com HTML
      4. upcitemdb trial API
    """
    barcode = extract_barcode(url_or_barcode)
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    sess.headers.setdefault(
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    )
    key = api_key if api_key is not None else os.environ.get("BARCODELOOKUP_API_KEY", "")

    errors: list[str] = []

    if key:
        try:
            return _lookup_barcodelookup_api(barcode, sess, timeout, key)
        except Exception as exc:  # noqa: BLE001 - collect and continue
            errors.append(f"barcodelookup API: {exc}")

    try:
        return _lookup_barcodelookup_html(barcode, sess, timeout)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"barcodelookup HTML: {exc}")

    try:
        return _lookup_go_upc_html(barcode, sess, timeout)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"go-upc HTML: {exc}")

    try:
        return _lookup_upcitemdb(barcode, sess, timeout)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"upcitemdb: {exc}")

    raise ProductLookupError(
        f"Unable to look up barcode {barcode}. Attempts: " + "; ".join(errors)
    )


def parse_barcodelookup_html(html: str, *, barcode: str = "", source_url: str = "") -> ProductData:
    """Parse a barcodelookup.com product page HTML document."""
    soup = BeautifulSoup(html, "lxml")
    if _looks_like_bot_challenge(soup, html):
        raise ProductLookupError("barcodelookup.com returned a security challenge page")

    name = _first_text(
        soup,
        [
            "h4.product-name",
            ".product-details h4",
            "#product-name",
            "h1.product-name",
            ".product-name",
            "h4",
            "h1",
        ],
    )
    meta = _label_value_map(soup)
    barcode = (
        barcode
        or meta.get("barcode")
        or meta.get("upc")
        or meta.get("ean")
        or _meta_content(soup, "og:upc")
        or ""
    )
    barcode = extract_barcode(barcode) if barcode else ""
    brand = meta.get("brand") or meta.get("manufacturer") or ""
    description = (
        meta.get("description")
        or _section_text_after_heading(soup, "description")
        or _meta_content(soup, "og:description")
        or ""
    )
    category = meta.get("category") or meta.get("categories") or ""
    images = _collect_images(soup, prefer_selectors=["#store-images img", ".product-images img", "img"])
    price = _parse_price(
        meta.get("price")
        or meta.get("lowest price")
        or meta.get("store price")
        or _first_store_price(soup)
    )

    data = ProductData(
        barcode=barcode,
        name=name,
        brand=brand,
        description=description,
        category=category,
        country_of_origin=meta.get("country") or meta.get("country of origin") or "",
        estimated_price=price,
        image_urls=images,
        source="barcodelookup_html",
        source_url=source_url or (f"https://{BARCODELOOKUP_HOST}/{barcode}" if barcode else ""),
        raw={"meta": meta},
    )
    return enrich_product_data(data)


def parse_go_upc_html(html: str, *, barcode: str = "", source_url: str = "") -> ProductData:
    """Parse a go-upc.com product page HTML document."""
    soup = BeautifulSoup(html, "lxml")
    name = _first_text(soup, ["h1.product-name", ".product-name", "h1"])
    meta = _label_value_map(soup)
    barcode = barcode or meta.get("upc") or meta.get("ean") or ""
    barcode = extract_barcode(barcode) if barcode else ""
    brand = meta.get("brand") or meta.get("manufacturer") or ""
    description = _section_text_after_heading(soup, "description") or ""
    ingredients = _section_text_after_heading(soup, "ingredients") or ""
    if ingredients and ingredients not in description:
        description = f"{description}\n\nIngredients: {ingredients}".strip()

    images = []
    for img in soup.select("figure.product-image img, .product-image img, img"):
        src = (img.get("src") or "").strip()
        if src.startswith("http") and src not in images:
            images.append(src)

    size = meta.get("size") or ""
    data = ProductData(
        barcode=barcode,
        name=name,
        brand=brand,
        description=description,
        category=meta.get("category") or "",
        image_urls=images,
        source="go_upc_html",
        source_url=source_url or (f"{GO_UPC_SEARCH_URL}?q={barcode}" if barcode else ""),
        raw={"meta": meta, "size": size},
    )
    return enrich_product_data(data)


def enrich_product_data(data: ProductData) -> ProductData:
    """Infer food-specific fields from name/description/category text."""
    blob = " ".join(
        part for part in [data.name, data.description, data.category, data.raw.get("size", "")] if part
    )

    if data.weight is None:
        data.weight = _infer_weight_lb(blob)
    if data.can_size is None and _looks_like_canned(blob):
        data.can_size = _infer_can_size_oz(blob)

    if data.product_type == "unknown":
        if _looks_like_canned(blob):
            data.product_type = "canned"
        elif _looks_like_kibble(blob) or data.weight is not None:
            data.product_type = "kibble"

    if not data.life_stages:
        data.life_stages = _infer_life_stage(blob)
    if not data.proteins:
        data.proteins = _infer_proteins(blob)
    data.grain_free = data.grain_free or bool(re.search(r"\bgrain[\s-]?free\b", blob, re.I))
    data.limited_ingredient = data.limited_ingredient or bool(
        re.search(r"\blimited\s+ingredient", blob, re.I)
    )
    if not data.special_diet:
        data.special_diet = _infer_special_diet(blob)
    return data


def _lookup_barcodelookup_api(
    barcode: str, session: requests.Session, timeout: float, api_key: str
) -> ProductData:
    response = session.get(
        BARCODELOOKUP_API_URL,
        params={"barcode": barcode, "formatted": "y", "key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    products = payload.get("products") or []
    if not products:
        raise ProductLookupError(f"No products returned for barcode {barcode}")
    item = products[0]
    stores = item.get("stores") or []
    prices = []
    for store in stores:
        price = _parse_price(store.get("price") or store.get("sale_price"))
        if price is not None:
            prices.append(price)
    images = [url for url in (item.get("images") or []) if url]
    data = ProductData(
        barcode=item.get("barcode_number") or barcode,
        name=item.get("title") or item.get("product_name") or "",
        brand=item.get("brand") or item.get("manufacturer") or "",
        description=item.get("description") or "",
        category=item.get("category") or "",
        estimated_price=min(prices) if prices else None,
        image_urls=images,
        weight=_infer_weight_lb(item.get("weight") or ""),
        source="barcodelookup_api",
        source_url=f"https://{BARCODELOOKUP_HOST}/{barcode}",
        raw=item,
    )
    return enrich_product_data(data)


def _lookup_barcodelookup_html(barcode: str, session: requests.Session, timeout: float) -> ProductData:
    url = f"https://{BARCODELOOKUP_HOST}/{barcode}"
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return parse_barcodelookup_html(response.text, barcode=barcode, source_url=url)


def _lookup_go_upc_html(barcode: str, session: requests.Session, timeout: float) -> ProductData:
    url = f"{GO_UPC_SEARCH_URL}?q={barcode}"
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    data = parse_go_upc_html(response.text, barcode=barcode, source_url=url)
    if not data.name:
        raise ProductLookupError(f"go-upc returned no product name for {barcode}")
    return data


def _lookup_upcitemdb(barcode: str, session: requests.Session, timeout: float) -> ProductData:
    response = session.get(UPCITEMDB_LOOKUP_URL, params={"upc": barcode}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items") or []
    if not items:
        raise ProductLookupError(f"upcitemdb returned no items for {barcode}")
    item = items[0]
    offers = item.get("offers") or []
    prices = [_parse_price(o.get("price")) for o in offers]
    prices = [p for p in prices if p is not None]
    lowest = item.get("lowest_recorded_price")
    estimated = _parse_price(lowest) if lowest not in (None, "") else (min(prices) if prices else None)
    data = ProductData(
        barcode=item.get("upc") or barcode,
        name=item.get("title") or "",
        brand=item.get("brand") or "",
        description=item.get("description") or "",
        category=item.get("category") or "",
        estimated_price=estimated,
        image_urls=[u for u in (item.get("images") or []) if u],
        weight=_infer_weight_lb(item.get("weight") or ""),
        source="upcitemdb",
        source_url=f"https://www.upcitemdb.com/upc/{barcode}",
        raw=item,
    )
    return enrich_product_data(data)


def _looks_like_bot_challenge(soup: BeautifulSoup, html: str) -> bool:
    title = (soup.title.string or "").lower() if soup.title else ""
    markers = (
        "security verification",
        "attention required",
        "just a moment",
        "cf-challenge",
        "captcha",
    )
    lowered = html.lower()
    return any(m in title for m in markers) or any(m in lowered for m in markers[:3])


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _meta_content(soup: BeautifulSoup, property_name: str) -> str:
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    return (tag.get("content") or "").strip() if tag else ""


def _label_value_map(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}

    for row in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0]:
            meta[cells[0].rstrip(":").lower()] = cells[1]

    for label in soup.select(
        ".metadata-label, .product-text-label, .product-meta-label, span.product-text-label"
    ):
        key = label.get_text(" ", strip=True).rstrip(":").lower()
        value = ""
        sibling = label.find_next_sibling()
        if sibling:
            value = sibling.get_text(" ", strip=True)
        elif label.parent:
            full = label.parent.get_text(" ", strip=True)
            value = full[len(label.get_text(" ", strip=True)) :].lstrip(": ").strip()
        if key and value:
            meta[key] = value

    for dt in soup.select("dt"):
        key = dt.get_text(" ", strip=True).rstrip(":").lower()
        dd = dt.find_next_sibling("dd")
        if key and dd:
            meta[key] = dd.get_text(" ", strip=True)

    return meta


def _section_text_after_heading(soup: BeautifulSoup, heading: str) -> str:
    target = heading.lower()
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        if tag.get_text(" ", strip=True).lower() == target:
            parts: list[str] = []
            for sibling in tag.next_siblings:
                if getattr(sibling, "name", None) in {"h1", "h2", "h3", "h4", "h5"}:
                    break
                text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
                if text:
                    parts.append(text)
            if parts:
                return " ".join(parts)
            parent_text = tag.parent.get_text(" ", strip=True) if tag.parent else ""
            if parent_text.lower().startswith(target):
                return parent_text[len(heading) :].strip(" :")
    return ""


def _collect_images(soup: BeautifulSoup, prefer_selectors: list[str]) -> list[str]:
    images: list[str] = []
    for selector in prefer_selectors:
        for img in soup.select(selector):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and src not in images:
                images.append(src)
        if images:
            break
    return images


def _first_store_price(soup: BeautifulSoup) -> str:
    for el in soup.select(".store-price, .price, .product-price, td.price"):
        text = el.get_text(" ", strip=True)
        if "$" in text or re.search(r"\d", text):
            return text
    return ""


def _parse_price(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value)
    match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _infer_weight_lb(text: str) -> float | None:
    if not text:
        return None
    match = WEIGHT_RE.search(text)
    if not match:
        return None
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    if unit in {"lb", "lbs", "pound", "pounds"}:
        return value
    if unit in {"oz", "ounce", "ounces"}:
        return round(value / 16.0, 4)
    if unit == "kg":
        return round(value * 2.20462, 4)
    if unit == "g":
        return round(value / 453.592, 4)
    return None


def _infer_can_size_oz(text: str) -> float | None:
    match = CAN_SIZE_RE.search(text or "")
    return float(match.group("value")) if match else None


def _looks_like_canned(text: str) -> bool:
    return bool(re.search(r"\b(canned|wet food|pate|pat[eé]|chunks in gravy|stew|shreds)\b", text, re.I))


def _looks_like_kibble(text: str) -> bool:
    return bool(re.search(r"\b(dry dog food|dry cat food|kibble|dry food)\b", text, re.I))


def _infer_life_stage(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\bpuppy\b|\bkitten\b", lowered):
        return "PUP"
    if re.search(r"\bsenior\b|\bmature\b|\bolder dogs?\b|\b5\+\b", lowered):
        return "SNR"
    if re.search(r"\ball[\s-]stages?\b|\ball life stages?\b", lowered):
        return "ALL"
    if re.search(r"\badult\b", lowered):
        return "ADL"
    return None


def _infer_proteins(text: str) -> list[str]:
    # Ingredient mentions like "fish oil" should not count as a primary protein.
    cleaned = re.sub(r"\bfish\s+oil\b", " ", text, flags=re.I)
    mapping = [
        (r"\bchicken\b", "CHKN"),
        (r"\bbeef\b", "BEEF"),
        (r"\blamb\b", "LAMB"),
        (r"\bturkey\b", "TURK"),
        (r"\bduck\b", "DUCK"),
        (r"\bsalmon\b", "SALM"),
        (r"\bwhitefish\b", "WTFS"),
        (r"\bpork\b", "PORK"),
        (r"\bvenison\b", "VENI"),
        (r"\bbison\b", "BSON"),
        (r"\brabbit\b", "RBBT"),
        (r"\bfish\b", "FISH"),
    ]
    found: list[str] = []
    for pattern, code in mapping:
        if re.search(pattern, cleaned, re.I) and code not in found:
            found.append(code)
    return found


def _infer_special_diet(text: str) -> str | None:
    mapping = [
        (r"sensitive stomach", "SENS"),
        (r"weight management|weight control|healthy weight", "WGHT"),
        (r"\bdental\b", "DENT"),
        (r"digestive health|digestive care|digestive support", "DIGE"),
        (r"joint health|joint support|hip and joint", "JONT"),
        (r"skin\s*(and|&)\s*coat|skin & coat", "SKIN"),
    ]
    for pattern, code in mapping:
        if re.search(pattern, text, re.I):
            return code
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI helper: ``python -m core.product_lookup <url-or-barcode>``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Look up product data by barcode / URL")
    parser.add_argument("url_or_barcode", help="Barcode or barcodelookup.com URL")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)

    try:
        product = lookup_product(args.url_or_barcode)
    except (ValueError, ProductLookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(product.to_dict(), indent=2))
    else:
        print(f"Source: {product.source}")
        print(f"Barcode: {product.barcode}")
        print(f"Name: {product.name}")
        print(f"Brand: {product.brand}")
        print(f"Type: {product.product_type}")
        print(f"Weight (lb): {product.weight}")
        print(f"Price: {product.estimated_price}")
        print(f"Life stage: {product.life_stages}")
        print(f"Proteins: {', '.join(product.proteins) or '-'}")
        print(f"Category: {product.category}")
        if product.description:
            print(f"Description: {product.description[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
