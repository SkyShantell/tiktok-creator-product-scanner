# TikTok Creator Product Scanner v6

Streamlit app powered by TikHub.

## v6 additions

- Scan one creator, paste multiple creators, or scan a saved group.
- Exact number of videos **per creator**.
- Named creator groups with add/remove/delete controls.
- Group backup + restore JSON.
- Combined video results across creators.
- Products view groups the same product across all creators and shows creator count + promoted-by list.
- Combined video CSV and unique-product CSV exports.
- Continues scanning the remaining creators if one creator fails.
- Existing TikTok Shop product detection, direct product links, product-name parsing and 7-day cache remain in place.

## Streamlit secret

```toml
TIKHUB_API_KEY = "your_actual_api_key"
```

## Note about saved groups on Streamlit Community Cloud

Saved groups use the app's local SQLite database. Streamlit Community Cloud does not guarantee local file persistence across redeploys/reboots. Use the **Backup creator groups** button to save a JSON copy. For permanent shared storage, connect the group library to Google Sheets/Supabase later.
