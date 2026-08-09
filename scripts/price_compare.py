#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.text import MIMEText
from html import escape, unescape
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "tracker_config.json"
BARCODE_RE = re.compile(r"(?<!\d)(\d{8,14})(?!\d)")
PRODUCT_ID_RE = re.compile(r"/p(\d+)(?:[/?#]|$)", re.I)
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789..")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_now(config: dict[str, Any]) -> datetime:
    tz_name = config.get("timezone") or "Asia/Riyadh"
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return now_utc()


def iso_now(config: dict[str, Any]) -> str:
    return local_now(config).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def load_config() -> dict[str, Any]:
    config = read_json(CONFIG_PATH, {})
    if not isinstance(config, dict):
        raise SystemExit("tracker_config.json must contain a JSON object.")
    return config


def resolve_path(value: str | None, default: str) -> Path:
    raw = value or default
    p = Path(raw)
    return p if p.is_absolute() else REPO_ROOT / p


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("ar") or value.get("en") or ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value if x)
    text = str(value).strip()
    if not text:
        return ""
    # Some older exported files were decoded as latin-1 although the original was UTF-8.
    # This repair keeps normal Arabic intact and fixes strings like "Ø¯Ø§Ø´".
    if any(marker in text for marker in ("Ø", "Ù", "Ã", "Â")):
        try:
            repaired = text.encode("latin1").decode("utf-8").strip()
            if repaired:
                text = repaired
        except Exception:
            pass
    return re.sub(r"\s+", " ", text)


def parse_money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).translate(ARABIC_DIGITS)
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def extract_codes(*values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).translate(ARABIC_DIGITS)
        for match in BARCODE_RE.findall(text):
            if match not in seen:
                seen.add(match)
                result.append(match)
    return result


def first_value(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def product_id_from_url(url: str) -> str:
    match = PRODUCT_ID_RE.search(url or "")
    return f"p{match.group(1)}" if match else ""


def normalize_product(raw: dict[str, Any], source: str) -> dict[str, Any]:
    url = clean_text(first_value(raw, ["url", "product_url", "link", "canonical", "@id"]))
    pid = clean_text(first_value(raw, ["id", "product_id", "productId"])) or product_id_from_url(url)

    name = clean_text(first_value(raw, ["name", "title", "product_name", "og:title"]))
    brand = clean_text(first_value(raw, ["brand", "brand_name", "manufacturer"]))
    category = clean_text(first_value(raw, ["category", "categories", "section"]))

    price = parse_money(first_value(raw, ["price", "store_price", "sale_price", "salePrice", "current_price", "lowPrice"]))
    regular_price = parse_money(first_value(raw, ["regular_price", "regularPrice", "original_price", "old_price", "list_price", "highPrice"]))
    sale_price = parse_money(first_value(raw, ["sale_price", "salePrice", "offer_price"]))
    if sale_price is None and regular_price is not None and price is not None and price < regular_price:
        sale_price = price

    sku = clean_text(first_value(raw, ["sku", "SKU", "mpn", "default_code"]))
    gtin = clean_text(first_value(raw, ["gtin", "gtin13", "gtin12", "gtin14", "barcode", "bar_code", "ean"]))
    if source == "depart":
        # Depart should match by explicit GTIN/barcode only. Some exported Salla
        # SKU fields contain a product slug ending in /p123..., and treating that
        # product id as a barcode would create false positives.
        barcodes = extract_codes(gtin, raw.get("barcode"), raw.get("bar_code"), raw.get("ean"))
        if not barcodes and sku and not PRODUCT_ID_RE.search(sku):
            barcodes = extract_codes(sku)
    else:
        barcodes = extract_codes(gtin, sku, raw.get("details") if source == "mokab" else None)
        pid_digits = re.sub(r"\D+", "", str(pid or ""))
        if pid_digits:
            barcodes = [code for code in barcodes if code != pid_digits]

    image = first_value(raw, ["image_url", "image", "thumbnail", "img"])
    if isinstance(image, list):
        image = image[0] if image else ""

    return {
        "source": source,
        "id": str(pid),
        "name": name,
        "brand": brand,
        "category": category,
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "currency": clean_text(first_value(raw, ["currency", "priceCurrency"])) or "SAR",
        "url": url,
        "image_url": clean_text(image),
        "sku": sku,
        "gtin": gtin,
        "barcodes": barcodes,
        "primary_barcode": barcodes[0] if barcodes else "",
        "availability": clean_text(first_value(raw, ["availability", "status", "stock_status"])) or "",
        "raw_barcode_count": len(barcodes),
    }


def load_products_file(path: Path, source: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"Missing product file: {path}")

    rows: list[dict[str, Any]]
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        data = read_json(path, {})
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict) and isinstance(data.get("products"), list):
            rows = data["products"]
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            rows = data["data"]
        else:
            raise SystemExit(f"Unsupported product JSON shape: {path}")

    products = [normalize_product(row, source) for row in rows if isinstance(row, dict)]
    return [p for p in products if p["name"] or p["url"] or p["barcodes"]]


