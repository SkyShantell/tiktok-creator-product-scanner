from parser import extract_product_id, extract_product_ids, normalize_creator


def test_normalize_creator():
    assert normalize_creator("@hello") == "hello"
    assert normalize_creator("https://www.tiktok.com/@hello") == "hello"


def test_placeholder_product_id():
    obj = {"share_info": {"share_url": "https://www.tiktok.com/@x/video/1?placeholder_product_id=1729405933664243180"}}
    assert extract_product_id(obj) == "1729405933664243180"


def test_nested_anchor_json_string():
    obj = {
        "anchors": [
            {
                "type": "shopping",
                "extra": '{"product_id":"1731050202505515549","title":"Example"}',
            }
        ]
    }
    assert "1731050202505515549" in extract_product_ids(obj)


def test_product_url():
    obj = {"commerce": {"url": "https://shop.tiktok.com/us/pdp/example-name-1729496283565954999"}}
    assert "1729496283565954999" in extract_product_ids(obj)


def test_generic_anchor_id():
    obj = {"anchor_info": {"shopping_anchor": {"id": "17294069642063424"}}}
    assert "17294069642063424" in extract_product_ids(obj)
