#!/usr/bin/env python3
"""
House Party podcast generator – split into 1‑hour MP3 parts, 192 kbps.
Feed title: "Triple J House Party Local"
Episode title format (no URL inside the title):
    "<date> – House Party Part N [Presenter]"
"""

import json, os, re, sys, subprocess, glob, math
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom
import requests

# ----------------------------------------------------------------------
# ★★★ USER‑CONFIGURABLE SETTINGS ★★★
AANTAL_AFLEVERINGEN = 2          # how many MP3 parts (hour‑chunks) to retain
AANTAL_MINUTEN    = "02:00:00"   # total length to capture per episode (HH:MM:SS), max 03:00:00
# ----------------------------------------------------------------------

BASE_URL = "https://www.abc.net.au/triplej/programs/house-party"
PROGRAM_PAGE = BASE_URL                     # https://www.abc.net.au/triplej/programs/house-party
COLLECTION_API = (
    "https://api.abc.net.au/v2/page/collection?"
    "path=/triplej/programs/house-party&size=20"
)
MP3_DIR = "docs/mp3"
os.makedirs(MP3_DIR, exist_ok=True)

# ----------------------------------------------------------------------
def parse_hms_to_seconds(hms: str) -> int:
    """Convert HH:MM:SS (or MM:SS) to total seconds."""
    parts = hms.strip().split(":")
    if len(parts) == 3:
        h, m, s = map(int, parts)
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = map(int, parts)
        return m * 60 + s
    return int(parts[0])


