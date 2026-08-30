from parser import extract_product_id, normalize_creator, extract_video_list, video_id


def test_normalize_creator():
    assert normalize_creator("@bob") == "bob"
    assert normalize_creator("https://www.tiktok.com/@bob") == "bob"
    assert normalize_creator("https://www.tiktok.com/@bob?lang=en") == "bob"


def test_extract_product_id():
    video = {
        "aweme_id": "123",
        "share_info": {
            "share_url": "https://www.tiktok.com/@bob/video/123?foo=1&placeholder_product_id=1729405933664243180&bar=2"
        },
    }
    assert extract_product_id(video) == "1729405933664243180"


def test_video_list_and_id():
    payload = {"data": {"aweme_list": [{"aweme_id": "111", "desc": "a"}, {"aweme_id": "222", "desc": "b"}]}}
    videos = extract_video_list(payload)
    assert len(videos) == 2
    assert video_id(videos[1]) == "222"
