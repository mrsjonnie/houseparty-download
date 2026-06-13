#!/usr/bin/env python3

import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.dom.minidom
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

import requests


# Hoeveel audio per aflevering wordt opgeslagen.
# Maximum: 3 uur.
AUDIO_LENGTE = "03:00:00"

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

SITE_BASE = "https://mrsjonnie.github.io/houseparty-download"
FEED_URL = f"{SITE_BASE}/feed.xml"

# TuneIn krijgt een M3U met de nieuwste aflevering van dit programma.
TUNEIN_PROGRAM_SLUG = "house-party"

DOCS_DIR = "docs"
MP3_DIR = os.path.join(DOCS_DIR, "mp3")
FEED_PATH = os.path.join(DOCS_DIR, "feed.xml")
TUNEIN_PATH = os.path.join(DOCS_DIR, "tunein.m3u")

REQUEST_TIMEOUT = 20
MAX_AUDIO_SECONDS = 3 * 3600
MIN_VALID_MP3_SIZE = 100_000
FORCE_REBUILD_MP3 = False

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"

register_namespace("itunes", ITUNES_NS)
register_namespace("atom", ATOM_NS)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def headers():
    return {"User-Agent": USER_AGENT}


def parse_hms_to_seconds(value):
    """Zet HH:MM:SS om naar seconden."""
    try:
        parts = [int(part) for part in value.split(":")]
    except (AttributeError, ValueError) as exc:
        raise ValueError("AUDIO_LENGTE moet de vorm HH:MM:SS hebben.") from exc

    if len(parts) != 3:
        raise ValueError("AUDIO_LENGTE moet de vorm HH:MM:SS hebben.")

    hours, minutes, seconds = parts

    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Ongeldige waarde voor AUDIO_LENGTE.")

    return hours * 3600 + minutes * 60 + seconds


def parse_datetime(value):
    """Lees een ISO-datum/tijd en geef een UTC-datetime terug."""
    if not value:
        return None

    text = str(value).strip()

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        try:
            result = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def format_date(upload_date_str):
    """Maak een korte datum voor in de titel."""
    if not upload_date_str:
        return ""

    try:
        dt = datetime.strptime(upload_date_str, "%Y%m%d")
    except ValueError:
        return ""

    return (
        f"{dt.strftime('%a')} "
        f"{dt.strftime('%d').lstrip('0')} "
        f"{dt.strftime('%b')} "
        f"{dt.strftime('%Y')}"
    )


