import hashlib
import logging
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf

EPS = 1e-8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("APEX-QUANT-4X")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class ModeSpec:
    name: str
    trigger_tf: str
    confirm_tf: str
    anchor_tf: str
    sl_atr_mult: float
    tp1_r: float
    tp2_r: float
    tp3_r: float
    min_score: float
    min_confluence: int
    cooldown_sec: int
    flip_guard_sec: int
    entry_zone_atr_mult: float
    trail_rule: str


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    asset_class: str  # "METALS", "INDEX", "CRYPTO"
    yf_primary: str
    yf_fallbacks: Tuple[str, ...]
    coinbase_product: Optional[str]
    kraken_pair: Optional[str]
    kucoin_symbol: Optional[str]
    contract_size: float
    min_lot: float
    lot_step: float
    max_lot: float
    price_offset: float
    price_multiplier: float
    default_spread: float
    slippage_pts: float
    session_gated: bool


@dataclass
class SignalDecision:
    symbol: str
    mode: str
    action: str  # BUY, SELL, NO_SIGNAL
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    zone_low: float
    zone_high: float
    lot: float
    score: float
    regime: str
    adx_val: float
    oos_pf: float
    trail_desc: str
    reasons: List[str]
    timestamp_utc: str


@dataclass
class BacktestSummary:
    symbol: str
    mode: str
    best_atr: int
    best_z: float
    trades: int
    win_rate: float
    profit_factor: float
    oos_trades: int
    oos_pf: float
    max_dd_pct: float
    net_return_pct: float
    equity_curve: List[float]


# ==============================================================================
# KONFIGURASI TERPUSAT (SALDO, RISIKO, OFFSET, MULTIPLIER, KONTRAK & GUARD)
# ==============================================================================
ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000.0"))
RISK_PERCENT: float = float(os.getenv("RISK_PERCENT", "1.0"))
SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "trading_signals.db")
BACKTEST_CHART_PATH: str = os.getenv("BACKTEST_CHART_PATH", "backtest_performance.png")
HTTP_TIMEOUT_SEC: int = int(os.getenv("HTTP_TIMEOUT_SEC", "15"))

# Parameter khusus NAS100 (^NDX / QQQ) agar mudah disesuaikan dengan broker MT5
NAS100_PRICE_OFFSET: float = float(os.getenv("NAS100_PRICE_OFFSET", "0.0"))
NAS100_PRICE_MULTIPLIER: float = float(os.getenv("NAS100_PRICE_MULTIPLIER", "1.0"))

# Parameter offset & multiplier per instrumen lain bila diperlukan
XAUUSD_PRICE_OFFSET: float = float(os.getenv("XAUUSD_PRICE_OFFSET", "0.0"))
XAUUSD_PRICE_MULTIPLIER: float = float(os.getenv("XAUUSD_PRICE_MULTIPLIER", "1.0"))
BTCUSD_PRICE_OFFSET: float = float(os.getenv("BTCUSD_PRICE_OFFSET", "0.0"))
BTCUSD_PRICE_MULTIPLIER: float = float(os.getenv("BTCUSD_PRICE_MULTIPLIER", "1.0"))
ETHUSD_PRICE_OFFSET: float = float(os.getenv("ETHUSD_PRICE_OFFSET", "0.0"))
ETHUSD_PRICE_MULTIPLIER: float = float(os.getenv("ETHUSD_PRICE_MULTIPLIER", "1.0"))

ASSETS: Dict[str, AssetSpec] = {
    "XAUUSD": AssetSpec(
        symbol="XAUUSD",
        asset_class="METALS",
        yf_primary="PAXG-USD",
        yf_fallbacks=("GC=F",),
        coinbase_product="PAXG-USD",
        kraken_pair="PAXGUSD",
        kucoin_symbol="PAXG-USDT",
        contract_size=float(os.getenv("XAU_CONTRACT_SIZE", "100.0")),
        min_lot=0.01,
        lot_step=0.01,
        max_lot=float(os.getenv("MAX_LOT_XAU", "5.0")),
        price_offset=XAUUSD_PRICE_OFFSET,
        price_multiplier=XAUUSD_PRICE_MULTIPLIER,
        default_spread=float(os.getenv("XAU_DEFAULT_SPREAD", "0.35")),
        slippage_pts=float(os.getenv("XAU_SLIPPAGE_PTS", "0.15")),
        session_gated=True,
    ),
    "NAS100": AssetSpec(
        symbol="NAS100",
        asset_class="INDEX",
        yf_primary="^NDX",
        yf_fallbacks=("QQQ", "^IXIC"),
        coinbase_product=None,
        kraken_pair=None,
        kucoin_symbol=None,
        contract_size=float(os.getenv("NAS_CONTRACT_SIZE", "1.0")),
        min_lot=0.01,
        lot_step=0.01,
        max_lot=float(os.getenv("MAX_LOT_NAS", "5.0")),
        price_offset=NAS100_PRICE_OFFSET,
        price_multiplier=NAS100_PRICE_MULTIPLIER,
        default_spread=float(os.getenv("NAS_DEFAULT_SPREAD", "1.50")),
        slippage_pts=float(os.getenv("NAS_SLIPPAGE_PTS", "0.75")),
        session_gated=True,
    ),
    "BTCUSD": AssetSpec(
        symbol="BTCUSD",
        asset_class="CRYPTO",
        yf_primary="BTC-USD",
        yf_fallbacks=("BTCUSD=X",),
        coinbase_product="BTC-USD",
        kraken_pair="XXBTZUSD",
        kucoin_symbol="BTC-USDT",
        contract_size=float(os.getenv("BTC_CONTRACT_SIZE", "1.0")),
        min_lot=0.01,
        lot_step=0.01,
        max_lot=float(os.getenv("MAX_LOT_BTC", "2.0")),
        price_offset=BTCUSD_PRICE_OFFSET,
        price_multiplier=BTCUSD_PRICE_MULTIPLIER,
        default_spread=float(os.getenv("BTC_DEFAULT_SPREAD", "15.0")),
        slippage_pts=float(os.getenv("BTC_SLIPPAGE_PTS", "7.5")),
        session_gated=False,
    ),
    "ETHUSD": AssetSpec(
        symbol="ETHUSD",
        asset_class="CRYPTO",
        yf_primary="ETH-USD",
        yf_fallbacks=("ETHUSD=X",),
        coinbase_product="ETH-USD",
        kraken_pair="XETHZUSD",
        kucoin_symbol="ETH-USDT",
        contract_size=float(os.getenv("ETH_CONTRACT_SIZE", "1.0")),
        min_lot=0.01,
        lot_step=0.01,
        max_lot=float(os.getenv("MAX_LOT_ETH", "3.0")),
        price_offset=ETHUSD_PRICE_OFFSET,
        price_multiplier=ETHUSD_PRICE_MULTIPLIER,
        default_spread=float(os.getenv("ETH_DEFAULT_SPREAD", "0.50")),
        slippage_pts=float(os.getenv("ETH_SLIPPAGE_PTS", "0.25")),
        session_gated=False,
    ),
}

MODES: Dict[str, ModeSpec] = {
    "SCALP": ModeSpec(
        name="SCALP",
        trigger_tf="5m",
        confirm_tf="15m",
        anchor_tf="1h",
        sl_atr_mult=1.25,
        tp1_r=1.2,
        tp2_r=2.0,
        tp3_r=3.0,
        min_score=76.0,
        min_confluence=4,
        cooldown_sec=int(os.getenv("COOLDOWN_SCALP_SEC", "1800")),
        flip_guard_sec=int(os.getenv("FLIP_GUARD_SCALP_SEC", "2700")),
        entry_zone_atr_mult=0.45,
        trail_rule="Aktif @ TP1 (BE)",
    ),
    "SWING": ModeSpec(
        name="SWING",
        trigger_tf="1h",
        confirm_tf="4h",
        anchor_tf="1d",
        sl_atr_mult=1.80,
        tp1_r=1.5,
        tp2_r=2.5,
        tp3_r=4.0,
        min_score=74.0,
        min_confluence=4,
        cooldown_sec=int(os.getenv("COOLDOWN_SWING_SEC", "7200")),
        flip_guard_sec=int(os.getenv("FLIP_GUARD_SWING_SEC", "10800")),
        entry_zone_atr_mult=0.65,
        trail_rule="Aktif @ TP1 (BE+0.5ATR)",
    ),
}

