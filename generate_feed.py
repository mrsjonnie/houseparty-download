#!/usr/bin/env python3

import json, os, re, sys, subprocess, glob, math, time
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom
import requests

AANTAL_AFLEVERINGEN = 2
AANTAL_MINUTEN = "03:00:00"
VERWIJDER_OUDER_DAN_DAGEN = 14

BASE_URL = "https://www.abc.net.au/triplej/programs/house-party"
PROGRAM_PAGE = BASE_URL
COLLECTION_API = (
    "https://api.abc.net.au/v2/page/collection?"
    "path=/triplej/programs/house-party&size=20"
)

MP3_DIR = "docs/mp3"
os.makedirs(MP3_DIR, exist_ok=True)


def parse_hms_to_seconds(hms):
    parts = hms.strip().split(":")
    if len(parts) == 3:
        h, m, s = map(int, parts)
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = map(int, parts)
        return m * 60 + s
    return int(parts[0])


def headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }


def get_episode_urls_from_api():
    try:
        r = requests.get(COLLECTION_API, headers=headers(), timeout=15)
        r.raise_for_status()
        data = r.json()

        urls = []
        for block in data.get("blocks", []):
            for promo in block.get("promos", []):
                url = promo.get("url")
                if url and "/house-party/" in url:
                    if url.startswith("/"):
                        url = "https://www.abc.net.au" + url
                    if url not in urls:
                        urls.append(url)

        return urls
    except Exception:
        return None


def get_episode_urls_from_program_page():
    try:
        r = requests.get(PROGRAM_PAGE, headers=headers(), timeout=15)
        r.raise_for_status()

        matches = re.findall(
            r'href="(/triplej/programs/house-party/house-party/\d+)"',
            r.text,
        )

        urls = []
        for m in matches:
            url = "https://www.abc.net.au" + m
            if url not in urls:
                urls.append(url)

        return urls
    except Exception as e:
        print(f"FOUT bij ophalen programmapiagina: {e}")
        return []


def extract_episode_info(page_url):
    try:
        r = requests.get(page_url, headers=headers(), timeout=15)
        r.raise_for_status()
        html = r.text

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>',
            html,
        )
        if not m:
            print(f"GEEN __NEXT_DATA__ in {page_url}")
            return None

        data = json.loads(m.group(1))
        props = data.get("props", {}).get("pageProps", {})
        doc = props.get("data", {}).get("documentProps", {})

        audio_url = None
        for rend in doc.get("renditions", []):
            url = rend.get("url")
            if url and (url.endswith(".aac") or ".m3u8" in url):
                audio_url = url
                break

        if not audio_url and doc.get("renditions"):
            audio_url = doc["renditions"][0].get("url")

        upload_date = None
        meta_date = re.search(
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
            html,
        )
        if meta_date:
            upload_date = meta_date.group(1)[:10].replace("-", "")
        else:
            for key in ("firstPublished", "datePublished", "uploadDate", "publishDate"):
                if doc.get(key):
                    upload_date = str(doc[key])[:10].replace("-", "")
                    break

        presenter_name = ""
        try:
            hero = doc.get("heroImageWithCTAPrepared", {})
            presenters = hero.get("presentersProps", {}).get("linkPrepared", [])
            if presenters:
                presenter_name = presenters[0].get("label", {}).get("full", "").strip()
        except Exception:
            pass

        return {
            "audio_url": audio_url,
            "upload_date": upload_date,
            "presenter_name": presenter_name,
        }

    except Exception as e:
        print(f"FOUT bij verwerken {page_url}: {e}")
        return None


def format_date(upload_date_str):
    try:
        dt = datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        return f"{dt.strftime('%a')} {dt.strftime('%d').lstrip('0')} {dt.strftime('%b')} {dt.strftime('%Y')} at 8:00am"
    except Exception:
        return ""


