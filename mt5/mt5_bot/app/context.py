"""
Application context: builds and holds every shared component.

BotContext is the single place where the whole system is assembled from config,
so each mode runner (live/paper/backtest/search/train) can grab exactly the
pieces it needs without duplicating setup code. Heavy pieces (learner, news) are
built lazily and honor the config toggles so the bot stays light on weak
hardware.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Optional

from config.loader import load_config, resolve_path
from core.utils.helpers import set_global_seed
from core.utils.logger import get_logger

# Import indicator package so all indicators register themselves.
import core.indicators  # noqa: F401

from core.data.mt5_connector import MT5Connector
from core.data.data_feed import DataFeed
from core.indicators.registry import build_enabled_indicators
from core.learning.factory import build_active_model
from core.learning.features import FeatureBuilder
from core.memory.store import MemoryStore
from core.news.aggregator import NewsAnalyzer
from core.timing.time_stats import TimeStats
from core.timing.time_context import TimeContextProvider
from core.decision.engine import DecisionEngine
from core.execution.risk_manager import RiskManager
from core.execution.order_manager import OrderManager


class BotContext(object):
    """Container that lazily builds and caches all shared components."""

    def __init__(self, config_path: Optional[str] = None):
        self.cfg = load_config(config_path)
        # Seed everything for reproducibility.
        set_global_seed(int(self.cfg.get_path("general.random_seed", 42)))
        self.log = get_logger("app.context", self.cfg)

        # Lazily-built singletons.
        self._connector: Optional[MT5Connector] = None
        self._data_feed: Optional[DataFeed] = None
        self._indicators = None
        self._learner = None
        self._feature_builder: Optional[FeatureBuilder] = None
        self._memory: Optional[MemoryStore] = None
        self._news: Optional[NewsAnalyzer] = None
        self._time_stats: Optional[TimeStats] = None
        self._timing: Optional[TimeContextProvider] = None
        self._risk: Optional[RiskManager] = None
        self._orders: Optional[OrderManager] = None
        self._engine: Optional[DecisionEngine] = None

    # ------------------------------------------------------------------ #
    @property
    def connector(self) -> MT5Connector:
        if self._connector is None:
            self._connector = MT5Connector(self.cfg)
        return self._connector

    @property
    def data_feed(self) -> DataFeed:
        if self._data_feed is None:
            self._data_feed = DataFeed(self.cfg, self.connector)
        return self._data_feed

    @property
    def indicators(self):
        if self._indicators is None:
            self._indicators = build_enabled_indicators(self.cfg)
        return self._indicators

    @property
    def feature_builder(self) -> FeatureBuilder:
        if self._feature_builder is None:
            self._feature_builder = FeatureBuilder(self.cfg, self.indicators)
        return self._feature_builder

    @property
    def learner(self):
        """
        Build the active learner and try to load its persisted model file.
        Returns a learner that may be 'not ready' (then it contributes neutral
        signals), so callers never need to special-case a missing model.
        """
        if self._learner is None:
            model = build_active_model(self.cfg)
            # Attempt to load a saved model for the active learner.
            name = self.cfg.get_path("learning.active_model", "ml_classifier")
            model_file = self.cfg.get_path("learning.%s.model_file" % name, "")
            if model_file:
                path = resolve_path(self.cfg, model_file)
                try:
                    model.load(path)
                except Exception as exc:
                    self.log.warning("Could not load model %s: %s", path, exc)
            self._learner = model
        return self._learner

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            self._memory = MemoryStore(self.cfg)
        return self._memory

    @property
    def news(self) -> Optional[NewsAnalyzer]:
        if self._news is None and bool(self.cfg.get_path("news.enabled", True)):
            self._news = NewsAnalyzer(self.cfg)
        return self._news

    @property
    def time_stats(self) -> TimeStats:
        """
        Phase 5 (user-update-request): the persistent per-time-bucket edge
        statistics store (shares the memory SQLite DB). Always available; it
        simply returns neutral edges until it has learned enough trades.
        """
        if self._time_stats is None:
            self._time_stats = TimeStats(self.cfg)
        return self._time_stats

    @property
    def timing(self) -> Optional[TimeContextProvider]:
        """
        Phase 5 (user-update-request): the time-context provider fed into the
        decision engine. Built only when timing is enabled in config, so the
        default light path skips it entirely.
        """
        if self._timing is None and bool(self.cfg.get_path("timing.enabled", False)):
            self._timing = TimeContextProvider(self.cfg, time_stats=self.time_stats)
        return self._timing

    @property
    def risk(self) -> RiskManager:
        if self._risk is None:
            self._risk = RiskManager(self.cfg, self.connector)
        return self._risk

    @property
    def orders(self) -> OrderManager:
        if self._orders is None:
            self._orders = OrderManager(self.cfg, self.connector, self.risk)
        return self._orders

    @property
    def engine(self) -> DecisionEngine:
        if self._engine is None:
            self._engine = DecisionEngine(
                self.cfg,
                learner=self.learner,
                feature_builder=self.feature_builder,
                news_analyzer=self.news,
                memory=self.memory,
                timing=self.timing,
            )
        return self._engine

    # ------------------------------------------------------------------ #
    def connect_mt5(self) -> bool:
        """Attempt to connect to MT5 if enabled in config. Returns success."""
        if not bool(self.cfg.get_path("mt5.enabled", True)):
            self.log.info("MT5 disabled in config; running without a terminal.")
            return False
        ok = self.connector.connect(raise_on_fail=False)
        if not ok:
            self.log.warning(
                "MT5 connection not established. The bot will use CSV data "
                "where available and will not send live orders."
            )
        return ok

    def shutdown(self) -> None:
        if self._connector is not None:
            self._connector.shutdown()
