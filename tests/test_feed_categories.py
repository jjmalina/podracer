"""_get_categories: turn a feed's flattened iTunes tags into clean genre tags.

feedparser lumps <itunes:category> and <itunes:keywords> together in f.tags, so
the whitelist against Apple's official category list is the only thing keeping
keyword spam out. These pin that behavior (and its known limitation)."""
from podracer.feed import _get_categories


def test_keeps_apple_categories_and_drops_keyword_spam():
    f = {"tags": [
        {"term": "changelog"}, {"term": "code"}, {"term": "hacker"},
        {"term": "open source"}, {"term": "Technology"},
    ]}
    assert _get_categories(f) == ["Technology"]


def test_keeps_multiple_real_categories_in_feed_order():
    f = {"tags": [{"term": "Business"}, {"term": "Investing"}, {"term": "News"}]}
    assert _get_categories(f) == ["Business", "Investing", "News"]


def test_normalizes_to_canonical_casing_and_dedups():
    f = {"tags": [{"term": "business"}, {"term": "BUSINESS"}, {"term": "Business"}]}
    assert _get_categories(f) == ["Business"]


def test_no_tags_yields_empty():
    assert _get_categories({}) == []
    assert _get_categories({"tags": []}) == []
    assert _get_categories({"tags": [{"term": ""}, {"term": "  "}]}) == []


def test_known_limitation_keyword_equal_to_category_name_passes_through():
    # A keyword that happens to be a real Apple category name is indistinguishable
    # from a genuine category and is (knowingly) kept. Documents current behavior.
    f = {"tags": [{"term": "Technology"}, {"term": "comedy"}]}
    assert _get_categories(f) == ["Technology", "Comedy"]
