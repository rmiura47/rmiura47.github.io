"""YouTube再生リストの動画を集めて works-auto.json を書き出す。

playlists.json の再生リストIDを読み、各再生リストの公開フィード
(APIキー不要)から動画を取得する。フィードは再生リストごとに最新15本まで。
GitHub Actions から定期実行される。
"""
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def fetch(playlist_id):
    req = urllib.request.Request(FEED.format(playlist_id), headers={"User-Agent": "portfolio-updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        items.append({
            "youtube": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "client": e.findtext("a:author/a:name", default="", namespaces=NS),
            "date": e.findtext("a:published", default="", namespaces=NS)[:10],
        })
    return items


def main():
    conf = json.loads((ROOT / "playlists.json").read_text(encoding="utf-8"))
    out, failed = [], []
    for pid in conf.get("playlists", []):
        if not pid:
            continue
        try:
            for it in fetch(pid):
                out.append({"playlist": pid, **it})
        except Exception as ex:  # 1つの再生リストが失敗しても他は続ける
            failed.append(f"{pid}: {ex}")
    if failed and not out:
        # 全部失敗したときは前回のデータを消さない
        print("取得に失敗:", *failed, sep="\n  ")
        sys.exit(1)
    for f in failed:
        print("取得に失敗:", f)
    out.sort(key=lambda w: w["date"], reverse=True)
    (ROOT / "works-auto.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(out)}本を書き出しました")


if __name__ == "__main__":
    main()