def format_duration(total_seconds):
    """Maak HH:MM:SS voor itunes:duration."""
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_episode_urls(slug):
    """Haal de meest recente afleveringspagina's van ABC op."""
    program_page = f"https://www.abc.net.au/triplej/programs/{slug}"
    api_url = (
        "https://api.abc.net.au/v2/page/collection?"
        f"path=/triplej/programs/{slug}&size=20"
    )

    try:
        response = requests.get(
            api_url,
            headers=headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        urls = []

        for block in data.get("blocks", []):
            for promo in block.get("promos", []):
                url = promo.get("url")

                if not url or f"/{slug}/" not in url:
                    continue

                if url.startswith("/"):
                    url = "https://www.abc.net.au" + url

                if url not in urls:
                    urls.append(url)

        if urls:
            return urls

        print(f"API gaf geen afleveringen voor {slug}; HTML-fallback wordt geprobeerd.")

    except (requests.RequestException, ValueError) as exc:
        print(f"API-fallback voor {slug}: {exc}")

    try:
        response = requests.get(
            program_page,
            headers=headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        matches = re.findall(
            rf'href="(/triplej/programs/{re.escape(slug)}/{re.escape(slug)}/\d+)"',
            response.text,
        )

        urls = []

        for match in matches:
            url = "https://www.abc.net.au" + match

            if url not in urls:
                urls.append(url)

        return urls

    except requests.RequestException as exc:
        print(f"FOUT bij ophalen {slug}: {exc}")
        return []


def find_published_value(html, document):
    """Zoek de originele publicatiedatum/tijd."""
    meta_match = re.search(
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+'
        r'content=["\']([^"\']+)',
        html,
        flags=re.IGNORECASE,
    )

    if meta_match:
        return meta_match.group(1)

    for key in (
        "firstPublished",
        "datePublished",
        "uploadDate",
        "publishDate",
    ):
        if document.get(key):
            return str(document[key])

    return None


def extract_episode_info(page_url):
    """Haal audiolink, publicatiedatum en presentator uit een ABC-pagina."""
    try:
        response = requests.get(
            page_url,
            headers=headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        html = response.text

        next_data_match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>',
            html,
        )

        if not next_data_match:
            print(f"GEEN __NEXT_DATA__ in {page_url}")
            return None

        data = json.loads(next_data_match.group(1))
        props = data.get("props", {}).get("pageProps", {})
        document = props.get("data", {}).get("documentProps", {})

        audio_url = None

        for rendition in document.get("renditions", []):
            url = rendition.get("url")
            lower_url = str(url).lower() if url else ""

            if url and (".aac" in lower_url or ".m3u8" in lower_url):
                audio_url = url
                break

        if not audio_url and document.get("renditions"):
            audio_url = document["renditions"][0].get("url")

        published_value = find_published_value(html, document)
        published_dt = parse_datetime(published_value)

        if published_dt:
            upload_date = published_dt.strftime("%Y%m%d")
            published_at = published_dt.isoformat()
        elif published_value:
            upload_date = str(published_value)[:10].replace("-", "")
            published_at = None
        else:
            upload_date = None
            published_at = None

        presenter_name = ""

        try:
            hero = document.get("heroImageWithCTAPrepared", {})
            presenters = hero.get("presentersProps", {}).get(
                "linkPrepared",
                [],
            )

            if presenters:
                presenter_name = (
                    presenters[0]
                    .get("label", {})
                    .get("full", "")
                    .strip()
                )
        except (AttributeError, IndexError, TypeError):
            presenter_name = ""

        return {
            "audio_url": audio_url,
            "upload_date": upload_date,
            "published_at": published_at,
            "presenter_name": presenter_name,
        }

    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        print(f"FOUT bij verwerken {page_url}: {exc}")
        return None


def item_timestamp(item):
    """Sorteersleutel voor feed- en playlistitems."""
    published_dt = parse_datetime(item.get("published_at"))

    if published_dt:
        return published_dt.timestamp()

    upload_date = item.get("date")

    if upload_date:
        try:
            return (
                datetime.strptime(upload_date, "%Y%m%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            pass

    return 0.0


def build_rss(items):
    """Bouw een geldige RSS 2.0-podcastfeed."""
    rss = Element("rss", {"version": "2.0"})
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "Triple J House Party & Doof – privéfeed"
    SubElement(channel, "link").text = "https://www.abc.net.au/triplej/programs"
    SubElement(channel, "description").text = (
        "Persoonlijke RSS-feed met House Party en Doof, "
        "verdeeld in delen van één uur."
    )
    SubElement(channel, "language").text = "en-au"
    SubElement(channel, "generator").text = "houseparty-download"
    SubElement(channel, "ttl").text = "60"

    SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": FEED_URL,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    SubElement(channel, "lastBuildDate").text = datetime.now(
        timezone.utc
    ).strftime("%a, %d %b %Y %H:%M:%S +0000")

    SubElement(channel, f"{{{ITUNES_NS}}}author").text = "Persoonlijke feed"
    SubElement(channel, f"{{{ITUNES_NS}}}type").text = "episodic"
    SubElement(channel, f"{{{ITUNES_NS}}}summary").text = (
        "House Party en Doof, verdeeld in delen van één uur."
    )
    SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "false"
    SubElement(channel, f"{{{ITUNES_NS}}}block").text = "yes"
    SubElement(
        channel,
        f"{{{ITUNES_NS}}}category",
        {"text": "Music"},
    )

    cover_path = os.path.join(DOCS_DIR, "cover.jpg")

    if os.path.exists(cover_path):
        cover_url = f"{SITE_BASE}/cover.jpg"

        image = SubElement(channel, "image")
        SubElement(image, "url").text = cover_url
        SubElement(image, "title").text = "Triple J House Party & Doof"
        SubElement(image, "link").text = "https://www.abc.net.au/triplej/programs"

        SubElement(
            channel,
            f"{{{ITUNES_NS}}}image",
            {"href": cover_url},
        )

    sorted_items = sorted(
        items,
        key=lambda item: (
            item_timestamp(item),
            -safe_int(item.get("program_index")),
            -safe_int(item.get("chunk_index")),
        ),
        reverse=True,
    )

    for feed_item in sorted_items:
        item = SubElement(channel, "item")

        SubElement(item, "title").text = feed_item["title"]
        SubElement(item, "link").text = feed_item["page_url"]
        SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = feed_item["guid"]

        description = (
            f'{feed_item.get("program_name", "Triple J")}, '
            f'deel {feed_item.get("chunk_index", "")}. '
            "Bron: ABC Triple J."
        )

        SubElement(item, "description").text = description
        SubElement(item, f"{{{ITUNES_NS}}}summary").text = description
        SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "false"
        SubElement(item, f"{{{ITUNES_NS}}}episodeType").text = "full"

        duration_seconds = safe_int(feed_item.get("duration_sec"))

        if duration_seconds > 0:
            SubElement(item, f"{{{ITUNES_NS}}}duration").text = (
                format_duration(duration_seconds)
            )

        published_dt = parse_datetime(feed_item.get("published_at"))

        if not published_dt and feed_item.get("date"):
            try:
                published_dt = datetime.strptime(
                    feed_item["date"],
                    "%Y%m%d",
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                published_dt = None

        if published_dt:
            SubElement(item, "pubDate").text = published_dt.strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            )

        enclosure = SubElement(item, "enclosure")
        enclosure.set("url", feed_item["url"])
        enclosure.set("type", "audio/mpeg")
        enclosure.set("length", str(feed_item.get("local_size", "0")))

    xml_bytes = tostring(rss, encoding="utf-8", xml_declaration=True)

    return xml.dom.minidom.parseString(xml_bytes).toprettyxml(
        indent="  ",
        encoding="utf-8",
    ).decode("utf-8")


def build_tunein_m3u(items, program_slug):
    """
    Maak een M3U met alle uurdelen van de nieuwste aflevering.

    TuneIn Custom URL ondersteunt niet officieel elke statische M3U-variant.
    De directe MP3-URL's in dit bestand blijven daarom ook los bruikbaar.
    """
    candidates = [
        item
        for item in items
        if item.get("program_slug") == program_slug
    ]

    if not candidates:
        return "#EXTM3U\n"

    episodes = {}

    for item in candidates:
        episodes.setdefault(item["episode_id"], []).append(item)

    def episode_sort_key(episode_id):
        episode_items = episodes[episode_id]

        return (
            max(item_timestamp(item) for item in episode_items),
            safe_int(episode_id),
        )

    latest_episode_id = max(episodes, key=episode_sort_key)

    latest_items = sorted(
        episodes[latest_episode_id],
        key=lambda item: safe_int(item.get("chunk_index")),
    )

    lines = ["#EXTM3U"]

    for item in latest_items:
        title = " ".join(str(item["title"]).splitlines()).strip()
        duration = safe_int(item.get("duration_sec"))
        lines.append(f"#EXTINF:{duration},{title}")
        lines.append(item["url"])

    return "\n".join(lines) + "\n"


def mp3_is_usable(path):
    return (
        os.path.isfile(path)
        and os.path.getsize(path) >= MIN_VALID_MP3_SIZE
    )


def convert_to_mp3(
    source_url,
    output_path,
    start_seconds,
    duration_seconds,
    title,
):
    """Download en converteer één audiodeel met ffmpeg."""
    ffmpeg_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-headers",
        f"User-Agent: {USER_AGENT}\r\n",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",
        "-ss",
        str(start_seconds),
        "-i",
        source_url,
        "-t",
        str(duration_seconds),
        "-map",
        "0:a:0?",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-id3v2_version",
        "3",
        "-metadata",
        f"title={title}",
        "-metadata",
        "artist=Triple J",
        "-write_xing",
        "1",
        "-avoid_negative_ts",
        "make_zero",
        output_path,
    ]

    result = subprocess.run(
        ffmpeg_command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(f"FOUT bij ffmpeg voor {title}:")
        print(result.stderr[-1500:])
        return False

    if not mp3_is_usable(output_path):
        print(f"FOUT: uitvoerbestand ontbreekt of is te klein: {output_path}")
        return False

    return True


def cleanup_old_mp3s(keep_filenames):
    """Verwijder MP3's die niet meer in de actuele feed horen."""
    for mp3_path in glob.glob(os.path.join(MP3_DIR, "*.mp3")):
        filename = os.path.basename(mp3_path)

        if filename in keep_filenames:
            continue

        try:
            os.remove(mp3_path)
            print(f"Verwijderd: {filename}")
        except OSError as exc:
            print(f"FOUT bij verwijderen {filename}: {exc}")


def write_text_file(path, content):
    """Schrijf een tekstbestand atomair."""
    temporary_path = path + ".tmp"

    with open(
        temporary_path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file_handle:
        file_handle.write(content)

    os.replace(temporary_path, path)


def main():
    os.makedirs(DOCS_DIR, exist_ok=True)
    os.makedirs(MP3_DIR, exist_ok=True)

    # Voorkomt dat GitHub Pages Jekyll-verwerking toepast.
    open(os.path.join(DOCS_DIR, ".nojekyll"), "a", encoding="utf-8").close()

    if shutil.which("ffmpeg") is None:
        print("FOUT: ffmpeg is niet geïnstalleerd of staat niet in PATH.")
        return 1

    try:
        requested_seconds = parse_hms_to_seconds(AUDIO_LENGTE)
    except ValueError as exc:
        print(f"FOUT: {exc}")
        return 1

    total_seconds_requested = min(
        requested_seconds,
        MAX_AUDIO_SECONDS,
    )

    if requested_seconds > MAX_AUDIO_SECONDS:
        print("AUDIO_LENGTE is begrensd op maximaal 03:00:00.")

    feed_items = []
    keep_filenames = set()
    all_programs_successful = True

    for program_index, program in enumerate(PROGRAMS):
        slug = program["slug"]
        name = program["name"]
        requested_episode_count = safe_int(
            program.get("aantal_afleveringen")
        )

        print(f"Ophalen afleveringenlijst voor {name}...")
        episode_urls = get_episode_urls(slug)

        if not episode_urls:
            print(f"Geen afleveringen gevonden voor {name}.")
            all_programs_successful = False
            continue

        processed_count = 0

        for page_url in episode_urls:
            if processed_count >= requested_episode_count:
                break

            print(f"Verwerken {name}: {page_url}")
            info = extract_episode_info(page_url)

            if not info or not info.get("audio_url"):
                print("Overgeslagen: geen audio-info.")
                continue

            id_match = re.search(
                rf"/{re.escape(slug)}/(\d+)",
                page_url,
            )
            episode_id = (
                id_match.group(1)
                if id_match
                else page_url.rstrip("/").split("/")[-1]
            )

            file_prefix = f"{slug}_{episode_id}"
            audio_url = info["audio_url"]
            upload_date = info.get("upload_date")
            published_at = info.get("published_at")
            date_text = format_date(upload_date)
            presenter = info.get("presenter_name", "")

            number_of_chunks = min(
                3,
                math.ceil(total_seconds_requested / 3600),
            )

            successful_chunks = 0

            for chunk_index_zero_based in range(number_of_chunks):
                start_seconds = chunk_index_zero_based * 3600
                duration_seconds = min(
                    3600,
                    total_seconds_requested - start_seconds,
                )

                if duration_seconds <= 0:
                    continue

                hour_number = chunk_index_zero_based + 1
                mp3_filename = (
                    f"{file_prefix}_uur{hour_number}.mp3"
                )
                mp3_path = os.path.join(MP3_DIR, mp3_filename)

                base_title = f"{name} – uur {hour_number}"

                if date_text:
                    title = f"{date_text} – {base_title}"
                else:
                    title = base_title

                if presenter:
                    title += f" [{presenter}]"

                if FORCE_REBUILD_MP3 or not mp3_is_usable(mp3_path):
                    print(f"Converteren {name} uur {hour_number}...")

                    converted = convert_to_mp3(
                        source_url=audio_url,
                        output_path=mp3_path,
                        start_seconds=start_seconds,
                        duration_seconds=duration_seconds,
                        title=title,
                    )

                    if not converted:
                        try:
                            if os.path.exists(mp3_path):
                                os.remove(mp3_path)
                        except OSError:
                            pass
                        continue
                else:
                    print(f"Bestaat al: {mp3_filename}")

                keep_filenames.add(mp3_filename)

                audio_url_for_feed = (
                    f"{SITE_BASE}/mp3/{mp3_filename}"
                )

                feed_items.append(
                    {
                        "title": title,
                        "url": audio_url_for_feed,
                        "page_url": page_url,
                        "guid": f"{page_url}#uur{hour_number}",
                        "date": upload_date,
                        "published_at": published_at,
                        "local_size": str(os.path.getsize(mp3_path)),
                        "program_name": name,
                        "program_slug": slug,
                        "program_index": program_index,
                        "episode_id": episode_id,
                        "chunk_index": hour_number,
                        "duration_sec": duration_seconds,
                    }
                )

                successful_chunks += 1
                print(f"OK: {mp3_filename}")

            if successful_chunks > 0:
                processed_count += 1

        if processed_count < requested_episode_count:
            print(
                f"Waarschuwing: voor {name} zijn "
                f"{processed_count} van de "
                f"{requested_episode_count} gewenste afleveringen verwerkt."
            )
            all_programs_successful = False

    if not feed_items:
        print("FOUT: er zijn geen geldige feeditems gemaakt.")
        return 1

    if all_programs_successful:
        cleanup_old_mp3s(keep_filenames)
    else:
        print(
            "Opschonen overgeslagen omdat niet alle programma's "
            "volledig konden worden verwerkt."
        )

    print(f"Feed bouwen met {len(feed_items)} onderdelen...")
    write_text_file(FEED_PATH, build_rss(feed_items))

    tunein_m3u = build_tunein_m3u(
        feed_items,
        program_slug=TUNEIN_PROGRAM_SLUG,
    )
    write_text_file(TUNEIN_PATH, tunein_m3u)

    print(f"Klaar: {FEED_PATH} ({len(feed_items)} items)")
    print(f"Klaar: {TUNEIN_PATH}")
    print(f"RSS: {FEED_URL}")
    print(f"TuneIn: {SITE_BASE}/tunein.m3u")

    return 0


if __name__ == "__main__":
    sys.exit(main())
