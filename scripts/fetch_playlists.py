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


def _walk(node, found):
    if isinstance(node, dict):
        r = node.get("playlistVideoRenderer")
        if isinstance(r, dict) and r.get("videoId"):
            found.append(r["videoId"])
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


def video_info(vid):
    html = get(WATCH.format(vid))
    date = re.search(r'"(?:publishDate|uploadDate)":"(\d{4}-\d{2}-\d{2})', html)
    title = re.search(r'"videoDetails":\{"videoId":"[^"]+","title":"((?:[^"\\]|\\.)*)"', html)
    owner = re.search(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"', html)
    return {
        "youtube": vid,
        "title": _unescape(title.group(1)) if title else "",
        "client": _unescape(owner.group(1)) if owner else "",
        "date": date.group(1) if date else "",
    }


def from_page(pid):
    html = get(PLAYLIST.format(pid))
    ids = []
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if m:
        try:
            _walk(json.loads(m.group(1)), ids)
        except Exception:
            pass
    if not ids:  # 形式が変わっていたら、ページ内の動画IDを順に拾う
        ids = re.findall(r'"videoId":"([\w-]{11})"', html)
    seen, uniq = set(), []
    for v in ids:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    items = []
    for vid in uniq[:MAX_PER_PLAYLIST]:
        try:
            items.append(video_info(vid))
        except Exception as ex:
            print(f"  動画 {vid} の情報を取れませんでした: {ex}")
        time.sleep(0.5)
    return items


def fetch(pid):
    try:
        items = from_feed(pid)
        if items:
            return items
    except (urllib.error.HTTPError, urllib.error.URLError, ET.ParseError) as ex:
        print(f"{pid}: フィードが使えないのでページから取得します({ex})")
    return from_page(pid)


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
    for f in failed:
        print("取得に失敗:", f)
    if not out:
        # 1本も取れなかったときは前回のデータを消さない
        sys.exit(1)
    out.sort(key=lambda w: w["date"], reverse=True)
    (ROOT / "works-auto.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(out)}本を書き出しました")


if __name__ == "__main__":
    main()
