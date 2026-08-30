from __future__ import annotations

import math
import os
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from parser import (
    extract_product_id,
    extract_product_ids,
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
.block-container {padding-top: 2rem; max-width: 1450px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.22); padding: 14px; border-radius: 12px;}
.small-note {opacity: .72; font-size: .88rem;}
</style>
""",
    unsafe_allow_html=True,
)


def get_api_key() -> str:
    key = os.getenv("TIKHUB_API_KEY", "")
    if key:
        return key
    try:
        return str(st.secrets.get("TIKHUB_API_KEY", ""))
    except Exception:
        return ""


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def detail_map(client: TikHubClient, ids: list[str], region: str, status_box: Any, progress: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    chunks = chunked(ids, 10)
    for i, group in enumerate(chunks, start=1):
        status_box.write(f"Inspecting video details — batch {i}/{len(chunks)}")
        for item in client.batch_video_details(group, region=region):
            vid = video_id(item)
            if vid:
                result[vid] = item
        progress.progress(i / max(len(chunks), 1))
    return result


def display_video_results(df: pd.DataFrame) -> None:
    visible_cols = [
        "cover_url",
        "posted_at",
        "views",
        "likes",
        "caption",
        "product_title",
        "price",
        "seller",
        "video_url",
        "product_url",
    ]
    visible_cols = [c for c in visible_cols if c in df.columns]
    st.dataframe(
        df[visible_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "cover_url": st.column_config.ImageColumn("Video"),
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


def display_products(df: pd.DataFrame) -> None:
    product_df = df[df["product_id"].fillna("") != ""].copy()
    if product_df.empty:
        st.info("No tagged products were detected in these videos.")
        return
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
        )
        .reset_index()
        .sort_values(["videos", "combined_views"], ascending=[False, False])
    )
    st.dataframe(
        grouped,
        use_container_width=True,
        hide_index=True,
        column_config={
            "product_image": st.column_config.ImageColumn("Product"),
            "product_title": st.column_config.TextColumn("Product name", width="large"),
            "seller": "Seller",
            "price": "Price",
            "videos": st.column_config.NumberColumn("Videos"),
            "combined_views": st.column_config.NumberColumn("Combined views", format="localized"),
            "best_video_views": st.column_config.NumberColumn("Best video", format="localized"),
            "product_url": st.column_config.LinkColumn("TikTok Shop", display_text="Open"),
        },
    )


st.title("Creator Product Scanner")
st.caption("Paste a TikTok creator profile → scan their videos → identify the TikTok Shop product attached to each video.")

api_key = get_api_key()
if not api_key:
    st.error("TikHub API key is not configured. Add TIKHUB_API_KEY to your environment or Streamlit secrets.")
    st.code('TIKHUB_API_KEY = "your_api_key"', language="toml")
    st.stop()

with st.form("scan_form"):
    creator_input = st.text_input(
        "TikTok creator",
        placeholder="https://www.tiktok.com/@creatorname  or  @creatorname",
    )
    c1, c2 = st.columns(2)
    with c1:
        video_limit = st.number_input(
            "Videos to scan",
            min_value=1,
            max_value=2000,
            value=20,
            step=1,
            help="Enter the exact number of recent videos you want to scan.",
        )
        video_limit = int(video_limit)
    with c2:
        region = st.selectbox("TikTok Shop region", ["US", "GB", "SG", "MY", "PH", "TH", "VN", "ID"], index=0)
    scan = st.form_submit_button("Scan creator", type="primary", use_container_width=True)

if scan:
    try:
        creator = normalize_creator(creator_input)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    client = TikHubClient(api_key=api_key)
    rows: list[dict[str, Any]] = []
    product_payloads: dict[str, dict[str, Any]] = {}

    status = st.status(f"Scanning @{creator}", expanded=True)
    creator_progress = st.progress(0.0)
    try:
        status.write("Loading creator videos…")
        feed_videos = client.get_creator_videos(
            creator,
            video_limit,
            progress=lambda done, total: creator_progress.progress(done / max(total, 1)),
        )
        creator_progress.progress(1.0)
        if not feed_videos:
            raise TikHubError("No public videos were returned for this creator.")

        feed_map = {video_id(v): v for v in feed_videos if video_id(v)}
        ids = list(feed_map.keys())
        status.write(f"Found {len(ids)} videos. Checking them for attached products in batches of 10…")
        detail_progress = st.progress(0.0)
        details = detail_map(client, ids, region, status, detail_progress)

        # Product detection is intentionally multi-source. TikTok frequently
        # moves Shop attachment metadata between app/web payloads. We scan the
        # complete payload from each source and stop as soon as a candidate is
        # found. Product candidates are validated by the product-detail call.
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
            base = feed_map.get(vid, {})
            detail = details.get(vid, {})
            if remember(vid, "feed", base) or remember(vid, "batch", detail):
                continue
            unresolved.append(vid)

        if unresolved:
            status.write(
                f"{len(unresolved)} videos need deeper Shop detection. "
                "Checking app + TikTok Web commerce metadata…"
            )
            commerce_progress = st.progress(0.0)
            still_unresolved: list[str] = []
            for index, vid in enumerate(unresolved, start=1):
                found = False
                checks = [
                    ("app_v2", lambda: client._request(
                        "GET", "/api/v1/tiktok/app/v3/fetch_one_video_v2",
                        params={"aweme_id": vid}
                    )),
                    ("app_v3_region", lambda: client.app_video_detail_v3(vid, region)),
                    ("web_v2", lambda: client.web_video_detail_v2(vid, region)),
                    ("web_v1", lambda: client.web_video_detail(vid, region)),
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
                if not found:
                    still_unresolved.append(vid)
                    if last_payload is not None and len(diagnostics) < 5:
                        diagnostics.setdefault(vid, {})["last_payload"] = last_payload
                commerce_progress.progress(index / max(len(unresolved), 1))

        # Validate candidate IDs through the Shop product endpoint. This filters
        # out unrelated numeric IDs that can appear inside generic anchor data.
        product_payloads: dict[str, dict[str, Any]] = {}
        validated_pid_by_video: dict[str, str] = {}
        all_candidate_ids = list(dict.fromkeys(
            pid for vid in ids for pid in product_candidates_by_video.get(vid, [])[:4]
        ))
        status.write(
            f"Found {len(all_candidate_ids)} product-ID candidates. "
            "Validating them against TikTok Shop…"
        )
        validation_progress = st.progress(0.0)
        validation_cache: dict[str, bool] = {}
        for idx, pid in enumerate(all_candidate_ids, start=1):
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
            validation_progress.progress(idx / max(len(all_candidate_ids), 1))
        if not all_candidate_ids:
            validation_progress.progress(1.0)

        for vid in ids:
            for pid in product_candidates_by_video.get(vid, [])[:4]:
                if validation_cache.get(pid):
                    validated_pid_by_video[vid] = pid
                    break

        product_ids = set(validated_pid_by_video.values())
        st.session_state["scan_detection_source"] = detection_source
        st.session_state["scan_diagnostics"] = diagnostics
        status.write(f"Detected {len(product_ids)} unique tagged products.")
        for vid in ids:
            feed = feed_map[vid]
            video_row = normalize_video(feed, creator)
            pid = validated_pid_by_video.get(vid)
            if pid:
                product_row = normalize_product(pid, product_payloads.get(pid, {}), region)
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

        df = pd.DataFrame(rows)
        st.session_state["scan_df"] = df
        st.session_state["scan_creator"] = creator
        st.session_state["scan_api_requests"] = client.usage.requests
        status.update(label=f"Scan complete — @{creator}", state="complete", expanded=False)
    except Exception as exc:
        status.update(label="Scan failed", state="error", expanded=True)
        st.error(str(exc))

if "scan_df" in st.session_state:
    df = st.session_state["scan_df"].copy()
    creator = st.session_state.get("scan_creator", "creator")

    product_mask = df["product_id"].fillna("") != ""
    unique_products = int(df.loc[product_mask, "product_id"].nunique())
    product_videos = int(product_mask.sum())
    requests_used = int(st.session_state.get("scan_api_requests", 0))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Videos scanned", len(df))
    m2.metric("Product videos", product_videos)
    m3.metric("Unique products", unique_products)
    m4.metric("API requests", requests_used)

    tabs = st.tabs(["Videos", "Products"])
    with tabs[0]:
        f1, f2 = st.columns([1, 2])
        with f1:
            only_products = st.checkbox("Product videos only", value=False)
        with f2:
            search = st.text_input("Filter", placeholder="Search caption, product or seller")
        shown = df.copy()
        if only_products:
            shown = shown[shown["product_id"].fillna("") != ""]
        if search.strip():
            q = search.strip().lower()
            mask = (
                shown["caption"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["product_title"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["seller"].fillna("").str.lower().str.contains(q, regex=False)
            )
            shown = shown[mask]
        display_video_results(shown)

    with tabs[1]:
        display_products(df)

    with st.expander("Detection diagnostics"):
        st.caption("Useful only if TikTok still hides Shop metadata for a video. No API key is included.")
        sources = st.session_state.get("scan_detection_source", {})
        if sources:
            source_counts = pd.Series(list(sources.values())).value_counts().rename_axis("source").reset_index(name="videos")
            st.dataframe(source_counts, hide_index=True, use_container_width=True)
        diagnostics = st.session_state.get("scan_diagnostics", {})
        if diagnostics:
            import json
            diagnostic_json = json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            st.download_button(
                "Download unresolved video diagnostics",
                data=diagnostic_json,
                file_name=f"{creator}_shop_detection_diagnostics.json",
                mime="application/json",
            )
        else:
            st.success("No unresolved diagnostic payloads were needed.")

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Export CSV",
        data=csv,
        file_name=f"{creator}_tiktok_products.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.markdown(
        '<div class="small-note">Product details are cached for 7 days by product ID + region to avoid duplicate paid lookups.</div>',
        unsafe_allow_html=True,
    )
