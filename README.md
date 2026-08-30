# TikTok Creator Product Scanner — v5

Streamlit app that scans a public TikTok creator's videos and maps each shoppable video to its attached TikTok Shop product.

## v5 fixes

- Correctly extracts the actual product name instead of TikTok UI copy such as `Explore more from {s_shopName}`.
- Scores product-name/title candidates rather than taking the first nested `title` field.
- Falls back to the video's shopping-anchor metadata when the Shop detail response does not expose a clean product title.
- Keeps exact custom scan counts and direct TikTok Shop product links from v4.

## Streamlit secret

```toml
TIKHUB_API_KEY = "your_api_key_here"
```

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
