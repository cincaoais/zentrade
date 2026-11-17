"""Built-in moomoo data plugin: OpenD connection, symbols, availability.

The plugin never talks to OpenD at import time or during ``availability()``.
Datasets are declared in ``plugin.yaml``; this module provides the provider
class plus the small helpers the rest of the plugin shares.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import polars as pl

from app import credentials
from app.config import settings
from app.data_providers.base import ProviderCapabilities
from app.market_clock import us_clock

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111
US_PREFIX = "US."
# Re-emit this many calendar days before the requested start so a symbol that
# returned no bars one evening (halt, OpenD hiccup) is repaired on a later run.
REPAIR_WINDOW_DAYS = 14
# Canonical daily row contract shared with kline_sync (no quote_ts on purpose).
DAILY_SCHEMA = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}

# QFQ closes carry 9 decimals, so the raw/QFQ level ratio has no cents-rounding
# noise: derive events at that precision instead of Alpaca's cent bound.
MOOMOO_QFQ_TICK = 5e-10
# Floor for the same bound: 9-decimal QFQ rounding leaves ~1e-9 relative noise.
MOOMOO_MIN_REL = 1e-8

# US tickers: letters, optionally one class suffix (BRK.B). This drops numeric /
# vendor-internal codes such as US.1061 and US.2618996D.
US_CODE_RE = re.compile(r"^US\.[A-Z]+(?:\.[A-Z])?$")
# moomoo exchange_type → stored exchange; everything else (PINK, OTC, ...) is out.
US_EXCHANGES = {"US_NYSE": "NYSE", "US_NASDAQ": "NASDAQ"}
# moomoo reports an unknown listing date as the Unix epoch.
UNKNOWN_LISTING_DATE = dt.date(1970, 1, 1)

# Realtime snapshots: OpenD allows 60 calls / 30 s and 400 codes per call. We pace
# at 50 to leave margin: pacing at exactly 60 hit "high frequency" on 2026-11-17
# (client and server windows do not line up).
SNAPSHOT_RPM = 50
SNAPSHOT_WINDOW_S = 30.0
# get_rehab: same 60 / 30 s server limit, same margin.
REHAB_CALLS_PER_WINDOW = 50
REHAB_WINDOW_S = 30.0
# After a "high frequency" reply, wait one full window and retry once.
RATE_LIMIT_BACKOFF_S = 30.0
SNAPSHOT_CHUNK = 400
UNIVERSE_CACHE_SECONDS = 3600.0

def _is_rate_limited(data: object) -> bool:
    """True for OpenD's rate-limit reply ("... high frequency ..." / "频率")."""
    text = str(data).lower()
    return "frequen" in text or "频率" in text


# Injected in tests (and usable to share one connection policy at process level).
_CTX_FACTORY: Callable[[str, int], Any] | None = None