def require_requests() -> Any:
    if requests is None:
        raise SystemExit("Live mode requires requests. Run: pip install -r requirements.txt")
    return requests


def http_session() -> Any:
    rq = require_requests()
    session = rq.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; DepartPriceTracker/1.0; +https://depart.com.sa)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ar,en;q=0.8",
        }
    )
    return session


def http_get_text(session: Any, url: str, timeout: tuple[int, int] = (15, 45)) -> str:
    retries = max(1, int(os.getenv("HTTP_RETRIES", "4") or "4"))
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = min(90, int(retry_after))
                else:
                    wait = min(90, 8 * (attempt + 1))
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding or "utf-8"
            return response.text
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(min(60, 5 * (attempt + 1)))
    assert last_exc is not None
    raise last_exc


def discover_product_urls(session: Any, sitemap_url: str, depth: int = 0, seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    if sitemap_url in seen or depth > 3:
        return []
    seen.add(sitemap_url)

    xml = http_get_text(session, sitemap_url, timeout=(15, 60))
    locs = [unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, flags=re.I | re.S)]
    urls: list[str] = []
    for loc in locs:
        if loc.lower().endswith(".xml"):
            urls.extend(discover_product_urls(session, loc, depth + 1, seen))
        elif PRODUCT_ID_RE.search(loc):
            urls.append(loc)

    deduped: list[str] = []
    seen_url: set[str] = set()
    for url in urls:
        pid = product_id_from_url(url) or url
        if pid not in seen_url:
            seen_url.add(pid)
            deduped.append(url)
    return deduped


def jsonld_product_candidates(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            items.extend(jsonld_product_candidates(item))
    elif isinstance(value, dict):
        type_value = value.get("@type")
        if isinstance(type_value, list):
            type_text = " ".join(str(x) for x in type_value)
        else:
            type_text = str(type_value or "")
        if "Product" in type_text:
            items.append(value)
        for key in ("@graph", "itemListElement", "mainEntity", "offers"):
            if key in value:
                items.extend(jsonld_product_candidates(value[key]))
    return items


def parse_jsonld_blocks(html: str) -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        blocks = re.findall(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.I | re.S,
        )
    else:
        soup = BeautifulSoup(html, "html.parser")
        blocks = [script.get_text("\n", strip=True) for script in soup.find_all("script", attrs={"type": "application/ld+json"})]

    candidates: list[dict[str, Any]] = []
    for block in blocks:
        text = unescape(block.strip())
        if not text:
            continue
        try:
            candidates.extend(jsonld_product_candidates(json.loads(text)))
        except Exception:
            continue
    return candidates


def meta_value(html: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I | re.S)
        if match:
            return unescape(match.group(1))
    return ""


def extract_barcode_near_labels(text: str) -> list[str]:
    matches: list[str] = []
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(text, "html.parser")
            for node in soup.select(".product-sku, .product__sku, [itemprop='sku']"):
                for code in extract_codes(node.get_text(" ", strip=True)):
                    if code not in matches:
                        matches.append(code)
            plain_text = soup.get_text(" ", strip=True)
        except Exception:
            plain_text = text
    else:
        plain_text = re.sub(r"<[^>]+>", " ", text)

    label_re = re.compile(
        r"(?:barcode|bar\s*code|gtin|ean|sku|model\s*number|باركود|الباركود|كود المنتج|رقم الموديل|الموديل)\D{0,140}(\d{8,14})",
        re.I,
    )
    for match in label_re.findall(plain_text.translate(ARABIC_DIGITS)):
        if match not in matches:
            matches.append(match)
    return matches


def parse_mokab_product_html(url: str, html: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": product_id_from_url(url),
        "url": url,
        "name": meta_value(html, "og:title"),
        "price": meta_value(html, "product:price:amount"),
        "sale_price": meta_value(html, "product:sale_price:amount"),
        "regular_price": meta_value(html, "product:original_price:amount") or meta_value(html, "product:regular_price:amount"),
        "currency": meta_value(html, "product:price:currency") or "SAR",
        "image_url": meta_value(html, "og:image"),
        "availability": meta_value(html, "product:availability"),
    }

    products = parse_jsonld_blocks(html)
    if products:
        product = products[0]
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        base.update(
            {
                "name": product.get("name") or base["name"],
                "brand": product.get("brand") or base.get("brand"),
                "sku": product.get("sku") or product.get("mpn") or base.get("sku"),
                "gtin": product.get("gtin13") or product.get("gtin") or product.get("gtin12") or product.get("gtin14") or base.get("gtin"),
                "price": offers.get("price") or offers.get("lowPrice") or base["price"],
                "regular_price": offers.get("highPrice") or base.get("regular_price"),
                "currency": offers.get("priceCurrency") or base["currency"],
                "availability": offers.get("availability") or base["availability"],
                "image_url": product.get("image") or base["image_url"],
                "category": product.get("category") or base.get("category"),
            }
        )

    label_codes = extract_barcode_near_labels(html)
    if label_codes:
        base["gtin"] = " ".join([str(base.get("gtin") or ""), *label_codes]).strip()

    return normalize_product(base, "mokab")


def scan_mokab_live(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session = http_session()
    sitemap_url = config.get("mokab_sitemap_url") or "https://mokab.com/sitemap.xml"
    urls = discover_product_urls(session, sitemap_url)
    limit = int(os.getenv("MOKAB_LIMIT", "0") or "0")
    if limit > 0:
        urls = urls[:limit]

    workers = max(1, int(os.getenv("MOKAB_WORKERS", "2") or "2"))
    request_delay = max(0.0, float(os.getenv("MOKAB_REQUEST_DELAY", "0.35") or "0.35"))
    products: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    def fetch_one(product_url: str) -> dict[str, Any]:
        if request_delay:
            time.sleep(request_delay)
        local_session = http_session()
        html = http_get_text(local_session, product_url)
        return parse_mokab_product_html(product_url, html)

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_one, url): url for url in urls}
        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            try:
                product = future.result()
                if product.get("name") or product.get("barcodes"):
                    products.append(product)
            except Exception as exc:
                failed.append({"url": url, "error": str(exc)[:240]})
            if idx % 100 == 0:
                print(f"Scanned {idx}/{len(urls)} Mokab URLs...", flush=True)

    meta = {
        "mode": "live",
        "sitemap_url": sitemap_url,
        "discovered_urls": len(urls),
        "scanned_products": len(products),
        "failed": failed,
        "failed_count": len(failed),
        "duration_seconds": round(time.time() - started, 1),
    }
    return products, meta


