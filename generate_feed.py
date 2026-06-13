#!/usr/bin/env python3
"""
Genereer een persoonlijke Triple J-feed voor GitHub Pages en Lyrion.

Uitvoer in docs/:
- feed.xml                         House Party podcast-RSS
- house-party.m3u                 nieuwste aflevering met uur 1, 2 en 3
- house-party1.m3u                alleen uur 1
- house-party2.m3u                alleen uur 2
- house-party3.m3u                alleen uur 3
- house-party.opml                bladerbare lijst met de losse uren
- tunein.m3u                      compatibiliteitsalias voor House Party
- mp3/*.mp3                       House Party-audio in delen van maximaal één uur

Doof wordt in dit project niet meer gegenereerd.

De zichtbare titel in RSS, M3U en OPML is dezelfde titel die als ID3-titel
in het MP3-bestand wordt opgeslagen. De technische bestandsnaam blijft
bijvoorbeeld house-party_123456_uur1.mp3.
"""

from __future__ import annotations

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
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

import requests


# ---------------------------------------------------------------------------
# Instellingen
# ---------------------------------------------------------------------------

# Totale lengte per ABC-aflevering die wordt verwerkt.
# Maximum: 03:00:00.
AUDIO_LENGTE = "03:00:00"

# Voeg hier programma's toe of pas aantallen aan.
# Voor elk programma worden automatisch een M3U en twee OPML-bestanden gemaakt.
PROGRAMS = [
    {
        "name": "House Party",
        "slug": "house-party",
        "aantal_afleveringen": 2,
    },
]

SITE_BASE = "https://mrsjonnie.github.io/houseparty-download"

DOCS_DIR = Path("docs")
MP3_DIR = DOCS_DIR / "mp3"
FEED_PATH = DOCS_DIR / "feed.xml"

# Oude URL behouden als alias voor House Party.
TUNEIN_ALIAS_PROGRAM_SLUG = "house-party"
TUNEIN_ALIAS_PATH = DOCS_DIR / "tunein.m3u"

REQUEST_TIMEOUT = 25
MAX_AUDIO_SECONDS = 3 * 3600
MIN_VALID_MP3_SIZE = 100_000

# Zet op True om alle bestaande MP3's opnieuw te bouwen.
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


# ---------------------------------------------------------------------------
# Algemene hulpfuncties
# ---------------------------------------------------------------------------

def headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT}


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_hms_to_seconds(value: str) -> int:
    """Zet HH:MM:SS om naar seconden."""
    try:
        parts = [int(part) for part in value.split(":")]
    except (AttributeError, ValueError) as exc:
        raise ValueError("AUDIO_LENGTE moet de vorm HH:MM:SS hebben.") from exc

    if len(parts) != 3:
        raise ValueError("AUDIO_LENGTE moet de vorm HH:MM:SS hebben.")

    hours, minutes, seconds = parts

    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Ongeldige AUDIO_LENGTE.")

    return hours * 3600 + minutes * 60 + seconds


def parse_datetime(value) -> datetime | None:
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


def format_date(upload_date: str | None) -> str:
    """Maak een nette Nederlandse datum voor TuneIn, M3U, OPML en ID3."""
    if not upload_date:
        return ""

    try:
        dt = datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        return ""

    months = (
        "januari",
        "februari",
        "maart",
        "april",
        "mei",
        "juni",
        "juli",
        "augustus",
        "september",
        "oktober",
        "november",
        "december",
    )

    return f"{dt.day} {months[dt.month - 1]} {dt.year}"


def format_duration(total_seconds: int) -> str:
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def item_timestamp(item: dict) -> float:
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


def pretty_xml(root: Element) -> str:
    """Maak nette UTF-8 XML zonder extra lege regels."""
    rough = tostring(root, encoding="utf-8", xml_declaration=True)
    parsed = xml.dom.minidom.parseString(rough)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    return "\n".join(line for line in pretty.splitlines() if line.strip()) + "\n"


