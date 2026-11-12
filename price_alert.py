"""株価アラート: alerts.csv の条件(○ドルを割ったら/超えたら)を満たしたら ntfy に通知する。

alerts.csv の列:
  ticker     … 銘柄(例: QLYS)
  condition  … below(割ったら) または above(超えたら)
  price      … 基準の株価(ドル)
  note       … メモ(通知に表示。空でも可)
  triggered  … 通知済みの日時(UTC)。空欄の行だけを監視する。もう一度監視したいときは空欄に戻す

環境変数は volume_alert.py と同じ(NTFY_TOPIC など)。
実行: python3 price_alert.py            # 条件を満たせば通知し、alerts.csv を更新
      python3 price_alert.py --dry-run  # 通知・更新せず、現在値と判定だけ表示
"""
import csv, datetime, json, sys, urllib.request

from volume_alert import UA, send

ALERTS = "alerts.csv"
FIELDS = ["ticker", "condition", "price", "note", "triggered"]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def quote_yahoo(t):
    d = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=1d"))
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])


def quote_stooq(t):
    # 例: Symbol,Date,Time,Open,High,Low,Close,Volume
    lines = _get(f"https://stooq.com/q/l/?s={t.lower()}.us&f=sd2t2ohlcv&h&e=csv").strip().splitlines()
    return float(lines[1].split(",")[6])


def quote(t):
    """現在値(取引時間中はほぼリアルタイム、時間外は直近の終値)。2つの取得先を順に試す。"""
    errs = []
    for f in (quote_yahoo, quote_stooq):
        try:
            p = f(t)
            if p > 0: return p
        except Exception as e:
            errs.append(f"{f.__name__}: {e}")
    raise RuntimeError("; ".join(errs))


def hit(cond, now, level):
    return now < level if cond == "below" else now > level


def main():
    dry = "--dry-run" in sys.argv
    with open(ALERTS, newline="") as f:
        rows = list(csv.DictReader(f))
    changed = False
    for r in rows:
        if r.get("triggered"): continue
        t, cond, level = r["ticker"].strip().upper(), r["condition"].strip(), float(r["price"])
        try:
            now = quote(t)
        except Exception as e:
            print(f"{t}: 株価取得失敗 ({e})"); continue
        ok = hit(cond, now, level)
        word, verb = ("割れ", "割り込みました") if cond == "below" else ("超え", "超えました")
        print(f"{t}: 現在 ${now:,.2f} / 条件 {cond} ${level:,.2f} → {'到達' if ok else '未到達'}")
        if not ok or dry: continue
        text = f"{t} が ${level:,.2f} を{verb}\n現在値 ${now:,.2f}"
        if r.get("note"): text += f"\n{r['note']}"
        send(f"株価アラート: {t} ${level:,.0f}{word}", text, priority="high")
        r["triggered"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        changed = True
    if changed:
        with open(ALERTS, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
            w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)


if __name__ == "__main__":
    main()
