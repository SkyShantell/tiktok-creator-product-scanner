from parser import extract_product_id, extract_product_ids, normalize_creator, normalize_product


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


def test_normalize_product_builds_shop_link_when_api_omits_it():
    row = normalize_product("1729497312752277323", {"data": {"productInfo": {}}}, "US")
    assert row["product_url"] == "https://shop.tiktok.com/view/product/1729497312752277323?region=US&locale=en"


def test_normalize_product_prefers_returned_detail_link():
    url = "https://shop.tiktok.com/view/product/1729497312752277323?region=US&locale=en-US"
    payload = {"data": {"productInfo": {"detail_link": url}}}
    row = normalize_product("1729497312752277323", payload, "US")
    assert row["product_url"] == url


def test_normalize_product_ignores_generic_shop_template_title():
    payload = {
        "data": {
            "productInfo": {
                "ui": {"title": "Explore more from {s_shopName}"},
                "product": {"title": "Men's Textured Knit Polo Shirt"},
                "shop": {"name": "HYPESTFIT"},
            }
        }
    }
    row = normalize_product("1729497312752277323", payload, "US")
    assert row["product_title"] == "Men's Textured Knit Polo Shirt"


def test_normalize_product_uses_feed_fallback_when_detail_title_is_template():
    payload = {
        "data": {
            "productInfo": {
                "ui": {"title": "Explore more from {s_shopName}"},
            }
        }
    }
    row = normalize_product(
        "1729497312752277323", payload, "US", fallback_title="Sleeveless Knit Polo Top"
    )
    assert row["product_title"] == "Sleeveless Knit Polo Top"


def test_extract_product_title_from_commerce_anchor():
    from parser import extract_product_title
    obj = {
        "anchors": [
            {
                "type": "shopping",
                "extra": {
                    "product_id": "1729497312752277323",
                    "product_name": "Premium Knitted Sleeveless Shirt",
                },
            }
        ]
    }
    assert extract_product_title(obj, "1729497312752277323") == "Premium Knitted Sleeveless Shirt"
