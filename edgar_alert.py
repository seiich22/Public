#!/usr/bin/env python3
"""
EDGAR 大口買い付けアラート

銘柄を限定せず、全上場企業の新規提出を確認して、次のものだけを通知します。
  - Form 4: 市場での買い付け(取引コード P)の合計額が MIN_PURCHASE_USD 以上
  - SCHEDULE 13D: 5%超の取得(全件)

使い方:
  1. 下の「設定」を確認する(通知の下限額など)
  2. 環境変数 SEC_USER_AGENT に "名前 メールアドレス" を設定する
  3. python3 edgar_alert.py          # 常駐して一定間隔で確認
     python3 edgar_alert.py --once   # 1回だけ確認(GitHub Actions・cron向け)
     python3 edgar_alert.py --test   # テスト通知を1件送る

初回は既存の提出を既読にするだけで、通知はしません。
標準ライブラリのみで動作します(Python 3.8以上)。
"""

import argparse
import json
import os
import re
import smtplib
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage

# ========== 設定 ==========

# SECは連絡先入りのUser-Agentを必須としています(例: "Taro Yamada taro@example.com")。
# 公開リポジトリに載せても連絡先が見えないよう、環境変数 SEC_USER_AGENT から読みます。
USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()

# Form 4 で通知する買い付け額の下限(米ドル)。1件の提出内の合計で判定します。
MIN_PURCHASE_USD = 1_000_000

# 5%超の取得を知らせる書類。訂正分も受け取るなら "SCHEDULE 13D/A" を追加。
# 受動的な大株主(インデックスファンド等)も知りたければ "SCHEDULE 13G" を追加。
# (旧称は "SC 13D" / "SC 13G")
STAKE_FORMS = ["SCHEDULE 13D"]

# 金額に関係なく、提出があれば必ず通知する銘柄(CIK 10桁)。不要なら空のまま。
ALWAYS_NOTIFY = [
    # "0001045810",  # NVIDIA
]

# 確認間隔(秒)。SECに負荷をかけないよう、300秒以上を推奨します。
INTERVAL = 300

# 1回の確認でさかのぼる最大ページ数(1ページ100件)。
# 提出が集中する時間帯の取りこぼしを防ぎます。
MAX_PAGES = 5

# 既読の提出を記録するファイルと、保持する最大件数
SEEN_FILE = "edgar_seen.json"
SEEN_LIMIT = 20000

# 通知先(使うものだけ環境変数を設定してください)
#   iPhone(ntfyアプリ): NTFY_TOPIC
#   Slack:  SLACK_WEBHOOK_URL
#   メール: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
# いずれも未設定なら、画面表示のみです。

# ==========================

ATOM = "{http://www.w3.org/2005/Atom}"
SEC_WAIT = 0.15  # SECの上限(毎秒10回)を超えないための待ち時間


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = res.read()
    time.sleep(SEC_WAIT)
    return data


# ---------- 新着一覧(Atomフィード) ----------

def fetch_feed(form_type, start=0):
    params = {
        "action": "getcurrent",
        "type": form_type,
        "company": "",
        "dateb": "",
        "owner": "include",
        "start": str(start),
        "count": "100",
        "output": "atom",
    }
    url = "https://www.sec.gov/cgi-bin/browse-edgar?" + urllib.parse.urlencode(params)
    return ET.fromstring(http_get(url))


def parse_feed(root, form_type):
    """フィードを提出(受付番号)単位にまとめる。

    同じ提出が「発行会社」と「提出者」の2件で載るため、受付番号で1つにする。
    戻り値は新しい順の [(受付番号, 情報dict), ...]。
    """
    filings = {}
    for e in root.findall(ATOM + "entry"):
        cat = e.find(ATOM + "category")
        term = (cat.get("term") if cat is not None else "").strip()
        # type=4 は前方一致で 4/A なども混ざるため、完全一致で絞る
        if term.upper() != form_type.upper():
            continue
        entry_id = e.findtext(ATOM + "id") or ""
        m = re.search(r"accession-number=([\d-]+)", entry_id)
        if not m:
            continue
        acc = m.group(1)
        link_el = e.find(ATOM + "link")
        link = link_el.get("href") if link_el is not None else ""
        title = (e.findtext(ATOM + "title") or "").strip()
        f = filings.setdefault(acc, {
            "form": term,
            "link": link,
            "updated": (e.findtext(ATOM + "updated") or "").strip(),
            "titles": [],
        })
        f["titles"].append(title)
    return list(filings.items())