def opend_address() -> tuple[str, int]:
    """Parse ``ZENTRADE_MOOMOO_OPEND`` (``host:port``); empty → 127.0.0.1:11111."""
    raw = credentials.get(credentials.MOOMOO_OPEND).strip()
    if not raw:
        return DEFAULT_HOST, DEFAULT_PORT
    host, sep, port_text = raw.rpartition(":")
    host = host.strip()
    port_text = port_text.strip()
    if not sep or not host or not port_text:
        raise ValueError(f"ZENTRADE_MOOMOO_OPEND 格式应为 host:port, 得到: {raw!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"ZENTRADE_MOOMOO_OPEND 端口不是整数: {raw!r}") from exc
    if not 0 < port < 65536:
        raise ValueError(f"ZENTRADE_MOOMOO_OPEND 端口超出范围: {raw!r}")
    return host, port


def to_moomoo(symbol: str) -> str:
    """``BRK.B`` → ``US.BRK.B`` (idempotent for already-prefixed codes)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol 不能为空")
    return sym if sym.startswith(US_PREFIX) else f"{US_PREFIX}{sym}"


def from_moomoo(code: str) -> str:
    """``US.BRK.B`` → ``BRK.B``; raises for every non-US code."""
    raw = (code or "").strip()
    if not raw.startswith(US_PREFIX) or len(raw) == len(US_PREFIX):
        raise ValueError(f"不是美股 moomoo 代码: {code!r}")
    return raw[len(US_PREFIX):]


def availability() -> tuple[bool, str]:
    """Installable + address parses. Deliberately never opens a connection."""
    try:
        import moomoo  # noqa: F401
    except Exception as e:
        return False, f"缺少 moomoo-api 依赖: {e}"
    try:
        opend_address()
    except ValueError as e:
        return False, str(e)
    return True, "ok"


def set_ctx_factory(factory: Callable[[str, int], Any] | None) -> None:
    """Inject a quote-context factory (tests). ``None`` restores the SDK default."""
    global _CTX_FACTORY
    _CTX_FACTORY = factory


OPEND_CONNECT_TIMEOUT_S = 1.0


def opend_reachable(host: str, port: int, timeout: float = OPEND_CONNECT_TIMEOUT_S) -> bool:
    """Plain TCP probe of OpenD. The SDK retries a closed port for minutes; this fails fast."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_ctx_factory(host: str, port: int) -> Any:
    import moomoo

    if not opend_reachable(host, port):
        raise ConnectionError(f"moomoo OpenD not reachable at {host}:{port}")
    return moomoo.OpenQuoteContext(host=host, port=port)


@contextlib.contextmanager
def open_ctx() -> Iterator[Any]:
    """Yield a quote context and always close it, even when the body raises."""
    host, port = opend_address()
    ctx = (_CTX_FACTORY or _default_ctx_factory)(host, port)
    try:
        yield ctx
    finally:
        close = getattr(ctx, "close", None)
        if callable(close):
            close()


def _unpack_ret(raw: Any) -> tuple[Any, Any]:
    """SDK methods return ``(ret, data)``; fakes may return ``data`` directly."""
    if isinstance(raw, tuple) and len(raw) == 2:
        return raw
    return 0, raw


def _pandas_or_polars(frame: Any) -> pl.DataFrame:
    """``get_cur_kline`` returns pandas with the real SDK, polars in fakes."""
    if isinstance(frame, pl.DataFrame):
        return frame
    if frame is None:
        return pl.DataFrame()
    if hasattr(frame, "reset_index"):  # pandas
        return pl.from_pandas(frame.reset_index(drop=True))
    return pl.DataFrame(frame)


def _wall_date(value: dt.datetime | dt.date) -> dt.date:
    """Naive datetimes are US/Eastern wall clock; take the calendar date."""
    return value.date() if isinstance(value, dt.datetime) else value


def _canonical_daily(symbol: str, frame: Any) -> pl.DataFrame:
    """One code's kline frame → the canonical 8 daily columns (before windowing)."""
    df = _pandas_or_polars(frame)
    if df.is_empty() or "time_key" not in df.columns:
        return pl.DataFrame(schema=DAILY_SCHEMA)

    def column(name: str) -> pl.Expr:
        return pl.col(name) if name in df.columns else pl.lit(None, dtype=pl.Float64)

    exprs = [
        pl.lit(symbol).alias("symbol"),
        pl.col("time_key")
        .cast(pl.Utf8)
        .str.slice(0, 10)
        .str.to_date(strict=False)
        .alias("date"),
    ]
    for name in ("open", "high", "low", "close", "volume"):
        exprs.append(column(name).cast(pl.Float64).alias(name))
    exprs.append(column("turnover").cast(pl.Float64).alias("amount"))
    return df.select(exprs)


def _event_bars(frame: Any) -> list[dict[str, Any]]:
    """Frame → the ``{"t", "c", "v"}`` bar dicts ``events_for_symbols`` wants."""
    df = _pandas_or_polars(frame)
    if df.is_empty() or "time_key" not in df.columns:
        return []
    close = pl.col("close").cast(pl.Float64) if "close" in df.columns else pl.lit(None)
    volume = pl.col("volume").cast(pl.Float64) if "volume" in df.columns else pl.lit(0.0)
    rows = df.select(
        pl.col("time_key").cast(pl.Utf8).str.slice(0, 10).alias("t"),
        close.alias("c"),
        volume.alias("v"),
    ).to_dicts()
    return [{"t": r["t"], "c": r["c"], "v": r["v"] or 0.0} for r in rows if r["t"]]


def _flag(value: Any) -> bool:
    """OpenD reports login flags as ``'1'``/``'0'`` strings; fakes use bools."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    if isinstance(value, float) and value != value:  # NaN from pandas columns
        return False
    return bool(value)


def _basicinfo_rows(data: Any) -> list[dict[str, Any]]:
    """Normalize the SDK's DataFrame / fake list-of-dicts to plain rows."""
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, (list, tuple)):
        return [row for row in data if isinstance(row, dict)]
    to_dicts = getattr(data, "to_dicts", None)  # polars
    if callable(to_dicts):
        return [row for row in to_dicts() if isinstance(row, dict)]
    to_dict = getattr(data, "to_dict", None)  # pandas
    if callable(to_dict):
        try:
            rows = to_dict("records")
        except (TypeError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict)]
    return []


