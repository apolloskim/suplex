from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reddit_playwright_probe.py"
SPEC = importlib.util.spec_from_file_location("reddit_playwright_probe", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_strip_query_and_fragment() -> None:
    assert MODULE.strip_query_and_fragment(
        "https://www.reddit.com/r/test/comments/abc123/post/?solution=foo#bar",
    ) == "https://www.reddit.com/r/test/comments/abc123/post/"


def test_reddit_absolute_url_normalizes_relative_paths() -> None:
    assert MODULE.reddit_absolute_url("/r/test/comments/abc123/post/") == (
        "https://www.reddit.com/r/test/comments/abc123/post/"
    )
    assert MODULE.reddit_absolute_url("https://www.reddit.com/r/test/comments/abc123/post/") == (
        "https://www.reddit.com/r/test/comments/abc123/post/"
    )


def test_is_reddit_post_url_requires_matching_subreddit() -> None:
    assert MODULE.is_reddit_post_url(
        "https://www.reddit.com/r/smallbusiness/comments/abc123/post/",
        "smallbusiness",
    )
    assert not MODULE.is_reddit_post_url(
        "https://www.reddit.com/r/entrepreneur/comments/abc123/post/",
        "smallbusiness",
    )


def test_build_duckduckgo_search_url_biases_to_subreddit_posts() -> None:
    url = MODULE.build_duckduckgo_search_url("smallbusiness", "looking for a good", "month")
    assert "duckduckgo.com" in url
    assert "site%3Areddit.com%2Fr%2Fsmallbusiness%2Fcomments" in url


def test_build_reddit_comments_json_url_strips_challenge_params() -> None:
    url = MODULE.build_reddit_comments_json_url(
        "https://www.reddit.com/r/test/comments/abc123/post/?solution=foo&sort=best#frag",
        "new",
    )
    assert url == (
        "https://www.reddit.com/r/test/comments/abc123/post/.json"
        "?sort=new&limit=500&depth=10&raw_json=1"
    )


def test_compact_text_removes_action_lines() -> None:
    text = "Author\nReply\nUseful answer\nShare\nAward"
    assert MODULE.compact_text(text) == "Author\nUseful answer"


def test_with_query_param_replaces_existing_value() -> None:
    url = MODULE.with_query_param(
        "https://www.reddit.com/r/test/comments/abc123/post/?sort=best&foo=bar",
        "sort",
        "new",
    )
    assert "sort=new" in url
    assert "foo=bar" in url


def test_matches_date_window_is_inclusive() -> None:
    start = MODULE.parse_iso_date("2025-07-01")
    end = MODULE.parse_iso_date("2025-07-31")
    assert MODULE.matches_date_window("2025-07-23T18:38:44.254000+0000", start, end)
    assert not MODULE.matches_date_window("2025-08-01T00:00:00.000000+0000", start, end)


def test_comment_from_payload_item_promotes_comment_metadata() -> None:
    comment = {
        "attrs": {
            "thingid": "t1_abc123",
            "author": "example_user",
            "arialabel": "Comment from example_user",
            "created": "2025-07-23T18:38:44.952000+0000",
            "permalink": "/r/test/comments/post/comment/abc123/",
            "depth": "1",
            "score": "7",
            "award-count": "2",
            "content-type": "text",
            "postid": "t3_postid",
            "reload-url": "/svc/shreddit/comment/t1_abc123?depth=1",
            "collapsed": "",
        },
        "badge_attrs": {"distinguished-as": "MODERATOR", "stickied": "", "locked": ""},
        "author_modifier_attrs": {},
        "author_url": "https://www.reddit.com/user/example_user/",
        "text": "Author\nBody text\nReply",
        "meta_text": "example_user\n•\n9mo ago",
        "body_text": "Body text",
        "body_text_source": "hidden",
        "body_html": "<div>Body text</div>",
        "body_links": [{"text": "Example", "href": "https://example.com/path?x=1"}],
        "parent_id": "t1_parent",
        "direct_reply_ids": ["t1_child"],
        "more_reply_links": [
            "https://www.reddit.com/r/test/comments/post/comment/child/?foo=bar",
        ],
    }

    item = MODULE.comment_from_payload_item(comment, debug=True)

    assert item["comment_id"] == "t1_abc123"
    assert item["short_id"] == "abc123"
    assert item["url"] == "https://www.reddit.com/r/test/comments/post/comment/abc123/"
    assert item["reload_url"] == "https://www.reddit.com/svc/shreddit/comment/t1_abc123?depth=1"
    assert item["is_collapsed"] is True
    assert item["distinguished_as"] == "MODERATOR"
    assert item["is_stickied"] is True
    assert item["is_locked"] is True
    assert item["body_text_source"] == "hidden"
    assert item["body_links"] == [{"text": "Example", "url": "https://example.com/path/"}]
    assert item["more_reply_links"] == [
        "https://www.reddit.com/r/test/comments/post/comment/child/",
    ]
    assert item["debug"]["attrs"]["thingid"] == "t1_abc123"


def test_comment_from_json_item_promotes_json_metadata() -> None:
    comment = {
        "comment_id": "t1_abc123",
        "parent_id": "t3_postid",
        "depth": 0,
        "data": {
            "author": "json_user",
            "body": "Body text",
            "body_html": "&lt;div class=\\\"md\\\"&gt;&lt;p&gt;Body &lt;a href=\\\"https://example.com/path?x=1\\\"&gt;link&lt;/a&gt;&lt;/p&gt;&lt;/div&gt;",
            "created_utc": 1753295924.0,
            "permalink": "/r/test/comments/post/comment/abc123/",
            "score": 9,
            "total_awards_received": 3,
            "collapsed": True,
            "distinguished": "moderator",
            "stickied": True,
            "locked": True,
            "link_id": "t3_postid",
        },
    }

    item = MODULE.comment_from_json_item(comment, debug=True)

    assert item["comment_id"] == "t1_abc123"
    assert item["author_url"] == "https://www.reddit.com/user/json_user/"
    assert item["parent_id"] is None
    assert item["is_top_level"] is True
    assert item["body_text_source"] == "json"
    assert item["distinguished_as"] == "MODERATOR"
    assert item["is_stickied"] is True
    assert item["is_locked"] is True
    assert item["body_links"] == [{"text": "link", "url": "https://example.com/path/"}]
    assert item["debug"]["json"]["author"] == "json_user"
