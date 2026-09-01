
import json
from datetime import datetime, timezone

import streamlit as st

QUEUE_TAB = "Scanner Queue"
QUEUE_HEADERS = [
    "Queued At", "Product Name", "Product Link", "Product ID",
    "Creators", "Creator Count", "Video Count", "Combined Views",
    "Source Video", "Status", "Imported At", "Flow Batch ID",
]


def _secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def _service_account_info():
    raw = _secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        if isinstance(raw, dict):
            return dict(raw)
        try:
            return json.loads(str(raw))
        except Exception:
            pass
    try:
        table = st.secrets.get("gcp_service_account")
        if table:
            return dict(table)
    except Exception:
        pass
    return None


def _sheet_ref():
    return str(
        _secret("FLOW_FASHION_GOOGLE_SHEET_URL")
        or _secret("GOOGLE_SHEET_URL")
        or ""
    ).strip()


def _norm_url(value):
    s = str(value or "").strip()
    return s if s.startswith(("http://", "https://")) else ""


def _to_int(value):
    try:
        return int(float(str(value or "0").replace(",", "").strip()))
    except Exception:
        return 0


def _first(row, names, default=""):
    if not isinstance(row, dict):
        return default
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
        if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
            return lowered[name.lower()]
    return default


def _looks_like_product_row(row):
    if not isinstance(row, dict):
        return False
    pid = str(_first(row, ["product_id", "Product ID", "productId"]) or "").strip()
    purl = _norm_url(_first(row, ["product_url", "Product URL", "product_link", "Product Link"]))
    pname = str(_first(row, ["product_title", "Product", "Product Name", "product_name", "title"]) or "").strip()
    return bool(pid or purl) and bool(pname or pid)


def _extract_records(value, depth=0):
    if depth > 4:
        return []
    out = []

    if hasattr(value, "to_dict") and value.__class__.__name__ == "DataFrame":
        try:
            value = value.to_dict("records")
        except Exception:
            return out

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and _looks_like_product_row(item):
                out.append(item)
            elif isinstance(item, (list, dict, tuple)):
                out.extend(_extract_records(item, depth + 1))
    elif isinstance(value, tuple):
        for item in value:
            out.extend(_extract_records(item, depth + 1))
    elif isinstance(value, dict):
        if _looks_like_product_row(value):
            out.append(value)
        else:
            for child in value.values():
                if isinstance(child, (dict, list, tuple)) or (
                    hasattr(child, "to_dict") and child.__class__.__name__ == "DataFrame"
                ):
                    out.extend(_extract_records(child, depth + 1))
    return out


def _session_rows():
    rows = []
    for key, value in st.session_state.items():
        if str(key).startswith("_"):
            continue
        try:
            rows.extend(_extract_records(value))
        except Exception:
            pass
    return rows


def _normalize(rows):
    grouped = {}

    for row in rows:
        pid = str(_first(row, ["product_id", "Product ID", "productId"]) or "").strip()
        purl = _norm_url(_first(row, ["product_url", "Product URL", "product_link", "Product Link"]))
        pname = str(_first(row, ["product_title", "Product", "Product Name", "product_name", "title"]) or "").strip()

        if not pid and purl:
            pid = purl.rstrip("/").split("/")[-1].split("?")[0]
        if not pid and not purl:
            continue

        creator = str(_first(row, ["creator", "Creator", "username", "author"]) or "").strip()
        creators_raw = _first(row, ["creators", "Creators", "creator_names"])
        creators = []
        if isinstance(creators_raw, (list, tuple, set)):
            creators = [str(x).strip() for x in creators_raw if str(x).strip()]
        elif creators_raw:
            creators = [x.strip() for x in str(creators_raw).replace("|", ",").split(",") if x.strip()]
        if creator:
            creators.append(creator)

        creator_count = _to_int(_first(row, ["creator_count", "Creator Count", "creators_promoting"]))
        video_count = _to_int(_first(row, ["video_count", "Video Count", "videos", "Videos"]))
        views = _to_int(_first(row, ["views", "Views", "view_count", "play_count"]))
        source_video = _norm_url(_first(row, ["video_url", "Video URL", "source_video", "Source Video"]))

        key = pid or purl
        item = grouped.setdefault(key, {
            "product_id": pid,
            "product_name": pname or f"Product {pid}",
            "product_url": purl,
            "creators": set(),
            "creator_count_reported": 0,
            "video_count": 0,
            "combined_views": 0,
            "source_video": source_video,
        })
        item["creators"].update(creators)
        item["creator_count_reported"] = max(item["creator_count_reported"], creator_count)
        item["video_count"] += video_count if video_count else 1
        item["combined_views"] += views
        if source_video and not item["source_video"]:
            item["source_video"] = source_video
        if pname and item["product_name"].startswith("Product "):
            item["product_name"] = pname
        if purl and not item["product_url"]:
            item["product_url"] = purl

    products = []
    for item in grouped.values():
        creators = sorted(item.pop("creators"))
        reported = item.pop("creator_count_reported")
        products.append({
            **item,
            "creators": ", ".join(creators),
            "creator_count": max(len(creators), reported),
        })

    products.sort(
        key=lambda x: (x["creator_count"], x["video_count"], x["combined_views"]),
        reverse=True,
    )
    return products