def collect_new(form_type, seen, first_run):
    """既読に追いつくまで、ページをさかのぼって新着を集める。"""
    new = []
    pages = 1 if first_run else MAX_PAGES
    for page in range(pages):
        items = parse_feed(fetch_feed(form_type, page * 100), form_type)
        caught_up = False
        for acc, info in items:
            if acc in seen:
                caught_up = True
            elif all(acc != a for a, _ in new):
                new.append((acc, info))
        if caught_up or len(items) == 0:
            break
    else:
        if not first_run:
            print(f"[注意] {form_type}: {MAX_PAGES}ページ分さかのぼっても既読に届きませんでした。"
                  "取りこぼしがあるかもしれません。")
    new.reverse()  # 古い順に処理する
    return new


# ---------- Form 4 の中身を読む ----------

def fetch_form4_xml(link, acc):
    """提出の全文テキストから、Form 4 本体のXMLを取り出す。"""
    m = re.search(r"/Archives/edgar/data/(\d+)/", link)
    if not m:
        raise ValueError(f"リンクからCIKを取得できません: {link}")
    url = f"https://www.sec.gov/Archives/edgar/data/{m.group(1)}/{acc}.txt"
    text = http_get(url).decode("utf-8", errors="replace")
    x = re.search(r"<XML>(.*?)</XML>", text, re.S | re.I)
    if not x:
        raise ValueError("XML部分が見つかりません")
    return ET.fromstring(x.group(1).strip())


def _text(el, path):
    v = el.findtext(path)
    return v.strip() if v else ""


def _num(el, path):
    try:
        return float(_text(el, path).replace(",", ""))
    except ValueError:
        return 0.0


