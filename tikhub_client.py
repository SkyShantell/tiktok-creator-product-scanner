from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from parser import (
    extract_pagination,
    extract_sec_user_id,
    extract_video_list,
    extract_video_objects_from_batch,
    video_id,
)

BASE_URL = "https://api.tikhub.io"


class TikHubError(RuntimeError):
    pass


@dataclass
class Usage:
    requests: int = 0


class TikHubClient:
    def __init__(self, api_key: str, timeout: int = 30):
        if not api_key:
            raise ValueError("TikHub API key is required.")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        self.usage = Usage()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
                self.usage.requests += 1
                if response.status_code == 429:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                if response.status_code >= 400:
                    # TikHub product detail docs recommend retrying 400s.
                    if response.status_code == 400 and attempt < retries:
                        time.sleep(0.7 * (attempt + 1))
                        continue
                    raise TikHubError(f"TikHub HTTP {response.status_code}: {response.text[:500]}")
                payload = response.json()
                if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
                    raise TikHubError(str(payload.get("message") or payload))
                return payload
            except (requests.RequestException, ValueError, TikHubError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(0.6 * (attempt + 1))
        raise TikHubError(str(last_error or "TikHub request failed"))

    def resolve_creator(self, username: str) -> str | None:
        payload = self._request(
            "GET",
            "/api/v1/tiktok/app/v3/get_user_id_and_sec_user_id_by_username",
            params={"username": username},
        )
        return extract_sec_user_id(payload)

    def get_creator_videos(
        self,
        username: str,
        limit: int,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        sec_user_id = self.resolve_creator(username)
        cursor: int | str = 0
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        safety_pages = max(3, (limit // 20) + 5)

        for _ in range(safety_pages):
            params: dict[str, Any] = {
                "max_cursor": cursor,
                "count": 20,
                "sort_type": 0,
            }
            if sec_user_id:
                params["sec_user_id"] = sec_user_id
            else:
                params["unique_id"] = username

            payload = self._request(
                "GET",
                "/api/v1/tiktok/app/v3/fetch_user_post_videos_v3",
                params=params,
            )
            page = extract_video_list(payload)
            if not page:
                break
            for item in page:
                vid = video_id(item)
                if vid and vid not in seen:
                    seen.add(vid)
                    collected.append(item)
                    if len(collected) >= limit:
                        break
            if progress:
                progress(min(len(collected), limit), limit)
            if len(collected) >= limit:
                break
            has_more, next_cursor = extract_pagination(payload)
            if not has_more or next_cursor in (None, cursor):
                break
            cursor = next_cursor
        return collected[:limit]

    def batch_video_details(self, aweme_ids: list[str], region: str = "US") -> list[dict[str, Any]]:
        if not aweme_ids:
            return []
        if len(aweme_ids) > 10:
            raise ValueError("TikHub batch endpoint accepts at most 10 video IDs.")
        try:
            payload = self._request(
                "POST",
                "/api/v1/tiktok/app/v3/fetch_multi_video",
                json=aweme_ids,
            )
            items = extract_video_objects_from_batch(payload)
            if items:
                return items
        except TikHubError:
            pass

        # Fallback keeps the app functional if the batch endpoint changes/errors.
        items: list[dict[str, Any]] = []
        for aweme_id in aweme_ids:
            payload = self._request(
                "GET",
                "/api/v1/tiktok/app/v3/fetch_one_video_v2",
                params={"aweme_id": aweme_id},
            )
            extracted = extract_video_objects_from_batch(payload)
            if extracted:
                items.extend(extracted)
            elif isinstance(payload.get("data"), dict):
                items.append(payload["data"])
        return items

    def single_video_product_detail(self, aweme_id: str) -> dict[str, Any] | None:
        """Fetch the V2 video payload TikHub documents for Shop product detection.

        TikHub's product-detection guide specifically points to
        $.data[0].share_info.share_url from fetch_one_video_v2, where a
        shoppable video may include placeholder_product_id=<id>.
        """
        payload = self._request(
            "GET",
            "/api/v1/tiktok/app/v3/fetch_one_video_v2",
            params={"aweme_id": aweme_id},
        )
        items = extract_video_objects_from_batch(payload)
        if items:
            return items[0]
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            return data
        return None


    def app_video_detail_v3(self, aweme_id: str, region: str) -> dict[str, Any]:
        """Region-aware app metadata. TikHub added this as a separate V3 path."""
        return self._request(
            "GET",
            "/api/v1/tiktok/app/v3/fetch_one_video_v3",
            params={"aweme_id": aweme_id, "region": region},
        )

    def web_video_detail_v2(self, item_id: str, region: str) -> dict[str, Any]:
        """TikHub's newer Web V2 response with more complete post metadata."""
        return self._request(
            "GET",
            "/api/v1/tiktok/web/fetch_post_detail_v2",
            params={"itemId": item_id, "region": region},
        )

    def web_video_detail(self, item_id: str, region: str) -> dict[str, Any]:
        """Simpler Web response; final public metadata fallback."""
        return self._request(
            "GET",
            "/api/v1/tiktok/web/fetch_post_detail",
            params={"itemId": item_id, "region": region},
        )

    def product_detail(self, product_id: str, region: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v1/tiktok/shop/web/fetch_product_detail_v3",
            params={"product_id": product_id, "region": region},
            retries=3,
        )
