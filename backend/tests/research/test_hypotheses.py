"""The 18 research hypotheses: entry/invalidation/sell rules per variant."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.research.hypotheses import VARIANTS, Inputs, variant_path

DATES = [date(2024, 1, 1) + timedelta(days=i) for i in range(300)]

VARIANTS_EXPECTED = {
    "H1-2B",
    "H1-3B",
    "H1-1B",
    "H2-spring",
    "H2-sos",
    "H2-lps",
    "H3-2B",
    "H3-3B",
    "H4-3B",
    "H5-w5",
    "H5-w10",
    "H6-H3-2B",
    "H6-H3-3B",
    "H6-H4-3B",
    "H6-H5-w5",
    "H6-H5-w10",
    "H1c-2B",
    "H1c-3B",
}

_POINT_SCHEMA = {
    "symbol": pl.String,
    "known_date": pl.Date,
    "type": pl.String,
    "invalidation_price": pl.Float64,
    "confirmed_date": pl.Date,
    "leg_prev_start": pl.Date,
    "leg_prev_end": pl.Date,
    "leg_last_start": pl.Date,
    "leg_last_end": pl.Date,
}

_EVENT_SCHEMA = {
    "symbol": pl.String,
    "known_date": pl.Date,
    "kind": pl.String,
    "stop_ref": pl.Float64,
    "range_hi": pl.Float64,
    "atr_known": pl.Float64,
}

_RANGE_SCHEMA = {
    "symbol": pl.String,
    "range_kind": pl.String,
    "known_date": pl.Date,
    "end_date": pl.Date,
    "lo": pl.Float64,
    "hi": pl.Float64,
}


def _chan_points(rows: list[dict]) -> pl.DataFrame:
    def maybe_day(row: dict, key: str):
        return DATES[row[key]] if row.get(key) is not None else None

    data = {column: [] for column in _POINT_SCHEMA}
    for row in rows:
        data["symbol"].append(row.get("symbol", "AAA"))
        data["known_date"].append(DATES[row["day"]])
        data["type"].append(row["type"])
        data["invalidation_price"].append(float(row.get("invalidation", 90.0)))
        data["confirmed_date"].append(maybe_day(row, "confirmed"))
        for leg in ("leg_prev_start", "leg_prev_end", "leg_last_start", "leg_last_end"):
            data[leg].append(maybe_day(row, leg))
    return pl.DataFrame(data, schema=_POINT_SCHEMA)


def _wyk_events(rows: list[dict]) -> pl.DataFrame:
    data = {column: [] for column in _EVENT_SCHEMA}
    for row in rows:
        data["symbol"].append(row.get("symbol", "AAA"))
        data["known_date"].append(DATES[row["day"]])
        data["kind"].append(row["kind"])
        data["stop_ref"].append(row.get("stop_ref"))
        data["range_hi"].append(row.get("range_hi"))
        data["atr_known"].append(row.get("atr_known"))
    return pl.DataFrame(data, schema=_EVENT_SCHEMA)


def _wyk_ranges(rows: list[dict]) -> pl.DataFrame:
    data = {column: [] for column in _RANGE_SCHEMA}
    for row in rows:
        data["symbol"].append(row.get("symbol", "AAA"))
        data["range_kind"].append(row["range_kind"])
        data["known_date"].append(DATES[row["known"]])
        data["end_date"].append(DATES[row["end"]] if row.get("end") is not None else None)
        data["lo"].append(float(row.get("lo", 80.0)))
        data["hi"].append(float(row.get("hi", 100.0)))
    return pl.DataFrame(data, schema=_RANGE_SCHEMA)


def _bars(
    symbol: str = "AAA",
    n: int = 60,
    volumes: list[float] | None = None,
) -> pl.DataFrame:
    volumes = volumes if volumes is not None else [1_000.0] * n
    closes = [100.0] * n
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": DATES[:n],
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": volumes,
            "amount": [1_000_000.0] * n,
            "raw_close": closes,
        }
    )


def _spy(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["SPY"] * len(closes),
            "date": DATES[: len(closes)],
            "close": closes,
        },
        schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64},
    )


def _inputs(
    chan_points: pl.DataFrame | None = None,
    wyk_events: pl.DataFrame | None = None,
    wyk_ranges: pl.DataFrame | None = None,
    bars: pl.DataFrame | None = None,
    spy: pl.DataFrame | None = None,
) -> Inputs:
    empty_points = pl.DataFrame(schema=_POINT_SCHEMA)
    empty_events = pl.DataFrame(schema=_EVENT_SCHEMA)
    empty_ranges = pl.DataFrame(schema=_RANGE_SCHEMA)
    return Inputs(
        chan_points=chan_points if chan_points is not None else empty_points,
        wyk_events=wyk_events if wyk_events is not None else empty_events,
        wyk_ranges=wyk_ranges if wyk_ranges is not None else empty_ranges,
        bars=bars if bars is not None else _bars(n=300),
        spy=spy if spy is not None else _spy([100.0] * 300),
    )


def test_h1_type_filter_and_sells() -> None:
    points = _chan_points(
        [
            {"day": 1, "type": "2B", "invalidation": 90.0},
            {"day": 2, "type": "3B", "invalidation": 91.0},
            {"day": 3, "type": "1S"},
            {"day": 4, "type": "2S"},
            {"day": 5, "type": "3S"},
        ]
    )
    signals = VARIANTS["H1-2B"](_inputs(chan_points=points))

    assert signals.entries.height == 1
    row = signals.entries.row(0, named=True)
    assert (row["symbol"], row["date"], row["invalidation"]) == ("AAA", DATES[1], 90.0)
    assert sorted(signals.sells["date"].to_list()) == [DATES[3], DATES[4], DATES[5]]

    signals_3b = VARIANTS["H1-3B"](_inputs(chan_points=points))
    assert signals_3b.entries["date"].to_list() == [DATES[2]]


def test_h1c_uses_confirmed_date_only() -> None:
    points = _chan_points(
        [
            {"day": 1, "type": "2B", "confirmed": None},
            {"day": 2, "type": "2B", "confirmed": 5},
            {"day": 3, "type": "3B", "confirmed": 6},
        ]
    )
    signals = VARIANTS["H1c-2B"](_inputs(chan_points=points))

    assert signals.entries["date"].to_list() == [DATES[5]]
    assert VARIANTS["H1c-3B"](_inputs(chan_points=points)).entries["date"].to_list() == [DATES[6]]


def test_h2_sos_invalidation_formula() -> None:
    events = _wyk_events(
        [
            {"day": 1, "kind": "sos", "stop_ref": 40.0, "range_hi": 50.0, "atr_known": 2.0},
            {"day": 2, "kind": "entry_steady", "stop_ref": 88.0},
            {"day": 3, "kind": "lps", "stop_ref": 87.0},
            {"day": 4, "kind": "sow"},
            {"day": 5, "kind": "utad_confirmed"},
            {"day": 6, "kind": "sos_failed"},
        ]
    )
    signals = VARIANTS["H2-sos"](_inputs(wyk_events=events))

    assert signals.entries.height == 1
    assert signals.entries["invalidation"].to_list() == [49.5]

    spring = VARIANTS["H2-spring"](_inputs(wyk_events=events))
    assert spring.entries["invalidation"].to_list() == [88.0]
    lps = VARIANTS["H2-lps"](_inputs(wyk_events=events))
    assert lps.entries["invalidation"].to_list() == [87.0]

    assert sorted(lps.sells["date"].to_list()) == [DATES[4], DATES[5], DATES[6]]


def test_h3_requires_active_acc_range_and_recent_event() -> None:
    points = _chan_points([{"day": 30, "type": "2B", "invalidation": 90.0}])
    events = _wyk_events([{"day": 10, "kind": "lps", "stop_ref": 85.0}])
    active_range = _wyk_ranges([{"range_kind": "accumulation", "known": 0, "end": None}])

    signals = VARIANTS["H3-2B"](
        _inputs(chan_points=points, wyk_events=events, wyk_ranges=active_range)
    )
    assert signals.entries["date"].to_list() == [DATES[30]]
    assert signals.entries["invalidation"].to_list() == [90.0]

    # wyk active_at: the range's ending bar still counts as active; the bar after does not.
    ended_at_t = _wyk_ranges([{"range_kind": "accumulation", "known": 0, "end": 30}])
    assert VARIANTS["H3-2B"](
        _inputs(chan_points=points, wyk_events=events, wyk_ranges=ended_at_t)
    ).entries["date"].to_list() == [DATES[30]]
    ended_before_t = _wyk_ranges([{"range_kind": "accumulation", "known": 0, "end": 29}])
    assert VARIANTS["H3-2B"](
        _inputs(chan_points=points, wyk_events=events, wyk_ranges=ended_before_t)
    ).entries.is_empty()

    # The event must be within the last 20 bars up to and including t.
    event_21_ago = _wyk_events([{"day": 9, "kind": "lps", "stop_ref": 85.0}])
    assert VARIANTS["H3-2B"](
        _inputs(chan_points=points, wyk_events=event_21_ago, wyk_ranges=active_range)
    ).entries.is_empty()

    # A non-accumulation range does not satisfy the requirement.
    distribution = _wyk_ranges([{"range_kind": "distribution", "known": 0, "end": None}])
    assert VARIANTS["H3-2B"](
        _inputs(chan_points=points, wyk_events=events, wyk_ranges=distribution)
    ).entries.is_empty()


def test_h4_volume_gates() -> None:
    def inputs(volumes: list[float], legs: bool = True) -> Inputs:
        points = _chan_points(
            [
                {
                    "day": 40,
                    "type": "3B",
                    "invalidation": 90.0,
                    **(
                        {
                            "leg_prev_start": 25,
                            "leg_prev_end": 29,
                            "leg_last_start": 34,
                            "leg_last_end": 39,
                        }
                        if legs
                        else {}
                    ),
                }
            ]
        )
        return _inputs(chan_points=points, bars=_bars(n=60, volumes=volumes))

    base = [1_000.0] * 60
    good = list(base)
    for i in range(25, 30):
        good[i] = 2_000.0
    for i in range(35, 40):
        good[i] = 700.0

    signals = VARIANTS["H4-3B"](inputs(good))
    assert signals.entries["date"].to_list() == [DATES[40]]

    weak_volume = list(good)
    for i in range(25, 30):
        weak_volume[i] = 1_490.0
    assert VARIANTS["H4-3B"](inputs(weak_volume)).entries.is_empty()

    heavy_pullback = list(good)
    for i in range(35, 40):
        heavy_pullback[i] = 810.0
    assert VARIANTS["H4-3B"](inputs(heavy_pullback)).entries.is_empty()

    assert VARIANTS["H4-3B"](inputs(good, legs=False)).entries.is_empty()


def test_h5_window_and_later_date() -> None:
    points = _chan_points([{"day": 5, "type": "1B", "invalidation": 90.0}])
    events = _wyk_events([{"day": 10, "kind": "lps", "stop_ref": 85.0}])
    inputs = _inputs(chan_points=points, wyk_events=events, bars=_bars(n=40))

    five = VARIANTS["H5-w5"](inputs)
    assert five.entries["date"].to_list() == [DATES[10]]
    assert five.entries["invalidation"].to_list() == [90.0]

    six_apart = _chan_points([{"day": 4, "type": "1B", "invalidation": 90.0}])
    inputs = _inputs(chan_points=six_apart, wyk_events=events, bars=_bars(n=40))
    assert VARIANTS["H5-w5"](inputs).entries.is_empty()
    ten = VARIANTS["H5-w10"](inputs)
    assert ten.entries["date"].to_list() == [DATES[10]]

    # The later date can come from the Chan side.
    chan_later = _chan_points([{"day": 12, "type": "1B", "invalidation": 95.0}])
    inputs = _inputs(chan_points=chan_later, wyk_events=events, bars=_bars(n=40))
    later = VARIANTS["H5-w5"](inputs)
    assert later.entries["date"].to_list() == [DATES[12]]
    assert later.entries["invalidation"].to_list() == [95.0]


def test_h6_spy_regime() -> None:
    def inputs(spy: pl.DataFrame) -> Inputs:
        points = _chan_points([{"day": 240, "type": "1B", "invalidation": 90.0}])
        events = _wyk_events([{"day": 238, "kind": "lps", "stop_ref": 85.0}])
        return _inputs(chan_points=points, wyk_events=events, bars=_bars(n=260), spy=spy)

    short_points = _chan_points([{"day": 150, "type": "1B", "invalidation": 90.0}])
    short_events = _wyk_events([{"day": 148, "kind": "lps", "stop_ref": 85.0}])
    short = VARIANTS["H6-H5-w5"](
        _inputs(
            chan_points=short_points,
            wyk_events=short_events,
            bars=_bars(n=160),
            spy=_spy([100.0] * 199),
        )
    )
    assert short.entries.is_empty()

    above = _spy([100.0] * 230 + [150.0] * 20)
    long_signals = VARIANTS["H6-H5-w5"](inputs(above))
    assert long_signals.entries["date"].to_list() == [DATES[240]]


def test_variant_ids_exact() -> None:
    assert len(VARIANTS) == 18
    assert set(VARIANTS) == VARIANTS_EXPECTED


def test_variant_path_is_content_addressed(tmp_path) -> None:
    table = pl.DataFrame({"symbol": ["AAA"], "date": [DATES[0]]})
    first = variant_path(tmp_path, "H1-2B", table)
    second = variant_path(tmp_path, "H1-2B", table)

    assert first == second
    assert first.parent == tmp_path / "research" / "variants" / "H1-2B"
    assert len(first.stem) == 16
    assert first.suffix == ".parquet"
