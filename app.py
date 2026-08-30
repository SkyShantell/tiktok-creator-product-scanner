from __future__ import annotations

import json
import os
import re
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
from storage import (
    add_creators_to_group,
    delete_creator_group,
    export_creator_groups,
    get_cached_product,
    import_creator_groups,
    list_creator_groups,
    remove_creators_from_group,
    set_cached_product,
)
from tikhub_client import TikHubClient, TikHubError

load_dotenv()

st.set_page_config(page_title="Creator Product Scanner", page_icon="🛍️", layout="wide")

st.markdown(
    """
<style>
.block-container {padding-top: 2rem; max-width: 1500px;}
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


def parse_creator_lines(text: str) -> tuple[list[str], list[str]]:
    """Accept one-per-line, commas, spaces, @handles, or profile URLs."""
    raw_items = [x.strip() for x in re.split(r"[\n,]+", text or "") if x.strip()]
    creators: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        try:
            creator = normalize_creator(item)
        except ValueError:
            errors.append(item)
            continue
        key = creator.lower()
        if key not in seen:
            seen.add(key)
            creators.append(creator)
    return creators, errors


def detail_map(client: TikHubClient, ids: list[str], region: str, status_box: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    chunks = chunked(ids, 10)
    for i, group in enumerate(chunks, start=1):
        status_box.write(f"Inspecting video details — batch {i}/{len(chunks)}")
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
    status_box: Any,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    status_box.write(f"@{creator}: loading up to {video_limit} recent videos…")
    feed_videos = client.get_creator_videos(creator, video_limit)
    if not feed_videos:
        raise TikHubError("No public videos were returned.")

    feed_map = {video_id(v): v for v in feed_videos if video_id(v)}
    ids = list(feed_map.keys())
    status_box.write(f"@{creator}: found {len(ids)} videos. Checking Shop attachments…")
    details = detail_map(client, ids, region, status_box)

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
        status_box.write(f"@{creator}: deeper Shop check for {len(unresolved)} video(s)…")
        for vid in unresolved:
            found = False
            checks = [
                (
                    "app_v2",
                    lambda vid=vid: client._request(
                        "GET",
                        "/api/v1/tiktok/app/v3/fetch_one_video_v2",
                        params={"aweme_id": vid},
                    ),
                ),
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
            if not found and last_payload is not None and len(diagnostics) < 5:
                diagnostics.setdefault(vid, {})["last_payload"] = last_payload

    product_payloads: dict[str, dict[str, Any]] = {}
    validated_pid_by_video: dict[str, str] = {}
    all_candidate_ids = list(
        dict.fromkeys(pid for vid in ids for pid in product_candidates_by_video.get(vid, [])[:4])
    )
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

    status_box.write(
        f"@{creator}: complete — {len(rows)} videos, "
        f"{sum(bool(r.get('product_id')) for r in rows)} product videos."
    )
    return rows, detection_source, diagnostics


def display_video_results(df: pd.DataFrame) -> None:
    visible_cols = [
        "creator",
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
            "creator": st.column_config.TextColumn("Creator", width="small"),
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


def build_product_summary(df: pd.DataFrame) -> pd.DataFrame:
    product_df = df[df["product_id"].fillna("") != ""].copy()
    if product_df.empty:
        return pd.DataFrame()

    def creator_list(series: pd.Series) -> str:
        return ", ".join(sorted({"@" + str(x).lstrip("@") for x in series if str(x).strip()}))

    grouped = (
        product_df.groupby("product_id", dropna=False)
        .agg(
            product_title=("product_title", "first"),
            product_image=("product_image", "first"),
            seller=("seller", "first"),
            price=("price", "first"),
            product_url=("product_url", "first"),
            creators=("creator", "nunique"),
            promoted_by=("creator", creator_list),
            videos=("video_id", "count"),
            combined_views=("views", "sum"),
            best_video_views=("views", "max"),
        )
        .reset_index()
        .sort_values(["creators", "videos", "combined_views"], ascending=[False, False, False])
    )
    return grouped


def display_products(df: pd.DataFrame) -> None:
    grouped = build_product_summary(df)
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
            "seller": "Seller",
            "price": "Price",
            "creators": st.column_config.NumberColumn("Creators"),
            "promoted_by": st.column_config.TextColumn("Promoted by", width="large"),
            "videos": st.column_config.NumberColumn("Videos"),
            "combined_views": st.column_config.NumberColumn("Combined views", format="localized"),
            "best_video_views": st.column_config.NumberColumn("Best video", format="localized"),
            "product_url": st.column_config.LinkColumn("TikTok Shop", display_text="Open"),
        },
    )


st.title("Creator Product Scanner")
st.caption(
    "Scan one creator, paste multiple creators, or save creator groups → identify the TikTok Shop product attached to each video."
)

api_key = get_api_key()
if not api_key:
    st.error("TikHub API key is not configured. Add TIKHUB_API_KEY to your environment or Streamlit secrets.")
    st.code('TIKHUB_API_KEY = "your_api_key"', language="toml")
    st.stop()

# ---------------- Creator library ----------------
with st.expander("Creator groups", expanded=False):
    st.caption("Save recurring creator lists so you can scan the same group again without pasting handles each time.")
    groups = list_creator_groups()

    left, right = st.columns(2)
    with left:
        with st.form("create_or_add_group"):
            group_name = st.text_input("Group name", placeholder="Men's Fashion")
            group_creators_text = st.text_area(
                "Creators to add",
                placeholder="@creator1\n@creator2\nhttps://www.tiktok.com/@creator3",
                height=140,
            )
            add_group_submit = st.form_submit_button("Add creators to group", type="primary")
        if add_group_submit:
            creators_to_add, invalid = parse_creator_lines(group_creators_text)
            if not group_name.strip():
                st.error("Enter a group name.")
            elif not creators_to_add:
                st.error("Enter at least one valid creator.")
            else:
                added = add_creators_to_group(group_name.strip(), creators_to_add)
                st.success(f"Added {added} new creator(s) to {group_name.strip()}.")
                if invalid:
                    st.warning("Skipped: " + ", ".join(invalid))
                st.rerun()

    with right:
        groups = list_creator_groups()
        if groups:
            selected_manage_group = st.selectbox("Manage saved group", list(groups.keys()), key="manage_group")
            members = groups.get(selected_manage_group, [])
            st.write(f"**{len(members)} creator(s)**")
            st.dataframe(
                pd.DataFrame({"Creator": ["@" + x for x in members]}),
                hide_index=True,
                use_container_width=True,
                height=min(250, 40 + 35 * max(len(members), 1)),
            )
            remove_members = st.multiselect(
                "Remove creators",
                members,
                format_func=lambda x: "@" + x,
                key="remove_group_members",
            )
            r1, r2 = st.columns(2)
            if r1.button("Remove selected", disabled=not remove_members, use_container_width=True):
                remove_creators_from_group(selected_manage_group, remove_members)
                st.rerun()
            if r2.button("Delete group", use_container_width=True):
                delete_creator_group(selected_manage_group)
                st.rerun()
        else:
            st.info("No saved groups yet. Create one on the left.")

    groups_backup = json.dumps(export_creator_groups(), indent=2).encode("utf-8")
    b1, b2 = st.columns(2)
    with b1:
        st.download_button(
            "Backup creator groups",
            groups_backup,
            file_name="creator_groups.json",
            mime="application/json",
            use_container_width=True,
        )
    with b2:
        restore_file = st.file_uploader("Restore groups JSON", type=["json"], label_visibility="collapsed")
        if restore_file is not None:
            try:
                restored_payload = json.load(restore_file)
                imported = import_creator_groups(restored_payload, replace=False)
                st.success(f"Restored {imported} creator memberships.")
            except Exception as exc:
                st.error(f"Could not restore groups: {exc}")
    st.markdown(
        '<div class="small-note">Streamlit Community Cloud does not guarantee permanent local disk storage across redeploys. Use Backup/Restore if you want to preserve groups; a Google Sheet can be added later for permanent shared storage.</div>',
        unsafe_allow_html=True,
    )

# ---------------- Scan configuration ----------------
with st.form("scan_form"):
    source_mode = st.radio(
        "Creators to scan",
        ["One creator", "Multiple creators", "Saved group"],
        horizontal=True,
    )

    creator_input = ""
    multi_creator_input = ""
    selected_scan_group = ""
    current_groups = list_creator_groups()

    if source_mode == "One creator":
        creator_input = st.text_input(
            "TikTok creator",
            placeholder="https://www.tiktok.com/@creatorname  or  @creatorname",
        )
    elif source_mode == "Multiple creators":
        multi_creator_input = st.text_area(
            "TikTok creators",
            placeholder="@creator1\n@creator2\n@creator3",
            height=150,
            help="Enter one creator per line. Profile URLs also work.",
        )
    else:
        if current_groups:
            selected_scan_group = st.selectbox("Saved group", list(current_groups.keys()))
            st.caption(
                f"{len(current_groups.get(selected_scan_group, []))} creator(s): "
                + ", ".join("@" + x for x in current_groups.get(selected_scan_group, [])[:8])
                + ("…" if len(current_groups.get(selected_scan_group, [])) > 8 else "")
            )
        else:
            st.warning("Create a saved group above first.")

    c1, c2 = st.columns(2)
    with c1:
        video_limit = int(
            st.number_input(
                "Videos to scan per creator",
                min_value=1,
                max_value=2000,
                value=20,
                step=1,
                help="Example: 5 creators × 20 videos = up to 100 videos total.",
            )
        )
    with c2:
        region = st.selectbox("TikTok Shop region", ["US", "GB", "SG", "MY", "PH", "TH", "VN", "ID"], index=0)
    scan = st.form_submit_button("Scan creators", type="primary", use_container_width=True)

if scan:
    creators: list[str] = []
    invalid: list[str] = []
    if source_mode == "One creator":
        try:
            creators = [normalize_creator(creator_input)]
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
    elif source_mode == "Multiple creators":
        creators, invalid = parse_creator_lines(multi_creator_input)
        if not creators:
            st.error("Enter at least one valid creator.")
            st.stop()
    else:
        creators = list_creator_groups().get(selected_scan_group, [])
        if not creators:
            st.error("That saved group is empty.")
            st.stop()

    if invalid:
        st.warning("Skipped invalid creator entries: " + ", ".join(invalid))

    client = TikHubClient(api_key=api_key)
    rows: list[dict[str, Any]] = []
    all_detection_sources: dict[str, str] = {}
    all_diagnostics: dict[str, Any] = {}
    failures: list[dict[str, str]] = []

    status = st.status(f"Scanning {len(creators)} creator(s)", expanded=True)
    overall_progress = st.progress(0.0)
    for index, creator in enumerate(creators, start=1):
        status.write(f"Creator {index}/{len(creators)} — @{creator}")
        try:
            creator_rows, creator_sources, creator_diagnostics = scan_one_creator(
                client, creator, video_limit, region, status
            )
            rows.extend(creator_rows)
            all_detection_sources.update({f"{creator}:{k}": v for k, v in creator_sources.items()})
            if creator_diagnostics:
                all_diagnostics[creator] = creator_diagnostics
        except Exception as exc:
            failures.append({"creator": creator, "error": str(exc)})
            status.write(f"@{creator}: FAILED — {exc}")
        overall_progress.progress(index / max(len(creators), 1))

    if rows:
        df = pd.DataFrame(rows)
        st.session_state["scan_df"] = df
        st.session_state["scan_creators"] = creators
        st.session_state["scan_label"] = selected_scan_group if source_mode == "Saved group" else f"{len(creators)} creators"
        st.session_state["scan_api_requests"] = client.usage.requests
        st.session_state["scan_detection_source"] = all_detection_sources
        st.session_state["scan_diagnostics"] = all_diagnostics
        st.session_state["scan_failures"] = failures
        status.update(label=f"Scan complete — {len(creators) - len(failures)}/{len(creators)} creator(s) succeeded", state="complete", expanded=False)
    else:
        status.update(label="Scan failed", state="error", expanded=True)
        st.error("No creator produced results.")

# ---------------- Results ----------------
if "scan_df" in st.session_state:
    df = st.session_state["scan_df"].copy()
    creators_scanned = sorted(df["creator"].dropna().astype(str).unique().tolist()) if "creator" in df else []
    label = st.session_state.get("scan_label", "creators")

    product_mask = df["product_id"].fillna("") != ""
    unique_products = int(df.loc[product_mask, "product_id"].nunique())
    product_videos = int(product_mask.sum())
    requests_used = int(st.session_state.get("scan_api_requests", 0))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Creators scanned", len(creators_scanned))
    m2.metric("Videos scanned", len(df))
    m3.metric("Product videos", product_videos)
    m4.metric("Unique products", unique_products)
    m5.metric("API requests", requests_used)

    failures = st.session_state.get("scan_failures", [])
    if failures:
        with st.expander(f"{len(failures)} creator(s) failed"):
            st.dataframe(pd.DataFrame(failures), hide_index=True, use_container_width=True)

    tabs = st.tabs(["Videos", "Products"])
    with tabs[0]:
        f1, f2, f3 = st.columns([1, 1, 2])
        with f1:
            only_products = st.checkbox("Product videos only", value=False)
        with f2:
            creator_filter = st.multiselect(
                "Creator",
                creators_scanned,
                default=[],
                format_func=lambda x: "@" + x,
            )
        with f3:
            search = st.text_input("Filter", placeholder="Search caption, product or seller")
        shown = df.copy()
        if only_products:
            shown = shown[shown["product_id"].fillna("") != ""]
        if creator_filter:
            shown = shown[shown["creator"].isin(creator_filter)]
        if search.strip():
            q = search.strip().lower()
            mask = (
                shown["caption"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["product_title"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["seller"].fillna("").str.lower().str.contains(q, regex=False)
                | shown["creator"].fillna("").str.lower().str.contains(q, regex=False)
            )
            shown = shown[mask]
        display_video_results(shown)

    with tabs[1]:
        display_products(df)

    with st.expander("Detection diagnostics"):
        st.caption("Useful only if TikTok hides Shop metadata for a video. No API key is included.")
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
                file_name="creator_group_shop_detection_diagnostics.json",
                mime="application/json",
            )
        else:
            st.success("No unresolved diagnostic payloads were needed.")

    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", str(label)).strip("_") or "creators"
    c1, c2 = st.columns(2)
    with c1:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Export all video results CSV",
            data=csv,
            file_name=f"{safe_label}_tiktok_products.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c2:
        product_summary = build_product_summary(df)
        product_csv = product_summary.to_csv(index=False).encode("utf-8") if not product_summary.empty else b""
        st.download_button(
            "Export unique products CSV",
            data=product_csv,
            file_name=f"{safe_label}_unique_products.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=product_summary.empty,
        )
    st.markdown(
        '<div class="small-note">Product details are cached for 7 days by product ID + region, including across different creators, to avoid duplicate paid lookups.</div>',
        unsafe_allow_html=True,
    )
