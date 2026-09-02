from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from parser import (
    extract_product_ids,
    extract_product_title,
    normalize_creator,
    normalize_product,
    normalize_video,
    video_id,
)
from storage import get_cached_product, set_cached_product
from tikhub_client import TikHubClient, TikHubError

load_dotenv()

st.set_page_config(page_title="Creator Product Scanner", page_icon="🛍️", layout="wide")

st.markdown(
    """
<style>
.block-container {padding-top: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.22); padding: 14px; border-radius: 12px;}
.small-note {opacity: .72; font-size: .88rem;}
.filter-card {border:1px solid rgba(128,128,128,.25); border-radius:16px; padding:14px 16px; margin-bottom:8px;}
</style>
""",
    unsafe_allow_html=True,
)

GROUP_DB = Path(__file__).with_name("scanner_cache.sqlite3")


def get_api_key() -> str:
    key = os.getenv("TIKHUB_API_KEY", "")
    if key:
        return key
    try:
        return str(st.secrets.get("TIKHUB_API_KEY", ""))
    except Exception:
        return ""


def _group_conn() -> sqlite3.Connection:
    """Open the group DB and migrate older creator_groups schemas in place."""
    conn = sqlite3.connect(GROUP_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_groups (
            name TEXT PRIMARY KEY,
            creators_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # v6 and earlier installs may already have creator_groups with a different
    # creators column. CREATE TABLE IF NOT EXISTS does not change an existing
    # SQLite schema, so migrate it safely instead of crashing on startup.
    columns = {
        str(row[1]): row
        for row in conn.execute("PRAGMA table_info(creator_groups)").fetchall()
    }

    if "creators_json" not in columns:
        conn.execute("ALTER TABLE creator_groups ADD COLUMN creators_json TEXT")
        legacy_source = next(
            (
                col
                for col in (
                    "creators",
                    "creator_list",
                    "members_json",
                    "handles_json",
                    "creator_handles",
                )
                if col in columns
            ),
            None,
        )
        if legacy_source:
            conn.execute(
                f'UPDATE creator_groups SET creators_json = "{legacy_source}" '
                "WHERE creators_json IS NULL OR creators_json = ''"
            )
        conn.execute(
            "UPDATE creator_groups SET creators_json = '[]' "
            "WHERE creators_json IS NULL OR creators_json = ''"
        )

    if "updated_at" not in columns:
        conn.execute(
            "ALTER TABLE creator_groups "
            "ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE creator_groups SET updated_at = strftime('%s','now') "
            "WHERE updated_at = 0"
        )

    conn.commit()
    return conn


def load_groups() -> dict[str, list[str]]:
    with _group_conn() as conn:
        rows = conn.execute("SELECT name, creators_json FROM creator_groups ORDER BY name COLLATE NOCASE").fetchall()
    result: dict[str, list[str]] = {}
    for name, payload in rows:
        try:
            values = json.loads(payload or "[]")
            if not isinstance(values, list):
                values = [values]
        except Exception:
            # Backward compatibility for older DBs that stored creators as
            # comma/newline-delimited text instead of JSON.
            values = [
                x.strip()
                for x in re.split(r"[\\n,]+", str(payload or ""))
                if x.strip()
            ]
        clean = unique_creators([str(x) for x in values if str(x).strip()])
        if clean:
            result[str(name)] = clean
    return result


def save_group(name: str, creators: list[str]) -> None:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        raise ValueError("Enter a group name.")
    clean = unique_creators(creators)
    if not clean:
        raise ValueError("Add at least one creator to the group.")
    with _group_conn() as conn:
        conn.execute(
            """
            INSERT INTO creator_groups(name, creators_json, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(name) DO UPDATE SET
              creators_json=excluded.creators_json,
              updated_at=excluded.updated_at
            """,
            (name, json.dumps(clean)),
        )


def delete_group(name: str) -> None:
    with _group_conn() as conn:
        conn.execute("DELETE FROM creator_groups WHERE name = ?", (name,))


def unique_creators(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        raw = (raw or "").strip()
        if not raw:
            continue
        creator = normalize_creator(raw)
        key = creator.lower()
        if key not in seen:
            seen.add(key)
            out.append(creator)
    return out


def parse_creator_lines(text: str) -> list[str]:
    return unique_creators([x for x in re.split(r"[\n,]+", text or "") if x.strip()])


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def date_cutoff(label: str) -> int | None:
    now = datetime.now(timezone.utc)
    if label == "Yesterday / past 24 hours":
        return int((now - timedelta(hours=24)).timestamp())
    if label == "Last 7 days":
        return int((now - timedelta(days=7)).timestamp())
    if label == "Last 30 days":
        return int((now - timedelta(days=30)).timestamp())
    return None


def apply_sort(df: pd.DataFrame, sort_label: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["views"] = pd.to_numeric(out.get("views"), errors="coerce").fillna(0)
    if sort_label == "Views — highest first":
        return out.sort_values(["views", "posted_at"], ascending=[False, False], kind="stable")
    if sort_label == "Views — lowest first":
        return out.sort_values(["views", "posted_at"], ascending=[True, False], kind="stable")
    if sort_label == "Oldest first":
        return out.sort_values("posted_at", ascending=True, kind="stable")
    return out.sort_values("posted_at", ascending=False, kind="stable")


def detail_map(client: TikHubClient, ids: list[str], region: str, status_box: Any, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    chunks = chunked(ids, 10)
    for i, group in enumerate(chunks, start=1):
        status_box.write(f"{label}: inspecting video details — batch {i}/{len(chunks)}")
        for item in client.batch_video_details(group, region=region):
            vid = video_id(item)
            if vid:
                result[vid] = item
    return result


def scan_one_creator(
    client: TikHubClient,
    creator: str,
    video_limit: int,
    region: str,
    posted_after: int | None,
    status_box: Any,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    status_box.write(f"@{creator}: loading creator videos…")
    feed_videos = client.get_creator_videos(
        creator,
        video_limit,
        posted_after=posted_after,
    )
    if not feed_videos:
        return [], {}, {}

    feed_map = {video_id(v): v for v in feed_videos if video_id(v)}
    ids = list(feed_map.keys())
    status_box.write(f"@{creator}: {len(ids)} videos match the date filter. Checking products…")
    details = detail_map(client, ids, region, status_box, f"@{creator}")

    product_candidates_by_video: dict[str, list[str]] = {}
    detection_source: dict[str, str] = {}
    diagnostics: dict[str, Any] = {}

    def remember(vid: str, source: str, payload: Any) -> bool:
        candidates = extract_product_ids(payload)
        if candidates:
            product_candidates_by_video[vid] = candidates
            detection_source[vid] = source
            return True
        return False

    unresolved: list[str] = []
    for vid in ids:
        if remember(vid, "feed", feed_map.get(vid, {})) or remember(vid, "batch", details.get(vid, {})):
            continue
        unresolved.append(vid)

    if unresolved:
        status_box.write(f"@{creator}: deeper Shop detection for {len(unresolved)} video(s)…")
        for vid in unresolved:
            found = False
            checks = [
                ("app_v2", lambda vid=vid: client._request(
                    "GET", "/api/v1/tiktok/app/v3/fetch_one_video_v2", params={"aweme_id": vid}
                )),
                ("app_v3_region", lambda vid=vid: client.app_video_detail_v3(vid, region)),
                ("web_v2", lambda vid=vid: client.web_video_detail_v2(vid, region)),
                ("web_v1", lambda vid=vid: client.web_video_detail(vid, region)),
            ]
            last_payload = None
            for source, fn in checks:
                try:
                    payload = fn()
                    last_payload = payload
                    if remember(vid, source, payload):
                        found = True
                        break
                except TikHubError as exc:
                    diagnostics.setdefault(vid, {})[source + "_error"] = str(exc)
            if not found and last_payload is not None and len(diagnostics) < 8:
                diagnostics.setdefault(vid, {})["last_payload"] = last_payload

    product_payloads: dict[str, dict[str, Any]] = {}
    validated_pid_by_video: dict[str, str] = {}
    all_candidate_ids = list(dict.fromkeys(
        pid for vid in ids for pid in product_candidates_by_video.get(vid, [])[:4]
    ))
    status_box.write(f"@{creator}: validating {len(all_candidate_ids)} product candidate(s)…")
    validation_cache: dict[str, bool] = {}
    for pid in all_candidate_ids:
        cached = get_cached_product(pid, region)
        if cached is not None:
            product_payloads[pid] = cached
            validation_cache[pid] = True
        else:
            try:
                payload = client.product_detail(pid, region)
                product_payloads[pid] = payload
                set_cached_product(pid, region, payload)
                validation_cache[pid] = True
            except TikHubError:
                validation_cache[pid] = False

    for vid in ids:
        for pid in product_candidates_by_video.get(vid, [])[:4]:
            if validation_cache.get(pid):
                validated_pid_by_video[vid] = pid
                break

    rows: list[dict[str, Any]] = []
    for vid in ids:
        feed = feed_map[vid]
        video_row = normalize_video(feed, creator)
        video_row["creator"] = creator
        pid = validated_pid_by_video.get(vid)
        if pid:
            fallback_title = (
                extract_product_title(feed, pid)
                or extract_product_title(details.get(vid, {}), pid)
            )
            product_row = normalize_product(
                pid,
                product_payloads.get(pid, {}),
                region,
                fallback_title=fallback_title,
            )
            product_row["product_status"] = "Tagged product"
            product_row["detection_source"] = detection_source.get(vid, "")
        else:
            product_row = {
                "product_id": "",
                "product_title": "No product",
                "product_description": "",
                "product_image": "",
                "price": "",
                "currency": "",
                "seller": "",
                "sold": None,
                "rating": None,
                "review_count": None,
                "product_url": "",
                "region": region,
                "product_status": "No product",
                "detection_source": "",
            }
        rows.append({**video_row, **product_row})

    return rows, detection_source, diagnostics


def display_video_results(df: pd.DataFrame) -> None:
    visible_cols = [
        "cover_url", "creator", "posted_at", "views", "likes", "caption",
        "product_title", "price", "seller", "video_url", "product_url",
    ]
    visible_cols = [c for c in visible_cols if c in df.columns]
    st.dataframe(
        df[visible_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "cover_url": st.column_config.ImageColumn("Video"),
            "creator": "Creator",
            "posted_at": "Posted",
            "views": st.column_config.NumberColumn("Views", format="localized"),
            "likes": st.column_config.NumberColumn("Likes", format="localized"),
            "caption": st.column_config.TextColumn("Caption", width="large"),
            "product_title": st.column_config.TextColumn("Product", width="medium"),
            "price": "Price",
            "seller": "Seller",
            "video_url": st.column_config.LinkColumn("Open video", display_text="Open"),
            "product_url": st.column_config.LinkColumn("Open product", display_text="Open"),
        },
    )


def grouped_products(df: pd.DataFrame, sort_choice: str) -> pd.DataFrame:
    product_df = df[df["product_id"].fillna("") != ""].copy()
    if product_df.empty:
        return pd.DataFrame()
    grouped = (
        product_df.groupby("product_id", dropna=False)
        .agg(
            product_title=("product_title", "first"),
            product_image=("product_image", "first"),
            seller=("seller", "first"),
            price=("price", "first"),
            product_url=("product_url", "first"),
            videos=("video_id", "count"),
            combined_views=("views", "sum"),
            best_video_views=("views", "max"),
            creators=("creator", lambda s: ", ".join(sorted(set(str(x) for x in s if str(x))))),
            creator_count=("creator", lambda s: len(set(str(x) for x in s if str(x)))),
        )
        .reset_index()
    )
    sort_map = {
        "Combined views": ("combined_views", False),
        "Best video views": ("best_video_views", False),
        "Creator count": ("creator_count", False),
        "Video count": ("videos", False),
    }
    col, asc = sort_map.get(sort_choice, ("combined_views", False))
    return grouped.sort_values(col, ascending=asc, kind="stable")


def display_products(df: pd.DataFrame) -> None:
    sort_choice = st.selectbox(
        "Sort products by",
        ["Combined views", "Best video views", "Creator count", "Video count"],
        index=0,
    )
    grouped = grouped_products(df, sort_choice)
    if grouped.empty:
        st.info("No tagged products were detected in these videos.")
        return
    st.dataframe(
        grouped,
        use_container_width=True,
        hide_index=True,
        column_config={
            "product_image": st.column_config.ImageColumn("Product"),
            "product_title": st.column_config.TextColumn("Product name", width="large"),
            "creators": st.column_config.TextColumn("Promoted by", width="medium"),
            "creator_count": st.column_config.NumberColumn("Creators"),
            "videos": st.column_config.NumberColumn("Videos"),
            "combined_views": st.column_config.NumberColumn("Combined views", format="localized"),
            "best_video_views": st.column_config.NumberColumn("Best video", format="localized"),
            "product_url": st.column_config.LinkColumn("TikTok Shop", display_text="Open"),
        },
    )


st.title("Creator Product Scanner")
st.caption(
    "Scan one creator, multiple creators, or a saved group → filter by posting date before paid Shop checks → identify attached TikTok Shop products."
)

api_key = get_api_key()
if not api_key:
    st.error("TikHub API key is not configured. Add TIKHUB_API_KEY to Streamlit secrets.")
    st.code('TIKHUB_API_KEY = "your_api_key"', language="toml")
    st.stop()

# ---------------- Creator groups ----------------
groups = load_groups()
with st.expander("Creator groups", expanded=False):
    gc1, gc2 = st.columns([1, 2])
    with gc1:
        group_name = st.text_input("Group name", placeholder="Menswear winners")
    with gc2:
        group_creators = st.text_area(
            "Creators in group",
            placeholder="@creator1\n@creator2\nhttps://www.tiktok.com/@creator3",
            height=100,
        )
    s1, s2 = st.columns(2)
    if s1.button("Save / update group", use_container_width=True):
        try:
            save_group(group_name, parse_creator_lines(group_creators))
            st.success(f"Saved group: {group_name}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if groups:
        delete_name = s2.selectbox("Delete group", ["—"] + list(groups.keys()), label_visibility="collapsed")
        if delete_name != "—" and s2.button("Delete selected group", use_container_width=True):
            delete_group(delete_name)
            st.rerun()

    backup = json.dumps({"creator_groups": groups}, indent=2).encode("utf-8")
    b1, b2 = st.columns(2)
    b1.download_button(
        "Backup creator groups",
        data=backup,
        file_name="creator_groups_backup.json",
        mime="application/json",
        use_container_width=True,
    )
    restore = b2.file_uploader("Restore creator groups", type=["json"], label_visibility="collapsed")
    if restore and st.button("Restore uploaded groups", use_container_width=True):
        try:
            payload = json.loads(restore.getvalue().decode("utf-8"))
            incoming = payload.get("creator_groups", payload)
            if not isinstance(incoming, dict):
                raise ValueError("Backup file is not a creator-group JSON object.")
            for name, values in incoming.items():
                if isinstance(values, list):
                    save_group(str(name), [str(x) for x in values])
            st.success("Creator groups restored.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

# ---------------- Scan form ----------------
with st.form("scan_form"):
    st.markdown("### Scan filters")
    f1, f2, f3, f4 = st.columns([1.15, 1.25, 1, 1])
    with f1:
        date_range = st.selectbox(
            "Date range",
            ["Yesterday / past 24 hours", "Last 7 days", "Last 30 days", "Any time"],
            index=1,
            help="Applied while creator pages are fetched, before product-detection calls.",
        )
    with f2:
        sort_results = st.selectbox(
            "Sort results",
            ["Views — highest first", "Newest first", "Views — lowest first", "Oldest first"],
            index=0,
        )
    with f3:
        video_limit = int(st.number_input(
            "Videos per creator",
            min_value=1,
            max_value=2000,
            value=20,
            step=1,
            help="Maximum matching videos to scan for each creator.",
        ))
    with f4:
        region = st.selectbox("TikTok Shop region", ["US", "GB", "SG", "MY", "PH", "TH", "VN", "ID"], index=0)

    st.markdown("### Creators")
    mode = st.radio("Scan mode", ["One creator", "Multiple creators", "Saved group"], horizontal=True)
    creator_values: list[str] = []
    if mode == "One creator":
        one_creator = st.text_input("TikTok creator", placeholder="@creatorname or TikTok profile URL")
        if one_creator.strip():
            creator_values = [one_creator]
    elif mode == "Multiple creators":
        many_creators = st.text_area(
            "TikTok creators",
            placeholder="@creator1\n@creator2\n@creator3",
            height=125,
        )
        creator_values = [x for x in re.split(r"[\n,]+", many_creators) if x.strip()]
    else:
        if groups:
            chosen_group = st.selectbox("Saved creator group", list(groups.keys()))
            creator_values = groups.get(chosen_group, [])
            st.caption(f"{len(creator_values)} creator(s) in this group")
        else:
            st.info("No saved groups yet. Create one above first.")

    scan = st.form_submit_button("Scan creators", type="primary", use_container_width=True)

if scan:
    try:
        creators = unique_creators(creator_values)
        if not creators:
            raise ValueError("Add at least one TikTok creator.")
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    client = TikHubClient(api_key=api_key)
    all_rows: list[dict[str, Any]] = []
    all_sources: dict[str, str] = {}
    all_diagnostics: dict[str, Any] = {}
    creator_errors: list[tuple[str, str]] = []
    cutoff = date_cutoff(date_range)

    status = st.status(f"Scanning {len(creators)} creator(s)", expanded=True)
    overall = st.progress(0.0)
    for idx, creator in enumerate(creators, start=1):
        try:
            rows, sources, diagnostics = scan_one_creator(
                client,
                creator,
                video_limit,
                region,
                cutoff,
                status,
            )
            all_rows.extend(rows)
            all_sources.update(sources)
            all_diagnostics.update({f"{creator}:{k}": v for k, v in diagnostics.items()})
            status.write(f"@{creator}: finished with {len(rows)} matching video(s).")
        except Exception as exc:
            creator_errors.append((creator, str(exc)))
            status.write(f"@{creator}: skipped after error — {exc}")
        overall.progress(idx / len(creators))

    if all_rows:
        df = apply_sort(pd.DataFrame(all_rows), sort_results)
        st.session_state["scan_df"] = df
        st.session_state["scan_creators"] = creators
        st.session_state["scan_creator"] = creators[0] if len(creators) == 1 else f"{len(creators)}_creators"
        st.session_state["scan_api_requests"] = client.usage.requests
        st.session_state["scan_detection_source"] = all_sources
        st.session_state["scan_diagnostics"] = all_diagnostics
        st.session_state["scan_creator_errors"] = creator_errors
        st.session_state["scan_date_range"] = date_range
        st.session_state["scan_sort"] = sort_results
        status.update(label=f"Scan complete — {len(all_rows)} video(s)", state="complete", expanded=False)
    else:
        status.update(label="No videos matched this scan", state="error", expanded=True)
        if creator_errors:
            for creator, error in creator_errors:
                st.error(f"@{creator}: {error}")
        else:
            st.info(f"No creator videos matched the selected date range: {date_range}.")

# ---------------- Results ----------------
if "scan_df" in st.session_state:
    df = st.session_state["scan_df"].copy()
    creators = st.session_state.get("scan_creators", [])
    creator_label = st.session_state.get("scan_creator", "creators")

    product_mask = df["product_id"].fillna("") != ""
    unique_products = int(df.loc[product_mask, "product_id"].nunique())
    product_videos = int(product_mask.sum())
    requests_used = int(st.session_state.get("scan_api_requests", 0))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Creators", len(creators) or int(df["creator"].nunique()))
    m2.metric("Videos scanned", len(df))
    m3.metric("Product videos", product_videos)
    m4.metric("Unique products", unique_products)
    m5.metric("API requests", requests_used)

    st.caption(
        f"Date filter: {st.session_state.get('scan_date_range', 'Any time')} · "
        f"Sort: {st.session_state.get('scan_sort', 'Views — highest first')}"
    )

    errors = st.session_state.get("scan_creator_errors", [])
    if errors:
        with st.expander(f"{len(errors)} creator(s) had errors"):
            for creator, error in errors:
                st.warning(f"@{creator}: {error}")

    tabs = st.tabs(["Videos", "Products"])
    with tabs[0]:
        f1, f2, f3 = st.columns([1, 1.4, 2])
        with f1:
            only_products = st.checkbox("Product videos only", value=False)
        with f2:
            display_sort = st.selectbox(
                "Display sort",
                ["Views — highest first", "Newest first", "Views — lowest first", "Oldest first"],
                index=["Views — highest first", "Newest first", "Views — lowest first", "Oldest first"].index(
                    st.session_state.get("scan_sort", "Views — highest first")
                ),
            )
        with f3:
            search = st.text_input("Filter", placeholder="Search creator, caption, product or seller")
        shown = df.copy()
        if only_products:
            shown = shown[shown["product_id"].fillna("") != ""]
        if search.strip():
            q = search.strip().lower()
            mask = (
                shown["creator"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["caption"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["product_title"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["seller"].fillna("").str.lower().str.contains(q, regex=False)
            )
            shown = shown[mask]
        display_video_results(apply_sort(shown, display_sort))

    with tabs[1]:
        display_products(df)

    with st.expander("Detection diagnostics"):
        sources = st.session_state.get("scan_detection_source", {})
        if sources:
            source_counts = pd.Series(list(sources.values())).value_counts().rename_axis("source").reset_index(name="videos")
            st.dataframe(source_counts, hide_index=True, use_container_width=True)
        diagnostics = st.session_state.get("scan_diagnostics", {})
        if diagnostics:
            diagnostic_json = json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            st.download_button(
                "Download unresolved video diagnostics",
                data=diagnostic_json,
                file_name=f"{creator_label}_shop_detection_diagnostics.json",
                mime="application/json",
            )
        else:
            st.success("No unresolved diagnostic payloads were needed.")

    e1, e2 = st.columns(2)
    e1.download_button(
        "Export video CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{creator_label}_tiktok_videos.csv",
        mime="text/csv",
        use_container_width=True,
    )
    product_export = grouped_products(df, "Combined views")
    e2.download_button(
        "Export unique-product CSV",
        data=product_export.to_csv(index=False).encode("utf-8"),
        file_name=f"{creator_label}_unique_products.csv",
        mime="text/csv",
        use_container_width=True,
        disabled=product_export.empty,
    )
    st.markdown(
        '<div class="small-note">Date filtering is applied before product detection. Product details are cached for 7 days by product ID + region.</div>',
        unsafe_allow_html=True,
    )
