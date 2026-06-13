#!/usr/bin/env python3

import json, os, re, sys, subprocess, glob, math
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
import xml.dom.minidom
import requests

AANTAL_MINUTEN = "03:00:00"

PROGRAMS = [
    {
        "name": "House Party",
        "slug": "house-party",
        "aantal_afleveringen": 2,
    },
    {
        "name": "Doof",
        "slug": "doof",
        "aantal_afleveringen": 1,
    },
]

MP3_DIR = "docs/mp3"
os.makedirs(MP3_DIR, exist_ok=True)


def headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }


def parse_hms_to_seconds(hms):
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def get_episode_urls(slug):
    program_page = f"https://www.abc.net.au/triplej/programs/{slug}"
    api_url = (
        "https://api.abc.net.au/v2/page/collection?"
        f"path=/triplej/programs/{slug}&size=20"
    )

    try:
        r = requests.get(api_url, headers=headers(), timeout=15)
        r.raise_for_status()
        data = r.json()

        urls = []
        for block in data.get("blocks", []):
            for promo in block.get("promos", []):
                url = promo.get("url")
                if url and f"/{slug}/" in url:
                    if url.startswith("/"):
                        url = "https://www.abc.net.au" + url
                    if url not in urls:
                        urls.append(url)

        if urls:
            return urls
    except Exception:
        pass

    try:
        r = requests.get(program_page, headers=headers(), timeout=15)
        r.raise_for_status()

        matches = re.findall(
            rf'href="(/triplej/programs/{re.escape(slug)}/{re.escape(slug)}/\d+)"',
            r.text,
        )

        urls = []
        for m in matches:
            url = "https://www.abc.net.au" + m
            if url not in urls:
                urls.append(url)

        return urls
    except Exception as e:
        print(f"FOUT bij ophalen {slug}: {e}")
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
        return (
            f"{dt.strftime('%a')} "
            f"{dt.strftime('%d').lstrip('0')} "
            f"{dt.strftime('%b')} "
            f"{dt.strftime('%Y')} at 8:00am"
        )
    except Exception:
        return ""


def build_rss(items):
    rss = Element("rss", version="2.0")
    rss.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")

    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Triple J Local"
    SubElement(ch, "link").text = "https://www.abc.net.au/triplej/programs"
    SubElement(ch, "description").text = "Triple J local MP3 feed"
    SubElement(ch, "language").text = "en-au"
    SubElement(ch, "itunes:author").text = "Triple J"
    SubElement(ch, "itunes:summary").text = "Triple J shows split into 1-hour MP3 parts."
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
                SubElement(item, "pubDate").text = dt.strftime(
                    "%a, %d %b %Y %H:%M:%S +0000"
                )
            except Exception:
                pass

        enc = SubElement(item, "enclosure")
        enc.set("url", it["url"])
        enc.set("type", "audio/mpeg")
        enc.set("length", it.get("local_size", "0"))

    return xml.dom.minidom.parseString(
        tostring(rss, encoding="unicode")
    ).toprettyxml(indent="  ")


def cleanup_old_mp3s(keep_prefixes):
    for mp3_path in glob.glob(os.path.join(MP3_DIR, "*.mp3")):
        fname = os.path.basename(mp3_path)

        keep = False
        for prefix in keep_prefixes:
            if fname.startswith(prefix + "_uur"):
                keep = True
                break

        if not keep:
            try:
                os.remove(mp3_path)
                print(f"Verwijderd: {fname}")
            except OSError as e:
                print(f"FOUT bij verwijderen {fname}: {e}")


if __name__ == "__main__":
    os.makedirs("docs", exist_ok=True)

    data = []
    keep_prefixes = []

    total_seconds_requested = min(
        parse_hms_to_seconds(AANTAL_MINUTEN),
        3 * 3600,
    )

    for program in PROGRAMS:
        slug = program["slug"]
        name = program["name"]
        aantal = program["aantal_afleveringen"]

        print(f"Ophalen afleveringenlijst voor {name}...")
        episode_urls = get_episode_urls(slug)

        if not episode_urls:
            print(f"Geen afleveringen gevonden voor {name}")
            continue

        processed_count = 0

        for url in episode_urls:
            if processed_count >= aantal:
                break

            print(f"Verwerken {name}: {url}")
            info = extract_episode_info(url)

            if not info or not info.get("audio_url"):
                print("Overgeslagen: geen audio-info")
                continue

            m = re.search(rf"/{re.escape(slug)}/(\d+)", url)
            episode_id = m.group(1) if m else url.rstrip("/").split("/")[-1]

            file_prefix = f"{slug}_{episode_id}"
            keep_prefixes.append(file_prefix)

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
                mp3_filename = f"{file_prefix}_uur{uur_nummer}.mp3"
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

                print(f"Converteren {name} uur {uur_nummer}")
                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    print(f"FOUT bij ffmpeg voor {name} uur {uur_nummer}:")
                    print(result.stderr[:500])
                    continue

                audio_url_for_feed = (
                    f"https://mrsjonnie.github.io/houseparty-download/mp3/{mp3_filename}"
                )

                title_parts = []
                if date_str:
                    title_parts.append(date_str)
                title_parts.append(f"– {name} uur {uur_nummer}")
                if presenter:
                    title_parts.append(f"[{presenter}]")

                data.append({
                    "title": " ".join(title_parts),
                    "url": audio_url_for_feed,
                    "page_url": url,
                    "guid": f"{url}#uur{uur_nummer}",
                    "date": upload_date,
                    "local_size": str(os.path.getsize(mp3_path)),
                })

                print(f"OK: {mp3_filename}")

            processed_count += 1

    cleanup_old_mp3s(set(keep_prefixes))

    print(f"Feed bouwen met {len(data)} onderdelen...")
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(build_rss(data))

    print(f"Klaar: docs/feed.xml ({len(data)} items)")
