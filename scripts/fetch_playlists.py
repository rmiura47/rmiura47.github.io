"""YouTube再生リストの動画を集めて works-auto.json を書き出す。

playlists.json の再生リストIDごとに、まず公開フィード(RSS)を試し、
使えないとき(404など)は再生リストのページから動画IDを読み取る。
投稿日とタイトルは各動画のページから取る。APIキーは不要。
GitHub Actions から定期実行される。
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"
PLAYLIST = "https://www.youtube.com/playlist?list={}&hl=ja"
WATCH = "https://www.youtube.com/watch?v={}&hl=ja"
MAX_PER_PLAYLIST = 60  # 1つの再生リストから見る動画の上限
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
    "Cookie": "CONSENT=YES+1; SOCS=CAI",
}


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def from_feed(pid):
    root = ET.fromstring(get(FEED.format(pid)))
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if vid:
            items.append({
                "youtube": vid,
                "title": e.findtext("a:title", default="", namespaces=NS),
                "client": e.findtext("a:author/a:name", default="", namespaces=NS),
                "date": e.findtext("a:published", default="", namespaces=NS)[:10],
            })
    return items


REL = [
    (r"(\d+)\s*秒前", 0), (r"(\d+)\s*分前", 0), (r"(\d+)\s*時間前", 0),
    (r"(\d+)\s*日前", 1), (r"(\d+)\s*週間前", 7), (r"(\d+)\s*(?:か月|ヶ月|カ月)前", 30), (r"(\d+)\s*年前", 365),
    (r"(\d+)\s*seconds? ago", 0), (r"(\d+)\s*minutes? ago", 0), (r"(\d+)\s*hours? ago", 0),
    (r"(\d+)\s*days? ago", 1), (r"(\d+)\s*weeks? ago", 7), (r"(\d+)\s*months? ago", 30), (r"(\d+)\s*years? ago", 365),
]


def _text(t):
    if not isinstance(t, dict):
        return ""
    if "simpleText" in t:
        return t["simpleText"]
    return "".join(r.get("text", "") for r in t.get("runs", []))


def _rel_date(text):
    """「3日前」「2 weeks ago」などからおおよその投稿日を出す(並び順用)"""
    import datetime
    for pat, days in REL:
        m = re.search(pat, text)
        if m:
            d = datetime.date.today() - datetime.timedelta(days=int(m.group(1)) * days)
            return d.isoformat()
    return ""


def _strings(node, out):
    """表示用の文字列(text / content / simpleText)をすべて集める"""
    if isinstance(node, dict):
        for k in ("simpleText", "content", "text"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for v in node.values():
            if isinstance(v, (dict, list)):
                _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)
    return out


def _walk(node, found):
    if isinstance(node, dict):
        r = node.get("playlistVideoRenderer")  # 旧形式
        if isinstance(r, dict) and r.get("videoId"):
            found.append({
                "youtube": r["videoId"],
                "title": _text(r.get("title")),
                "client": _text(r.get("shortBylineText")),
                "date": _rel_date(_text(r.get("videoInfo"))),
            })
            return
        r = node.get("lockupViewModel")  # 新形式
        if isinstance(r, dict) and len(str(r.get("contentId", ""))) == 11:
            md = (r.get("metadata") or {}).get("lockupMetadataViewModel") or {}
            title = ((md.get("title") or {}).get("content")) or ""
            rows = _strings(md.get("metadata"), [])
            found.append({
                "youtube": r["contentId"],
                "title": title,
                "client": rows[0] if rows else "",
                "date": _rel_date(" ".join(rows)),
            })
            return
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def _unescape(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


def _post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={**HEADERS, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def video_info(vid):
    """タイトル・チャンネル名・公開日を取る。oEmbed → InnerTube(player) → 動画ページの順に試す"""
    info = {"youtube": vid, "title": "", "client": "", "date": ""}
    try:
        o = json.loads(get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"))
        info["title"], info["client"] = o.get("title", ""), o.get("author_name", "")
    except Exception as ex:
        print(f"  oEmbed失敗 {vid}: {ex}")
    for client in ({"clientName": "WEB", "clientVersion": "2.20250101.00.00"},
                   {"clientName": "MWEB", "clientVersion": "2.20250101.00.00"}):
        if info["date"]:
            break
        try:
            d = _post_json("https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                           {"videoId": vid, "context": {"client": {**client, "hl": "ja", "gl": "JP"}}})
            mf = (d.get("microformat") or {}).get("playerMicroformatRenderer") or {}
            info["date"] = (mf.get("publishDate") or mf.get("uploadDate") or "")[:10]
            vd = d.get("videoDetails") or {}
            info["title"] = info["title"] or vd.get("title", "")
            info["client"] = info["client"] or vd.get("author", "")
        except Exception as ex:
            print(f"  player失敗 {vid}: {ex}")
    if not info["date"]:
        try:
            html = get(WATCH.format(vid))
            m = re.search(r'"(?:publishDate|uploadDate)":"(\d{4}-\d{2}-\d{2})', html) or \
                re.search(r'itemprop="(?:datePublished|uploadDate)" content="(\d{4}-\d{2}-\d{2})', html)
            if m:
                info["date"] = m.group(1)
        except Exception as ex:
            print(f"  動画ページ失敗 {vid}: {ex}")
    return info


def from_page(pid):
    html = get(PLAYLIST.format(pid))
    items = []
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if m:
        try:
            _walk(json.loads(m.group(1)), items)
        except Exception as ex:
            print(f"  ページの読み取りに失敗: {ex}")
    if not items:
        print("  ページの形式を読めなかったので動画IDだけ拾います")
        items = [{"youtube": v, "title": "", "client": "", "date": ""}
                 for v in re.findall(r'"videoId":"([\w-]{11})"', html)]
    else:
        print(f"  ページから{len(items)}本を読み取り(日付あり {sum(1 for i in items if i['date'])}本)")
    seen, uniq = set(), []
    for it in items:
        if it["youtube"] not in seen:
            seen.add(it["youtube"])
            uniq.append(it)
    # タイトルか日付が欠けている動画だけ、動画ページから補う
    for it in uniq[:MAX_PER_PLAYLIST]:
        if it["title"] and it["date"]:
            continue
        try:
            extra = video_info(it["youtube"])
            for k in ("title", "client", "date"):
                if not it[k] and extra[k]:
                    it[k] = extra[k]
        except Exception:
            pass
        time.sleep(0.5)
    uniq = uniq[:MAX_PER_PLAYLIST]
    # 日付が大まかで同じ日になる回は、再生リストの並び順で新しい回を先にする
    dated = [it["date"] for it in uniq if it["date"]]
    ascending = len(dated) < 2 or dated[0] <= dated[-1]
    for i, it in enumerate(uniq):
        it["order"] = i if ascending else -i
    return uniq


def fetch(pid):
    """再生リストのページから取る(最大60本・新しい回も含む)。だめならフィード(最初の15本だけ)"""
    try:
        items = from_page(pid)
        if items:
            return items
    except Exception as ex:
        print(f"{pid}: ページから取れないのでフィードを使います({ex})")
    return from_feed(pid)


def main():
    conf = json.loads((ROOT / "playlists.json").read_text(encoding="utf-8"))
    out, failed = [], []
    for pid in conf.get("playlists", []):
        if not pid:
            continue
        try:
            items = fetch(pid)
            print(f"{pid}: {len(items)}本")
            out.extend({"playlist": pid, **it} for it in items)
        except Exception as ex:  # 1つの再生リストが失敗しても他は続ける
            failed.append(f"{pid}: {ex}")
    # チャンネルの動画から、タイトルにキーワードを含むものをシリーズに足す(再生リストと重複は除く)
    uploads_cache = {}
    norm = lambda t: re.sub(r"\s+", "", t).lower()
    for f in conf.get("channel_filters", []):
        up, kw, pl = f.get("uploads"), f.get("keyword", ""), f.get("playlist")
        if not (up and kw and pl):
            continue
        try:
            if up not in uploads_cache:
                uploads_cache[up] = from_page(up)
            have = {w["youtube"] for w in out if w.get("playlist") == pl}
            have_titles = {norm(w.get("title", "")) for w in out if w.get("playlist") == pl}
            added = 0
            for it in uploads_cache[up]:
                if norm(kw) in norm(it.get("title", "")) and it["youtube"] not in have and norm(it.get("title", "")) not in have_titles:
                    out.append({"playlist": pl, **{k: v for k, v in it.items() if k != "order"}, "order": 0})
                    have.add(it["youtube"]); added += 1
            print(f"{kw}: チャンネルから{added}本を追加")
        except Exception as ex:
            failed.append(f"{kw}: {ex}")

    # 単発の動画(videos.json)。一度取れた情報は使い回す
    prev = {}
    try:
        for w in json.loads((ROOT / "works-auto.json").read_text(encoding="utf-8")):
            if w.get("single") and w.get("title"):
                prev[w["youtube"]] = w
    except Exception:
        pass
    vconf_path = ROOT / "videos.json"
    if vconf_path.exists():
        vconf = json.loads(vconf_path.read_text(encoding="utf-8"))
        vids = vconf.get("videos", [])
        got = 0
        for v in vids:
            v = {"id": v} if isinstance(v, str) else v
            vid = v["id"]
            base = prev.get(vid)
            if not base or not base.get("title"):
                try:
                    base = {"playlist": "", "single": True, **video_info(vid)}
                except Exception as ex:
                    failed.append(f"動画 {vid}: {ex}")
                    continue
                time.sleep(0.4)
            item = dict(base)
            if v.get("date"):
                item["date"] = v["date"]          # 手動の公開日を優先
            cmap = vconf.get("channel_artist", {})
            item["artists"] = v.get("artists") or [cmap.get(item.get("client", ""), item.get("client", ""))]
            if v.get("role"):
                item["role"] = v["role"]
            else:
                item.pop("role", None)
            out.append(item); got += 1
        print(f"単発の動画: {got}/{len(vids)}本")
    for f in failed:
        print("取得に失敗:", f)
    if not out:
        # 1本も取れなかったときは前回のデータを消さない
        sys.exit(1)
    out.sort(key=lambda w: (w["date"], w.get("order", 0)), reverse=True)
    (ROOT / "works-auto.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(out)}本を書き出しました")


if __name__ == "__main__":
    main()
