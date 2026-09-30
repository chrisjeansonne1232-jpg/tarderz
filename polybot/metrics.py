"""Performance statistics over paper trades (used by `report` and the dashboard)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

# Two-sided 95% Student-t critical values by degrees of freedom.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
        18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t_crit_95(df: int) -> float:
    if df <= 0:
        return math.inf
    if df in _T95:
        return _T95[df]
    for k in (40, 60, 120):
        if df < k:
            return _T95[k]
    return 1.96


def start_of_day(now: float, tz: str) -> float:
    d = datetime.fromtimestamp(now, ZoneInfo(tz))
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _get(t: Any, k: str) -> Any:
    return t[k] if isinstance(t, dict) else getattr(t, k)


def mean_ci(vals: list[float]) -> tuple[float | None, list[float] | None]:
    """Mean and two-sided 95% t confidence interval."""
    n = len(vals)
    if not n:
        return None, None
    m = sum(vals) / n
    if n < 2:
        return m, None
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    half = t_crit_95(n - 1) * sd / math.sqrt(n)
    return m, [m - half, m + half]


def by_window(settled: list[Any]) -> dict[str, Any]:
    """P&L per market window. Trades in the same window win or lose together,
    so windows, not trades, are the independent samples."""
    per: dict[Any, float] = {}
    for i, t in enumerate(settled):
        key = (t.get("slug") if isinstance(t, dict) else getattr(t, "slug", None)) or ("trade", i)
        per[key] = per.get(key, 0.0) + float(_get(t, "pnl") or 0.0)
    m, ci = mean_ci(list(per.values()))
    return {"n": len(per), "mean_pnl": m, "ci95": ci}


def summarize(trades: Iterable[Any], starting: float, tz: str, now: float) -> dict[str, Any]:
    trades = list(trades)
    settled = sorted((t for t in trades if _get(t, "status") in ("WON", "LOST")), key=lambda t: _get(t, "settled_ts") or 0)
    open_ = [t for t in trades if _get(t, "status") in ("OPEN", "PENDING")]
    pnls = [float(_get(t, "pnl") or 0.0) for t in settled]
    zero = [float(_get(t, "pnl_zero_fee") or 0.0) for t in settled]
    wins = sum(1 for t in settled if _get(t, "status") == "WON")
    net = sum(pnls)
    fees_settled = sum(float(_get(t, "fee")) for t in settled)
    fees_all = sum(float(_get(t, "fee")) for t in trades)
    pos = sum(p for p in pnls if p > 0)
    neg = -sum(p for p in pnls if p < 0)

    # Equity curve and drawdown on realized (settled) net P&L.
    equity: list[list[float]] = []
    cum = cum0 = peak = max_dd = 0.0
    peak_at_dd = 0.0
    for t, p, z in zip(settled, pnls, zero):
        cum += p
        cum0 += z
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd, peak_at_dd = dd, peak
        equity.append([float(_get(t, "settled_ts")), round(cum, 6), round(cum0, 6), round(-dd, 6)])

    n = len(pnls)
    mean = net / n if n else None
    ci = None
    sd = None
    if n >= 2:
        sd = math.sqrt(sum((p - net / n) ** 2 for p in pnls) / (n - 1))
        half = t_crit_95(n - 1) * sd / math.sqrt(n)
        ci = [net / n - half, net / n + half]

    streak = 0
    for t in reversed(settled):
        won = _get(t, "status") == "WON"
        if streak == 0:
            streak = 1 if won else -1
        elif (streak > 0) == won:
            streak += 1 if won else -1
        else:
            break

    sod = start_of_day(now, tz)
    today_fills = [t for t in trades if float(_get(t, "ts_fill")) >= sod]
    today_settled = [t for t in settled if float(_get(t, "settled_ts") or 0) >= sod]
    today_wins = sum(1 for t in today_settled if _get(t, "status") == "WON")

    def per_share(vals: list[float], shares: list[float]) -> float | None:
        return sum(v / s for v, s in zip(vals, shares) if s) / len(vals) if vals else None

    shares = [float(_get(t, "shares")) for t in settled]
    return {
        "starting": starting,
        "bankroll": starting + net,
        "bankroll_pct": net / starting if starting else 0.0,
        "zero_fee_bankroll": starting + sum(zero),
        "trades": len(trades),
        "settled": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": wins / n if n else None,
        "net_pnl": net,
        "gross_pnl": net + fees_settled,
        "zero_fee_pnl": sum(zero),
        "fees_settled": fees_settled,
        "fees_all": fees_all,
        "profit_factor": (pos / neg if neg > 0 else (math.inf if pos > 0 else None)),
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd / (starting + peak_at_dd) if max_dd else 0.0,
        "mean_pnl": mean,
        "sd_pnl": sd,
        "ci95": ci,
        "by_window": by_window(settled),
        "avg_edge_entry": (sum(float(_get(t, "edge_entry")) for t in settled) / n) if n else None,
        "avg_realized_per_share": per_share(pnls, shares),
        "streak": streak,
        "best": max(pnls) if pnls else None,
        "worst": min(pnls) if pnls else None,
        "open_count": len(open_),
        "at_risk": sum(float(_get(t, "cost")) + (float(_get(t, "fee")) if _get(t, "fee_in") == "collateral" else 0.0) for t in open_),
        "equity": equity,
        "today": {
            "start": sod,
            "trades": len(today_fills),
            "settled": len(today_settled),
            "win_rate": today_wins / len(today_settled) if today_settled else None,
            "net_pnl": sum(float(_get(t, "pnl") or 0) for t in today_settled),
            "zero_fee_pnl": sum(float(_get(t, "pnl_zero_fee") or 0) for t in today_settled),
            "fees": sum(float(_get(t, "fee")) for t in today_fills),
        },
    }


def format_report(s: dict[str, Any]) -> str:
    def money(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.2f}"

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{100 * x:.1f}%"

    def per_share(x: float | None) -> str:
        return "n/a" if x is None else f"{100 * x:.2f}¢/share"

    pf = s["profit_factor"]
    pf_txt = "n/a" if pf is None else ("inf" if pf == math.inf else f"{pf:.2f}")
    ci = s["ci95"]
    lines = [
        "PAPER TRADING REPORT (simulated; no real orders)",
        f"  trades                 {s['trades']}  (settled {s['settled']}, open/pending {s['open_count']})",
        f"  win rate               {pct(s['win_rate'])}  ({s['wins']}W / {s['losses']}L)",
        f"  gross P&L              {money(s['gross_pnl'])}   (settled trades, before fees)",
        f"  total fees             {s['fees_settled']:.2f}   (all fills incl. open: {s['fees_all']:.2f})",
        f"  net P&L                {money(s['net_pnl'])}",
        f"  zero-fee P&L           {money(s['zero_fee_pnl'])}   (same trades, no fees)",
        f"  bankroll               ${s['bankroll']:.2f}  ({100 * s['bankroll_pct']:+.2f}% from ${s['starting']:.2f})",
        f"  profit factor          {pf_txt}",
        f"  max drawdown           {s['max_drawdown']:.2f}  ({pct(s['max_drawdown_pct'])})",
        f"  avg edge at entry      {per_share(s['avg_edge_entry'])}",
        f"  avg realized           {per_share(s['avg_realized_per_share'])}",
        f"  avg P&L per trade      {money(s['mean_pnl'])}"
        + (f"   95% CI [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "   95% CI n/a (need ≥ 2 settled trades)"),
    ]
    bw = s.get("by_window") or {}
    wci = bw.get("ci95")
    if bw.get("mean_pnl") is not None:
        lines.append(f"  avg P&L per window     {money(bw['mean_pnl'])}   over {bw['n']} market windows"
                     + (f"   95% CI [{wci[0]:+.3f}, {wci[1]:+.3f}]" if wci else ""))
        lines.append("                         (trades in the same window win or lose together, so this range is the honest one)")
    ci_used = wci or ci
    if ci_used:
        verdict = ("CI excludes 0: positive edge" if ci_used[0] > 0 else
                   "CI excludes 0: negative edge" if ci_used[1] < 0 else
                   "CI includes 0: no statistically reliable edge yet")
        lines.append(f"  → {verdict}")
    return "\n".join(lines)


def format_whatif(rows: list[dict[str, Any]], since: float | None, tz: str) -> str:
    """Side-by-side comparison of the latency what-if wallets (see `report`)."""
    if not rows:
        return "LATENCY WHAT-IF\n  no what-if wallets have traded yet"
    when = datetime.fromtimestamp(since, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") if since else "?"
    lines = [
        f"LATENCY WHAT-IF  (same rules, orders filled after each delay; trades since {when} {tz})",
        f"  {'wallet':<22} {'trades':>6} {'won':>11} {'net P&L':>10} {'zero-fee':>10} {'windows':>7}  per-window P&L, 95% range",
    ]
    for r in rows:
        s = r["stats"]
        bw = s["by_window"]
        won = f"{s['wins']}/{s['settled']}" + (f" {100 * s['win_rate']:.0f}%" if s["win_rate"] is not None else "")
        rng = (f"{bw['mean_pnl']:+.2f}" + (f" [{bw['ci95'][0]:+.2f}, {bw['ci95'][1]:+.2f}]" if bw["ci95"] else "")
               if bw["mean_pnl"] is not None else "n/a")
        lines.append(f"  {r['label']:<22} {s['trades']:>6} {won:>11} {s['net_pnl']:>+10.2f} {s['zero_fee_pnl']:>+10.2f} {bw['n']:>7}  {rng}")
    lines += [
        "  Faster orders only matter if the faster rows beat the control clearly and their",
        "  per-window range sits above 0. Overlapping ranges = no demonstrated difference yet.",
    ]
    return "\n".join(lines)