def load_mokab(config: dict[str, Any], mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot_path = resolve_path(config.get("mokab_snapshot_path"), "data/mokab_snapshot.json")
    if mode == "snapshot":
        products = load_products_file(snapshot_path, "mokab")
        return products, {"mode": "snapshot", "snapshot_path": str(snapshot_path), "scanned_products": len(products), "failed_count": 0}

    try:
        products, meta = scan_mokab_live(config)
        if products:
            return products, meta
        raise RuntimeError("Live scan returned no products.")
    except Exception as exc:
        if snapshot_path.exists():
            products = load_products_file(snapshot_path, "mokab")
            return products, {
                "mode": "snapshot_fallback",
                "snapshot_path": str(snapshot_path),
                "live_error": str(exc)[:500],
                "scanned_products": len(products),
                "failed_count": 1,
            }
        raise


def barcode_index(products: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    idx: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        for code in product.get("barcodes", []):
            idx.setdefault(code, []).append(product)
    return idx


def compare_products(mokab: list[dict[str, Any]], depart: list[dict[str, Any]], previous: dict[str, Any] | None) -> dict[str, Any]:
    depart_idx = barcode_index(depart)
    matched_depart_keys: set[str] = set()
    matches: list[dict[str, Any]] = []
    unmatched_mokab: list[dict[str, Any]] = []

    for m in mokab:
        codes = m.get("barcodes", [])
        hits: list[tuple[str, dict[str, Any]]] = []
        for code in codes:
            for d in depart_idx.get(code, []):
                hits.append((code, d))
        if not hits:
            unmatched_mokab.append(trim_product(m))
            continue

        seen_pairs: set[str] = set()
        for code, d in hits:
            pair_key = f"{code}|{d.get('id')}"
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            matched_depart_keys.add(str(d.get("id")) or code)
            match_type = "exact_barcode" if len(codes) == 1 else "bundle_contains"
            m_price = m.get("price")
            d_price = d.get("price")
            diff = None
            diff_pct = None
            status = "needs_price"
            if m_price is not None and d_price is not None and m_price:
                diff = round(float(d_price) - float(m_price), 2)
                diff_pct = round(diff / float(m_price) * 100, 1)
                if abs(diff) < 0.01:
                    status = "same_price"
                elif diff > 0:
                    status = "depart_higher"
                else:
                    status = "depart_lower"

            matches.append(
                {
                    "key": f"{match_type}|{code}|m{m.get('id')}|d{d.get('id')}",
                    "barcode": code,
                    "match_type": match_type,
                    "confidence": "high" if match_type == "exact_barcode" else "review_bundle",
                    "price_status": status,
                    "depart_minus_mokab": diff,
                    "gap_pct_vs_mokab": diff_pct,
                    "mokab": trim_product(m),
                    "depart": trim_product(d),
                }
            )

    unmatched_depart: list[dict[str, Any]] = []
    for d in depart:
        key = str(d.get("id")) or (d.get("primary_barcode") or "")
        if key not in matched_depart_keys:
            unmatched_depart.append(trim_product(d))

    previous_rows = {}
    if previous and isinstance(previous.get("matches"), list):
        previous_rows = {row.get("key"): row for row in previous["matches"] if row.get("key")}

    price_changes: list[dict[str, Any]] = []
    for row in matches:
        old = previous_rows.get(row["key"])
        if not old:
            continue
        changes = []
        old_m = ((old.get("mokab") or {}).get("price"))
        new_m = ((row.get("mokab") or {}).get("price"))
        old_d = ((old.get("depart") or {}).get("price"))
        new_d = ((row.get("depart") or {}).get("price"))
        if old_m is not None and new_m is not None and abs(float(old_m) - float(new_m)) > 0.001:
            changes.append({"side": "mokab", "old": old_m, "new": new_m, "delta": round(float(new_m) - float(old_m), 2)})
        if old_d is not None and new_d is not None and abs(float(old_d) - float(new_d)) > 0.001:
            changes.append({"side": "depart", "old": old_d, "new": new_d, "delta": round(float(new_d) - float(old_d), 2)})
        if changes:
            price_changes.append({"row": row, "changes": changes})

    exact_rows = [r for r in matches if r["match_type"] == "exact_barcode"]
    bundle_rows = [r for r in matches if r["match_type"] == "bundle_contains"]
    exact_priced = [r for r in exact_rows if r.get("depart_minus_mokab") is not None]

    summary = {
        "mokab_products": len(mokab),
        "depart_products": len(depart),
        "mokab_with_barcodes": sum(1 for p in mokab if p.get("barcodes")),
        "depart_with_barcodes": sum(1 for p in depart if p.get("barcodes")),
        "match_rows": len(matches),
        "exact_matches": len(exact_rows),
        "bundle_matches": len(bundle_rows),
        "unmatched_mokab_products": len(unmatched_mokab),
        "unmatched_depart_products": len(unmatched_depart),
        "depart_higher": sum(1 for r in exact_priced if r["price_status"] == "depart_higher"),
        "depart_lower": sum(1 for r in exact_priced if r["price_status"] == "depart_lower"),
        "same_price": sum(1 for r in exact_priced if r["price_status"] == "same_price"),
        "price_changes": len(price_changes),
    }

    return {
        "summary": summary,
        "matches": sorted(matches, key=lambda r: (r["match_type"] != "exact_barcode", abs(r.get("depart_minus_mokab") or 0)), reverse=True),
        "unmatched_mokab": unmatched_mokab,
        "unmatched_depart": unmatched_depart,
        "price_changes": price_changes,
    }


def trim_product(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product.get("id") or "",
        "name": product.get("name") or "",
        "brand": product.get("brand") or "",
        "category": product.get("category") or "",
        "price": product.get("price"),
        "regular_price": product.get("regular_price"),
        "sale_price": product.get("sale_price"),
        "currency": product.get("currency") or "SAR",
        "url": product.get("url") or "",
        "image_url": product.get("image_url") or "",
        "sku": product.get("sku") or "",
        "gtin": product.get("gtin") or "",
        "barcodes": product.get("barcodes") or [],
        "availability": product.get("availability") or "",
    }


def money(value: Any, currency: str = "SAR") -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.2f} {currency}"
    except Exception:
        return f"{value} {currency}"


def pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.1f}%"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [str(x).replace("|", "\\|").replace("\n", " ") for x in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def build_issue_body(report: dict[str, Any], config: dict[str, Any], previous_exists: bool) -> str:
    summary = report["summary"]
    pages_url = config.get("pages_url_hint") or "فعّل GitHub Pages من مجلد docs ثم ضع الرابط في tracker_config.json"
    date_text = report["generated_at"]

    rows = [
        ["منتجات مكعب", summary["mokab_products"]],
        ["منتجات ديبارت", summary["depart_products"]],
        ["تطابق مباشر بالباركود", summary["exact_matches"]],
        ["تطابق داخل باقة", summary["bundle_matches"]],
        ["ديبارت أعلى من مكعب", summary["depart_higher"]],
        ["ديبارت أقل من مكعب", summary["depart_lower"]],
        ["نفس السعر", summary["same_price"]],
        ["تغييرات سعر منذ آخر تشغيل", summary["price_changes"]],
    ]

    body = [
        "## 🔔 تقرير مقارنة أسعار مكعب وديبارت",
        "",
        f"وقت التشغيل: **{date_text}**",
        "",
        md_table(["المؤشر", "القيمة"], rows),
        "",
        f"لوحة التقرير: {pages_url}",
        "",
    ]

    if not previous_exists:
        body += [
            "### ملاحظة أول تشغيل",
            "",
            "هذا التشغيل أنشأ خط الأساس. في التشغيل التالي ستظهر تغييرات الأسعار مقارنة بهذا الخط.",
            "",
        ]

    changes = report.get("price_changes") or []
    if changes:
        change_rows = []
        for item in changes[:15]:
            row = item["row"]
            changes_text = " / ".join(f"{c['side']}: {money(c['old'])} → {money(c['new'])}" for c in item["changes"])
            change_rows.append([
                row["barcode"],
                row["mokab"]["name"],
                row["depart"]["name"],
                changes_text,
                row["mokab"]["url"],
            ])
        body += ["### تغييرات الأسعار", "", md_table(["الباركود", "منتج مكعب", "منتج ديبارت", "التغيير", "رابط مكعب"], change_rows), ""]

    exact = [r for r in report["matches"] if r["match_type"] == "exact_barcode" and r.get("depart_minus_mokab") is not None]
    higher = sorted([r for r in exact if r["price_status"] == "depart_higher"], key=lambda r: r["depart_minus_mokab"], reverse=True)[:10]
    lower = sorted([r for r in exact if r["price_status"] == "depart_lower"], key=lambda r: r["depart_minus_mokab"])[:10]

    if higher:
        body += [
            "### أعلى فجوات: ديبارت أعلى من مكعب",
            "",
            md_table(
                ["الباركود", "مكعب", "سعر مكعب", "ديبارت", "سعر ديبارت", "الفرق"],
                [[r["barcode"], r["mokab"]["name"], money(r["mokab"]["price"]), r["depart"]["name"], money(r["depart"]["price"]), money(r["depart_minus_mokab"])] for r in higher],
            ),
            "",
        ]
    if lower:
        body += [
            "### فرص سعرية: ديبارت أقل من مكعب",
            "",
            md_table(
                ["الباركود", "مكعب", "سعر مكعب", "ديبارت", "سعر ديبارت", "الفرق"],
                [[r["barcode"], r["mokab"]["name"], money(r["mokab"]["price"]), r["depart"]["name"], money(r["depart"]["price"]), money(r["depart_minus_mokab"])] for r in lower],
            ),
            "",
        ]

    if summary["bundle_matches"]:
        body += [
            "### تنبيه الباقات",
            "",
            f"يوجد **{summary['bundle_matches']}** تطابق داخل SKU يحتوي أكثر من باركود. هذه تظهر في اللوحة للمراجعة ولا تُعامل كسعر منتج مفرد.",
            "",
        ]

    return "\n".join(body)


