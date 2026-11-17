# Ported from wyk/features/volume.py @ fc142999 (user's own code).
"""Causal volume features (docs/01 §5.1). Every value at bar i uses only bars < i as its reference.

rv        V_i / median(V over the previous n reference bars)
deseason  V_i / median(V at the same clock slot over the previous n sessions)  (intraday only)
ae        abnormal effort: standardised residual of log V_i on log(range_i / ATR_{i-1}), the regression fitted on
          the previous `window` bars only (the D1 regressor)
ae intraday (S0-3 AE-SESSION): the same residual with the session-adjusted regressor
          log(deseason(range_i / ATR_{i-1}, slot, n, exclude)), i.e. range/ATR divided by its trailing n-session
          same-slot median, the slot convention already used for V. This is d.0's "+ session dummies for intraday
          bars" implemented through the project's slot convention (abnormal_effort_session).

Reference bars exclude NaN volume and any bar flagged in `exclude` (holidays / short sessions), so a holiday
neither triggers a low-volume condition through its own tiny count nor depresses the next days' reference.
"""

import numpy as np
import pandas as pd


def _trailing_median(values: pd.Series, valid: pd.Series, n: int) -> pd.Series:
    """At position i: median of the last n valid values at positions < i (NaN until n exist)."""
    v = values.where(valid)
    comp = v.dropna()
    med = comp.rolling(n, min_periods=n).median()          # median of n valid values ending at that position
    out = pd.Series(np.nan, index=values.index)
    out.loc[med.index] = med.to_numpy()
    return out.ffill().shift(1)


def rv(volume: pd.Series, n: int = 20, exclude: pd.Series | None = None) -> pd.Series:
    exclude = pd.Series(False, index=volume.index) if exclude is None else exclude.astype(bool)
    valid = volume.notna() & (volume > 0) & ~exclude
    ref = _trailing_median(volume, valid, n)
    return volume / ref


def deseason(volume: pd.Series, slot: pd.Series, n: int = 20, exclude: pd.Series | None = None) -> pd.Series:
    """Divide by the trailing median of the same slot (e.g. server hour) over the previous n valid sessions."""
    exclude = pd.Series(False, index=volume.index) if exclude is None else exclude.astype(bool)
    valid = volume.notna() & (volume > 0) & ~exclude
    ref = pd.Series(np.nan, index=volume.index)
    for _, idx in volume.groupby(slot).groups.items():
        ref.loc[idx] = _trailing_median(volume.loc[idx], valid.loc[idx], n).to_numpy()
    return volume / ref


def abnormal_effort(volume: pd.Series, rng: pd.Series, atr_prev: pd.Series, window: int = 250,
                    min_obs: int = 100, exclude: pd.Series | None = None) -> pd.Series:
    """Standardised residual of log V on log(range/ATR_prev), OLS on the previous `window` valid bars."""
    exclude = pd.Series(False, index=volume.index) if exclude is None else exclude.astype(bool)
    x = np.log(rng / atr_prev)
    y = np.log(volume)
    valid = np.isfinite(x) & np.isfinite(y) & ~exclude
    xs, ys = x.where(valid), y.where(valid)
    one = valid.astype(float)

    def roll(s):   # sum over the previous `window` positions (current excluded)
        return s.fillna(0.0).rolling(window, min_periods=1).sum().shift(1)

    n, sx, sy = roll(one), roll(xs), roll(ys)
    sxx, sxy, syy = roll(xs * xs), roll(xs * ys), roll(ys * ys)
    varx = sxx / n - (sx / n) ** 2
    covxy = sxy / n - (sx / n) * (sy / n)
    b = covxy / varx
    a = sy / n - b * sx / n
    # residual variance of the fit on the window: var(y) - b^2 var(x)
    vary = syy / n - (sy / n) ** 2
    res_var = ((vary - b * b * varx) * n / (n - 2)).where(n > 2)
    res_var = res_var.where(res_var > 0)
    z = (y - (a + b * x)) / np.sqrt(res_var)
    return z.where((n >= min_obs) & np.isfinite(z))


def session_range(rng: pd.Series, atr_prev: pd.Series, slot: pd.Series, n: int = 20,
                  exclude: pd.Series | None = None) -> pd.Series:
    """range_i / ATR_{i-1} divided by its trailing median at the same slot over the previous n valid sessions
    (S0-3 AE-SESSION). Bars < i only; excluded (holiday) bars are never a reference."""
    return deseason(rng / atr_prev, slot, n=n, exclude=exclude)


def abnormal_effort_session(volume: pd.Series, rng: pd.Series, atr_prev: pd.Series, slot: pd.Series, n: int = 20,
                            window: int = 250, min_obs: int = 100, exclude: pd.Series | None = None,
                            x_session: pd.Series | None = None) -> pd.Series:
    """Intraday abnormal effort (S0-3 AE-SESSION): abnormal_effort with the regressor session_range(...) and a unit
    ATR, i.e. the standardised residual of log V on log(deseason(range/ATR_prev, slot, n)). The OLS window, min_obs
    and standardisation are abnormal_effort's. x_session: a precomputed session_range with the same arguments."""
    x = session_range(rng, atr_prev, slot, n, exclude) if x_session is None else x_session
    return abnormal_effort(volume, x, pd.Series(1.0, index=volume.index), window=window, min_obs=min_obs,
                           exclude=exclude)
