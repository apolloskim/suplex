from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from suplex.reddit_watch import (
    build_subreddit_feed_url,
    evaluate_post_action,
    matches_query,
    next_revisit_at,
    parse_feed_posts,
    resolve_policy,
)


def test_matches_query_uses_title_and_selftext() -> None:
    post = {
        "title": "Looking for a Clay alternative",
        "selftext": "Need enrichment help fast",
    }
    assert matches_query(post, "clay")
    assert matches_query(post, "enrichment")
    assert not matches_query(post, "apollo")
    assert matches_query(post, "")


def test_parse_feed_posts_extracts_reddit_listing_fields() -> None:
    payload = """
    {
      "data": {
        "children": [
          {
            "kind": "t3",
            "data": {
              "name": "t3_abc123",
              "subreddit": "smallbusiness",
              "title": "Need a CRM fast",
              "author": "example_user",
              "created_utc": 1753295924,
              "permalink": "/r/smallbusiness/comments/abc123/need_a_crm_fast/",
              "score": 12,
              "num_comments": 4,
              "selftext": "Budget approved",
              "is_self": true,
              "domain": "self.smallbusiness",
              "link_flair_text": "Question"
            }
          }
        ]
      }
    }
    """

    posts = parse_feed_posts(payload)

    assert len(posts) == 1
    assert posts[0]["id"] == "t3_abc123"
    assert posts[0]["short_id"] == "abc123"
    assert posts[0]["url"] == "https://www.reddit.com/r/smallbusiness/comments/abc123/need_a_crm_fast/"
    assert posts[0]["num_comments"] == 4
    assert posts[0]["created_timestamp"] == "2025-07-23T18:38:44.000000+0000"


def test_build_subreddit_feed_url_supports_sort_and_top_time_window() -> None:
    assert build_subreddit_feed_url("coldemail", "new", 5, "month") == (
        "https://www.reddit.com/r/coldemail/new.json?limit=5&raw_json=1"
    )
    assert build_subreddit_feed_url("coldemail", "top", 5, "week") == (
        "https://www.reddit.com/r/coldemail/top.json?limit=5&raw_json=1&t=week"
    )


def test_resolve_policy_uses_fallback_then_explicit_overrides() -> None:
    args = argparse.Namespace(
        policy=None,
        hydrate_mode="manual",
        min_age_minutes=None,
        hydrate_min_score=9,
        hydrate_min_comments=None,
        revisit_after_minutes="5,15",
    )

    policy = resolve_policy(
        args,
        fallback={
            "hydrate_mode": "rules",
            "min_age_minutes": 45,
            "hydrate_min_score": 4,
            "hydrate_min_comments": 2,
            "revisit_after_minutes": [30, 180],
        },
    )

    assert policy["hydrate_mode"] == "manual"
    assert policy["min_age_minutes"] == 45
    assert policy["hydrate_min_score"] == 9
    assert policy["hydrate_min_comments"] == 2
    assert policy["revisit_after_minutes"] == [5, 15]


def test_resolve_policy_defaults_to_less_eager_balanced_schedule() -> None:
    args = argparse.Namespace(
        policy=None,
        hydrate_mode=None,
        min_age_minutes=None,
        hydrate_min_score=None,
        hydrate_min_comments=None,
        revisit_after_minutes=None,
    )

    policy = resolve_policy(args)

    assert policy["hydrate_mode"] == "rules"
    assert policy["min_age_minutes"] == 30
    assert policy["hydrate_min_score"] == 3
    assert policy["hydrate_min_comments"] == 2
    assert policy["revisit_after_minutes"] == [60, 360, 1440]


def test_evaluate_post_action_prefers_revisit_for_fresh_post() -> None:
    post = {
        "created_timestamp": "2026-04-09T11:55:00.000000+0000",
        "score": 1,
        "num_comments": 0,
    }
    now = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    action, revisit_at = evaluate_post_action(
        post,
        now=now,
        policy={
            "hydrate_mode": "rules",
            "min_age_minutes": 30,
            "hydrate_min_score": 3,
            "hydrate_min_comments": 2,
            "revisit_after_minutes": [15, 60, 360],
        },
    )

    assert action == "revisit"
    assert revisit_at == "2026-04-09T12:10:00+00:00"


def test_evaluate_post_action_queues_hydration_once_thresholds_are_met() -> None:
    post = {
        "created_timestamp": "2026-04-09T10:30:00.000000+0000",
        "score": 4,
        "num_comments": 1,
    }
    now = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    action, revisit_at = evaluate_post_action(
        post,
        now=now,
        policy={
            "hydrate_mode": "rules",
            "min_age_minutes": 30,
            "hydrate_min_score": 3,
            "hydrate_min_comments": 2,
            "revisit_after_minutes": [15, 60, 360],
        },
    )

    assert action == "hydrate"
    assert revisit_at is None


def test_next_revisit_at_returns_none_when_schedule_is_exhausted() -> None:
    now = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    assert (
        next_revisit_at(
            "2026-04-09T00:00:00.000000+0000",
            now,
            [15, 60],
        )
        is None
    )
