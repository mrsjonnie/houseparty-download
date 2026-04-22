#!/usr/bin/env python3
"""
House Party podcast generator – configurable preview length and episode count.

Set:
    AANTAL_AFLEVERINGEN   – how many episodes to keep (newest first)
    AANTAL_MINUTEN        – length of the preview per episode (MM:SS or HH:MM:SS)

The script:
    • Tries the ABC collection API (fallback: scrape the program page).
    • For each episode (newest first) downloads the AAC stream,
      keeps only the first AANTAL_MINUTEN, converts to MP3 (ffmpeg, XING header).
    • Stores MP3 files under docs/mp3/.
    • Builds an RSS feed (docs/feed.xml) whose <title> follows:
          "<date> – House Party [Presenter Name](Presenter URL)"
    • After processing, keeps only the newest AANTAL_AFLEVERINGEN MP3 files,
      deleting the rest.
"""

import json, os, re, sys, subprocess, glob
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom
import requests

# ----------------------------------------------------------------------
# ★★★ USER‑CONFIGURABLE SETTINGS ★★★
AANTAL_AFLEVERINGEN = 2          # how many episodes to retain
AANTAL_MINUTEN    = "02:00:00"   # preview length per episode (HH:MM:SS)
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


def convert_preview_to_mp3(aac_url, mp3_path):
    """Download the first AANTAL_MINUTEN of AAC and convert to MP3 (adds XING header)."""
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",                     # overwrite output
        "-i", aac_url,
        "-t", AANTAL_MINUTEN,    # take only the first N minutes
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-write_xing", "1",      # crucial for seeking on MP3
        mp3_path,
    ]
    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  FOUT bij ffmpeg conversie: {e.stderr[:200]}")
        return False


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
    """Build RSS feed with iTunes namespace."""
    rss = Element("rss", version="2.0")
    rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Triple J House Party"
    SubElement(ch, "link").text = BASE_URL
    SubElement(ch, "description").text = "Triple J House Party DJ mix show"
    SubElement(ch, "language").text = "en-au"
    for ep in items:
        it = SubElement(ch, "item")
        SubElement(it, "title").text = ep["title"]
        SubElement(it, "link").text = ep["page_url"]
        SubElement(it, "guid", isPermaLink="false").text = ep["page_url"]
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
            enc.set("length", "0")
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
            presenter_part = (
                f"[{presenter_name}]({presenter_url})" if presenter_url else presenter_name
            )
        else:
            presenter_part = ""

        # Build MP3 filename from the numeric ID in the URL
        m = re.search(r"/house-party/(\d+)", url)
        episode_id = m.group(1) if m else url.split("/")[-1]
        mp3_filename = f"{episode_id}.mp3"
        mp3_path = os.path.join(MP3_DIR, mp3_filename)

        # Convert preview to MP3
        print(f"  Converteren eerste {AANTAL_MINUTEN} naar MP3 …")
        ok = convert_preview_to_mp3(audio_url, mp3_path)
        if not ok:
            print("  OVERGESLAGEN (conversie mislukt)")
            continue
        else:
            print(f"  MP3 klaar: {mp3_filename}")

        # Enclosure URL points to the MP3 served by GitHub Pages
        audio_url_for_feed = f"https://mrsjonnie.github.io/houseparty-feed/mp3/{mp3_filename}"

        # Build title: <date> – House Party [Presenter](URL)
        parts = []
        if date_str:
            parts.append(date_str)
        parts.append("– House Party")
        if presenter_part:
            parts.append(f"[{presenter_part}]")
        title = " ".join(parts)

        data.append(
            {
                "title": title,
                "url": audio_url_for_feed,
                "page_url": url,
                "date": upload_date,
                "description": "",  # optional
            }
        )
        print(f"  OK: {title}")

        # Stop after we have processed AANTAL_AFLEVERINGEN episodes (newest first)
        if len(data) >= AANTAL_AFLEVERINGEN:
            print(f"  MAX {AANTAL_AFLEVERINGEN} AFLEVERINGEN BEREikt – stoppen")
            break

    # Keep only the newest AANTAL_AFLEVERINGEN MP3 files
    cleanup_old_mp3s()

    print(f"Feed bouwen met {len(data)} afleveringen …")
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(build_rss(data))
    print(f"Klaar: docs/feed.xml ({len(data)} items)")