def build_email_html(report: dict[str, Any], config: dict[str, Any]) -> str:
    summary = report["summary"]
    pages_url = escape(config.get("pages_url_hint") or "")
    rows = "".join(
        f"<tr><td>{escape(k)}</td><td>{v}</td></tr>"
        for k, v in [
            ("منتجات مكعب", summary["mokab_products"]),
            ("منتجات ديبارت", summary["depart_products"]),
            ("تطابق مباشر", summary["exact_matches"]),
            ("تطابق داخل باقة", summary["bundle_matches"]),
            ("ديبارت أعلى", summary["depart_higher"]),
            ("ديبارت أقل", summary["depart_lower"]),
            ("تغييرات سعر", summary["price_changes"]),
        ]
    )
    link = f'<p><a href="{pages_url}">فتح لوحة التقرير</a></p>' if pages_url else ""
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head><meta charset="utf-8"><style>
body{{font-family:Arial,Tahoma,sans-serif;line-height:1.7;color:#172033}}
table{{border-collapse:collapse;width:100%;max-width:720px}}td,th{{border:1px solid #d9e1ee;padding:8px 10px}}th{{background:#f4f7fb}}
</style></head>
<body>
<h2>تقرير مقارنة أسعار مكعب وديبارت</h2>
<p>وقت التشغيل: {escape(report["generated_at"])}</p>
<table><tbody>{rows}</tbody></table>
{link}
</body></html>"""


def status_label(status: str) -> str:
    return {
        "depart_higher": "ديبارت أعلى",
        "depart_lower": "ديبارت أقل",
        "same_price": "نفس السعر",
        "needs_price": "سعر ناقص",
    }.get(status, status)


def build_dashboard(report: dict[str, Any], config: dict[str, Any]) -> str:
    data_json = json.dumps(report, ensure_ascii=False)
    summary = report["summary"]
    generated = escape(report["generated_at"])
    project = escape(config.get("project_name") or "مقارنة أسعار مكعب وديبارت")
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{project}</title>
  <style>
    :root {{
      --bg:#f4f6fb; --panel:#ffffff; --ink:#121826; --muted:#657084; --line:#e2e8f3;
      --green:#0f9f6e; --red:#d33f49; --blue:#2563eb; --amber:#c57911; --soft:#eef4ff;
      --shadow:0 18px 45px rgba(15,23,42,.08); --radius:22px;
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;background:linear-gradient(135deg,#f7f9fd,#eef3fb);font-family:"IBM Plex Sans Arabic","Tajawal","Segoe UI",Tahoma,Arial,sans-serif;color:var(--ink)}}
    a{{color:var(--blue);text-decoration:none}} a:hover{{text-decoration:underline}}
    .wrap{{max-width:1440px;margin:0 auto;padding:28px}}
    header{{display:flex;gap:18px;align-items:flex-start;justify-content:space-between;margin-bottom:18px}}
    .title h1{{margin:0 0 6px;font-size:30px;letter-spacing:-.5px}}
    .title p{{margin:0;color:var(--muted)}}
    .badge{{display:inline-flex;align-items:center;gap:8px;background:#111827;color:white;border-radius:999px;padding:9px 14px;font-size:13px;white-space:nowrap}}
    .grid{{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:12px;margin:18px 0}}
    .card{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:15px;box-shadow:var(--shadow)}}
    .card .k{{color:var(--muted);font-size:12px;margin-bottom:8px}} .card .v{{font-size:24px;font-weight:800}}
    .card.good .v{{color:var(--green)}} .card.bad .v{{color:var(--red)}} .card.warn .v{{color:var(--amber)}}
    .toolbar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px;box-shadow:var(--shadow);margin-bottom:14px}}
    input,select,button{{font:inherit;border:1px solid var(--line);border-radius:14px;background:white;padding:10px 12px;color:var(--ink)}}
    input{{min-width:290px;flex:1}} button{{cursor:pointer}} button.active{{background:#111827;color:white;border-color:#111827}}
    .panel{{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}}
    .panel-head{{display:flex;justify-content:space-between;gap:12px;padding:16px 18px;border-bottom:1px solid var(--line);align-items:center}}
    .panel-head h2{{font-size:18px;margin:0}} .count{{color:var(--muted);font-size:13px}}
    .table-wrap{{overflow:auto;max-height:70vh}}
    table{{width:100%;border-collapse:separate;border-spacing:0;min-width:1180px}}
    th,td{{padding:12px 12px;border-bottom:1px solid var(--line);vertical-align:top;text-align:right}}
    th{{position:sticky;top:0;background:#f8fafc;z-index:2;color:#445065;font-size:12px}}
    td{{font-size:13px}} .name{{font-weight:750;max-width:330px}} .muted{{color:var(--muted);font-size:12px}}
    .pill{{display:inline-flex;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;white-space:nowrap}}
    .pill.high{{background:#fee2e2;color:#991b1b}} .pill.low{{background:#dcfce7;color:#166534}}
    .pill.same{{background:#e0f2fe;color:#075985}} .pill.bundle{{background:#fef3c7;color:#92400e}}
    .price{{font-weight:800;white-space:nowrap}} .diff.pos{{color:var(--red)}} .diff.neg{{color:var(--green)}} .diff.zero{{color:var(--blue)}}
    .empty{{padding:30px;color:var(--muted);text-align:center}}
    .meta{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}}
    .note{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:18px;padding:14px;line-height:1.75}}
    @media(max-width:1000px){{.grid{{grid-template-columns:repeat(2,1fr)}}header{{display:block}}.badge{{margin-top:12px}}.meta{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="title">
      <h1>{project}</h1>
      <p>مطابقة بالباركود أولًا، مع فصل الباقات التي تحتوي أكثر من باركود عن المقارنة المباشرة.</p>
    </div>
    <div class="badge">آخر تحديث: {generated}</div>
  </header>

  <section class="grid">
    <div class="card"><div class="k">منتجات مكعب</div><div class="v">{summary["mokab_products"]}</div></div>
    <div class="card"><div class="k">منتجات ديبارت</div><div class="v">{summary["depart_products"]}</div></div>
    <div class="card good"><div class="k">تطابق مباشر</div><div class="v">{summary["exact_matches"]}</div></div>
    <div class="card warn"><div class="k">تطابق باقة</div><div class="v">{summary["bundle_matches"]}</div></div>
    <div class="card bad"><div class="k">ديبارت أعلى</div><div class="v">{summary["depart_higher"]}</div></div>
    <div class="card good"><div class="k">ديبارت أقل</div><div class="v">{summary["depart_lower"]}</div></div>
    <div class="card"><div class="k">غير موجود بمكعب</div><div class="v">{summary["unmatched_depart_products"]}</div></div>
    <div class="card"><div class="k">تغييرات اليوم</div><div class="v">{summary["price_changes"]}</div></div>
  </section>

  <section class="toolbar">
    <input id="q" placeholder="بحث بالاسم، البراند، الباركود...">
    <select id="matchType">
      <option value="all">كل التطابقات</option>
      <option value="exact_barcode">تطابق مباشر فقط</option>
      <option value="bundle_contains">باقات تحتاج مراجعة</option>
      <option value="unmatched_mokab">موجود عند مكعب فقط</option>
      <option value="unmatched_depart">موجود عند ديبارت فقط</option>
    </select>
    <select id="status">
      <option value="all">كل حالات السعر</option>
      <option value="depart_higher">ديبارت أعلى</option>
      <option value="depart_lower">ديبارت أقل</option>
      <option value="same_price">نفس السعر</option>
      <option value="needs_price">سعر ناقص</option>
    </select>
    <button id="reset">إعادة ضبط</button>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2 id="tableTitle">جدول المقارنة</h2>
      <div class="count" id="visibleCount"></div>
    </div>
    <div class="table-wrap" id="tableWrap"></div>
  </section>

  <div class="meta">
    <div class="note">التطابق المباشر يعني أن باركود مكعب يساوي GTIN ديبارت. هذا هو النوع المعتمد لاتخاذ قرار تسعير.</div>
    <div class="note">تطابق الباقة يعني أن SKU في مكعب يحتوي عدة باركودات. يظهر للمراجعة لأنه غالبًا Bundle وليس منتجًا مفردًا.</div>
  </div>
</div>

<script id="report-data" type="application/json">{escape(data_json)}</script>
<script>
const report = JSON.parse(document.getElementById('report-data').textContent);
const q = document.getElementById('q');
const matchType = document.getElementById('matchType');
const statusFilter = document.getElementById('status');
const tableWrap = document.getElementById('tableWrap');
const visibleCount = document.getElementById('visibleCount');
const tableTitle = document.getElementById('tableTitle');
document.getElementById('reset').onclick = () => {{ q.value=''; matchType.value='all'; statusFilter.value='all'; render(); }};
[q, matchType, statusFilter].forEach(el => el.addEventListener('input', render));

function price(v) {{ return v === null || v === undefined || v === '' ? '—' : Number(v).toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}}) + ' SAR'; }}
function safe(s) {{ return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function link(url, label) {{ return url ? `<a href="${{safe(url)}}" target="_blank" rel="noopener">${{label}}</a>` : '—'; }}
function statusPill(row) {{
  if (row.match_type === 'bundle_contains') return '<span class="pill bundle">باقة/مراجعة</span>';
  if (row.price_status === 'depart_higher') return '<span class="pill high">ديبارت أعلى</span>';
  if (row.price_status === 'depart_lower') return '<span class="pill low">ديبارت أقل</span>';
  if (row.price_status === 'same_price') return '<span class="pill same">نفس السعر</span>';
  return '<span class="pill">سعر ناقص</span>';
}}
function diffClass(v) {{ return v > 0 ? 'pos' : v < 0 ? 'neg' : 'zero'; }}
function rowText(row) {{
  const m = row.mokab || row;
  const d = row.depart || {{}};
  return [row.barcode, row.match_type, row.price_status, m.name, m.brand, m.category, m.sku, m.gtin, d.name, d.brand, d.sku, d.gtin].join(' ').toLowerCase();
}}
function sourceRows() {{
  const type = matchType.value;
  if (type === 'unmatched_mokab') return report.unmatched_mokab.map(p => ({{kind:'unmatched_mokab', mokab:p}}));
  if (type === 'unmatched_depart') return report.unmatched_depart.map(p => ({{kind:'unmatched_depart', depart:p}}));
  return report.matches.filter(r => type === 'all' || r.match_type === type);
}}
function render() {{
  const needle = q.value.trim().toLowerCase();
  const st = statusFilter.value;
  let rows = sourceRows().filter(r => !needle || rowText(r).includes(needle));
  if (matchType.value !== 'unmatched_mokab' && matchType.value !== 'unmatched_depart' && st !== 'all') rows = rows.filter(r => r.price_status === st);
  visibleCount.textContent = rows.length + ' صف ظاهر';
  tableTitle.textContent = matchType.options[matchType.selectedIndex].textContent;
  if (!rows.length) {{ tableWrap.innerHTML = '<div class="empty">لا توجد نتائج بهذا الفلتر.</div>'; return; }}
  if (matchType.value === 'unmatched_mokab' || matchType.value === 'unmatched_depart') {{
    tableWrap.innerHTML = `<table><thead><tr><th>المصدر</th><th>المنتج</th><th>البراند</th><th>الفئة</th><th>السعر</th><th>الباركود/SKU</th><th>الرابط</th></tr></thead><tbody>` + rows.map(r => {{
      const p = r.mokab || r.depart; const src = r.mokab ? 'مكعب' : 'ديبارت';
      return `<tr><td>${{src}}</td><td class="name">${{safe(p.name)}}<div class="muted">${{safe(p.id)}}</div></td><td>${{safe(p.brand)}}</td><td>${{safe(p.category)}}</td><td class="price">${{price(p.price)}}</td><td><div>${{safe((p.barcodes||[]).join(', '))}}</div><div class="muted">${{safe(p.sku || p.gtin)}}</div></td><td>${{link(p.url,'فتح')}}</td></tr>`;
    }}).join('') + '</tbody></table>';
    return;
  }}
  tableWrap.innerHTML = `<table><thead><tr><th>الحالة</th><th>الباركود</th><th>منتج مكعب</th><th>سعر مكعب</th><th>منتج ديبارت</th><th>سعر ديبارت</th><th>الفرق</th><th>%</th><th>روابط</th></tr></thead><tbody>` + rows.map(r => {{
    const diff = r.depart_minus_mokab;
    return `<tr>
      <td>${{statusPill(r)}}<div class="muted">${{safe(r.confidence)}}</div></td>
      <td>${{safe(r.barcode)}}</td>
      <td class="name">${{safe(r.mokab.name)}}<div class="muted">${{safe(r.mokab.brand)}} · ${{safe(r.mokab.category)}}</div></td>
      <td class="price">${{price(r.mokab.price)}}<div class="muted">الأصلي: ${{price(r.mokab.regular_price)}}</div></td>
      <td class="name">${{safe(r.depart.name)}}<div class="muted">${{safe(r.depart.brand)}} · ${{safe(r.depart.category)}}</div></td>
      <td class="price">${{price(r.depart.price)}}</td>
      <td class="price diff ${{diffClass(diff || 0)}}">${{price(diff)}}</td>
      <td class="diff ${{diffClass(diff || 0)}}">${{r.gap_pct_vs_mokab == null ? '—' : (r.gap_pct_vs_mokab > 0 ? '+' : '') + r.gap_pct_vs_mokab + '%'}}</td>
      <td>${{link(r.mokab.url,'مكعب')}}<br>${{link(r.depart.url,'ديبارت')}}</td>
    </tr>`;
  }}).join('') + '</tbody></table>';
}}
render();
</script>
</body>
</html>"""


def send_email_if_configured(html_body: str) -> bool:
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM", "EMAIL_TO"]
    if not all(os.getenv(k) for k in required):
        print("Email skipped: SMTP secrets are not fully configured.", flush=True)
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ["EMAIL_FROM"]
    recipients = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = "تقرير مقارنة أسعار مكعب وديبارت"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    try:
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
        print(f"Email sent to {', '.join(recipients)}", flush=True)
        return True
    finally:
        server.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Mokab and Depart prices by barcode.")
    parser.add_argument("--mode", choices=["snapshot", "live"], default=os.getenv("TRACKER_MODE", "snapshot"))
    parser.add_argument("--send-email", action="store_true", help="Send email when SMTP env vars are configured.")
    args = parser.parse_args()

    config = load_config()
    docs_dir = resolve_path(config.get("docs_dir"), "docs")
    history_dir = resolve_path(config.get("history_dir"), "data/history")
    latest_state_path = resolve_path(config.get("latest_state_path"), "data/latest_comparison.json")
    depart_path = resolve_path(config.get("depart_products_path"), "data/depart_products.json")

    previous = read_json(latest_state_path, None)
    previous_exists = isinstance(previous, dict)

    depart_products = load_products_file(depart_path, "depart")
    mokab_products, mokab_meta = load_mokab(config, args.mode)

    comparison = compare_products(mokab_products, depart_products, previous)
    report = {
        "project": config.get("project_name") or "مقارنة أسعار مكعب وديبارت",
        "generated_at": iso_now(config),
        "mode": args.mode,
        "source_meta": {"mokab": mokab_meta, "depart_path": str(depart_path)},
        **comparison,
    }

    date_key = local_now(config).strftime("%Y-%m-%d")
    docs_data_dir = docs_dir / "data"
    docs_data_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    write_json(latest_state_path, report)
    write_json(history_dir / f"{date_key}.json", report)
    write_json(docs_data_dir / "latest.json", report)

    dashboard = build_dashboard(report, config)
    (docs_dir / "index.html").write_text(dashboard, encoding="utf-8")

    issue_body = build_issue_body(report, config, previous_exists)
    alert_only = bool(config.get("alert_only_on_changes"))
    if not alert_only or report["summary"]["price_changes"] or not previous_exists:
        (REPO_ROOT / "issue_body.md").write_text(issue_body, encoding="utf-8")
    elif (REPO_ROOT / "issue_body.md").exists():
        (REPO_ROOT / "issue_body.md").unlink()

    email_html = build_email_html(report, config)
    (REPO_ROOT / "email_body.html").write_text(email_html, encoding="utf-8")
    if args.send_email:
        send_email_if_configured(email_html)

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print("Dashboard generated: docs/index.html", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