def summarize_form4(doc):
    """市場での買い付け(コード P)を集計する。"""
    issuer = doc.find("issuer")
    owners = []
    for ro in doc.findall("reportingOwner"):
        name = _text(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if _text(rel, "isDirector") in ("1", "true"):
                roles.append("取締役")
            if _text(rel, "isOfficer") in ("1", "true"):
                roles.append(_text(rel, "officerTitle") or "役員")
            if _text(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10%超株主")
            if _text(rel, "isOther") in ("1", "true"):
                roles.append(_text(rel, "otherText") or "その他")
        owners.append(f"{name}({'・'.join(roles)})" if roles else name)

    shares = value = 0.0
    dates = set()
    for t in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(t, "transactionCoding/transactionCode") != "P":
            continue
        if _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value") == "D":
            continue
        s = _num(t, "transactionAmounts/transactionShares/value")
        p = _num(t, "transactionAmounts/transactionPricePerShare/value")
        shares += s
        value += s * p
        d = _text(t, "transactionDate/value")
        if d:
            dates.add(d[:10])

    return {
        "issuer": _text(issuer, "issuerName") if issuer is not None else "",
        "ticker": _text(issuer, "issuerTradingSymbol") if issuer is not None else "",
        "cik": (_text(issuer, "issuerCik") if issuer is not None else "").zfill(10),
        "owners": owners,
        "shares": shares,
        "value": value,
        "dates": sorted(dates),
    }


# ---------- 通知 ----------

def notify(text, link=""):
    print(text, flush=True)
    print("-" * 40, flush=True)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        headers = {"Title": "EDGAR alert", "Priority": "high", "Tags": "moneybag"}
        if link:
            headers["Click"] = link  # 通知をタップするとEDGARの書類が開く
        req = urllib.request.Request(
            "https://ntfy.sh/" + urllib.parse.quote(topic),
            data=text.encode("utf-8"), headers=headers,
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as ex:
            print(f"[ntfy通知失敗] {ex}")

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as ex:
            print(f"[Slack通知失敗] {ex}")

    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if host and to:
        msg = EmailMessage()
        msg["Subject"] = "EDGAR大口買い付けアラート"
        msg["From"] = os.environ.get("SMTP_USER", to)
        msg["To"] = to
        msg.set_content(text)
        try:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        except Exception as ex:
            print(f"[メール通知失敗] {ex}")


def format_form4(s, link):
    name = s["issuer"] + (f" ({s['ticker']})" if s["ticker"] else "")
    avg = s["value"] / s["shares"] if s["shares"] else 0
    lines = [
        f"[Form 4 買い付け] {name}",
        f"買い手: {' / '.join(s['owners'])}",
        f"金額: ${s['value']:,.0f}({s['shares']:,.0f}株 × 平均${avg:,.2f})",
    ]
    if s["dates"]:
        lines.append(f"取引日: {', '.join(s['dates'])}")
    lines.append(link)
    return "\n".join(lines)


def in_always_list(text):
    return any(c in text for c in ALWAYS_NOTIFY)


# ---------- 既読管理 ----------

def load_seen():
    """既読の受付番号を、記録した順番を保ったまま読み込む。

    dict はキーの挿入順を保持し、in による検索も set と同じく高速なので、
    「順序つきの集合」として使う。
    """
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return dict.fromkeys(json.load(f))
        except (json.JSONDecodeError, OSError) as ex:
            print(f"[既読ファイル読込失敗、空から開始] {ex}")
    return {}


def save_seen(seen):
    """古いものから削って、新しい SEEN_LIMIT 件だけ残す。"""
    ids = list(seen)
    if len(ids) > SEEN_LIMIT:
        for old in ids[:-SEEN_LIMIT]:
            del seen[old]
        ids = ids[-SEEN_LIMIT:]
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ids, f)
    os.replace(tmp, SEEN_FILE)


# ---------- 1回分の確認 ----------

def check_form4(seen, first_run):
    count = 0
    for acc, info in collect_new("4", seen, first_run):
        if first_run:
            seen[acc] = None
            continue
        try:
            s = summarize_form4(fetch_form4_xml(info["link"], acc))
        except ET.ParseError as ex:
            print(f"[Form 4 解析失敗、スキップ] {acc}: {ex}")
            seen[acc] = None
            continue
        except Exception as ex:
            # 通信エラーなどは既読にせず、次回やり直す
            print(f"[Form 4 取得失敗、次回再試行] {acc}: {ex}")
            continue
        seen[acc] = None
        if s["value"] >= MIN_PURCHASE_USD or (s["cik"] in ALWAYS_NOTIFY):
            notify(format_form4(s, info["link"]), info["link"])
            count += 1
    return count


def check_stakes(seen, first_run):
    count = 0
    for form_type in STAKE_FORMS:
        for acc, info in collect_new(form_type, seen, first_run):
            seen[acc] = None
            if first_run:
                continue
            # 「(Subject)」が取得された側の会社、「(Filed by)」が取得した側
            titles = sorted(info["titles"], key=lambda t: "(Subject)" not in t)
            body = "\n".join(t.split(" - ", 1)[-1] for t in titles)
            notify(f"[{info['form']} 5%超の取得]\n{body}\n{info['updated']}\n{info['link']}",
                   info["link"])
            count += 1
    return count


def check_once(seen, first_run):
    count = 0
    for fn in (check_form4, check_stakes):
        try:
            count += fn(seen, first_run)
        except Exception as ex:
            print(f"[取得失敗] {fn.__name__}: {ex}")
    save_seen(seen)
    if first_run:
        print("初回のため、既存の提出を既読にしました。次回から新着を通知します。")
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1回だけ確認して終了")
    parser.add_argument("--test", action="store_true", help="テスト通知を1件送って終了")
    args = parser.parse_args()

    if args.test:
        notify("EDGARアラートのテスト通知です。これが届けば設定は完了です。",
               "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent")
        return

    if not USER_AGENT or "@" not in USER_AGENT:
        print('環境変数 SEC_USER_AGENT に "名前 メールアドレス" を設定してください。')
        raise SystemExit(1)

    first_run = not os.path.exists(SEEN_FILE)
    seen = load_seen()

    if args.once:
        check_once(seen, first_run)
        return

    print(f"監視開始(間隔 {INTERVAL} 秒、Form 4 は ${MIN_PURCHASE_USD:,} 以上)。Ctrl+C で終了。")
    while True:
        check_once(seen, first_run)
        first_run = False
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
