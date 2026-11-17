"""Matrix-native bridge that replays a precomputed research signal table.

The research harness writes one row per candidate signal to a variant table
(``symbol, date, entry, exit, score``). This strategy projects that table onto
the backtest panel so the ordinary portfolio engine can trade it unchanged.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix

META = {
    "id": "research_signal_table",
    "name": "研究信号表",
    "description": "回放研究流程生成的 symbol/date 信号表",
    "tags": ["研究", "信号表"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "research_only": True,
    # The table already carries the final score; keep META scoring empty so the
    # framework pipeline passes the strategy score through unchanged.
    "params": [
        {
            "id": "table_path",
            "label": "Signal table",
            "type": "str",
            "default": "",
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100000,
}

EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.15
MAX_HOLD_DAYS = 20
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = ["signal_research_entry"]
EXIT_SIGNALS = ["signal_research_exit"]

_TABLE_COLUMNS = ("symbol", "date", "entry", "exit", "score")


class ResearchSignalTableStrategy:
    """Project a research variant table onto the market data matrix."""

    def __init__(self) -> None:
        # Rows outside the panel are dropped; expose the count for callers that
        # need it (the AI-strategy import whitelist forbids a logging import).
        self.ignored_rows = 0

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 1

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        table_path = str((params or {}).get("table_path") or "").strip()
        if not table_path:
            raise ValueError("table_path required")

        table = pl.read_parquet(table_path)
        missing = sorted(set(_TABLE_COLUMNS) - set(table.columns))
        if missing:
            raise ValueError(f"signal table missing columns: {missing}")

        rows = table.select(
            pl.col("symbol").cast(pl.Utf8).alias("symbol"),
            pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"),
            pl.col("entry").cast(pl.Boolean).fill_null(False).alias("entry"),
            pl.col("exit").cast(pl.Boolean).fill_null(False).alias("exit"),
            pl.col("score").cast(pl.Float32).fill_null(0.0).alias("score"),
        )

        row_by_date = {
            str(label)[:10]: index
            for index, label in enumerate(market.timestamp_labels)
        }
        column_by_symbol = {
            str(symbol): index
            for index, symbol in enumerate(market.symbols)
        }

        entry = np.zeros(market.shape, dtype=np.uint8)
        exit_ = np.zeros(market.shape, dtype=np.uint8)
        score = np.zeros(market.shape, dtype=np.float32)
        ignored = 0
        for symbol, date_label, is_entry, is_exit, value in rows.iter_rows():
            row = row_by_date.get(date_label)
            column = column_by_symbol.get(symbol)
            if row is None or column is None:
                ignored += 1
                continue
            if is_entry:
                entry[row, column] = 1
                score[row, column] = np.float32(value)
            if is_exit:
                exit_[row, column] = 1
        self.ignored_rows = ignored

        return make_signal_matrix(
            market.shape,
            entry=entry,
            exit=exit_,
            score=score,
            entry_signal_code=np.where(entry != 0, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_ != 0, 0, -1).astype(np.int16),
            entry_signal_ids=tuple(ENTRY_SIGNALS),
            exit_signal_ids=tuple(EXIT_SIGNALS),
        )


MATRIX_STRATEGY = ResearchSignalTableStrategy()