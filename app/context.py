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

import os
from typing import Any, Dict, Optional

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
from core.strategy.council import StrategyCouncil
from core.strategy.decay_monitor import DecayMonitor
from core.strategy.meta_label import MetaLabeler
from core.strategy.regime_router import RegimeRouter
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
        # Per-symbol learner cache (A5 / P3.4). Only used when
        # learning.per_symbol is true; keyed by the raw symbol string.
        self._symbol_learners: Dict[str, Any] = {}
        self._feature_builder: Optional[FeatureBuilder] = None
        self._memory: Optional[MemoryStore] = None
        # Phase 5 / P5.3: optional live strategy council (built only when
        # decision.council.enabled is true).
        self._council: Optional[StrategyCouncil] = None
        # Phase 5 / P5.6: optional strategy decay monitor (built only when
        # decision.decay_monitor.enabled is true).
        self._decay_monitor: Optional[DecayMonitor] = None
        self._news: Optional[NewsAnalyzer] = None
        self._time_stats: Optional[TimeStats] = None
        self._timing: Optional[TimeContextProvider] = None
        self._risk: Optional[RiskManager] = None
        self._orders: Optional[OrderManager] = None
        self._engine: Optional[DecisionEngine] = None
        # UPGRADE_PLAN U6.1: optional meta-labeling gate (built only when
        # decision.meta_label.enabled is true; loads its persisted model once).
        self._meta_labeler = None
        # UPGRADE_PLAN U6.2: lazily built regime router (only when
        # decision.regime_router.enabled is true; loads its persisted champion
        # map once). None when disabled so the engine falls back to parity top-1.
        self._regime_router = None

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

    # ------------------------------------------------------------------ #
    @staticmethod
    def _per_symbol_model_file(base_model_file: str, symbol: str) -> str:
        """
        Derive a per-symbol model filename from the shared model_file (A5).

        Mirrors app/runners.py::_per_symbol_model_file exactly so training and
        the decision-engine lookup agree on where each symbol's model lives:
        insert a sanitized symbol before the extension, e.g.
        "models/ml_classifier.pkl" -> "models/ml_classifier_EURUSD.pkl". A
        broker symbol like "EURUSD.m" is sanitized to safe filename characters.
        """
        safe = "".join(
            ch for ch in str(symbol) if ch.isalnum() or ch in ("_", "-", ".")
        ) or "SYMBOL"
        root, ext = os.path.splitext(base_model_file)
        if ext:
            return "%s_%s%s" % (root, safe, ext)
        return "%s_%s" % (base_model_file, safe)

    def learner_for(self, symbol: str):
        """
        Return the learner the decision engine should use for `symbol` (A5 / P3.4).

        When `learning.per_symbol` is false (default) this is simply the shared
        `learner` singleton, so the light path is unchanged. When true, it builds
        and caches ONE learner per symbol and tries to load that symbol's
        persisted model file (models/<model>_<SYMBOL>.pkl). If the per-symbol
        file is missing or fails to load (e.g. the symbol was never trained), it
        falls back to the shared learner so the bot never crashes and simply
        contributes a neutral signal when nothing is ready.
        """
        if not bool(self.cfg.get_path("learning.per_symbol", False)):
            return self.learner

        key = str(symbol)
        if key in self._symbol_learners:
            return self._symbol_learners[key]

        name = self.cfg.get_path("learning.active_model", "ml_classifier")
        base_file = self.cfg.get_path("learning.%s.model_file" % name, "")
        model = build_active_model(self.cfg)
        loaded = False
        if base_file:
            sym_file = self._per_symbol_model_file(base_file, key)
            path = resolve_path(self.cfg, sym_file)
            if os.path.exists(path):
                try:
                    model.load(path)
                    loaded = True
                except Exception as exc:
                    self.log.warning(
                        "Could not load per-symbol model %s: %s", path, exc)
            else:
                self.log.info(
                    "No per-symbol model for %s at %s; using shared model.",
                    key, path)

        if not loaded:
            # Fall back to the shared learner (which may itself be loaded or
            # neutral); this keeps behavior graceful when a symbol is untrained.
            model = self.learner
        self._symbol_learners[key] = model
        return model

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
    def council(self) -> Optional[StrategyCouncil]:
        """
        Phase 5 / P5.3 (Track B / B1): the live strategy council that supplies a
        per-strategy credibility weight to the ensemble blend. Built ONLY when
        ``decision.council.enabled`` is true, so the default light path skips it
        entirely and behaves byte-for-byte as before. On first build its rolling
        rewards are restored from the memory store (P5.2) so live credibility
        survives restarts.
        """
        if self._council is None and bool(
            self.cfg.get_path("decision.council.enabled", False)
        ):
            council = StrategyCouncil(self.cfg)
            try:
                self.memory.load_council(council)
            except Exception as exc:
                self.log.error("council: load from memory failed: %s", exc)
            self._council = council
        return self._council

    @property
    def decay_monitor(self) -> Optional[DecayMonitor]:
        """
        Phase 5 / P5.6 (Track B / B3): the strategy decay monitor that flags
        registry strategies whose recent live PnL has drifted below their
        walk-forward distribution. Built ONLY when
        ``decision.decay_monitor.enabled`` is true, so the default light path
        skips it entirely and behaves byte-for-byte as before.
        """
        if self._decay_monitor is None and bool(
            self.cfg.get_path("decision.decay_monitor.enabled", False)
        ):
            self._decay_monitor = DecayMonitor(self.cfg)
        return self._decay_monitor

    def decay_suspects(self) -> set:
        """
        Compute the set of strategy fingerprints the decay monitor currently
        deems "suspect" (recent live PnL has decayed vs their walk-forward
        reference). Returns an empty set when the monitor is disabled or no
        strategy has enough live evidence, so callers can pass it unconditionally
        and the engine simply drops nothing. Degrades gracefully (any error ->
        the fingerprints assessed so far).
        """
        monitor = self.decay_monitor
        if monitor is None or not monitor.enabled:
            return set()
        suspects: set = set()
        try:
            fingerprints = self.memory.live_trade_fingerprints()
        except Exception as exc:
            self.log.error("decay_suspects: fingerprint fetch failed: %s", exc)
            return suspects
        window = int(self.cfg.get_path(
            "decision.decay_monitor.recent_window", 200))
        for fp in fingerprints:
            try:
                reference = self.memory.reference_pnls(fp)
                recent = self.memory.recent_live_pnls(fp, limit=window)
                if monitor.assess(reference, recent).suspect:
                    suspects.add(fp)
            except Exception as exc:
                self.log.error("decay_suspects: assess %s failed: %s", fp, exc)
        if suspects:
            self.log.info("decay monitor flagged %d suspect strategy(ies): %s",
                          len(suspects), ", ".join(sorted(suspects)))
        return suspects

    @property
    def risk(self) -> RiskManager:
        if self._risk is None:
            self._risk = RiskManager(self.cfg, self.connector)
        return self._risk

    @property
    def orders(self) -> OrderManager:
        if self._orders is None:
            self._orders = OrderManager(self.cfg, self.connector, self.risk,
                                        memory=self.memory)
        return self._orders

    @property
    def meta_labeler(self):
        """UPGRADE_PLAN U6.1: lazily build the meta-labeling gate.

        Only constructed when decision.meta_label.enabled is true; it loads its
        persisted model (dict keyed by strategy fingerprint) once. Returns None
        when disabled so the engine never receives a gate and the default path
        is unchanged. The config carries project_root (set by the loader) so the
        model path resolves relative to the repo like other artifacts.
        """
        if not bool(self.cfg.get_path("decision.meta_label.enabled", False)):
            return None
        if self._meta_labeler is None:
            self._meta_labeler = MetaLabeler(self.cfg)
            try:
                self._meta_labeler.load()
            except Exception as exc:
                self.log.error("meta_labeler load failed: %s", exc)
        return self._meta_labeler

    @property
    def regime_router(self):
        """UPGRADE_PLAN U6.2: lazily build the regime router.

        Only constructed when decision.regime_router.enabled is true; it loads
        its persisted per-regime champion map once. Returns None when disabled
        (engine falls back to plain parity top-1) or when no champion map has
        been trained yet, so the default path is unchanged.
        """
        if not bool(self.cfg.get_path("decision.regime_router.enabled", False)):
            return None
        if self._regime_router is None:
            router = RegimeRouter(self.cfg, memory=self.memory)
            try:
                router.load()
            except Exception as exc:
                self.log.error("regime_router load failed: %s", exc)
            self._regime_router = router
        return self._regime_router

    @property
    def engine(self) -> DecisionEngine:
        if self._engine is None:
            # A5 / P3.4: when per-symbol ML is enabled, give the engine a
            # provider so it uses each deciding symbol's own model; otherwise
            # leave it None so the shared learner is used everywhere (unchanged).
            per_symbol = bool(self.cfg.get_path("learning.per_symbol", False))
            provider = self.learner_for if per_symbol else None
            self._engine = DecisionEngine(
                self.cfg,
                learner=self.learner,
                feature_builder=self.feature_builder,
                news_analyzer=self.news,
                memory=self.memory,
                timing=self.timing,
                learner_provider=provider,
                council=self.council,
                decay_suspects=self.decay_suspects(),
                meta_labeler=self.meta_labeler,
                regime_router=self.regime_router,
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