def _open_book():
    info = _service_account_info()
    ref = _sheet_ref()
    if not info:
        raise RuntimeError("Add the same Google service-account secret used by Flow Fashion.")
    if not ref:
        raise RuntimeError("Add FLOW_FASHION_GOOGLE_SHEET_URL or GOOGLE_SHEET_URL to Scanner secrets.")

    import gspread
    gc = gspread.service_account_from_dict(info)
    return gc.open_by_url(ref) if ref.startswith(("http://", "https://")) else gc.open_by_key(ref)


def _ensure_queue(book):
    import gspread
    try:
        ws = book.worksheet(QUEUE_TAB)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=QUEUE_TAB, rows=1000, cols=14)
        ws.update(range_name="A1", values=[QUEUE_HEADERS], value_input_option="RAW")
    return ws


def _push(products, requeue_imported=False):
    ws = _ensure_queue(_open_book())
    existing = ws.get_all_values()
    by_key = {}

    for row_num, raw in enumerate(existing[1:], start=2):
        padded = raw + [""] * max(0, len(QUEUE_HEADERS) - len(raw))
        rec = dict(zip(QUEUE_HEADERS, padded))
        key = str(rec.get("Product ID") or rec.get("Product Link") or "").strip()
        if key:
            by_key[key] = (row_num, rec)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    added = updated = skipped = 0

    for p in products:
        key = str(p.get("product_id") or p.get("product_url") or "").strip()
        if not key:
            continue

        row = [
            now,
            p.get("product_name", ""),
            p.get("product_url", ""),
            p.get("product_id", ""),
            p.get("creators", ""),
            str(p.get("creator_count", 0)),
            str(p.get("video_count", 0)),
            str(p.get("combined_views", 0)),
            p.get("source_video", ""),
            "Pending",
            "",
            "",
        ]

        if key in by_key:
            row_num, old = by_key[key]
            if str(old.get("Status") or "").strip().lower() == "imported" and not requeue_imported:
                skipped += 1
                continue
            ws.update(range_name=f"A{row_num}:L{row_num}", values=[row], value_input_option="USER_ENTERED")
            updated += 1
        else:
            ws.append_row(row, value_input_option="USER_ENTERED")
            added += 1

    return added, updated, skipped


st.set_page_config(page_title="Send to Flow Fashion", page_icon="➡️", layout="wide")
st.title("Send to Flow Fashion")
st.caption("Select products from the current Creator Scanner results and send them to the shared Flow Fashion queue.")

ready = bool(_sheet_ref() and _service_account_info())
if not ready:
    st.warning("Add the same Google Sheet URL and Google service-account credentials used by Flow Fashion to this Scanner app's secrets.")

products = _normalize(_session_rows())
if not products:
    st.info("Run a Creator Scanner scan first, then return to this page.")
    st.stop()

st.success(f"Found {len(products)} unique product(s) in the current scan.")

rows = [{
    "Send": False,
    "Product": p["product_name"],
    "Creators": p["creators"],
    "Creator Count": p["creator_count"],
    "Video Count": p["video_count"],
    "Views": p["combined_views"],
    "Product Link": p["product_url"],
    "Product ID": p["product_id"],
    "Source Video": p["source_video"],
} for p in products]

edited = st.data_editor(
    rows,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Send": st.column_config.CheckboxColumn("Send", default=False),
        "Product Link": st.column_config.LinkColumn("Product Link"),
        "Source Video": st.column_config.LinkColumn("Source Video"),
    },
    disabled=["Product", "Creators", "Creator Count", "Video Count", "Views", "Product Link", "Product ID", "Source Video"],
    key="flow_fashion_send_editor",
)

selected_idx = [i for i, row in enumerate(edited) if bool(row.get("Send"))]
selected = [products[i] for i in selected_idx]

c1, c2 = st.columns(2)
c1.metric("Selected", len(selected))
requeue = c2.checkbox("Requeue products already marked Imported", value=False)

if st.button(
    f"Send {len(selected)} selected product(s) to Flow Fashion",
    type="primary",
    use_container_width=True,
    disabled=not bool(selected and ready),
):
    try:
        with st.spinner("Updating Scanner Queue…"):
            added, updated, skipped = _push(selected, requeue_imported=requeue)
        st.success(
            f"Queue updated: {added} new, {updated} refreshed"
            + (f", {skipped} already imported and skipped." if skipped else ".")
        )
    except Exception as exc:
        st.error(f"Could not send products: {exc}")
