#!/usr/bin/env python3
"""出来高急増銘柄のうち、前日比プラスのものを毎日ntfyへ通知する(標準ライブラリのみ)。

環境変数: NTFY_TOPIC(必須)、NTFY_SERVER(省略時 https://ntfy.sh)、NTFY_TOKEN(任意)
実行: python3 volume_alert.py            # 通知する
      python3 volume_alert.py --dry-run  # 通知せず画面に出す
"""
import os, re, sys, time, urllib.request
from html.parser import HTMLParser

SCANNER_URL = "https://thetrading.tools/unusual-volume"
HISTORY_URL = "https://stockanalysis.com/stocks/{}/history/"
UA = "Mozilla/5.0 (compatible; volume-alert/1.0)"
MAX_SYMBOLS = 40          # 取得上限(相手サイトへの配慮)
TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


class Rows(HTMLParser):
    """HTMLの表を、行ごとのセル文字列のリストにする。"""
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None
    def handle_starttag(self, tag, a):
        if tag == "tr": self._row = []
        elif tag in ("td", "th") and self._row is not None: self._cell = []
    def handle_data(self, d):
        if self._cell is not None: self._cell.append(d)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split())); self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row: self.rows.append(self._row)
            self._row = None


def table_rows(html):
    p = Rows(); p.feed(html); return p.rows


def scanner_symbols(html):
    out = []
    for r in table_rows(html):
        # 先頭付近のセルにある、ティッカーらしい文字列を採る
        for c in r[:2]:
            t = c.split(" ")[0]
            if TICKER.match(t):
                if t not in out: out.append(t)
                break
    return out[:MAX_SYMBOLS]


def num(s):
    s = s.replace(",", "").replace("$", "").strip()
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def history(html, n=23):
    """履歴表(新しい順)から (日付, 終値) を最大n件返す。約1か月分。"""
    closes = []
    for r in table_rows(html):
        if len(r) >= 5 and re.match(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$", r[0]):
            c = num(r[4])
            if c is not None: closes.append((r[0], c))
        if len(closes) == n: break
    return closes


def day_change(closes):
    """直近終値と前日比(%)を返す。"""
    if len(closes) < 2: return None
    (d, c0), (_, c1) = closes[0], closes[1]
    return d, c0, (c0 / c1 - 1) * 100


def make_chart(series, path="charts.png"):
    """前日比プラス銘柄の1か月終値を、1枚の画像(小さな複数グラフ)にする。"""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    n = len(series)
    if n == 0: return None
    cols = 4; rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(3.2 * cols, 2.2 * rows), squeeze=False)
    for ax in axes.flat: ax.axis("off")
    for ax, (sym, cl, chg) in zip(axes.flat, series):
        ys = [c for _, c in reversed(cl)]
        ax.axis("on")
        ax.plot(range(len(ys)), ys, color="#1a7f4b" if ys[-1] >= ys[0] else "#c0392b", lw=1.6)
        ax.set_title(f"{sym}  {ys[-1]:,.2f}  ({chg:+.1f}% d/d)", fontsize=9)
        ax.tick_params(labelsize=7); ax.set_xticks([])
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    fig.suptitle("1-month close", fontsize=10)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    return path


def build_message():
    syms = scanner_symbols(get(SCANNER_URL))
    if not syms:
        raise RuntimeError("スキャナ表から銘柄を取得できませんでした(画面形式が変わった可能性)")
    ups, failed, hist = [], [], {}
    for s in syms:
        try:
            cl = history(get(HISTORY_URL.format(s.lower())))
            r = day_change(cl)
        except Exception:
            r = None
        if r is None: failed.append(s)
        elif r[2] > 0: ups.append((s, r[0], r[1], r[2])); hist[s] = cl
        time.sleep(1)
    ups.sort(key=lambda x: -x[3])
    build_message.series = [(s, hist[s], p) for s, _, _, p in ups]
    asof = ups[0][1] if ups else "-"
    lines = [f"出来高急増 {len(syms)}銘柄中、前日比プラス {len(ups)}銘柄({asof}終値)"]
    lines += [f"{s} ${c:,.2f} {p:+.2f}%" for s, _, c, p in ups]
    if failed: lines.append("株価取得失敗: " + ", ".join(failed))
    return "\n".join(lines)


def send_image(path, title):
    import email.header
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    h = {"Filename": "charts.png", "Title": email.header.Header(title, "utf-8").encode()}
    if os.environ.get("NTFY_TOKEN"): h["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    with open(path, "rb") as f:
        req = urllib.request.Request(f"{server}/{topic}", data=f.read(), headers=h, method="PUT")
    urllib.request.urlopen(req, timeout=60).read()


def send(title, text, priority="default"):
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    import email.header
    # ヘッダはASCII限定のため、日本語タイトルはRFC2047でエンコードする
    h = {"Title": email.header.Header(title, "utf-8").encode(), "Priority": priority}
    if os.environ.get("NTFY_TOKEN"): h["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(f"{server}/{topic}", data=text.encode("utf-8"), headers=h)
    urllib.request.urlopen(req, timeout=30).read()


def main():
    dry = "--dry-run" in sys.argv
    try:
        msg = build_message(); title = "出来高急増・前日比プラス"; pri = "default"
    except Exception as e:
        msg = f"取得に失敗しました: {e}"; title = "出来高アラート エラー"; pri = "high"
    print(msg)
    if dry: return
    send(title, msg, pri)
    series = getattr(build_message, "series", [])
    if series:
        try:
            img = make_chart(series)
            if img: send_image(img, "1か月チャート(前日比プラス銘柄)")
        except Exception as e:
            print("チャート送信失敗:", e)


if __name__ == "__main__":
    main()
