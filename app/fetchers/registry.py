"""Fetcher registry — single place that lists all fetchers for the scheduler/API.

To add a new source: implement BaseFetcher subclass, then add an instance here.
"""
from __future__ import annotations

from typing import List

from .base import BaseFetcher
from .airdrops import (
    AirdropsIoFetcher,
    DefiLlamaAirdropFetcher,
    GalxeCampaignFetcher,
    Layer3Fetcher,
)
from .bounties import BinanceSquareFetcher, GitcoinRoundsFetcher, ImmunefiBountiesFetcher
from .bounties_bitcointalk import BitcoinTalkBountyFetcher
from .crypto_extra import (
    CryptoRankTrendingFetcher,
    DEXScreenerBoostedTokensFetcher,
    DEXScreenerTrendingMetasFetcher,
    FearGreedIndexFetcher,
)
from .crypto_trending import (
    CoinGeckoGainersFetcher,
    CoinGeckoTrendingFetcher,
    # NOTE: DEXScreenerTrendingFetcher + DEXScreenerTrendingVolumeFetcher removed —
    # both hit dead endpoints (/token-boosts/top-in-24h and /tokens/v1/trending are 404).
    # Replaced by DEXScreenerTrendingMetasFetcher + DEXScreenerBoostedTokensFetcher
    # in crypto_extra.py (new endpoints /metas/trending/v1 and /token-boosts/top/v1).
)
from .emerging_chains import (
    EmergingChainNewsFetcher,
    EmergingChainStatusFetcher,
    EmergingChainTwitterFetcher,
)
from .idx_news import (
    BisnisMarketRSSFetcher,
    CNNIndonesiaMarketRSSFetcher,
    IdxAnnouncementsFetcher,
    InvestingIdRSSFetcher,
    KontanMarketRSSFetcher,
)
from .sentiment import (
    NitterTrendingFetcher,
    RedditHotFetcher,
    RedditSentimentIdxFetcher,
    RedditTrendingFetcher,
)
from .threads import ThreadsTrendingFetcher
from .sentiment_crypto import (
    CoinDeskRssFetcher,
    CoinJournalRssFetcher,
    CoinTelegraphRssFetcher,
    CryptoBriefingRssFetcher,
    CryptoPotatoRssFetcher,
    DecryptRssFetcher,
    EthNewsRssFetcher,
    GoogleNewsAirdropCryptoFetcher,
    GoogleNewsCryptoTrendingFetcher,
    NewsBtcRssFetcher,
    TheBlockRssFetcher,
    TheDefiantRssFetcher,
    UTodayRssFetcher,
)
from .sentiment_idx import (
    GoogleNewsIdxFetcher,
    GoogleNewsIdxSharesFetcher,
    IDXChannelRssFetcher,
    TempoBisnisRssFetcher,
)
from .cmc_trending import (
    CoinMarketCapListingsFetcher,
    CoinMarketCapGainersFetcher,
)
from .hl_whales import HyperliquidWhaleWatcher
from .iqplus import IQPlusNewsFetcher
from .new_tokens import NewTokenPoolsFetcher
from .tree_news import TreeNewsFetcher