def _listing_date(value: Any) -> dt.date | None:
    """Parse the SDK's listing time; epoch / ``N/A`` / unparseable → None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text.lower() in {"n/a", "none", "nat", "nan", "-"}:
            return None
        try:
            parsed = dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None if parsed <= UNKNOWN_LISTING_DATE else parsed


def _number(value: Any) -> float | None:
    """Snapshot cell → finite float; NaN / None / junk → None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _epoch_ms_et(value: Any) -> int | None:
    """SDK ``update_time`` → epoch ms; naive wall clock is US/Eastern."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text.lower() in {"n/a", "none", "nat", "nan", "-"}:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=us_clock.tz)
    return int(parsed.timestamp() * 1000)


class MoomooProvider:
    """Plugin contract implementation; the remaining datasets land in later tasks."""

    name = "moomoo"
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        adj_factor=True,
        minute=False,
        realtime=True,
        financial=False,
    )
    min_realtime_interval_seconds = 15.0

    def __init__(self) -> None:
        # .config 兼容垫片: 与 YAML 自定义源同形, 供 cfg.rpm / cfg.batch 消费。
        from types import SimpleNamespace

        self.config = SimpleNamespace(
            datasets={
                "daily": SimpleNamespace(rpm=120, batch=100),
                "adj_factor": SimpleNamespace(rpm=120, batch=100),
                "realtime": SimpleNamespace(rpm=120, batch=400),
            }
        )

        # Deterministic clock/sleep for tests (the rotation holds 61 s per batch).
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        # Universe memo: (expires_at, symbols); instruments refresh hourly.
        self._universe_cache: tuple[float, list[str]] | None = None

    def close(self) -> None:
        pass

    def opend_status(self) -> dict[str, Any]:
        """Probe OpenD global state without leaking exceptions to callers."""
        status: dict[str, Any] = {
            "ok": False,
            "qot_logined": False,
            "market_us": None,
            "error": None,
        }
        try:
            with open_ctx() as ctx:
                ret, data = _unpack_ret(ctx.get_global_state())
                if ret != 0:
                    raise RuntimeError(f"get_global_state 失败: ret={ret!r} data={data!r}")
                if not isinstance(data, dict):
                    raise RuntimeError(f"get_global_state 返回非字典: {data!r}")
                status["qot_logined"] = _flag(data.get("qot_logined"))
                status["market_us"] = data.get("market_us")
                status["ok"] = True
        except Exception as e:
            status["error"] = str(e) or type(e).__name__
        return status

    # ── 日 K + 当日除权事件缓存 ─────────────────────────────
    @staticmethod
    def _batch_frame(
        bars: dict[str, tuple[Any, Any]], *, lower: dt.date | None, end: dt.date
    ) -> pl.DataFrame:
        """Batch raw frames → one canonical frame within ``[start - 14d, end]``."""
        frames: list[pl.DataFrame] = []
        for code, (raw_frame, _qfq_frame) in bars.items():
            symbol = from_moomoo(code)
            frame = _canonical_daily(symbol, raw_frame).filter(pl.col("date").is_not_null())
            if lower is not None:
                frame = frame.filter(pl.col("date") >= lower)
            frame = frame.filter(pl.col("date") <= end)
            if not frame.is_empty():
                frames.append(frame)
        if not frames:
            return pl.DataFrame(schema=DAILY_SCHEMA)
        return pl.concat(frames, how="vertical")

    @staticmethod
    def _write_events_cache(bars: dict[str, tuple[Any, Any]]) -> None:
        """Derive events per symbol and merge them into today's cache.

        A symbol whose raw/QFQ data is invalid must NOT be recorded as covered:
        it has no cache events, so ``get_adj_factors`` has to fall back to
        ``get_rehab`` for it instead of silently losing its splits.
        """
        from app.plugins.moomoo import events_cache
        from app.services.adjustment_events import events_for_symbols

        events: list[dict[str, Any]] = []
        covered: set[str] = set()
        for code, (raw_frame, qfq_frame) in bars.items():
            raw_rows = _event_bars(raw_frame)
            qfq_rows = _event_bars(qfq_frame)
            if not raw_rows or not qfq_rows:
                continue
            symbol = from_moomoo(code)
            try:
                symbol_events = events_for_symbols(
                    ((symbol, row) for row in raw_rows),
                    ((symbol, row) for row in qfq_rows),
                    skip_invalid_symbols=False,
                    price_tick=MOOMOO_QFQ_TICK,
                    min_rel=MOOMOO_MIN_REL,
                )
            except ValueError as exc:
                logger.warning(
                    "moomoo daily: %s 的复权数据异常, 不计入事件缓存 (改用 rehab): %s",
                    symbol,
                    exc,
                )
                continue
            events.extend(symbol_events)
            covered.add(symbol)
        if not covered:
            return
        events_cache.write(settings.data_dir, us_clock.today(), events, covered)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """One canonical frame per subscription batch of raw daily bars."""
        if asset_type != "stock" or not symbols:
            return
        from app.plugins.moomoo.rotation import rotate_daily

        codes = [to_moomoo(symbol) for symbol in symbols]
        end_day = _wall_date(end_time) if end_time is not None else us_clock.today()
        lower = (
            _wall_date(start_time) - dt.timedelta(days=REPAIR_WINDOW_DAYS)
            if start_time is not None
            else None
        )
        done = 0
        with open_ctx() as ctx:
            for result in rotate_daily(
                ctx, codes, clock=self._clock, sleep=self._sleep
            ):
                done += len(result.bars) + len(result.failed)
                if result.failed:
                    logger.warning(
                        "moomoo daily: %d 只标的取数失败 (跳过, 不中断批次): %s",
                        len(result.failed),
                        result.failed[:10],
                    )
                self._write_events_cache(result.bars)
                frame = self._batch_frame(result.bars, lower=lower, end=end_day)
                if not frame.is_empty():
                    yield frame
                if on_chunk_done:
                    on_chunk_done(done, len(codes))

    def get_daily(
        self,
        symbols: list[str],
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """Concatenation of every ``iter_daily`` batch."""
        frames = list(
            self.iter_daily(symbols, start_time, end_time, asset_type, on_chunk_done)
        )
        if not frames:
            return pl.DataFrame(schema=DAILY_SCHEMA)
        return pl.concat(frames, how="vertical").sort(["symbol", "date"])

    # ── 除权事件: 当日缓存优先, 未覆盖的标的走 get_rehab ────────
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
        anchor_dates: dict[str, dt.date] | None = None,
    ) -> pl.DataFrame:
        """Event rows from today's daily-sync cache, else paced ``get_rehab``.

        Rehab returns the full lifetime of events, so the cache can serve any
        window and ``anchor_dates`` (for long-halt window widening) is unused.
        A symbol whose rows cannot be mapped is logged and skipped; other
        symbols are unaffected.
        """
        from app.plugins.moomoo import events_cache
        from app.plugins.moomoo.pacer import Pacer
        from app.plugins.moomoo.rehab import rehab_events
        from app.services.kline_sync import _ADJ_EVENT_SCHEMA

        empty = pl.DataFrame(schema=_ADJ_EVENT_SCHEMA)
        if asset_type != "stock" or not symbols:
            return empty

        wanted: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            key = (symbol or "").strip().upper()
            if key and key not in seen:
                seen.add(key)
                wanted.append(key)

        cached = events_cache.read(settings.data_dir, us_clock.today())
        cached_frame = empty
        covered: set[str] = set()
        if cached is not None:
            cached_frame, covered_raw = cached
            covered = {str(item).strip().upper() for item in covered_raw}

        frames: list[pl.DataFrame] = []
        from_cache = [symbol for symbol in wanted if symbol in covered]
        if from_cache:
            frames.append(cached_frame.filter(pl.col("symbol").is_in(from_cache)))

        pending = [symbol for symbol in wanted if symbol not in covered]
        if pending:
            pacer = Pacer(
                REHAB_CALLS_PER_WINDOW, REHAB_WINDOW_S, clock=self._clock, sleep=self._sleep
            )
            fetched: list[dict[str, Any]] = []
            total = len(pending)
            with open_ctx() as ctx:
                for index, symbol in enumerate(pending, start=1):
                    pacer.wait()
                    try:
                        ret, data = _unpack_ret(ctx.get_rehab(to_moomoo(symbol)))
                        if ret != 0 and _is_rate_limited(data):
                            self._sleep(RATE_LIMIT_BACKOFF_S)
                            pacer.wait()
                            ret, data = _unpack_ret(ctx.get_rehab(to_moomoo(symbol)))
                        if ret != 0:
                            raise RuntimeError(
                                f"get_rehab 失败: ret={ret!r} data={data!r}"
                            )
                        fetched.extend(rehab_events(symbol, _basicinfo_rows(data)))
                    except ValueError as exc:
                        logger.warning("moomoo adj: 跳过 %s: %s", symbol, exc)
                    if on_chunk_done:
                        on_chunk_done(index, total)
            if fetched:
                frames.append(pl.DataFrame(fetched, schema=_ADJ_EVENT_SCHEMA))

        if not frames:
            return empty
        frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="vertical")
        if start_time is not None:
            frame = frame.filter(pl.col("trade_date") >= _wall_date(start_time))
        if end_time is not None:
            frame = frame.filter(pl.col("trade_date") <= _wall_date(end_time))
        return (
            frame.select(list(_ADJ_EVENT_SCHEMA))
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    # ── 实时快照 (研究报价) ─────────────────────────────────
    def _universe_symbols(self) -> list[str]:
        """Bare symbols, memozied for an hour to keep snapshots cheap."""
        now = self._clock()
        if self._universe_cache is not None and now < self._universe_cache[0]:
            return self._universe_cache[1]
        symbols = [
            str(row.get("symbol") or "")
            for row in self.get_instruments("stock")
        ]
        symbols = [symbol for symbol in symbols if symbol]
        self._universe_cache = (now + UNIVERSE_CACHE_SECONDS, symbols)
        return symbols

    @staticmethod
    def _snapshot_rows(data: Any, now: dt.datetime) -> list[dict[str, Any]]:
        """Snapshot frame rows → the realtime record contract (no ``feed``)."""
        out: list[dict[str, Any]] = []
        for row in _basicinfo_rows(data):
            code = str(row.get("code") or "").strip()
            try:
                symbol = from_moomoo(code)
            except ValueError:
                continue
            ts = _epoch_ms_et(row.get("update_time"))
            delay = (
                max(0.0, now.timestamp() - ts / 1000.0) if ts is not None else None
            )
            last = _number(row.get("last_price"))
            prev_close = _number(row.get("prev_close_price"))
            change = (
                last - prev_close
                if last is not None and prev_close is not None
                else None
            )
            out.append(
                {
                    "symbol": symbol,
                    "name": symbol,
                    "last_price": last,
                    "prev_close": prev_close,
                    "open": _number(row.get("open_price")),
                    "high": _number(row.get("high_price")),
                    "low": _number(row.get("low_price")),
                    "volume": _number(row.get("volume")),
                    "amount": _number(row.get("turnover")),
                    "change_pct": change / prev_close if change is not None and prev_close else None,
                    "change_amount": change,
                    "amplitude": None,     # 由 pipeline 按价格口径回算
                    "turnover_rate": None,  # 由 pipeline 按股本口径回算
                    "timestamp": ts,
                    "quote_ts": ts,
                    "volume_ts": ts,
                    "data_delay": delay,
                    "session": "regular",
                }
            )
        return out

    def get_realtime(self) -> list[dict]:
        """Full-market US snapshots in ≤400-code, paced ``get_market_snapshot`` calls."""
        from app.plugins.moomoo.pacer import Pacer

        symbols = self._universe_symbols()
        if not symbols:
            return []
        pacer = Pacer(
            SNAPSHOT_RPM, SNAPSHOT_WINDOW_S, clock=self._clock, sleep=self._sleep
        )
        now = us_clock.now()
        records: list[dict[str, Any]] = []
        with open_ctx() as ctx:
            for start in range(0, len(symbols), SNAPSHOT_CHUNK):
                codes = [to_moomoo(s) for s in symbols[start : start + SNAPSHOT_CHUNK]]
                pacer.wait()
                ret, data = _unpack_ret(ctx.get_market_snapshot(codes))
                if ret != 0 and _is_rate_limited(data):
                    self._sleep(RATE_LIMIT_BACKOFF_S)
                    pacer.wait()
                    ret, data = _unpack_ret(ctx.get_market_snapshot(codes))
                if ret != 0:
                    raise RuntimeError(
                        f"get_market_snapshot 失败: ret={ret!r} data={data!r}"
                    )
                records.extend(self._snapshot_rows(data, now))
        return records

    # ── 标的维表 ────────────────────────────────────────────
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """US NYSE/NASDAQ equity universe (other asset types are unsupported)."""
        if asset_type != "stock":
            return []

        import moomoo

        with open_ctx() as ctx:
            ret, data = _unpack_ret(
                ctx.get_stock_basicinfo(moomoo.Market.US, moomoo.SecurityType.STOCK)
            )
        if ret != 0:
            raise RuntimeError(f"get_stock_basicinfo 失败: ret={ret!r} data={data!r}")

        rows = _basicinfo_rows(data)
        suspensions: set[str] = set()
        instruments: list[dict] = []
        for row in rows:
            suspensions.add(str(row.get("suspension")))
            exchange = US_EXCHANGES.get(str(row.get("exchange_type") or "").strip().upper())
            code = str(row.get("code") or "").strip()
            if exchange is None or not US_CODE_RE.match(code):
                continue
            if _flag(row.get("delisting")):
                continue
            try:
                symbol = from_moomoo(code)
            except ValueError:
                continue
            instruments.append(
                {
                    "symbol": symbol,
                    "name": str(row.get("name") or "").strip(),
                    "code": symbol,  # same as Alpaca: bare ticker (moomoo code is US.<symbol>)
                    "exchange": exchange,
                    "asset_type": "stock",
                    "source": "moomoo",
                    "region": "US",
                    "listing_date": _listing_date(row.get("listing_date")),
                }
            )

        # Suspended-name filtering is decided by the Task 9 live check; log what
        # the field actually contains so that decision has evidence.
        logger.info(
            "moomoo get_stock_basicinfo: %d rows, suspension 值=%s",
            len(rows),
            sorted(suspensions),
        )
        return instruments