TF_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

MIN_BARS_REQUIRED: Dict[str, int] = {
    "5m": 80,
    "15m": 80,
    "1h": 80,
    "4h": 70,
    "1d": 65,
}


class CircuitBreaker:
    def __init__(self, max_failures: int = 5, reset_after_sec: int = 300):
        self.max_failures = max_failures
        self.reset_after_sec = reset_after_sec
        self.failures: Dict[str, int] = {}
        self.opened_at: Dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        opened = self.opened_at.get(key)
        if opened is not None:
            if now - opened >= self.reset_after_sec:
                self.failures[key] = 0
                del self.opened_at[key]
                return True
            return False
        return True

    def record_success(self, key: str) -> None:
        self.failures[key] = 0
        if key in self.opened_at:
            del self.opened_at[key]

    def record_failure(self, key: str) -> None:
        count = self.failures.get(key, 0) + 1
        self.failures[key] = count
        if count >= self.max_failures and key not in self.opened_at:
            self.opened_at[key] = time.monotonic()
            logger.warning("CircuitBreaker OPEN untuk '%s' setelah %d kegagalan.", key, count)


class ResilientHttpClient:
    def __init__(self, breaker: CircuitBreaker, timeout: int = 15):
        self.breaker = breaker
        self.timeout = timeout
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "APEX-QUANT-4X/2.1"})
        return sess

    def _reset_session(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._new_session()

    def get_json(
        self, url: str, params: Optional[Dict[str, Any]] = None, service_key: str = "http"
    ) -> Optional[Any]:
        if not self.breaker.allow(service_key):
            return None
        backoffs = [2.0, 4.0, 8.0, 12.0]
        for attempt, wait_s in enumerate(backoffs, start=1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code in (401, 403):
                    logger.error(
                        "HTTP %d (Auth/Token tidak valid) pada %s; hentikan retry untuk run ini.",
                        resp.status_code,
                        service_key,
                    )
                    self.breaker.record_failure(service_key)
                    return None
                if resp.status_code == 429:
                    logger.warning("HTTP 429 pada %s (percobaan %d), tidur %.1fs", service_key, attempt, wait_s)
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                self.breaker.record_success(service_key)
                return resp.json()
            except Exception as exc:
                logger.warning("GET gagal %s (%s), percobaan %d: %s", service_key, url, attempt, exc)
                self._reset_session()
                if attempt < len(backoffs):
                    time.sleep(wait_s)
        self.breaker.record_failure(service_key)
        return None

    def post_json(
        self, url: str, payload: Dict[str, Any], service_key: str = "http_post"
    ) -> Optional[Dict[str, Any]]:
        if not self.breaker.allow(service_key):
            return None
        backoffs = [2.0, 4.0, 8.0, 12.0]
        for attempt, wait_s in enumerate(backoffs, start=1):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
                if resp.status_code in (401, 403):
                    logger.error(
                        "HTTP %d (Auth/Token tidak valid) pada POST %s; hentikan retry untuk run ini.",
                        resp.status_code,
                        service_key,
                    )
                    self.breaker.record_failure(service_key)
                    return None
                if resp.status_code == 429:
                    logger.warning("HTTP 429 POST %s (percobaan %d), tidur %.1fs", service_key, attempt, wait_s)
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                self.breaker.record_success(service_key)
                data = resp.json()
                return data if isinstance(data, dict) else {"ok": True}
            except Exception as exc:
                logger.warning("POST gagal %s (percobaan %d): %s", service_key, attempt, exc)
                self._reset_session()
                if attempt < len(backoffs):
                    time.sleep(wait_s)
        self.breaker.record_failure(service_key)
        return None


def flatten_and_clean_ohlcv(
    df: pd.DataFrame,
    tf: str,
    multiplier: float = 1.0,
    offset: float = 0.0,
    session_gated: bool = False,
) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    work = df.copy()
    if isinstance(work.columns, pd.MultiIndex):
        work.columns = work.columns.get_level_values(0)
    work.columns = [str(c).strip().lower() for c in work.columns]

    rename_map: Dict[str, str] = {}
    if "datetime" in work.columns and "timestamp" not in work.columns:
        rename_map["datetime"] = "timestamp"
    elif "date" in work.columns and "timestamp" not in work.columns:
        rename_map["date"] = "timestamp"
    if rename_map:
        work = work.rename(columns=rename_map)

    if "timestamp" not in work.columns:
        if isinstance(work.index, pd.DatetimeIndex):
            work = work.reset_index().rename(columns={work.index.name or "index": "timestamp"})
        else:
            return None

    required = ["timestamp", "open", "high", "low", "close"]
    if not all(col in work.columns for col in required):
        return None
    if "volume" not in work.columns:
        work["volume"] = 0.0

    work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=["timestamp", "open", "high", "low", "close"])
    work["volume"] = work["volume"].fillna(0.0)
    work = work[work["close"] > 0.0]
    work = work[work["high"] >= work["low"]]
    work = work.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)

    min_req = MIN_BARS_REQUIRED.get(tf, 70)
    if len(work) < min_req:
        return None

    tf_sec = TF_SECONDS.get(tf, 300)
    now_utc = datetime.now(timezone.utc)
    last_open = work["timestamp"].iloc[-1].to_pydatetime()
    if last_open.tzinfo is None:
        last_open = last_open.replace(tzinfo=timezone.utc)
    if (now_utc - last_open).total_seconds() < tf_sec:
        work = work.iloc[:-1].copy().reset_index(drop=True)

    if len(work) < min_req:
        return None

    latest_bar = work["timestamp"].iloc[-1].to_pydatetime()
    if latest_bar.tzinfo is None:
        latest_bar = latest_bar.replace(tzinfo=timezone.utc)

    max_stale_sec = (86400 * 4) if (session_gated and now_utc.weekday() >= 5) else max(tf_sec * 5, 1800)
    if (now_utc - latest_bar).total_seconds() > max_stale_sec:
        logger.warning("Data stale pada tf=%s: last_bar=%s now_utc=%s", tf, latest_bar, now_utc)
        return None

    ret = work["close"].pct_change().abs().fillna(0.0)
    spike_limit = 0.45 if tf == "1d" else 0.30
    if float(ret.tail(35).max()) > spike_limit:
        logger.warning("Spike anomali ekstrem terdeteksi pada tf=%s.", tf)
        return None

    mult = multiplier if multiplier != 0.0 else 1.0
    if mult != 1.0 or abs(offset) > 0.0:
        for col in ["open", "high", "low", "close"]:
            work[col] = (work[col] * mult) + offset

    return work[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


class MarketDataProvider:
    def __init__(self, http_client: ResilientHttpClient, breaker: CircuitBreaker):
        self.http = http_client
        self.breaker = breaker

    def _from_coinbase(self, asset: AssetSpec, tf: str, limit: int) -> Optional[pd.DataFrame]:
        if not asset.coinbase_product:
            return None
        gran_map = {"5m": 300, "15m": 900, "1h": 3600, "4h": 21600, "1d": 86400}
        gran = gran_map.get(tf)
        if not gran:
            return None
        url = f"https://api.exchange.coinbase.com/products/{asset.coinbase_product}/candles"
        raw = self.http.get_json(url, params={"granularity": gran}, service_key="coinbase")
        if not isinstance(raw, list) or not raw:
            return None
        rows = []
        for item in raw[:limit]:
            if isinstance(item, (list, tuple)) and len(item) >= 6:
                rows.append(
                    {
                        "timestamp": int(item[0]),
                        "low": float(item[1]),
                        "high": float(item[2]),
                        "open": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    }
                )
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return flatten_and_clean_ohlcv(
            df, tf, asset.price_multiplier, asset.price_offset, asset.session_gated
        )

    def _from_kraken(self, asset: AssetSpec, tf: str, limit: int) -> Optional[pd.DataFrame]:
        if not asset.kraken_pair:
            return None
        interval_map = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        interval = interval_map.get(tf)
        if not interval:
            return None
        url = "https://api.kraken.com/0/public/OHLC"
        data = self.http.get_json(
            url, params={"pair": asset.kraken_pair, "interval": interval}, service_key="kraken"
        )
        if not isinstance(data, dict) or data.get("error"):
            return None
        result = data.get("result") or {}
        series_key = next((k for k in result.keys() if k != "last"), None)
        if not series_key or not isinstance(result.get(series_key), list):
            return None
        rows = []
        for item in result[series_key][-limit:]:
            if isinstance(item, (list, tuple)) and len(item) >= 7:
                rows.append(
                    {
                        "timestamp": int(item[0]),
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[6]),
                    }
                )
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return flatten_and_clean_ohlcv(
            df, tf, asset.price_multiplier, asset.price_offset, asset.session_gated
        )

    def _from_kucoin(self, asset: AssetSpec, tf: str, limit: int) -> Optional[pd.DataFrame]:
        if not asset.kucoin_symbol:
            return None
        tf_map = {"5m": "5min", "15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day"}
        k_tf = tf_map.get(tf)
        if not k_tf:
            return None
        url = "https://api.kucoin.com/api/v1/market/candles"
        data = self.http.get_json(
            url, params={"symbol": asset.kucoin_symbol, "type": k_tf}, service_key="kucoin"
        )
        if (
            not isinstance(data, dict)
            or data.get("code") != "200000"
            or not isinstance(data.get("data"), list)
        ):
            return None
        rows = []
        for item in data["data"][:limit]:
            if isinstance(item, (list, tuple)) and len(item) >= 6:
                rows.append(
                    {
                        "timestamp": int(float(item[0])),
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]),
                    }
                )
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return flatten_and_clean_ohlcv(
            df, tf, asset.price_multiplier, asset.price_offset, asset.session_gated
        )

    def _from_yfinance_ticker(self, ticker: str, asset: AssetSpec, tf: str, limit: int) -> Optional[pd.DataFrame]:
        cb_key = f"yf_{ticker}"
        if not self.breaker.allow(cb_key):
            return None
        yf_map = {
            "5m": ("5m", "15d"),
            "15m": ("15m", "30d"),
            "1h": ("60m", "90d"),
            "4h": ("1h", "120d"),
            "1d": ("1d", "400d"),
        }
        interval, period = yf_map.get(tf, ("1h", "90d"))
        try:
            raw = yf.download(
                tickers=ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            if raw is None or raw.empty:
                self.breaker.record_failure(cb_key)
                return None
            df = flatten_and_clean_ohlcv(
                raw, tf, asset.price_multiplier, asset.price_offset, asset.session_gated
            )
            if df is not None and tf == "4h":
                df = (
                    df.set_index("timestamp")
                    .resample("4h")
                    .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                    .dropna(subset=["open", "high", "low", "close"])
                    .reset_index()
                )
                df = flatten_and_clean_ohlcv(df, "4h", 1.0, 0.0, asset.session_gated)
            if df is not None and len(df) >= MIN_BARS_REQUIRED.get(tf, 70):
                self.breaker.record_success(cb_key)
                return df.tail(limit).reset_index(drop=True)
            self.breaker.record_failure(cb_key)
            return None
        except Exception as exc:
            logger.warning("yfinance gagal ticker=%s tf=%s: %s", ticker, tf, exc)
            self.breaker.record_failure(cb_key)
            return None

    def fetch(self, symbol: str, tf: str, limit: int = 300) -> Tuple[Optional[pd.DataFrame], str]:
        asset = ASSETS[symbol]
        min_req = MIN_BARS_REQUIRED.get(tf, 70)

        candidates: List[Tuple[str, Any]] = []
        if asset.coinbase_product:
            candidates.append(("Coinbase", lambda: self._from_coinbase(asset, tf, limit)))
        if asset.kraken_pair:
            candidates.append(("Kraken", lambda: self._from_kraken(asset, tf, limit)))
        if asset.kucoin_symbol:
            candidates.append(("KuCoin", lambda: self._from_kucoin(asset, tf, limit)))
        for tkr in (asset.yf_primary, *asset.yf_fallbacks):
            candidates.append((f"YF:{tkr}", lambda t=tkr: self._from_yfinance_ticker(t, asset, tf, limit)))

        for src_label, fn in candidates:
            df = fn()
            if df is not None and len(df) >= min_req:
                return df.tail(limit).reset_index(drop=True), src_label
        return None, "NONE"


class SessionAndRegimeFilter:
    @staticmethod
    def session_status(asset: AssetSpec, now_utc: datetime) -> Tuple[bool, str]:
        if not asset.session_gated:
            return True, "CRYPTO_24_7"

        weekday = now_utc.weekday()
        if weekday >= 5:
            return False, "WEEKEND_CLOSED"

        hour = now_utc.hour
        if asset.symbol == "XAUUSD":
            if 7 <= hour <= 21:
                return True, "GOLD_ACTIVE_SESSION"
            return False, f"GOLD_LOW_LIQ({hour:02d}UTC)"

        if asset.symbol == "NAS100":
            if 13 <= hour <= 21:
                return True, "US_EQUITY_SESSION"
            return False, f"NAS_OFF_SESSION({hour:02d}UTC)"

        return True, "SESSION_OK"


class QuantFeatureEngine:
    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.ewm(alpha=1.0 / max(period, 1), adjust=False).mean()

    @staticmethod
    def rolling_zscore(series: pd.Series, window: int = 20) -> pd.Series:
        mean = series.rolling(window=window, min_periods=window).mean()
        std = series.rolling(window=window, min_periods=window).std().fillna(0.0)
        return (series - mean) / (std + EPS)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        up = df["high"].diff()
        down = -df["low"].diff()
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
        atr_s = QuantFeatureEngine.atr(df, period=period)
        plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (atr_s + EPS)
        minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (atr_s + EPS)
        dx = 100.0 * (plus_di - minus_di).abs() / ((plus_di + minus_di).abs() + EPS)
        return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)

    @staticmethod
    def detect_smc_structure(df: pd.DataFrame, lookback: int = 4) -> Dict[str, Any]:
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        n = len(df)

        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []

        for i in range(lookback, n - lookback):
            win_h = highs[i - lookback : i + lookback + 1]
            win_l = lows[i - lookback : i + lookback + 1]
            if highs[i] == np.max(win_h):
                swing_highs.append((i, float(highs[i])))
            if lows[i] == np.min(win_l):
                swing_lows.append((i, float(lows[i])))

        last_close = float(closes[-1])
        prev_close = float(closes[-2])
        last_sh = swing_highs[-1][1] if swing_highs else float(np.max(highs[-20:]))
        prev_sh = swing_highs[-2][1] if len(swing_highs) >= 2 else last_sh
        last_sl = swing_lows[-1][1] if swing_lows else float(np.min(lows[-20:]))
        prev_sl = swing_lows[-2][1] if len(swing_lows) >= 2 else last_sl

        if last_sh >= prev_sh and last_sl >= prev_sl:
            major_bias = "BULLISH"
        elif last_sh <= prev_sh and last_sl <= prev_sl:
            major_bias = "BEARISH"
        else:
            major_bias = "NEUTRAL"

        bos_bull = last_close > last_sh and prev_close <= last_sh
        bos_bear = last_close < last_sl and prev_close >= last_sl
        choch_bull = major_bias == "BEARISH" and last_close > last_sh
        choch_bear = major_bias == "BULLISH" and last_close < last_sl

        return {
            "major_bias": major_bias,
            "last_sh": last_sh,
            "last_sl": last_sl,
            "bos_bull": bos_bull,
            "bos_bear": bos_bear,
            "choch_bull": choch_bull,
            "choch_bear": choch_bear,
        }

    @staticmethod
    def order_flow_and_wick_trap(df: pd.DataFrame, smc: Dict[str, Any]) -> Dict[str, float]:
        last = df.iloc[-1]
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        c = float(last["close"])
        v = float(last["volume"])

        vol_ma = float(df["volume"].tail(20).mean())
        vol_ratio = v / (vol_ma + EPS) if vol_ma > 0 else 1.0

        total_range = max(h - l, EPS)
        body = abs(c - o)
        upper_wick = max(0.0, h - max(o, c))
        lower_wick = max(0.0, min(o, c) - l)

        body_ratio = body / total_range
        upper_wick_ratio = upper_wick / total_range
        lower_wick_ratio = lower_wick / total_range
        vol_weighted_body = body_ratio * min(max(vol_ratio, 0.5), 2.5)

        bull_liquidity_sweep = (l < float(smc["last_sl"])) and (c > float(smc["last_sl"])) and (c >= o)
        bear_liquidity_sweep = (h > float(smc["last_sh"])) and (c < float(smc["last_sh"])) and (c <= o)
        bull_wick_rejection = (lower_wick_ratio >= 0.42) and (c >= o) and not (
            lower_wick_ratio > 0.65 and body_ratio < 0.15
        )
        bear_wick_rejection = (upper_wick_ratio >= 0.42) and (c <= o) and not (
            upper_wick_ratio > 0.65 and body_ratio < 0.15
        )

        return {
            "body_ratio": body_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "vol_ratio": vol_ratio,
            "vol_weighted_body": vol_weighted_body,
            "bull_wick_edge": 1.0 if (bull_liquidity_sweep or bull_wick_rejection) else 0.0,
            "bear_wick_edge": 1.0 if (bear_liquidity_sweep or bear_wick_rejection) else 0.0,
        }

    @staticmethod
    def regime_and_shock_filter(
        df: pd.DataFrame,
        asset: AssetSpec,
        mode: str,
        atr_period: int = 14,
        is_weekend: bool = False,
    ) -> Tuple[str, bool, float, float]:
        atr_s = QuantFeatureEngine.atr(df, period=atr_period)
        adx_s = QuantFeatureEngine.adx(df, period=14)
        ema_fast = df["close"].ewm(span=20, adjust=False).mean()
        ema_slow = df["close"].ewm(span=50, adjust=False).mean()

        last_tr = float(df["high"].iloc[-1] - df["low"].iloc[-1])
        med_atr = float(atr_s.rolling(30, min_periods=10).median().iloc[-1])
        shock_ratio = last_tr / (med_atr + EPS)

        if asset.asset_class == "CRYPTO":
            shock_limit = 2.25 if is_weekend else 2.60
            adx_threshold = (22.0 if mode == "SCALP" else 19.0) + (2.0 if is_weekend else 0.0)
        else:
            shock_limit = 2.50
            adx_threshold = 19.0 if mode == "SCALP" else 17.0

        volatility_shock = shock_ratio > shock_limit
        adx_val = float(adx_s.iloc[-1])
        fast_val = float(ema_fast.iloc[-1])
        slow_val = float(ema_slow.iloc[-1])

        if volatility_shock:
            regime = "VOLATILITY_SHOCK"
        elif adx_val >= adx_threshold and fast_val > slow_val:
            regime = "TREND_BULL"
        elif adx_val >= adx_threshold and fast_val < slow_val:
            regime = "TREND_BEAR"
        else:
            regime = "CHOP_RANGE"

        return regime, volatility_shock, adx_val, shock_ratio