def all_fetchers() -> List[BaseFetcher]:
    """Return one instance of every configured fetcher."""
    return [
        # Crypto Trending (CoinGecko + DEXScreener v2 + CryptoRank + Fear&Greed + CMC)
        CoinGeckoTrendingFetcher(),
        CoinGeckoGainersFetcher(),
        CoinMarketCapListingsFetcher(),
        CoinMarketCapGainersFetcher(),
        # DEXScreener v2 endpoints (new live URLs — see crypto_extra.py)
        DEXScreenerTrendingMetasFetcher(),
        DEXScreenerBoostedTokensFetcher(),
        CryptoRankTrendingFetcher(),
        FearGreedIndexFetcher(),
        # Airdrops
        DefiLlamaAirdropFetcher(),
        AirdropsIoFetcher(),
        Layer3Fetcher(),
        GalxeCampaignFetcher(),
        # Bounties (BitcoinTalk board 238 = primary; Immunefi/Gitcoin/Binance = silent fallbacks)
        BitcoinTalkBountyFetcher(),
        ImmunefiBountiesFetcher(),
        GitcoinRoundsFetcher(),
        BinanceSquareFetcher(),
        # Sentiment — Coin/Token (Reddit + X + Threads + crypto news RSS + GoogleNews)
        RedditHotFetcher(),
        RedditTrendingFetcher(),
        NitterTrendingFetcher(),  # CATEGORY sentiment.x, applies to coin/token side
        ThreadsTrendingFetcher(),  # CATEGORY sentiment.threads
        CoinDeskRssFetcher(),
        DecryptRssFetcher(),
        CoinTelegraphRssFetcher(),
        TheBlockRssFetcher(),
        CryptoBriefingRssFetcher(),
        UTodayRssFetcher(),
        TheDefiantRssFetcher(),
        NewsBtcRssFetcher(),
        EthNewsRssFetcher(),
        CoinJournalRssFetcher(),
        CryptoPotatoRssFetcher(),
        GoogleNewsCryptoTrendingFetcher(),
        GoogleNewsAirdropCryptoFetcher(),
        # Sentiment — Saham IDX (RedditIDX + Google News IHSG + IDX Channel + Tempo)
        RedditSentimentIdxFetcher(),
        IDXChannelRssFetcher(),
        TempoBisnisRssFetcher(),
        GoogleNewsIdxFetcher(),
        GoogleNewsIdxSharesFetcher(),
        # Emerging chains
        EmergingChainStatusFetcher(),
        EmergingChainNewsFetcher(),
        EmergingChainTwitterFetcher(),
        # IDX News (announcements + investing.com RSS + IQPlus wire)
        IdxAnnouncementsFetcher(),
        BisnisMarketRSSFetcher(),
        KontanMarketRSSFetcher(),
        CNNIndonesiaMarketRSSFetcher(),
        InvestingIdRSSFetcher(),
        IQPlusNewsFetcher(),
        # Signal layer (see docs/RISET-SIGNAL-FILTER.md + docs/METRIK-FILTER.md)
        TreeNewsFetcher(),
        HyperliquidWhaleWatcher(),
        NewTokenPoolsFetcher(),
    ]


def fetchers_for(category_prefix: str) -> List[BaseFetcher]:
    """Filter fetchers whose CATEGORY starts with prefix (e.g. 'crypto' or 'idx')."""
    return [f for f in all_fetchers() if f.CATEGORY.startswith(category_prefix)]


# Group definitions for the UI.
# Per user request 2026-08-09: split Sentiment into 2 tabs (Coin/Token + Saham IDX).
# Each tab groups: reddit + x + news (Threads/Facebook not scrapable in 2026, excluded).
FETCHER_GROUPS = [
    {
        "key": "crypto_trending",
        "label": "Crypto Trending",
        "categories": ["crypto.trending", "crypto.gainers"],
        "icon": "🔥",
    },
    {"key": "airdrops", "label": "Airdrops", "categories": ["airdrop"], "icon": "🎁"},
    {"key": "bounties", "label": "Bounties", "categories": ["bounty"], "icon": "🛡️"},
    {
        "key": "sentiment_coin",
        "label": "Sentiment Coin/Token",
        "categories": ["sentiment.reddit", "sentiment.x", "sentiment.threads", "sentiment.coin.news"],
        "icon": "💬",
    },
    {
        "key": "sentiment_idx",
        "label": "Sentiment Saham IDX",
        "categories": ["sentiment.reddit_idx", "sentiment.idx.news"],
        "icon": "📈",
    },
    {
        "key": "emerging",
        "label": "Emerging Chains",
        "categories": ["emerging_chain.status", "emerging_chain.news", "emerging_chain.social"],
        "icon": "🚀",
    },
    {
        "key": "idx_news",
        "label": "IDX News",
        "categories": ["idx.news", "idx.announcement"],
        "icon": "📰",
    },
    {
        "key": "signals",
        "label": "Signal Filter",
        "categories": ["signal.news", "signal.onchain", "signal.newtoken"],
        "icon": "⚡",
    },
]