def build_rss(items):
    rss = Element("rss", version="2.0")
    rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")

    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Triple J House Party Local"
    SubElement(ch, "link").text = BASE_URL
    SubElement(ch, "description").text = "Triple J House Party DJ mix show"
    SubElement(ch, "language").text = "en-au"
    SubElement(ch, "itunes:author").text = "Triple J"
    SubElement(ch, "itunes:summary").text = "House Party feed split into 1-hour MP3 parts."
    SubElement(ch, "itunes:explicit").text = "false"

    for it in items:
        item = SubElement(ch, "item")
        SubElement(item, "title").text = it["title"]
        SubElement(item, "link").text = it["page_url"]
        SubElement(item, "guid", isPermaLink="false").text = it["guid"]
        SubElement(item, "description").text = ""

        if it.get("date"):
            try:
                dt = datetime.strptime(it["date"], "%Y%m%d").replace(tzinfo=timezone.utc)
                SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                pass

        enc = SubElement(item, "enclosure")
        enc.set("url", it["url"])
        enc.set("type", "audio/mpeg")
        enc.set("length", it.get("local_size", "0"))

    return xml.dom.minidom.parseString(
        tostring(rss, encoding="unicode")
    ).toprettyxml(indent="  ")


def cleanup_mp3s_older_than(days):
    cutoff = time.time() - days * 24 * 60 * 60

    for mp3_path in glob.glob(os.path.join(MP3_DIR, "*.mp3")):
        try:
            if os.path.getmtime(mp3_path) < cutoff:
                os.remove(mp3_path)
                print(f"Verwijderd ouder dan {days} dagen: {os.path.basename(mp3_path)}")
        except OSError as e:
            print(f"FOUT bij verwijderen {mp3_path}: {e}")


if __name__ == "__main__":
    os.makedirs("docs", exist_ok=True)

    print("Ophalen afleveringenlijst...")
    episode_urls = get_episode_urls_from_api()

    if episode_urls is None:
        print("API blokkeert mogelijk, programmapiagina scrapen...")
        episode_urls = get_episode_urls_from_program_page()

    if not episode_urls:
        print("FOUT: geen afleveringen gevonden.")
        sys.exit(1)

    data = []
    processed_count = 0

    total_seconds_requested = min(
        parse_hms_to_seconds(AANTAL_MINUTEN),
        3 * 3600,
    )

    for url in episode_urls:
        if processed_count >= AANTAL_AFLEVERINGEN:
            break

        print(f"Verwerken: {url}")
        info = extract_episode_info(url)

        if not info or not info.get("audio_url"):
            print("Overgeslagen: geen audio-info")
            continue

        m = re.search(r"/house-party/(\d+)", url)
        episode_id = m.group(1) if m else url.rstrip("/").split("/")[-1]

        audio_url = info["audio_url"]
        upload_date = info["upload_date"]
        date_str = format_date(upload_date) if upload_date else ""
        presenter = info["presenter_name"]

        num_chunks = min(3, math.ceil(total_seconds_requested / 3600))

        for chunk_idx in range(num_chunks):
            start_sec = chunk_idx * 3600
            duration_sec = min(3600, total_seconds_requested - start_sec)

            if duration_sec <= 0:
                continue

            uur_nummer = chunk_idx + 1
            mp3_filename = f"{episode_id}_uur{uur_nummer}.mp3"
            mp3_path = os.path.join(MP3_DIR, mp3_filename)

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-ss", str(start_sec),
                "-i", audio_url,
                "-t", str(duration_sec),
                "-map", "0:a?",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                "-write_xing", "1",
                "-avoid_negative_ts", "make_zero",
                "-headers", (
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                mp3_path,
            ]

            print(f"Converteren uur {uur_nummer}: {start_sec}s tot {start_sec + duration_sec}s")
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"FOUT bij ffmpeg voor uur {uur_nummer}:")
                print(result.stderr[:500])
                continue

            audio_url_for_feed = (
                f"https://mrsjonnie.github.io/houseparty-download/mp3/{mp3_filename}"
            )

            title_parts = []
            if date_str:
                title_parts.append(date_str)
            title_parts.append(f"– House Party uur {uur_nummer}")
            if presenter:
                title_parts.append(f"[{presenter}]")

            title = " ".join(title_parts)

            data.append({
                "title": title,
                "url": audio_url_for_feed,
                "page_url": url,
                "guid": f"{url}#uur{uur_nummer}",
                "date": upload_date,
                "local_size": str(os.path.getsize(mp3_path)),
            })

            print(f"OK: {mp3_filename}")

        processed_count += 1

    cleanup_mp3s_older_than(VERWIJDER_OUDER_DAN_DAGEN)

    print(f"Feed bouwen met {len(data)} onderdelen...")
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(build_rss(data))

    print(f"Klaar: docs/feed.xml ({len(data)} items)")