def get_episode_urls_from_api():
    """Try to fetch episode URLs from the ABC collection API (may return 403)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(COLLECTION_API, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        urls = []
        for block in data.get("blocks", []):
            for promo in block.get("promos", []):
                url = promo.get("url")
                if url and "/house-party/" in url:
                    if url.startswith("/"):
                        url = "https://www.abc.net.au" + url
                    urls.append(url)
        # deduplicate while keeping order (newest first as returned by API)
        seen = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq
    except Exception:
        return None   # signal failure → fall back to scraping


def get_episode_urls_from_program_page():
    """Scrape the program page for episode links (newest first)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(PROGRAM_PAGE, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text
        # Find all <a href="/triplej/programs/house-party/house-party/xxxxxx">
        pattern = r'href="(/triplej/programs/house-party/house-party/\d+)"'
        matches = re.findall(pattern, html)
        urls = []
        for m in matches:
            abs_url = "https://www.abc.net.au" + m
            if abs_url not in urls:      # keep first occurrence only (newest first)
                urls.append(abs_url)
        return urls
    except Exception as e:
        print(f"  FOUT bij ophalen programmapiagina: {e}")
        return []


def extract_episode_info(page_url):
    """Return dict with audio_url, upload_date, presenter_name, presenter_url, title_raw."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(page_url, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text

        # --- locate __NEXT_DATA__ JSON ---
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>',
            html,
        )
        if not m:
            print(f"  GEEN __NEXT_DATA__ in {page_url}")
            return None
        data = json.loads(m.group(1))
        props = data.get("props", {}).get("pageProps", {})

        # ----- audio URL (still .aac) -----
        audio_url = None
        try:
            renditions = props["data"]["documentProps"]["renditions"]
            if renditions and isinstance(renditions, list):
                for rend in renditions:
                    url = rend.get("url")
                    if url and (url.endswith(".aac") or ".m3u8" in url):
                        audio_url = url
                        break
                else:
                    audio_url = renditions[0].get("url")
        except (KeyError, TypeError, IndexError):
            pass

        # ----- upload date -----
        upload_date = None
        # Prefer meta article:published_time (ISO 8601)
        meta_date = re.search(
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
            html,
        )
        if meta_date:
            upload_date = meta_date.group(1)[:10].replace("-", "")
        else:
            doc = props.get("data", {}).get("documentProps", {})
            for key in ("firstPublished", "datePublished", "uploadDate", "publishDate"):
                if doc.get(key):
                    upload_date = str(doc[key])[:10].replace("-", "")
                    break
            # Last resort: scan JSON for any YYYYMMDD string
            if not upload_date:
                def find_date(obj):
                    if isinstance(obj, dict):
                        for v in obj.values():
                            if isinstance(v, str) and re.fullmatch(r"\d{8}", v):
                                return v
                            res = find_date(v)
                            if res:
                                return res
                    elif isinstance(obj, list):
                        for v in obj:
                            res = find_date(v)
                            if res:
                                return res
                    return None
                upload_date = find_date(props)

        # ----- presenter -----
        presenter_name = ""
        presenter_url = ""
        try:
            hero = props.get("data", {}).get("documentProps", {}).get(
                "heroImageWithCTAPrepared", {}
            )
            prep = hero.get("presentersProps", {}).get("linkPrepared", [])
            if prep and isinstance(prep, list) and len(prep) > 0:
                item = prep[0]
                presenter_name = item.get("label", {}).get("full", "").strip()
                presenter_url = item.get("canonicalURL", "")
                if presenter_url and presenter_url.startswith("/"):
                    presenter_url = "https://www.abc.net.au" + presenter_url
        except Exception:
            pass

        # ----- title raw -----
        title_raw = ""
        doc = props.get("data", {}).get("documentProps", {})
        if doc.get("title"):
            title_raw = doc["title"]
        elif doc.get("programTitle"):
            title_raw = doc["programTitle"]
        else:
            title_raw = "House Party"

        return {
            "audio_url": audio_url,
            "upload_date": upload_date,
            "presenter_name": presenter_name,
            "presenter_url": presenter_url,
            "title_raw": title_raw,
        }
    except Exception as e:
        print(f"  FOUT bij verwerken {page_url}: {e}")
        return None


def format_date(upload_date_str):
    """Convert YYYYMMDD → 'Sat 17 Apr 2026 at 8:00am' (fixed 08:00 am)."""
    try:
        dt = datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        day_name = dt.strftime("%a")
        day = dt.strftime("%d").lstrip("0")
        month = dt.strftime("%b")
        year = dt.strftime("%Y")
        return f"{day_name} {day} {month} {year} at 8:00am"
    except Exception:
        return ""


def build_rss(items):
    """Build RSS feed with iTunes namespace – feed title = “Triple J House Party Local”."""
    rss = Element("rss", version="2.0")
    rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Triple J House Party Local"
    SubElement(ch, "link").text = BASE_URL
    SubElement(ch, "description").text = "Triple J House Party DJ mix show"
    SubElement(ch, "language").text = "en-au"
    # Optional iTunes tags (feel free to keep or remove)
    SubElement(ch, "itunes:author").text = "Triple J"
    SubElement(ch, "itunes:summary").text = (
        "House Party preview feed – first 2 hours split into 1‑hour MP3 parts."
    )
    SubElement(ch, "itunes:explicit").text = "false"

    for ep in items:
        it = SubElement(ch, "item")
        SubElement(it, "title").text = ep["title"]
        SubElement(it, "link").text = ep["page_url"]
        SubElement(it, "guid", isPermaLink="false").text = ep["guid"]
        SubElement(it, "description").text = ep.get("description", "")[:500]
        if ep.get("date"):
            try:
                dt = datetime.strptime(ep["date"], "%Y%m%d").replace(tzinfo=timezone.utc)
                SubElement(it, "pubDate").text = dt.strftime(
                    "%a, %d %b %Y %H:%M:%S +0000"
                )
            except Exception:
                pass
        if ep.get("url"):
            enc = SubElement(it, "enclosure")
            enc.set("url", ep["url"])
            enc.set("type", "audio/mpeg")
            # Use the real file size we stored when creating the item
            enc.set("length", ep.get("local_size", "0"))
    return xml.dom.minidom.parseString(
        tostring(rss, encoding="unicode")
    ).toprettyxml(indent="  ")


def cleanup_old_mp3s(keep_n=AANTAL_AFLEVERINGEN):
    """Keep only the newest `keep_n` MP3 files in MP3_DIR; delete the rest."""
    mp3_files = glob.glob(os.path.join(MP3_DIR, "*.mp3"))
    # sort by modification time, newest first
    mp3_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old_file in mp3_files[keep_n:]:
        try:
            os.remove(old_file)
            print(f"  Removed old MP3: {os.path.basename(old_file)}")
        except OSError as e:
            print(f"  FOUT bij verwijderen {old_file}: {e}")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    os.makedirs("docs", exist_ok=True)
    print("Ophalen afleveringenlijst …")
    episode_urls = get_episode_urls_from_api()
    if episode_urls is None:
        print("WAARSCHUWLING: API blokkeert (403), scrapen programmapiagina …")
        episode_urls = get_episode_urls_from_program_page()
        if not episode_urls:
            print("FOUT: Kon geen afleveringen vinden – eindigt.")
            sys.exit(1)

    data = []
    total_seconds_requested = parse_hms_to_seconds(AANTAL_MINUTEN)
    total_seconds_requested = min(total_seconds_requested, 3 * 3600)  # max 3 h

    for url in episode_urls:
        print(f"Verwerken: {url}")
        info = extract_episode_info(url)
        if not info or not info.get("audio_url"):
            print("  OVERGESLAGEN (geen audio‑info)")
            continue

        audio_url = info["audio_url"]
        upload_date = info["upload_date"]
        date_str = format_date(upload_date) if upload_date else ""
        presenter_name = info["presenter_name"]
        presenter_url = info["presenter_url"]
        if presenter_name:
            presenter_part = f"[{presenter_name}]"
        else:
            presenter_part = ""

        if total_seconds_requested <= 0:
            continue
        num_chunks = min(3, math.ceil(total_seconds_requested / 3600))

        for chunk_idx in range(num_chunks):
            start_sec = chunk_idx * 3600
            remaining = total_seconds_requested - start_sec
            duration_sec = min(3600, remaining)

            m = re.search(r"/house-party/(\d+)", url)
            episode_id = m.group(1) if m else url.split("/")[-1]
            mp3_filename = f"{episode_id}_h{chunk_idx + 1}.mp3"
            mp3_path = os.path.join(MP3_DIR, mp3_filename)

            # ffmpeg command:
            #   -ss after -i  → accurate (though slower) seeking
            #   -map 0:a?     → take the first audio stream if present
            #   -headers      → inject a proper User‑Agent so the CDN doesn’t block us
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i", audio_url,
                "-ss", str(start_sec),
                "-t", str(duration_sec),
                "-map", "0:a?",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                "-write_xing", "1",
                "-avoid_negative_ts", "make_zero",
                "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                mp3_path,
            ]
            print(f"  Converteren chunk {chunk_idx+1}/{num_chunks} ({start_sec}s–{start_sec+duration_sec}s) → MP3 …")
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  FOUT bij ffmpeg conversie voor chunk {chunk_idx+1}:")
                print(result.stderr[:500])
                continue
            else:
                print(f"  MP3 klaar: {mp3_filename}")

            # Enclosure URL – must point to the GitHub Pages URL of the *download* repo
            audio_url_for_feed = (
                f"https://mrsjonnie.github.io/houseparty-download/mp3/{mp3_filename}"
            )
            # Store the real file size so the RSS <enclosure> gets a non‑zero length
            local_size = str(os.path.getsize(mp3_path))

            parts = []
            if date_str:
                parts.append(date_str)
            parts.append(f"– House Party Part {chunk_idx + 1}")
            if presenter_part:
                parts.append(presenter_part)
            title = " ".join(parts)

            guid = f"{url}#part{chunk_idx + 1}"

            data.append(
                {
                    "title": title,
                    "url": audio_url_for_feed,
                    "page_url": url,
                    "guid": guid,
                    "date": upload_date,
                    "description": "",
                    # Pass the size to build_rss
                    "local_size": local_size,
                }
            )
            print(f"  OK: {title}")

        if len(data) >= AANTAL_AFLEVERINGEN:
            print(f"  MAX {AANTAL_AFLEVERINGEN} MP3‑onderdelen BEREikt – stoppen")
            break

    cleanup_old_mp3s()

    print(f"Feed bouwen met {len(data)} MP3‑onderdelen …")
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(build_rss(data))
    print(f"Klaar: docs/feed.xml ({len(data)} items)")