class WalkForwardGridOptimizer:
    def __init__(
        self,
        atr_candidates: Optional[List[int]] = None,
        z_candidates: Optional[List[float]] = None,
    ):
        self.atr_candidates = atr_candidates or [10, 14, 20]
        self.z_candidates = z_candidates or [1.5, 2.0, 2.5]

    @staticmethod
    def _simulate_slice(
        closes: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        atr_arr: np.ndarray,
        ema20: np.ndarray,
        ema50: np.ndarray,
        z_arr: np.ndarray,
        z_th: float,
        mode: str,
        asset: AssetSpec,
        start_idx: int,
        end_idx: int,
    ) -> Tuple[List[float], List[float], float]:
        trades_pnl: List[float] = []
        equity = ACCOUNT_BALANCE
        eq_curve = [equity]

        i = max(start_idx, 52)
        while i < end_idx - 3:
            trend_up = ema20[i] > ema50[i] and closes[i] > ema20[i]
            trend_dn = ema20[i] < ema50[i] and closes[i] < ema20[i]
            atr_i = max(atr_arr[i], EPS)

            dyn_cost = max(
                asset.default_spread + asset.slippage_pts,
                (highs[i] - lows[i]) * 0.025 + asset.slippage_pts,
            )

            long_cond = trend_up and (-z_th <= z_arr[i] <= 3.5)
            short_cond = trend_dn and (-3.5 <= z_arr[i] <= z_th)

            if long_cond or short_cond:
                direction = 1.0 if long_cond else -1.0
                entry = closes[i] + (direction * dyn_cost * 0.5)
                sl_dist = atr_i * (1.25 if mode == "SCALP" else 1.80)
                tp_dist = sl_dist * (1.5 if mode == "SCALP" else 2.2)

                exit_px = closes[min(i + 3, end_idx - 1)]
                for j in range(i + 1, min(i + 4, end_idx)):
                    if direction > 0:
                        if lows[j] <= entry - sl_dist:
                            exit_px = entry - sl_dist
                            break
                        if highs[j] >= entry + tp_dist:
                            exit_px = entry + tp_dist
                            break
                    else:
                        if highs[j] >= entry + sl_dist:
                            exit_px = entry + sl_dist
                            break
                        if lows[j] <= entry - tp_dist:
                            exit_px = entry - tp_dist
                            break

                net_pts = direction * (exit_px - entry) - dyn_cost
                risk_units = min(max((equity * (RISK_PERCENT / 100.0)) / (sl_dist + EPS), 0.01), 5.0)
                pnl_usd = net_pts * risk_units * asset.contract_size
                trades_pnl.append(pnl_usd)
                equity = max(equity + pnl_usd, 100.0)
                eq_curve.append(equity)
                i += 3
            else:
                i += 1

        wins = [p for p in trades_pnl if p > 0]
        losses = [abs(p) for p in trades_pnl if p < 0]
        gross_win = float(sum(wins))
        gross_loss = float(sum(losses))
        pf = (gross_win / (gross_loss + EPS)) if gross_loss > 0 else (2.0 if gross_win > 0 else 0.0)
        return trades_pnl, eq_curve, pf

    def optimize_and_backtest(
        self,
        symbol: str,
        mode: str,
        df: pd.DataFrame,
        min_trades: int = 3,
    ) -> BacktestSummary:
        asset = ASSETS[symbol]
        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        n = len(df)
        split_idx = max(int(n * 0.70), 60)

        best_obj = -1e18
        best_result: Optional[BacktestSummary] = None

        for atr_p in self.atr_candidates:
            atr_arr = QuantFeatureEngine.atr(df, period=atr_p).to_numpy(dtype=float)
            ema20 = df["close"].ewm(span=20, adjust=False).mean().to_numpy(dtype=float)
            ema50 = df["close"].ewm(span=50, adjust=False).mean().to_numpy(dtype=float)

            for z_th in self.z_candidates:
                z_arr = QuantFeatureEngine.rolling_zscore(df["close"], window=20).to_numpy(dtype=float)

                oos_pnl, _, oos_pf = self._simulate_slice(
                    closes, highs, lows, atr_arr, ema20, ema50, z_arr, z_th, mode, asset, split_idx, n
                )
                all_pnl, full_curve, full_pf = self._simulate_slice(
                    closes, highs, lows, atr_arr, ema20, ema50, z_arr, z_th, mode, asset, 0, n
                )

                t_count = len(all_pnl)
                oos_count = len(oos_pnl)
                wins = [p for p in all_pnl if p > 0]
                win_rate = (len(wins) / t_count * 100.0) if t_count > 0 else 0.0

                eq_arr = np.array(full_curve, dtype=float)
                peak = np.maximum.accumulate(eq_arr)
                dd_series = ((peak - eq_arr) / (peak + EPS)) * 100.0
                max_dd = float(np.max(dd_series)) if len(dd_series) > 0 else 0.0
                net_ret_pct = ((eq_arr[-1] - ACCOUNT_BALANCE) / max(ACCOUNT_BALANCE, EPS)) * 100.0

                sample_penalty = 0.0 if t_count >= min_trades else (min_trades - t_count) * 2.5
                oos_penalty = 0.0 if (oos_count >= 1 and oos_pf >= 0.90) else 4.0
                dd_penalty = max_dd * 0.95

                obj = (
                    (net_ret_pct * 0.5)
                    + (min(full_pf, 4.0) * 3.0)
                    + (min(oos_pf, 4.0) * 3.5)
                    - dd_penalty
                    - sample_penalty
                    - oos_penalty
                )

                if obj > best_obj:
                    best_obj = obj
                    best_result = BacktestSummary(
                        symbol=symbol,
                        mode=mode,
                        best_atr=atr_p,
                        best_z=z_th,
                        trades=t_count,
                        win_rate=round(win_rate, 2),
                        profit_factor=round(min(full_pf, 99.99), 2),
                        oos_trades=oos_count,
                        oos_pf=round(min(oos_pf, 99.99), 2),
                        max_dd_pct=round(max_dd, 2),
                        net_return_pct=round(net_ret_pct, 2),
                        equity_curve=full_curve,
                    )

        assert best_result is not None
        return best_result


