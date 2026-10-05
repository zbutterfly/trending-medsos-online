"""Social sentiment fetchers — Reddit via PRAW (verified real) + X via nitter.

Reddit: PRAW requires credentials (`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`).
If missing, fetcher returns []. We do NOT hallucinate fake 'reddit-mcp' servers.

X / Twitter / Threads: no official free API anymore. Use self-hosted cache of
working nitter instances; fallback gracefully to nothing. Never hallucinate.

Watch subreddits (hard-coded list, swappable):
- r/CryptoCurrency, r/CryptoAirdrops, r/airdrops
- r/CryptoCurrency (rising / new)
- r/InfiniteVouchers: skip; doesn't exist
- r/Monad, r/MegaETH (if they exist — guard)
- r/abstractionists for Abstract chain
- r/IndonesianInvestments, r/stocks, r/indonesia (IDX sentiment)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.config import settings
from ..core.http import Stealth
from .base import BaseFetcher

log = logging.getLogger(__name__)


CRYPTO_SUBREDDITS = [
    "CryptoCurrency",
    "CryptoAirdrops",
    "AirdropAlert",
    "airdrops",
    "satoshistreetbets",
    "defi",
    "altcoin",
    "MonadLight",
    "MegaETH",
]
IDX_SUBREDDITS = [
    "IndonesianInvestments",
    "indonesia",
    "StockMarket",
    "stocks",
]


def _praw_client():  # pragma: no cover
    """Lazily import + build PRAW client. Skip if credentials missing."""
    import praw

    reddit = praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
        username=settings.reddit_username or None,
        password=settings.reddit_password or None,
    )
    reddit.read_only = True
    return reddit


class RedditHotFetcher(BaseFetcher):
    CATEGORY = "sentiment.reddit"
    SOURCE = "reddit-hot"

    def __init__(self, subreddits: Optional[List[str]] = None, limit_per_sub: int = 15) -> None:
        self.subreddits = subreddits or CRYPTO_SUBREDDITS
        self.limit_per_sub = limit_per_sub

    async def fetch(self) -> List[Dict[str, Any]]:
        if not settings.reddit_enabled:
            log.info("Reddit disabled (no creds). Falling back to public JSON for %s", self.SOURCE)
            return await self._fetch_public()
        
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_sync)

    async def _fetch_public(self) -> List[Dict[str, Any]]:
        """Fallback to Reddit public JSON when PRAW credentials missing."""
        from ..core.http import Stealth
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_public_sync)

    def _fetch_public_sync(self) -> List[Dict[str, Any]]:
        out = []
        stealth = Stealth()
        try:
            for sub in self.subreddits:
                url = f"https://www.reddit.com/r/{sub}/hot.json?limit={self.limit_per_sub}"
                try:
                    resp = stealth.get(url, timeout=15, headers={"Accept":"application/json"})
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for child in data.get("data",{}).get("children",[]):
                        d = child.get("data",{})
                        if d.get("distinguished") or d.get("stickied"):
                            continue
                        out.append(self.record(
                            id=f"rd-pub-{d.get('id')}",
                            subreddit=sub,
                            post_id=d.get("id"),
                            title=d.get("title",""),
                            score=d.get("score",0),
                            num_comments=int(d.get("num_comments",0) or 0),
                            created_utc=int(d.get("created_utc",0)),
                            author=str(d.get("author") or ""),
                            url=d.get("url",""),
                            permalink=f"https://reddit.com{d.get('permalink','')}",
                            kind="hot-public",
                        ))
                except Exception:
                    continue
        finally:
            stealth.close()
        return out

    def _run_sync(self) -> List[Dict[str, Any]]:
        try:
            reddit = _praw_client()
        except Exception as e:  # noqa: BLE001
            log.warning("Reddit init failed: %s", e)
            return []

        out: List[Dict[str, Any]] = []
        for sub in self.subreddits:
            try:
                for post in reddit.subreddit(sub).hot(limit=self.limit_per_sub):
                    if post.spoiler or post.stickied:
                        continue
                    out.append(
                        self.record(
                            id=f"rd-hot-{post.id}",
                            subreddit=sub,
                            post_id=post.id,
                            title=post.title,
                            score=post.score,
                            num_comments=int(getattr(post, "num_comments", 0) or 0),
                            created_utc=int(post.created_utc),
                            author=str(post.author) if getattr(post, "author", None) else None,
                            flair=str(post.link_flair_text) if getattr(post, "link_flair_text", None) else None,
                            upvote_ratio=float(getattr(post, "upvote_ratio", 0) or 0),
                            url=post.url,
                            permalink=f"https://reddit.com{post.permalink}",
                            kind="hot",
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(" subreddit %s failed: %s", sub, e)
        return out


class RedditTrendingFetcher(BaseFetcher):
    """Top posts in last 24h across the watchlist — best 'trending' signal."""

    CATEGORY = "sentiment.reddit"
    SOURCE = "reddit-top24h"

    def __init__(self, subreddits: Optional[List[str]] = None, limit_per_sub: int = 10) -> None:
        self.subreddits = subreddits or CRYPTO_SUBREDDITS
        self.limit_per_sub = limit_per_sub

    async def fetch(self) -> List[Dict[str, Any]]:
        if not settings.reddit_enabled:
            log.info("Reddit disabled (no creds). Falling back to public JSON for %s", self.SOURCE)
            return await self._fetch_public()
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_sync)

    async def _fetch_public(self) -> List[Dict[str, Any]]:
        from ..core.http import Stealth
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_public_sync)

    def _fetch_public_sync(self) -> List[Dict[str, Any]]:
        out = []
        stealth = Stealth()
        try:
            for sub in self.subreddits:
                url = f"https://www.reddit.com/r/{sub}/top.json?t=day&limit={self.limit_per_sub}"
                try:
                    resp = stealth.get(url, timeout=15, headers={"Accept":"application/json"})
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for child in data.get("data",{}).get("children",[]):
                        d = child.get("data",{})
                        out.append(self.record(
                            id=f"rd-pub-top-{d.get('id')}",
                            subreddit=sub,
                            post_id=d.get("id"),
                            title=d.get("title",""),
                            score=d.get("score",0),
                            num_comments=int(d.get("num_comments",0) or 0),
                            created_utc=int(d.get("created_utc",0)),
                            author=str(d.get("author") or ""),
                            url=d.get("url",""),
                            permalink=f"https://reddit.com{d.get('permalink','')}",
                            kind="top-day-public",
                        ))
                except Exception:
                    continue
        finally:
            stealth.close()
        return out

    def _run_sync(self) -> List[Dict[str, Any]]:
        reddit = _praw_client()
        out: List[Dict[str, Any]] = []
        for sub in self.subreddits:
            try:
                for post in reddit.subreddit(sub).top(time_filter="day", limit=self.limit_per_sub):
                    out.append(
                        self.record(
                            id=f"rd-top-{post.id}",
                            subreddit=sub,
                            post_id=post.id,
                            title=post.title,
                            score=post.score,
                            num_comments=int(getattr(post, "num_comments", 0) or 0),
                            created_utc=int(post.created_utc),
                            author=str(post.author) if getattr(post, "author", None) else None,
                            upvote_ratio=float(getattr(post, "upvote_ratio", 0) or 0),
                            permalink=f"https://reddit.com{post.permalink}",
                            url=post.url,
                            kind="top-day",
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(" subreddit %s top24h failed: %s", sub, e)
        return out


class NitterTrendingFetcher(BaseFetcher):
    """X/Twitter trending via nitter self-hosted instances.

    Nitter RSS: https://nitter.privacydev.net/{user}/rss returns recent posts
    of a user. For trending, we use search: `/?f=tweets&q=<keyword>`.

    Many nitter instances are down as of 2026. We cycle through the configured
    list and stop on first success. If all dead, return [].
    """

    CATEGORY = "sentiment.x"
    SOURCE = "nitter"

    DEFAULT_KEYWORDS = ["airdrop", "new coin", "trending crypto", "MegaETH", "Monad", "Abstract chain"]

    def __init__(self, keywords: Optional[List[str]] = None, limit_per_keyword: int = 8) -> None:
        self.keywords = keywords or self.DEFAULT_KEYWORDS
        self.limit_per_keyword = limit_per_keyword

    async def fetch(self) -> List[Dict[str, Any]]:
        from selectolax.parser import HTMLParser

        out: List[Dict[str, Any]] = []
        for kw in self.keywords:
            records = await self._search_keyword(kw)
            out.extend(records[: self.limit_per_keyword])
        return out

    async def _search_keyword(self, keyword: str) -> List[Dict[str, Any]]:
        from selectolax.parser import HTMLParser as _HTML  # local

        for base in settings.nitter_list:
            try:
                stealth = Stealth()
                try:
                    resp = stealth.get(
                        base,
                        params={"f": "tweets", "q": keyword},
                        headers={"Accept": "text/html"},
                        timeout=15,
                    )
                    if resp.status_code != 200:
                        continue
                    tree = HTMLParser(resp.text)
                    tweets = tree.css("div.timeline-item, .tweet")
                    if not tweets:
                        continue
                    out: List[Dict[str, Any]] = []
                    for i, t in enumerate(tweets[: self.limit_per_keyword]):
                        # Nitter tweet DOM: .tweet-text, .tweet-date a
                        body = t.css_first(".tweet-text, .timeline-item .tweet-content")
                        anchor = t.css_first(".tweet-date a, a.tweet-link")
                        author = t.css_first(".username, .fullname")
                        tweet_url = anchor.attributes.get("href") if anchor else None
                        full_url = (base + tweet_url) if tweet_url and tweet_url.startswith("/") else tweet_url
                        out.append(
                            self.record(
                                id=f"x-{i}-{(tweet_url or keyword)[-40:]}",
                                query=keyword,
                                source_instance=base,
                                author=author.text(strip=True) if author else None,
                                body=body.text(strip=True) if body else "",
                                url=full_url or "",
                                kind="nitter",
                            )
                        )
                    return out
                finally:
                    stealth.close()
            except Exception as e:  # noqa: BLE001
                log.debug(" nitter %s failed for '%s': %s", base, keyword, e)
                continue

        log.info("All nitter instances failed for '%s'", keyword)
        return []


class RedditSentimentIdxFetcher(RedditHotFetcher):
    """Specialized Reddit fetcher for IDX subreddits."""

    CATEGORY = "sentiment.reddit_idx"
    SOURCE = "reddit-idx"

    def __init__(self) -> None:
        super().__init__(subreddits=IDX_SUBREDDITS, limit_per_sub=10)
