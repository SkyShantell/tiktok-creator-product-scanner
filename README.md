# TikTok Creator Product Scanner

A Streamlit MVP that maps a TikTok creator's public videos to the TikTok Shop product attached to each video.

## What it does

1. Paste `@username` or a TikTok creator profile URL.
2. Resolves the creator via TikHub.
3. Pulls the newest 20/50/100/250/500 public videos.
4. Inspects videos in TikHub batches of up to 10.
5. Detects `placeholder_product_id` in each video's public share URL.
6. Fetches TikTok Shop product details only once per unique product ID.
7. Shows a Videos view and a grouped Products view.
8. Exports CSV.
9. Caches product payloads for 7 days in SQLite.

## Setup

### Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set TIKHUB_API_KEY
streamlit run app.py
```

### Streamlit Community Cloud

Deploy the repo, then add this under **App settings → Secrets**:

```toml
TIKHUB_API_KEY = "your_tikhub_api_key"
```

Do not commit your real API key.

## TikHub endpoints used

- `GET /api/v1/tiktok/app/v3/get_user_id_and_sec_user_id_by_username`
- `GET /api/v1/tiktok/app/v3/fetch_user_post_videos_v3`
- `POST /api/v1/tiktok/app/v3/fetch_multi_video` (10 IDs per call)
- `GET /api/v1/tiktok/app/v3/fetch_one_video_v3` (fallback only)
- `GET /api/v1/tiktok/shop/web/fetch_product_detail_v3`

## Detection method

TikHub documents the public-video product signal as:

`data[*].share_info.share_url` containing `placeholder_product_id=<TikTok Shop product id>`.

This MVP deliberately uses that public method. It does **not** require a TikTok Creator cookie.

## Notes / limitations

- TikTok/TikHub response shapes can change. The parsers are intentionally defensive and support several common key variants.
- A video with multiple associated products may need TikHub's Creator API "Video Associated Product List" endpoint, which requires Creator-cookie authentication. This MVP detects the public `placeholder_product_id` path only.
- Product detail V3 requires the correct TikTok Shop region. Select the creator/product market in the UI.
- SQLite cache is local to the deployment and may reset on hosts with ephemeral storage/redeploys.

## Run tests

```bash
pip install pytest
pytest -q
```


## Product detection
The scanner first uses TikHub batch video calls for efficiency. If a video does not expose a product ID in the batch/feed payload, it automatically calls TikHub `fetch_one_video_v2`, which is the endpoint TikHub documents for detecting `placeholder_product_id` in `share_info.share_url`.
