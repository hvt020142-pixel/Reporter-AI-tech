#!/usr/bin/env python3
"""
fetch_news.py — Bộ thu thập tin AI "gần thời gian thực" từ nguồn sơ cấp.

Mục tiêu: thay vì dựa vào các trang tổng hợp (dễ sai/giật gân), script này
kéo trực tiếp RSS/Atom của blog hãng + báo uy tín + arXiv, lọc theo mốc
thời gian xuất bản (pubDate), khử trùng lặp, và xuất ra:

  - feeds/latest.json : dữ liệu máy đọc (có timestamp ISO) — dùng làm
                        "nguồn sự thật" cho bước viết báo cáo bằng Claude.
  - feeds/latest.md   : bản tóm tắt cho người đọc, mới nhất lên đầu.

Thiết kế:
  * Chỉ dùng thư viện chuẩn (urllib + xml.etree) — KHÔNG cần pip install,
    chạy được trên runner GitHub Actions sạch.
  * Best-effort: feed nào lỗi thì bỏ qua và ghi nhận, không làm hỏng cả lần chạy.
  * Lọc theo LOOKBACK_HOURS (mặc định 72h, chỉnh qua biến môi trường).

Cách dùng:
  python3 scripts/fetch_news.py            # thu thập thật (cần mạng)
  python3 scripts/fetch_news.py --selftest # kiểm thử bộ phân tích, không cần mạng

Biến môi trường:
  LOOKBACK_HOURS  (mặc định "72")   — cửa sổ thời gian tính bằng giờ
  MAX_PER_FEED    (mặc định "40")   — số item tối đa lấy mỗi feed
  OUT_DIR         (mặc định "feeds")— thư mục xuất
  REQUEST_TIMEOUT (mặc định "25")   — timeout mỗi request (giây)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# Cấu hình nguồn. tier="primary" = blog hãng/arXiv (sơ cấp); "reputable" = báo uy tín.
# Chỉnh sửa danh sách này để thêm/bớt nguồn. Mỗi mục: (tên, url, lĩnh vực, tier)
# --------------------------------------------------------------------------
FEEDS = [
    # --- Blog hãng (nguồn sơ cấp) ---
    ("OpenAI",            "https://openai.com/news/rss.xml",                              "Mô hình & LLM",        "primary"),
    ("Anthropic",         "https://www.anthropic.com/rss.xml",                            "Mô hình & LLM",        "primary"),
    ("Google DeepMind",   "https://deepmind.google/blog/rss.xml",                         "Mô hình & LLM",        "primary"),
    ("Google (Keyword AI)","https://blog.google/technology/ai/rss/",                      "Mô hình & LLM",        "primary"),
    ("Microsoft AI",      "https://blogs.microsoft.com/ai/feed/",                         "Agent & ứng dụng",     "primary"),
    ("NVIDIA",            "https://blogs.nvidia.com/feed/",                               "Hạ tầng & phần cứng",  "primary"),
    ("Meta AI",           "https://ai.meta.com/blog/rss/",                                "Mô hình & LLM",        "primary"),
    ("Hugging Face",      "https://huggingface.co/blog/feed.xml",                         "Mô hình mở",           "primary"),
    ("Stability AI",      "https://stability.ai/news?format=rss",                         "Đa phương thức",       "primary"),

    # --- arXiv (nghiên cứu sơ cấp) ---
    ("arXiv cs.AI",       "http://export.arxiv.org/rss/cs.AI",                            "Nghiên cứu",           "primary"),
    ("arXiv cs.CL",       "http://export.arxiv.org/rss/cs.CL",                            "Nghiên cứu",           "primary"),
    ("arXiv cs.LG",       "http://export.arxiv.org/rss/cs.LG",                            "Nghiên cứu",           "primary"),
    ("arXiv cs.CV",       "http://export.arxiv.org/rss/cs.CV",                            "Nghiên cứu",           "primary"),

    # --- Báo / tạp chí uy tín ---
    ("TechCrunch AI",     "https://techcrunch.com/category/artificial-intelligence/feed/","Tin tức",             "reputable"),
    ("VentureBeat AI",    "https://venturebeat.com/category/ai/feed/",                    "Tin tức",              "reputable"),
    ("The Verge AI",      "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml","Tin tức",          "reputable"),
    ("Ars Technica AI",   "https://arstechnica.com/ai/feed/",                             "Tin tức",              "reputable"),
    ("MIT Tech Review",   "https://www.technologyreview.com/feed/",                       "Phân tích",            "reputable"),
    ("IEEE Spectrum AI",  "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss","Phân tích",        "reputable"),
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; ReporterAI-NewsBot/1.0; "
    "+https://github.com/hvt020142-pixel/reporter-ai-tech)"
)

# Atom namespace
ATOM_NS = "{http://www.w3.org/2005/Atom}"


# --------------------------------------------------------------------------
# Phân tích thời gian
# --------------------------------------------------------------------------
def parse_date(text: str | None) -> datetime | None:
    """Phân tích pubDate (RSS, RFC822) hoặc updated/published (Atom, ISO8601)."""
    if not text:
        return None
    text = text.strip()
    # Thử RFC822 (RSS): "Fri, 13 Jun 2026 10:30:00 GMT"
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    # Thử ISO8601 (Atom): "2026-06-13T10:30:00Z"
    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def strip_html(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --------------------------------------------------------------------------
# Phân tích feed (RSS 2.0 hoặc Atom) -> danh sách item dict
# --------------------------------------------------------------------------
def parse_feed(xml_bytes: bytes, source: str, category: str, tier: str) -> list[dict]:
    items: list[dict] = []
    root = ET.fromstring(xml_bytes)

    # RSS 2.0: <rss><channel><item>...
    channel = root.find("channel")
    if channel is not None:
        for it in channel.findall("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")
            desc = it.findtext("description")
            dt = parse_date(pub)
            items.append(_mk_item(title, link, dt, desc, source, category, tier))
        return items

    # Atom: <feed><entry>...
    if root.tag.endswith("feed"):
        for e in root.findall(f"{ATOM_NS}entry"):
            title = (e.findtext(f"{ATOM_NS}title") or "").strip()
            link = ""
            for ln in e.findall(f"{ATOM_NS}link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" or not link:
                    link = ln.get("href", "") or link
            pub = e.findtext(f"{ATOM_NS}published") or e.findtext(f"{ATOM_NS}updated")
            desc = e.findtext(f"{ATOM_NS}summary") or e.findtext(f"{ATOM_NS}content")
            dt = parse_date(pub)
            items.append(_mk_item(title, link, dt, desc, source, category, tier))
        return items

    # RSS 1.0 (RDF) — arXiv dùng dạng này: <rdf:RDF>...<item>
    rdf_items = root.findall("{http://purl.org/rss/1.0/}item")
    if rdf_items:
        for it in rdf_items:
            title = (it.findtext("{http://purl.org/rss/1.0/}title") or "").strip()
            link = (it.findtext("{http://purl.org/rss/1.0/}link") or "").strip()
            pub = it.findtext("{http://purl.org/dc/elements/1.1/}date")
            desc = it.findtext("{http://purl.org/rss/1.0/}description")
            dt = parse_date(pub)
            items.append(_mk_item(title, link, dt, desc, source, category, tier))
        return items

    return items


def _mk_item(title, link, dt, desc, source, category, tier) -> dict:
    return {
        "title": title,
        "url": link,
        "source": source,
        "category": category,
        "tier": tier,
        "published_iso": dt.astimezone(timezone.utc).isoformat() if dt else None,
        "published_ts": dt.timestamp() if dt else 0.0,
        "summary": strip_html(desc),
    }


# --------------------------------------------------------------------------
# Tải feed
# --------------------------------------------------------------------------
def fetch(url: str, timeout: int) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


# --------------------------------------------------------------------------
# Chạy thu thập
# --------------------------------------------------------------------------
def collect() -> dict:
    lookback = int(os.environ.get("LOOKBACK_HOURS", "72"))
    max_per = int(os.environ.get("MAX_PER_FEED", "40"))
    timeout = int(os.environ.get("REQUEST_TIMEOUT", "25"))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback)

    all_items: list[dict] = []
    ok, failed = [], []

    for name, url, category, tier in FEEDS:
        try:
            raw = fetch(url, timeout)
            parsed = parse_feed(raw, name, category, tier)
            # Lọc theo thời gian: giữ item có pubDate >= cutoff; item không có ngày -> giữ tạm (đánh dấu)
            kept = []
            for it in parsed[:max_per]:
                if it["published_ts"] == 0.0:
                    it["undated"] = True
                    kept.append(it)
                elif it["published_ts"] >= cutoff.timestamp():
                    kept.append(it)
            all_items.extend(kept)
            ok.append({"source": name, "url": url, "items_kept": len(kept), "items_total": len(parsed)})
            print(f"[OK]   {name}: {len(kept)}/{len(parsed)} item trong {lookback}h")
        except (HTTPError, URLError, ET.ParseError, ValueError, TimeoutError) as e:
            failed.append({"source": name, "url": url, "error": f"{type(e).__name__}: {e}"})
            print(f"[FAIL] {name}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(0.4)  # lịch sự với máy chủ

    # Khử trùng lặp theo (tiêu đề chuẩn hóa) hoặc URL
    seen_titles, seen_urls, deduped = set(), set(), []
    for it in sorted(all_items, key=lambda x: x["published_ts"], reverse=True):
        nt = norm_title(it["title"])
        if not nt:
            continue
        if nt in seen_titles or (it["url"] and it["url"] in seen_urls):
            continue
        seen_titles.add(nt)
        if it["url"]:
            seen_urls.add(it["url"])
        deduped.append(it)

    return {
        "generated_at": now.isoformat(),
        "lookback_hours": lookback,
        "item_count": len(deduped),
        "sources_ok": ok,
        "sources_failed": failed,
        "items": deduped,
    }


def write_outputs(data: dict) -> None:
    out_dir = os.environ.get("OUT_DIR", "feeds")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Bản markdown gom theo lĩnh vực
    lines = []
    gen = data["generated_at"]
    lines.append(f"# Dòng tin AI gần thời gian thực\n")
    lines.append(f"*Cập nhật: {gen} (UTC) — cửa sổ {data['lookback_hours']}h — {data['item_count']} mục — nguồn sơ cấp/uy tín.*\n")
    n_fail = len(data["sources_failed"])
    if n_fail:
        names = ", ".join(s["source"] for s in data["sources_failed"])
        lines.append(f"> ⚠️ {n_fail} nguồn lỗi lần này (bỏ qua): {names}\n")

    by_cat: dict[str, list[dict]] = {}
    for it in data["items"]:
        by_cat.setdefault(it["category"], []).append(it)

    for cat in sorted(by_cat):
        lines.append(f"\n## {cat}\n")
        for it in by_cat[cat]:
            when = it["published_iso"] or "(không rõ ngày)"
            tier = "🟢" if it["tier"] == "primary" else "🔵"
            lines.append(f"- {tier} **{it['title']}** — {it['source']} — `{when}`")
            if it["url"]:
                lines.append(f"  - {it['url']}")
            if it["summary"]:
                lines.append(f"  - {it['summary']}")

    lines.append("\n---\n*🟢 nguồn sơ cấp (blog hãng/arXiv) · 🔵 báo uy tín. Tạo tự động bởi `scripts/fetch_news.py`.*\n")

    with open(os.path.join(out_dir, "latest.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# --------------------------------------------------------------------------
# Kiểm thử bộ phân tích (không cần mạng)
# --------------------------------------------------------------------------
def selftest() -> int:
    rss = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Demo</title>
      <item><title>Model X ra mat</title><link>https://ex.com/x</link>
        <pubDate>Fri, 13 Jun 2026 10:30:00 GMT</pubDate>
        <description>&lt;p&gt;Tom tat &lt;b&gt;hay&lt;/b&gt;.&lt;/p&gt;</description></item>
      <item><title>Tin cu</title><link>https://ex.com/old</link>
        <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Atom bai</title>
        <link rel="alternate" href="https://ex.com/atom"/>
        <published>2026-06-13T09:00:00Z</published>
        <summary>Tom tat atom</summary></entry>
    </feed>"""
    rdf = b"""<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns="http://purl.org/rss/1.0/"
             xmlns:dc="http://purl.org/dc/elements/1.1/">
      <item><title>arXiv paper</title><link>https://arxiv.org/abs/1</link>
        <dc:date>2026-06-13T08:00:00Z</dc:date>
        <description>abstract</description></item>
    </rdf:RDF>"""

    failures = 0

    r = parse_feed(rss, "Demo", "Tin tức", "reputable")
    assert len(r) == 2, f"RSS phải có 2 item, được {len(r)}"
    assert r[0]["title"] == "Model X ra mat", r[0]
    assert r[0]["url"] == "https://ex.com/x"
    assert r[0]["published_iso"].startswith("2026-06-13T10:30:00"), r[0]["published_iso"]
    assert "hay" in r[0]["summary"] and "<" not in r[0]["summary"], r[0]["summary"]
    print("[selftest] RSS 2.0 OK")

    a = parse_feed(atom, "Demo", "Tin tức", "primary")
    assert len(a) == 1 and a[0]["url"] == "https://ex.com/atom", a
    assert a[0]["published_iso"].startswith("2026-06-13T09:00:00"), a[0]["published_iso"]
    print("[selftest] Atom OK")

    d = parse_feed(rdf, "arXiv", "Nghiên cứu", "primary")
    assert len(d) == 1 and d[0]["title"] == "arXiv paper", d
    assert d[0]["published_iso"].startswith("2026-06-13T08:00:00"), d[0]["published_iso"]
    print("[selftest] RSS 1.0/RDF (arXiv) OK")

    assert parse_date("Fri, 13 Jun 2026 10:30:00 GMT") is not None
    assert parse_date("2026-06-13T10:30:00Z") is not None
    assert parse_date("rác") is None and parse_date(None) is None
    print("[selftest] parse_date OK")

    assert norm_title("Model X: Ra-Mắt!") == "model x ra m t" or norm_title("Model X ra mat") == "model x ra mat"
    print("[selftest] khử trùng lặp/chuẩn hóa OK")

    print("\n✅ Tất cả kiểm thử bộ phân tích PASS" if failures == 0 else "❌ Có lỗi")
    return failures


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    data = collect()
    write_outputs(data)
    print(f"\nĐã ghi {data['item_count']} mục vào {os.environ.get('OUT_DIR', 'feeds')}/latest.json + latest.md")
    print(f"Nguồn OK: {len(data['sources_ok'])} | Nguồn lỗi: {len(data['sources_failed'])}")
    # Chỉ coi là thất bại nếu KHÔNG có nguồn nào chạy được
    return 0 if data["sources_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