def write_text_file(path: Path, content: str) -> None:
    """Schrijf atomair zodat GitHub Pages nooit een half bestand ziet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary_path, path)


def mp3_is_usable(path: Path) -> bool:
    return path.is_file() and path.stat().st_size >= MIN_VALID_MP3_SIZE


# ---------------------------------------------------------------------------
# ABC ophalen
# ---------------------------------------------------------------------------

def get_episode_urls(slug: str) -> list[str]:
    """Haal recente afleveringspagina's van ABC op."""
    program_page = f"https://www.abc.net.au/triplej/programs/{slug}"
    api_url = (
        "https://api.abc.net.au/v2/page/collection?"
        f"path=/triplej/programs/{slug}&size=30"
    )

    try:
        response = requests.get(
            api_url,
            headers=headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        urls: list[str] = []

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

        print(f"API gaf geen afleveringen voor {slug}; HTML-fallback volgt.")

    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"ABC API-fallback voor {slug}: {exc}")

    try:
        response = requests.get(
            program_page,
            headers=headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        matches = re.findall(
            rf'href=["\'](/triplej/programs/{re.escape(slug)}/'
            rf'(?:{re.escape(slug)}/)?\d+)["\']',
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


def find_published_value(html: str, document: dict):
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


def extract_episode_info(page_url: str) -> dict | None:
    """Haal audiolink, datum, titel en presentator uit een ABC-pagina."""
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

            if url and (
                ".aac" in lower_url
                or ".m3u8" in lower_url
                or ".mp3" in lower_url
            ):
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

        episode_title = (
            document.get("title")
            or document.get("displayTitle")
            or document.get("shortTitle")
            or ""
        )

        return {
            "audio_url": audio_url,
            "upload_date": upload_date,
            "published_at": published_at,
            "presenter_name": presenter_name,
            "episode_title": str(episode_title).strip(),
        }

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        print(f"FOUT bij verwerken {page_url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# MP3 maken
# ---------------------------------------------------------------------------

def build_album_title(
    program_name: str,
    upload_date: str | None,
) -> str:
    """Titel van de volledige uitzending/playlist."""
    date_text = format_date(upload_date)

    if date_text:
        return f"{program_name} – {date_text}"

    return f"{program_name} – nieuwste uitzending"


def build_audio_title(
    program_name: str,
    hour_number: int,
    upload_date: str | None,
    presenter: str,
) -> str:
    """
    Maak de zichtbare TuneIn-/MP3-naam.

    Deze tekst wordt gebruikt als:
    - ID3-titel in het MP3-bestand;
    - titel in RSS;
    - #EXTINF-omschrijving in M3U;
    - zichtbare naam in OPML.
    """
    album_title = build_album_title(program_name, upload_date)
    title = f"{album_title} – uur {hour_number}"

    if presenter:
        title += f" – {presenter}"

    return title


def convert_to_mp3(
    source_url: str,
    output_path: Path,
    start_seconds: int,
    duration_seconds: int,
    title: str,
    album_title: str,
    program_name: str,
    episode_title: str,
    hour_number: int,
    total_tracks: int,
) -> bool:
    """Download en converteer één audiodeel met FFmpeg."""
    temporary_path = output_path.with_suffix(".part.mp3")

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
        "-metadata",
        f"album={album_title}",
        "-metadata",
        f"comment={episode_title or 'Bron: ABC Triple J'}",
        "-metadata",
        f"track={hour_number}/{total_tracks}",
        "-write_xing",
        "1",
        "-avoid_negative_ts",
        "make_zero",
        str(temporary_path),
    ]

    result = subprocess.run(
        ffmpeg_command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(f"FOUT bij FFmpeg voor {title}:")
        print(result.stderr[-1500:])
        temporary_path.unlink(missing_ok=True)
        return False

    if not mp3_is_usable(temporary_path):
        print(f"FOUT: uitvoer ontbreekt of is te klein: {temporary_path}")
        temporary_path.unlink(missing_ok=True)
        return False

    os.replace(temporary_path, output_path)
    return True


def read_mp3_metadata(path: Path) -> dict[str, str]:
    """Lees relevante ID3-tags met ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=title,artist,album,track,comment",
        "-of",
        "json",
        str(path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return {}

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    tags = payload.get("format", {}).get("tags", {}) or {}
    return {str(key).lower(): str(value) for key, value in tags.items()}


def ensure_mp3_metadata(
    path: Path,
    title: str,
    album_title: str,
    program_name: str,
    episode_title: str,
    hour_number: int,
    total_tracks: int,
) -> bool:
    """
    Werk bestaande ID3-tags bij zonder de audio opnieuw te downloaden.

    FFmpeg neemt de audiostream ongewijzigd over met -c:a copy.
    """
    desired = {
        "title": title,
        "artist": "Triple J",
        "album": album_title,
        "track": f"{hour_number}/{total_tracks}",
        "comment": episode_title or "Bron: ABC Triple J",
    }

    current = read_mp3_metadata(path)

    if all(current.get(key) == value for key, value in desired.items()):
        return True

    temporary_path = path.with_suffix(".metadata.mp3")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
        "-id3v2_version",
        "3",
        "-metadata",
        f"title={desired['title']}",
        "-metadata",
        f"artist={desired['artist']}",
        "-metadata",
        f"album={desired['album']}",
        "-metadata",
        f"track={desired['track']}",
        "-metadata",
        f"comment={desired['comment']}",
        str(temporary_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not mp3_is_usable(temporary_path):
        print(f"Waarschuwing: ID3-tags konden niet worden bijgewerkt voor {path.name}")
        if result.stderr:
            print(result.stderr[-800:])
        temporary_path.unlink(missing_ok=True)
        return False

    os.replace(temporary_path, path)
    print(f"ID3 bijgewerkt: {path.name} → {title}")
    return True


def cleanup_old_mp3s(keep_filenames: set[str]) -> None:
    """Verwijder MP3's die niet meer bij de actuele configuratie horen."""
    for mp3_path_string in glob.glob(str(MP3_DIR / "*.mp3")):
        mp3_path = Path(mp3_path_string)

        if mp3_path.name in keep_filenames:
            continue

        try:
            mp3_path.unlink()
            print(f"Verwijderd: {mp3_path.name}")
        except OSError as exc:
            print(f"FOUT bij verwijderen {mp3_path.name}: {exc}")


# ---------------------------------------------------------------------------
# Selectie nieuwste aflevering
# ---------------------------------------------------------------------------

def latest_program_items(items: list[dict], program_slug: str) -> list[dict]:
    """Geef alle uurdelen van de nieuwste aflevering van één programma."""
    candidates = [
        item
        for item in items
        if item.get("program_slug") == program_slug
    ]

    if not candidates:
        return []

    episodes: dict[str, list[dict]] = {}

    for item in candidates:
        episode_id = str(item.get("episode_id", ""))
        episodes.setdefault(episode_id, []).append(item)

    def episode_sort_key(episode_id: str):
        episode_items = episodes[episode_id]
        return (
            max(item_timestamp(item) for item in episode_items),
            safe_int(episode_id),
        )

    latest_episode_id = max(episodes, key=episode_sort_key)

    return sorted(
        episodes[latest_episode_id],
        key=lambda item: safe_int(item.get("chunk_index")),
    )


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def build_rss(items: list[dict]) -> str:
    """Bouw één geldige RSS 2.0-podcastfeed."""
    rss = Element("rss", {"version": "2.0"})
    channel = SubElement(rss, "channel")

    feed_url = f"{SITE_BASE}/feed.xml"

    SubElement(channel, "title").text = "Triple J House Party – privéfeed"
    SubElement(channel, "link").text = "https://www.abc.net.au/triplej/programs"
    SubElement(channel, "description").text = (
        "Persoonlijke feed met Triple J House Party, "
        "verdeeld in delen van maximaal één uur."
    )
    SubElement(channel, "language").text = "en-au"
    SubElement(channel, "generator").text = "houseparty-download"
    SubElement(channel, "ttl").text = "60"

    SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": feed_url,
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
        "Triple J House Party in delen van maximaal één uur."
    )
    SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "false"
    SubElement(channel, f"{{{ITUNES_NS}}}block").text = "yes"
    SubElement(
        channel,
        f"{{{ITUNES_NS}}}category",
        {"text": "Music"},
    )

    cover_path = DOCS_DIR / "cover.jpg"

    if cover_path.exists():
        cover_url = f"{SITE_BASE}/cover.jpg"

        image = SubElement(channel, "image")
        SubElement(image, "url").text = cover_url
        SubElement(image, "title").text = "Triple J House Party"
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

        description = feed_item["title"]

        if feed_item.get("episode_title"):
            description += f'. Originele aflevering: {feed_item["episode_title"]}.'

        description += " Bron: ABC Triple J."

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
            # Elk uur krijgt één seconde verschil voor een stabiele volgorde.
            published_dt = published_dt.replace(
                second=min(
                    59,
                    published_dt.second
                    + safe_int(feed_item.get("chunk_index"))
                    - 1,
                )
            )
            SubElement(item, "pubDate").text = published_dt.strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            )

        enclosure = SubElement(item, "enclosure")
        enclosure.set("url", feed_item["url"])
        enclosure.set("type", "audio/mpeg")
        enclosure.set("length", str(feed_item.get("local_size", "0")))

    return pretty_xml(rss)


# ---------------------------------------------------------------------------
# M3U en OPML voor Lyrion
# ---------------------------------------------------------------------------

def build_program_m3u(items: list[dict], program_slug: str) -> str:
    """
    Maak één online M3U voor de nieuwste gedownloade aflevering.

    TuneIn kan de #EXTINF-titel tonen. Daarnaast bevatten de MP3-bestanden
    dezelfde nette titel in hun ID3-tags.
    """
    latest_items = latest_program_items(items, program_slug)

    if not latest_items:
        return "#EXTM3U\n"

    first_item = latest_items[0]
    album_title = (
        first_item.get("album_title")
        or build_album_title(
            str(first_item.get("program_name") or program_slug),
            first_item.get("date"),
        )
    )

    lines = [
        "#EXTM3U",
        f"#PLAYLIST:{album_title}",
        f"#EXTALB:{album_title}",
        "#EXTART:Triple J",
    ]

    for item in latest_items:
        title = " ".join(str(item["title"]).splitlines()).strip()
        duration = safe_int(item.get("duration_sec"))

        lines.append(f"#EXTINF:{duration},{title}")
        lines.append(f"#EXTGRP:{album_title}")
        lines.append(item["url"])

    return "\n".join(lines) + "\n"


def build_single_hour_m3u(
    items: list[dict],
    program_slug: str,
    hour_number: int,
) -> str:
    """
    Maak een M3U met precies één uur van de nieuwste aflevering.

    De bestandsnaam van deze M3U wordt bijvoorbeeld house-party1.m3u.
    Daardoor is de URL in TuneIn herkenbaarder, ook wanneer TuneIn de
    #PLAYLIST- of #EXTINF-titel niet als favorietennaam gebruikt.
    """
    latest_items = latest_program_items(items, program_slug)

    selected = next(
        (
            item
            for item in latest_items
            if safe_int(item.get("chunk_index")) == hour_number
        ),
        None,
    )

    if not selected:
        return "#EXTM3U\n"

    album_title = (
        selected.get("album_title")
        or build_album_title(
            str(selected.get("program_name") or program_slug),
            selected.get("date"),
        )
    )
    track_title = " ".join(
        str(selected["title"]).splitlines()
    ).strip()
    duration = safe_int(selected.get("duration_sec"))

    lines = [
        "#EXTM3U",
        f"#PLAYLIST:{track_title}",
        f"#EXTALB:{album_title}",
        "#EXTART:Triple J",
        f"#EXTINF:{duration},{track_title}",
        f"#EXTGRP:{album_title}",
        selected["url"],
    ]

    return "\n".join(lines) + "\n"


def add_cover_attribute(attributes: dict[str, str]) -> None:
    if (DOCS_DIR / "cover.jpg").exists():
        attributes["image"] = f"{SITE_BASE}/cover.jpg"


def build_program_opml(
    items: list[dict],
    program_name: str,
    program_slug: str,
) -> str:
    """
    Maak een bladerbare OPML met losse uren.

    'text' en 'title' zijn exact de titel/omschrijving die ook in het MP3
    als ID3-titel staat.
    """
    latest_items = latest_program_items(items, program_slug)

    opml = Element("opml", {"version": "2.0"})
    head = SubElement(opml, "head")

    SubElement(head, "title").text = f"{program_name} – nieuwste uitzending"
    SubElement(head, "cachetime").text = "1"
    SubElement(head, "forceRefresh").text = "1"

    body = SubElement(opml, "body")

    for item in latest_items:
        attributes = {
            "text": item["title"],
            "title": item["title"],
            "type": "audio",
            "url": item["url"],
            "duration": str(safe_int(item.get("duration_sec"))),
            "description": item.get("episode_title") or item["title"],
        }
        add_cover_attribute(attributes)
        SubElement(body, "outline", attributes)

    return pretty_xml(opml)


# ---------------------------------------------------------------------------
# Hoofdprogramma
# ---------------------------------------------------------------------------

def remove_unused_numbered_playlists(
    program_slug: str,
    available_hours: set[int],
) -> None:
    """Verwijder oude genummerde M3U's waarvoor geen audiodeel meer bestaat."""
    for hour_number in (1, 2, 3):
        if hour_number in available_hours:
            continue

        path = DOCS_DIR / f"{program_slug}{hour_number}.m3u"

        if path.exists():
            path.unlink()
            print(f"Verouderde uurplaylist verwijderd: {path}")


def remove_obsolete_generated_files() -> None:
    """Verwijder bestanden die niet meer bij het House Party-project horen."""
    obsolete_paths = [
        DOCS_DIR / "programmas.opml",
        DOCS_DIR / "house-party-play.opml",
        DOCS_DIR / "doof.opml",
        DOCS_DIR / "doof-play.opml",
        DOCS_DIR / "doof.m3u",
        DOCS_DIR / "doof1.m3u",
        DOCS_DIR / "doof2.m3u",
        DOCS_DIR / "doof3.m3u",
    ]

    for path in obsolete_paths:
        if path.exists():
            path.unlink()
            print(f"Verouderd bestand verwijderd: {path}")


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    MP3_DIR.mkdir(parents=True, exist_ok=True)

    # Geen Jekyll-verwerking op GitHub Pages.
    (DOCS_DIR / ".nojekyll").touch()

    remove_obsolete_generated_files()

    if shutil.which("ffmpeg") is None:
        print("FOUT: ffmpeg is niet geïnstalleerd of staat niet in PATH.")
        return 1

    try:
        requested_seconds = parse_hms_to_seconds(AUDIO_LENGTE)
    except ValueError as exc:
        print(f"FOUT: {exc}")
        return 1

    total_seconds_requested = min(requested_seconds, MAX_AUDIO_SECONDS)

    if requested_seconds > MAX_AUDIO_SECONDS:
        print("AUDIO_LENGTE is begrensd op maximaal 03:00:00.")

    feed_items: list[dict] = []
    keep_filenames: set[str] = set()
    all_programs_successful = True

    for program_index, program in enumerate(PROGRAMS):
        slug = str(program["slug"])
        name = str(program["name"])
        requested_episode_count = max(
            1,
            safe_int(program.get("aantal_afleveringen"), 1),
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
                rf"/{re.escape(slug)}/(?:{re.escape(slug)}/)?(\d+)",
                page_url,
            )

            if not id_match:
                id_match = re.search(r"/(\d+)(?:[/?#]|$)", page_url)

            episode_id = (
                id_match.group(1)
                if id_match
                else re.sub(r"[^A-Za-z0-9_-]+", "-", page_url.rstrip("/").split("/")[-1])
            )

            file_prefix = f"{slug}_{episode_id}"
            audio_url = str(info["audio_url"])
            upload_date = info.get("upload_date")
            published_at = info.get("published_at")
            presenter = str(info.get("presenter_name") or "")
            episode_title = str(info.get("episode_title") or "")
            album_title = build_album_title(name, upload_date)

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
                mp3_filename = f"{file_prefix}_uur{hour_number}.mp3"
                mp3_path = MP3_DIR / mp3_filename

                # Dit is de "originele naam/omschrijving" die overal zichtbaar is.
                title = build_audio_title(
                    program_name=name,
                    hour_number=hour_number,
                    upload_date=upload_date,
                    presenter=presenter,
                )

                if FORCE_REBUILD_MP3 or not mp3_is_usable(mp3_path):
                    print(f"Converteren {title}...")

                    converted = convert_to_mp3(
                        source_url=audio_url,
                        output_path=mp3_path,
                        start_seconds=start_seconds,
                        duration_seconds=duration_seconds,
                        title=title,
                        album_title=album_title,
                        program_name=name,
                        episode_title=episode_title,
                        hour_number=hour_number,
                        total_tracks=number_of_chunks,
                    )

                    if not converted:
                        mp3_path.unlink(missing_ok=True)
                        continue
                else:
                    print(f"Bestaat al: {mp3_filename}")

                ensure_mp3_metadata(
                    path=mp3_path,
                    title=title,
                    album_title=album_title,
                    program_name=name,
                    episode_title=episode_title,
                    hour_number=hour_number,
                    total_tracks=number_of_chunks,
                )

                keep_filenames.add(mp3_filename)
                audio_url_for_feed = f"{SITE_BASE}/mp3/{mp3_filename}"

                feed_items.append(
                    {
                        "title": title,
                        "album_title": album_title,
                        "filename": mp3_filename,
                        "url": audio_url_for_feed,
                        "page_url": page_url,
                        "guid": f"{page_url}#uur{hour_number}",
                        "date": upload_date,
                        "published_at": published_at,
                        "local_size": str(mp3_path.stat().st_size),
                        "program_name": name,
                        "program_slug": slug,
                        "program_index": program_index,
                        "episode_id": episode_id,
                        "episode_title": episode_title,
                        "chunk_index": hour_number,
                        "duration_sec": duration_seconds,
                    }
                )

                successful_chunks += 1
                print(f"OK: {mp3_filename} → {title}")

            if successful_chunks == number_of_chunks:
                processed_count += 1
            elif successful_chunks > 0:
                processed_count += 1
                all_programs_successful = False
                print(
                    f"Waarschuwing: {name} aflevering {episode_id} heeft "
                    f"{successful_chunks} van {number_of_chunks} delen."
                )

        if processed_count < requested_episode_count:
            print(
                f"Waarschuwing: voor {name} zijn "
                f"{processed_count} van de "
                f"{requested_episode_count} gewenste afleveringen verwerkt."
            )
            all_programs_successful = False

    if not feed_items:
        print("FOUT: er zijn geen geldige House Party-items gemaakt.")
        return 1

    if all_programs_successful:
        cleanup_old_mp3s(keep_filenames)
    else:
        print(
            "Opschonen van oude MP3's overgeslagen omdat niet alle "
            "programma's volledig konden worden verwerkt."
        )

    # Gecombineerde RSS.
    write_text_file(FEED_PATH, build_rss(feed_items))
    print(f"Klaar: {FEED_PATH} ({len(feed_items)} items)")

    # Per programma één M3U en één bladerbare OPML.
    for program in PROGRAMS:
        program_name = str(program["name"])
        program_slug = str(program["slug"])
        latest_items = latest_program_items(feed_items, program_slug)

        if not latest_items:
            print(
                f"Geen playlistbestanden gemaakt voor {program_name}: "
                "geen geldige items."
            )
            continue

        m3u_path = DOCS_DIR / f"{program_slug}.m3u"
        browse_opml_path = DOCS_DIR / f"{program_slug}.opml"

        write_text_file(
            m3u_path,
            build_program_m3u(feed_items, program_slug),
        )
        write_text_file(
            browse_opml_path,
            build_program_opml(
                feed_items,
                program_name,
                program_slug,
            ),
        )

        available_hours = {
            safe_int(item.get("chunk_index"))
            for item in latest_items
        }

        for hour_number in sorted(available_hours):
            if hour_number not in (1, 2, 3):
                continue

            single_m3u_path = (
                DOCS_DIR / f"{program_slug}{hour_number}.m3u"
            )

            write_text_file(
                single_m3u_path,
                build_single_hour_m3u(
                    feed_items,
                    program_slug,
                    hour_number,
                ),
            )

            print(f"Klaar: {single_m3u_path}")
            print(
                f"  TuneIn uur {hour_number}: "
                f"{SITE_BASE}/{program_slug}{hour_number}.m3u"
            )

        remove_unused_numbered_playlists(
            program_slug,
            available_hours,
        )

        print(f"Klaar: {m3u_path}")
        print(f"Klaar: {browse_opml_path}")
        print(f"  Alles:    {SITE_BASE}/{program_slug}.m3u")
        print(f"  Bekijken: {SITE_BASE}/{program_slug}.opml")

    # Oude House Party-URL behouden.
    if latest_program_items(feed_items, TUNEIN_ALIAS_PROGRAM_SLUG):
        write_text_file(
            TUNEIN_ALIAS_PATH,
            build_program_m3u(
                feed_items,
                TUNEIN_ALIAS_PROGRAM_SLUG,
            ),
        )
        print(f"Compatibiliteitsalias: {SITE_BASE}/tunein.m3u")

    print(f"RSS: {SITE_BASE}/feed.xml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