class RiskAndPriceValidator:
    @staticmethod
    def compute_lot(
        balance: float,
        risk_pct: float,
        entry: float,
        sl: float,
        asset: AssetSpec,
    ) -> float:
        stop_dist = abs(entry - sl)
        if stop_dist <= EPS or balance <= 0.0 or risk_pct <= 0.0:
            return asset.min_lot
        risk_amount = balance * (risk_pct / 100.0)
        value_per_lot = max(stop_dist * asset.contract_size, EPS)
        raw_lot = risk_amount / value_per_lot
        step = max(asset.lot_step, 0.01)
        stepped_lot = math.floor(raw_lot / step) * step
        cap_lot = max(asset.min_lot, asset.max_lot)
        return round(max(asset.min_lot, min(stepped_lot, cap_lot)), 2)

    @staticmethod
    def validate_price_order(action: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float) -> bool:
        if action == "BUY":
            return (sl < entry) and (entry < tp1) and (tp1 < tp2) and (tp2 < tp3)
        if action == "SELL":
            return (sl > entry) and (entry > tp1) and (tp1 > tp2) and (tp2 > tp3)
        return False


class HyperionMultiAssetEngine:
    def __init__(self, balance: float = ACCOUNT_BALANCE, risk_pct: float = RISK_PERCENT):
        self.balance = balance
        self.risk_pct = risk_pct

    @staticmethod
    def _passes_walkforward_gate(opt: BacktestSummary) -> bool:
        if opt.trades < 3 or opt.max_dd_pct > 30.0:
            return False
        standard_ok = opt.profit_factor >= 1.05 and opt.oos_pf >= 0.90
        strong_oos_recovery_ok = opt.profit_factor >= 0.98 and opt.oos_pf >= 1.25 and opt.oos_trades >= 6
        return standard_ok or strong_oos_recovery_ok

    def evaluate(
        self,
        symbol: str,
        mode_spec: ModeSpec,
        opt_summary: BacktestSummary,
        data_bundle: Dict[str, Dict[str, Optional[pd.DataFrame]]],
        now_utc: datetime,
    ) -> SignalDecision:
        asset = ASSETS[symbol]
        now_utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        is_weekend = now_utc.weekday() >= 5

        df_trig = data_bundle.get(symbol, {}).get(mode_spec.trigger_tf)
        df_conf = data_bundle.get(symbol, {}).get(mode_spec.confirm_tf)
        df_anch = data_bundle.get(symbol, {}).get(mode_spec.anchor_tf)

        def no_signal(
            reasons: List[str],
            regime: str = "FILTERED",
            adx_val: float = 0.0,
            score: float = 0.0,
        ) -> SignalDecision:
            return SignalDecision(
                symbol=symbol,
                mode=mode_spec.name,
                action="NO_SIGNAL",
                entry=0.0,
                sl=0.0,
                tp1=0.0,
                tp2=0.0,
                tp3=0.0,
                zone_low=0.0,
                zone_high=0.0,
                lot=0.0,
                score=round(score, 1),
                regime=regime,
                adx_val=round(adx_val, 1),
                oos_pf=opt_summary.oos_pf,
                trail_desc=mode_spec.trail_rule,
                reasons=reasons,
                timestamp_utc=now_utc_str,
            )

        session_ok, session_tag = SessionAndRegimeFilter.session_status(asset, now_utc)
        if not session_ok:
            return no_signal([f"SESSION_FILTER({session_tag})"], regime="SESSION_CLOSED")

        if df_trig is None or df_conf is None or df_anch is None:
            return no_signal(["DATA_MTF_INCOMPLETE"])

        if (
            len(df_trig) < MIN_BARS_REQUIRED.get(mode_spec.trigger_tf, 70)
            or len(df_conf) < MIN_BARS_REQUIRED.get(mode_spec.confirm_tf, 70)
            or len(df_anch) < MIN_BARS_REQUIRED.get(mode_spec.anchor_tf, 65)
        ):
            return no_signal(["INSUFFICIENT_BARS"])

        if not self._passes_walkforward_gate(opt_summary):
            return no_signal(
                [
                    f"OPT_GATE_FAIL(T={opt_summary.trades},PF={opt_summary.profit_factor},OOS_PF={opt_summary.oos_pf},DD={opt_summary.max_dd_pct}%)"
                ]
            )

        regime, shock, adx_val, shock_ratio = QuantFeatureEngine.regime_and_shock_filter(
            df_trig, asset=asset, mode=mode_spec.name, atr_period=opt_summary.best_atr, is_weekend=is_weekend
        )
        if shock or regime == "VOLATILITY_SHOCK":
            return no_signal([f"VOLATILITY_SHOCK({shock_ratio:.2f}xATR)"], regime="VOLATILITY_SHOCK", adx_val=adx_val)

        if regime == "CHOP_RANGE":
            return no_signal([f"CHOP_ANTI_WHIPSAW(ADX={adx_val:.1f})"], regime="CHOP_RANGE", adx_val=adx_val)

        smc_trig = QuantFeatureEngine.detect_smc_structure(df_trig, lookback=4)
        smc_conf = QuantFeatureEngine.detect_smc_structure(df_conf, lookback=4)
        of_trig = QuantFeatureEngine.order_flow_and_wick_trap(df_trig, smc_trig)
        z_series = QuantFeatureEngine.rolling_zscore(df_trig["close"], window=20)
        z_now = float(z_series.iloc[-1])

        anch_ema20 = float(df_anch["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        anch_ema50 = float(df_anch["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        conf_ema20 = float(df_conf["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        conf_ema50 = float(df_conf["close"].ewm(span=50, adjust=False).mean().iloc[-1])

        anch_bull = float(df_anch["close"].iloc[-1]) > anch_ema20 and anch_ema20 >= anch_ema50
        anch_bear = float(df_anch["close"].iloc[-1]) < anch_ema20 and anch_ema20 <= anch_ema50
        conf_bull = float(df_conf["close"].iloc[-1]) > conf_ema20 and conf_ema20 >= conf_ema50
        conf_bear = float(df_conf["close"].iloc[-1]) < conf_ema20 and conf_ema20 <= conf_ema50

        atr_series = QuantFeatureEngine.atr(df_trig, period=opt_summary.best_atr)
        atr_now = float(atr_series.iloc[-1])
        if atr_now <= EPS:
            return no_signal(["ATR_ZERO"], regime=regime, adx_val=adx_val)

        close_now = float(df_trig["close"].iloc[-1])
        open_now = float(df_trig["open"].iloc[-1])
        reasons: List[str] = []
        confluence = 0
        score = 20.0

        buy_bias_ok = regime == "TREND_BULL" and conf_bull and anch_bull
        sell_bias_ok = regime == "TREND_BEAR" and conf_bear and anch_bear

        if buy_bias_ok and not sell_bias_ok:
            confluence += 1
            score += 20.0
            reasons.append("MTF_BULL")

            if smc_trig["bos_bull"] or smc_trig["choch_bull"] or smc_conf["major_bias"] == "BULLISH":
                confluence += 1
                score += 16.0
                reasons.append("SMC_BULL")

            of_ok = (of_trig["vol_weighted_body"] >= 0.25 and close_now >= open_now) or (
                of_trig["bull_wick_edge"] > 0.5 and close_now >= open_now
            )
            if of_ok:
                confluence += 1
                score += 15.0
                reasons.append("OF_BULL")

            if z_now <= -opt_summary.best_z or (-1.1 <= z_now <= 1.2 and adx_val >= 22.0):
                confluence += 1
                score += 15.0
                reasons.append(f"Z_OK({z_now:+.2f})")

            if of_trig["bull_wick_edge"] > 0.5:
                score += 8.0
                reasons.append("WICK_SWEEP_BULL")
            if opt_summary.oos_pf >= 1.20:
                score += 6.0

            if confluence < mode_spec.min_confluence or score < mode_spec.min_score:
                return no_signal(
                    [f"NEED_C4_S{mode_spec.min_score:.0f}(C={confluence}/4,S={score:.1f},ADX={adx_val:.1f})"],
                    regime=regime,
                    adx_val=adx_val,
                    score=score,
                )

            bar_range = float(df_trig["high"].iloc[-1] - df_trig["low"].iloc[-1])
            eff_cost = max(asset.default_spread + asset.slippage_pts, bar_range * 0.02 + asset.slippage_pts)
            entry = round(close_now + (eff_cost * 0.5), 2)
            sl_dist = atr_now * mode_spec.sl_atr_mult
            sl = round(entry - sl_dist, 2)
            r_unit = max(entry - sl, EPS)
            tp1 = round(entry + (r_unit * mode_spec.tp1_r), 2)
            tp2 = round(entry + (r_unit * mode_spec.tp2_r), 2)
            tp3 = round(entry + (r_unit * mode_spec.tp3_r), 2)

            if not RiskAndPriceValidator.validate_price_order("BUY", entry, sl, tp1, tp2, tp3):
                return no_signal(["INVALID_PRICE_ORDER_BUY"], regime=regime, adx_val=adx_val, score=score)

            zone_half = max(atr_now * mode_spec.entry_zone_atr_mult, eff_cost)
            zone_low = round(entry - zone_half, 2)
            zone_high = round(entry + zone_half, 2)
            lot = RiskAndPriceValidator.compute_lot(self.balance, self.risk_pct, entry, sl, asset)

            return SignalDecision(
                symbol=symbol,
                mode=mode_spec.name,
                action="BUY",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                zone_low=zone_low,
                zone_high=zone_high,
                lot=lot,
                score=round(min(score, 99.0), 1),
                regime=regime,
                adx_val=round(adx_val, 1),
                oos_pf=opt_summary.oos_pf,
                trail_desc=mode_spec.trail_rule,
                reasons=reasons,
                timestamp_utc=now_utc_str,
            )

        if sell_bias_ok and not buy_bias_ok:
            confluence += 1
            score += 20.0
            reasons.append("MTF_BEAR")

            if smc_trig["bos_bear"] or smc_trig["choch_bear"] or smc_conf["major_bias"] == "BEARISH":
                confluence += 1
                score += 16.0
                reasons.append("SMC_BEAR")

            of_ok = (of_trig["vol_weighted_body"] >= 0.25 and close_now <= open_now) or (
                of_trig["bear_wick_edge"] > 0.5 and close_now <= open_now
            )
            if of_ok:
                confluence += 1
                score += 15.0
                reasons.append("OF_BEAR")

            if z_now >= opt_summary.best_z or (-1.2 <= z_now <= 1.1 and adx_val >= 22.0):
                confluence += 1
                score += 15.0
                reasons.append(f"Z_OK({z_now:+.2f})")

            if of_trig["bear_wick_edge"] > 0.5:
                score += 8.0
                reasons.append("WICK_SWEEP_BEAR")
            if opt_summary.oos_pf >= 1.20:
                score += 6.0

            if confluence < mode_spec.min_confluence or score < mode_spec.min_score:
                return no_signal(
                    [f"NEED_C4_S{mode_spec.min_score:.0f}(C={confluence}/4,S={score:.1f},ADX={adx_val:.1f})"],
                    regime=regime,
                    adx_val=adx_val,
                    score=score,
                )

            bar_range = float(df_trig["high"].iloc[-1] - df_trig["low"].iloc[-1])
            eff_cost = max(asset.default_spread + asset.slippage_pts, bar_range * 0.02 + asset.slippage_pts)
            entry = round(close_now - (eff_cost * 0.5), 2)
            sl_dist = atr_now * mode_spec.sl_atr_mult
            sl = round(entry + sl_dist, 2)
            r_unit = max(sl - entry, EPS)
            tp1 = round(entry - (r_unit * mode_spec.tp1_r), 2)
            tp2 = round(entry - (r_unit * mode_spec.tp2_r), 2)
            tp3 = round(entry - (r_unit * mode_spec.tp3_r), 2)

            if not RiskAndPriceValidator.validate_price_order("SELL", entry, sl, tp1, tp2, tp3):
                return no_signal(["INVALID_PRICE_ORDER_SELL"], regime=regime, adx_val=adx_val, score=score)

            zone_half = max(atr_now * mode_spec.entry_zone_atr_mult, eff_cost)
            zone_low = round(entry - zone_half, 2)
            zone_high = round(entry + zone_half, 2)
            lot = RiskAndPriceValidator.compute_lot(self.balance, self.risk_pct, entry, sl, asset)

            return SignalDecision(
                symbol=symbol,
                mode=mode_spec.name,
                action="SELL",
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                zone_low=zone_low,
                zone_high=zone_high,
                lot=lot,
                score=round(min(score, 99.0), 1),
                regime=regime,
                adx_val=round(adx_val, 1),
                oos_pf=opt_summary.oos_pf,
                trail_desc=mode_spec.trail_rule,
                reasons=reasons,
                timestamp_utc=now_utc_str,
            )

        return no_signal(
            [f"MTF_CONFLICT(ADX={adx_val:.1f},Z={z_now:+.2f})"],
            regime=regime,
            adx_val=adx_val,
            score=score,
        )


class StateRepository:
    def __init__(self, db_path: str = SQLITE_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sig_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry REAL NOT NULL,
                    sl REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    zone_low REAL NOT NULL DEFAULT 0.0,
                    zone_high REAL NOT NULL DEFAULT 0.0,
                    lot REAL NOT NULL,
                    score REAL NOT NULL,
                    regime TEXT NOT NULL,
                    dispatched INTEGER NOT NULL DEFAULT 1,
                    created_at_epoch REAL NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT v FROM bot_state WHERE k = ?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bot_state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
            conn.commit()

    def check_cooldown_and_flip(
        self,
        symbol: str,
        mode: str,
        action: str,
        entry: float,
        cooldown_sec: int,
        flip_guard_sec: int,
        now_epoch: float,
    ) -> Optional[str]:
        with self._connect() as conn:
            last_same = conn.execute(
                """
                SELECT created_at_epoch, entry FROM signals
                WHERE symbol = ? AND mode = ? AND action = ? AND dispatched = 1
                ORDER BY created_at_epoch DESC LIMIT 1
                """,
                (symbol, mode, action),
            ).fetchone()
            if last_same:
                elapsed_same = now_epoch - float(last_same[0])
                prev_entry = float(last_same[1])
                price_shift_pct = abs(entry - prev_entry) / max(abs(prev_entry), EPS) * 100.0
                if elapsed_same < float(cooldown_sec) and price_shift_pct < 0.8:
                    return "COOLDOWN_ACTIVE"
                if elapsed_same < (float(cooldown_sec) * 0.5):
                    return "COOLDOWN_MIN_WINDOW"

            last_any = conn.execute(
                """
                SELECT created_at_epoch, action FROM signals
                WHERE symbol = ? AND mode = ? AND dispatched = 1
                ORDER BY created_at_epoch DESC LIMIT 1
                """,
                (symbol, mode),
            ).fetchone()
            if last_any:
                prev_action = str(last_any[1])
                elapsed_any = now_epoch - float(last_any[0])
                if prev_action != action and elapsed_any < float(flip_guard_sec):
                    return "ANTI_FLIP_FLOP_ACTIVE"

        return None

    def save_signal(self, sig: SignalDecision, now_epoch: float, dispatched: bool = True) -> None:
        raw_key = f"{sig.symbol}|{sig.mode}|{sig.action}|{sig.entry}|{sig.sl}|{sig.timestamp_utc}"
        sig_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signals(
                    sig_hash, symbol, mode, action, entry, sl, tp1, tp2, tp3,
                    zone_low, zone_high, lot, score, regime, dispatched, created_at_epoch, created_at_utc
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig_hash,
                    sig.symbol,
                    sig.mode,
                    sig.action,
                    sig.entry,
                    sig.sl,
                    sig.tp1,
                    sig.tp2,
                    sig.tp3,
                    sig.zone_low,
                    sig.zone_high,
                    sig.lot,
                    sig.score,
                    sig.regime,
                    1 if dispatched else 0,
                    now_epoch,
                    sig.timestamp_utc,
                ),
            )
            conn.commit()

    def recent_signals(self, limit: int = 8) -> List[Tuple[Any, ...]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, mode, action, entry, sl, tp1, lot, score, created_at_utc
                FROM signals ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return list(rows)


def generate_backtest_chart(
    summaries: List[BacktestSummary], out_path: str = BACKTEST_CHART_PATH
) -> str:
    fig, axes = plt.subplots(4, 2, figsize=(14, 14))
    fig.patch.set_facecolor("#0f172a")
    ax_list = axes.flatten()

    for idx, ax in enumerate(ax_list):
        ax.set_facecolor("#1e293b")
        ax.tick_params(colors="#e2e8f0", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#334155")

        if idx < len(summaries):
            s = summaries[idx]
            curve = s.equity_curve if s.equity_curve else [ACCOUNT_BALANCE]
            ax.plot(curve, color="#38bdf8", linewidth=1.6)
            ax.set_title(
                f"{s.symbol} {s.mode} (ATR={s.best_atr}, Z={s.best_z}) | PF={s.profit_factor} | OOS={s.oos_pf} | DD={s.max_dd_pct}%",
                color="#f8fafc",
                fontsize=8.5,
                fontweight="bold",
            )
            ax.set_xlabel("Trade Step", color="#94a3b8", fontsize=8)
            ax.set_ylabel("Equity ($)", color="#94a3b8", fontsize=8)
            ax.grid(True, linestyle="--", alpha=0.2, color="#94a3b8")
        else:
            ax.axis("off")

    fig.suptitle(
        "APEX-QUANT-4X — Walk-Forward Backtest (Spread + Slippage)",
        color="#f8fafc",
        fontsize=12,
        fontweight="bold",
    )
    plt.tight_layout(rect=[0, 0.02, 1, 0.96])
    plt.savefig(out_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


class AlertNotifier:
    def __init__(self, http_client: ResilientHttpClient):
        self.http = http_client
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    @staticmethod
    def format_signal_message(sig: SignalDecision) -> str:
        hh_mm = sig.timestamp_utc[11:16] if len(sig.timestamp_utc) >= 16 else sig.timestamp_utc
        return (
            f"⚡ <b>{sig.symbol} | {sig.mode} | {sig.action}</b>\n"
            f"🎯 Entry: <code>{sig.entry:.2f}</code> | 🛑 SL: <code>{sig.sl:.2f}</code>\n"
            f"✅ TP1: <code>{sig.tp1:.2f}</code> | TP2: <code>{sig.tp2:.2f}</code> | TP3: <code>{sig.tp3:.2f}</code>\n"
            f"📐 Lot: <code>{sig.lot:.2f}</code> | Skor: <code>{sig.score:.0f}/100</code> | Regime: <code>{sig.regime}</code>\n"
            f"🔄 {sig.trail_desc} | 🕒 <code>{hh_mm} UTC</code>\n"
            f"📊 Zona: <code>{sig.zone_low:.2f}-{sig.zone_high:.2f}</code> | ADX: <code>{sig.adx_val:.1f}</code> | OOS PF: <code>{sig.oos_pf:.2f}</code>"
        )

    def send_text_telegram(self, text: str, chat_id: Optional[str] = None) -> bool:
        target_chat = (chat_id or self.tg_chat_id).strip()
        if not self.tg_token or not target_chat:
            logger.info("Telegram token/chat_id belum diset; pesan dilewati.")
            return False
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        res = self.http.post_json(url, payload, service_key="telegram_send")
        return bool(res and res.get("ok"))

    def send_signal(self, sig: SignalDecision) -> bool:
        msg = self.format_signal_message(sig)
        tg_ok = self.send_text_telegram(msg)
        dc_ok = False
        if self.discord_url:
            plain_msg = (
                msg.replace("<b>", "**")
                .replace("</b>", "**")
                .replace("<code>", "`")
                .replace("</code>", "`")
            )
            dc_res = self.http.post_json(self.discord_url, {"content": plain_msg}, service_key="discord_webhook")
            dc_ok = bool(dc_res)
        return tg_ok or dc_ok

    def send_error(self, context_msg: str) -> None:
        hh_mm = datetime.now(timezone.utc).strftime("%H:%M UTC")
        err_text = f"🚨 <b>APEX-QUANT-4X ERROR</b> | 🕒 <code>{hh_mm}</code>\n<code>{context_msg[:900]}</code>"
        self.send_text_telegram(err_text)


class TelegramCommandHandler:
    def __init__(
        self, http_client: ResilientHttpClient, repo: StateRepository, notifier: AlertNotifier
    ):
        self.http = http_client
        self.repo = repo
        self.notifier = notifier
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    def poll_and_handle(self, latest_decisions: List[SignalDecision]) -> int:
        if not self.token or not self.allowed_chat_id:
            return 0
        last_update_raw = self.repo.get_meta("tg_last_update_id", "0")
        try:
            offset_val = int(last_update_raw) + 1
        except ValueError:
            offset_val = 0

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        data = self.http.get_json(
            url, params={"offset": offset_val, "timeout": 2}, service_key="telegram_poll"
        )
        if not isinstance(data, dict) or not data.get("ok"):
            return 0

        updates = data.get("result") or []
        handled = 0
        max_upd = int(last_update_raw) if last_update_raw.isdigit() else 0

        for upd in updates:
            if not isinstance(upd, dict):
                continue
            upd_id = int(upd.get("update_id", 0))
            if upd_id > max_upd:
                max_upd = upd_id

            message = upd.get("message") or upd.get("edited_message") or {}
            chat = message.get("chat") or {}
            sender_chat_id = str(chat.get("id", "")).strip()
            text = str(message.get("text", "")).strip()

            if not text.startswith("/check"):
                continue

            if sender_chat_id != self.allowed_chat_id:
                logger.warning("Akses /check ditolak untuk chat_id=%s", sender_chat_id)
                continue

            parts = [p.strip().lower() for p in text.split()[1:]]
            sym_filter = (
                "XAUUSD"
                if ("xau" in parts or "gold" in parts or "xauusd" in parts)
                else (
                    "NAS100"
                    if ("nas" in parts or "ndx" in parts or "nas100" in parts)
                    else ("BTCUSD" if "btc" in parts else ("ETHUSD" if "eth" in parts else None))
                )
            )
            mode_filter = "SCALP" if "scalp" in parts else ("SWING" if "swing" in parts else None)

            if "history" in parts:
                hist = self.repo.recent_signals(limit=6)
                if not hist:
                    reply = "ℹ️ Belum ada sinyal tersimpan di SQLite."
                else:
                    lines = ["📜 <b>RIWAYAT SINYAL (SQLITE)</b>"]
                    for r in hist:
                        lines.append(
                            f"• <code>{r[0]} {r[1]} {r[2]}</code> @ <code>{r[3]:.2f}</code> (Lot {r[6]:.2f}, S {r[7]:.0f})"
                        )
                    reply = "\n".join(lines)
                self.notifier.send_text_telegram(reply, chat_id=sender_chat_id)
                handled += 1
                continue

            summary_lines = ["🔎 <b>STATUS 4 INSTRUMEN (/CHECK)</b>"]
            for sym in ["XAUUSD", "NAS100", "BTCUSD", "ETHUSD"]:
                if sym_filter and sym != sym_filter:
                    continue
                items = [
                    d
                    for d in latest_decisions
                    if d.symbol == sym and (mode_filter is None or d.mode == mode_filter)
                ]
                for d in items:
                    if d.action == "NO_SIGNAL":
                        r_short = ",".join(d.reasons[:2])
                        summary_lines.append(
                            f"• <code>{d.symbol} {d.mode}</code>: NO_SIGNAL | {d.regime} | <code>{r_short}</code>"
                        )
                    else:
                        summary_lines.append(
                            f"• <code>{d.symbol} {d.mode} {d.action}</code> @ <code>{d.entry:.2f}</code> "
                            f"SL <code>{d.sl:.2f}</code> TP1 <code>{d.tp1:.2f}</code> Lot <code>{d.lot:.2f}</code>"
                        )
            self.notifier.send_text_telegram("\n".join(summary_lines), chat_id=sender_chat_id)
            handled += 1

        if max_upd > 0:
            self.repo.set_meta("tg_last_update_id", str(max_upd))
        return handled


def forensic_audit_log(
    now_utc_str: str,
    sources_used: Dict[str, Dict[str, str]],
    summaries: List[BacktestSummary],
    decisions: List[SignalDecision],
    dispatched_count: int,
    failed_dispatch_count: int,
) -> None:
    sep = "=" * 106
    dash = "-" * 106
    print(sep)
    print(f"APEX-QUANT-4X — FORENSIC AUDIT LOG | {now_utc_str}")
    print(sep)
    for sym in ["XAUUSD", "NAS100", "BTCUSD", "ETHUSD"]:
        src_map = sources_used.get(sym, {})
        src_str = ", ".join(f"{k}:{v}" for k, v in src_map.items())
        print(f"[DATA FEED] {sym:<6} -> {src_str}")
    print(dash)
    for s in summaries:
        print(
            f"[WF/BT    ] {s.symbol:<6} {s.mode:<5} | ATR={s.best_atr:<2} Z={s.best_z:<3} | "
            f"T={s.trades:<3} WR={s.win_rate:>6.2f}% PF={s.profit_factor:>5.2f} "
            f"OOS_PF={s.oos_pf:>5.2f}({s.oos_trades}T) DD={s.max_dd_pct:>5.2f}% Ret={s.net_return_pct:>+7.2f}%"
        )
    print(dash)
    for d in decisions:
        reasons_txt = ";".join(d.reasons) if d.reasons else "CONFLUENCE_MET"
        print(
            f"[SIGNAL   ] {d.symbol:<6} {d.mode:<5} -> {d.action:<9} | "
            f"Entry={d.entry:>9.2f} SL={d.sl:>9.2f} TP1={d.tp1:>9.2f} Lot={d.lot:>5.2f} "
            f"Skor={d.score:>5.1f} Regime={d.regime:<15} | {reasons_txt}"
        )
    print(dash)
    print(f"[DISPATCH ] Sinyal Terkirim={dispatched_count} | Gagal Kirim={failed_dispatch_count}")
    print(sep)


def run_pipeline() -> None:
    now_dt = datetime.now(timezone.utc)
    now_epoch = now_dt.timestamp()
    now_utc_str = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    breaker = CircuitBreaker(max_failures=5, reset_after_sec=300)
    http_client = ResilientHttpClient(breaker=breaker, timeout=HTTP_TIMEOUT_SEC)
    provider = MarketDataProvider(http_client=http_client, breaker=breaker)
    optimizer = WalkForwardGridOptimizer(atr_candidates=[10, 14, 20], z_candidates=[1.5, 2.0, 2.5])
    engine = HyperionMultiAssetEngine(balance=ACCOUNT_BALANCE, risk_pct=RISK_PERCENT)
    repo = StateRepository(db_path=SQLITE_DB_PATH)
    notifier = AlertNotifier(http_client=http_client)
    cmd_handler = TelegramCommandHandler(http_client=http_client, repo=repo, notifier=notifier)

    symbols_order = ["XAUUSD", "NAS100", "BTCUSD", "ETHUSD"]
    needed_tfs = {"5m", "15m", "1h", "4h", "1d"}
    data_bundle: Dict[str, Dict[str, Optional[pd.DataFrame]]] = {s: {} for s in symbols_order}
    sources_used: Dict[str, Dict[str, str]] = {s: {} for s in symbols_order}

    for sym in symbols_order:
        for tf in sorted(needed_tfs, key=lambda x: TF_SECONDS[x]):
            df, src = provider.fetch(sym, tf, limit=300)
            data_bundle[sym][tf] = df
            sources_used[sym][tf] = src

    summaries: List[BacktestSummary] = []
    summary_map: Dict[Tuple[str, str], BacktestSummary] = {}

    for sym in symbols_order:
        for mode_name, mode_spec in MODES.items():
            bt_df = data_bundle[sym].get(mode_spec.confirm_tf)
            if bt_df is None or len(bt_df) < MIN_BARS_REQUIRED.get(mode_spec.confirm_tf, 70):
                bt_df = data_bundle[sym].get(mode_spec.trigger_tf)
            if bt_df is not None and len(bt_df) >= 65:
                summ = optimizer.optimize_and_backtest(symbol=sym, mode=mode_name, df=bt_df, min_trades=3)
            else:
                summ = BacktestSummary(
                    symbol=sym,
                    mode=mode_name,
                    best_atr=14,
                    best_z=2.0,
                    trades=0,
                    win_rate=0.0,
                    profit_factor=0.0,
                    oos_trades=0,
                    oos_pf=0.0,
                    max_dd_pct=0.0,
                    net_return_pct=0.0,
                    equity_curve=[ACCOUNT_BALANCE],
                )
            summaries.append(summ)
            summary_map[(sym, mode_name)] = summ

    generate_backtest_chart(summaries, out_path=BACKTEST_CHART_PATH)

    decisions: List[SignalDecision] = []
    dispatched = 0
    failed_dispatch = 0

    for sym in symbols_order:
        for mode_name, mode_spec in MODES.items():
            dec = engine.evaluate(
                symbol=sym,
                mode_spec=mode_spec,
                opt_summary=summary_map[(sym, mode_name)],
                data_bundle=data_bundle,
                now_utc=now_dt,
            )
            decisions.append(dec)

            if dec.action in ("BUY", "SELL"):
                block_reason = repo.check_cooldown_and_flip(
                    symbol=dec.symbol,
                    mode=dec.mode,
                    action=dec.action,
                    entry=dec.entry,
                    cooldown_sec=mode_spec.cooldown_sec,
                    flip_guard_sec=mode_spec.flip_guard_sec,
                    now_epoch=now_epoch,
                )
                if block_reason is None:
                    sent_ok = notifier.send_signal(dec)
                    repo.save_signal(dec, now_epoch=now_epoch, dispatched=sent_ok)
                    if sent_ok:
                        dispatched += 1
                    else:
                        failed_dispatch += 1
                        dec.reasons.append("DISPATCH_FAILED_NETWORK")
                else:
                    dec.action = "NO_SIGNAL"
                    dec.reasons.append(block_reason)

    cmd_handler.poll_and_handle(decisions)
    forensic_audit_log(
        now_utc_str=now_utc_str,
        sources_used=sources_used,
        summaries=summaries,
        decisions=decisions,
        dispatched_count=dispatched,
        failed_dispatch_count=failed_dispatch,
    )


def main() -> None:
    notifier: Optional[AlertNotifier] = None
    try:
        breaker = CircuitBreaker(max_failures=5, reset_after_sec=300)
        notifier = AlertNotifier(http_client=ResilientHttpClient(breaker=breaker, timeout=HTTP_TIMEOUT_SEC))
        run_pipeline()
    except Exception as exc:
        logger.exception("Kesalahan fatal pada eksekusi APEX-QUANT-4X: %s", exc)
        if notifier is not None:
            notifier.send_error(f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
