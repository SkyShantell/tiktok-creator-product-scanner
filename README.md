# TikTok Creator Product Scanner v3

Streamlit app: paste a public TikTok creator profile, retrieve their videos, detect the TikTok Shop product attached to each video, group products, and export CSV.

## v3 detection changes

TikTok moves Shop metadata between different response families. v3 no longer relies only on `share_info.share_url` / `placeholder_product_id`.

For unresolved videos it tries, in order:

1. feed/batch metadata
2. TikHub TikTok App V2 post detail
3. TikHub TikTok App V3 region-aware post detail
4. TikHub TikTok Web V2 post detail (newer richer metadata path)
5. TikHub TikTok Web V1 post detail

It scans product/shop/commerce/anchor metadata for candidate product IDs, then validates candidates through TikHub's TikTok Shop product-detail endpoint before labeling a video as shoppable.

A **Detection diagnostics** expander can export raw unresolved metadata for up to a few failed videos. It never includes the TikHub API key.

## Streamlit secret

```toml
TIKHUB_API_KEY = "your_key_here"
```


## v4 changes
- `Videos to scan` is now an exact number input (1–2000).
- Every validated product gets a direct TikTok Shop URL. If TikHub omits the URL, the app builds the canonical Shop product link from the validated product ID and region.
- The direct product URL is clickable in both app views and included in `product_url` in CSV exports.
