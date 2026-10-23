#!/usr/bin/env python3
"""
EPG aggregator: downloads XMLTV sources, merges channels and programmes,
outputs gzip-compressed XMLTV files per country, combined, and a slim feed
containing only the channels referenced by channel_map.yaml.
"""

import argparse
import gzip
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Set, Tuple
from urllib.parse import urlsplit

import requests
import yaml

LOG = logging.getLogger("epg")

MANUAL_SOURCE_ID = "manual_overlay"


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    id: str
    name: str
    url: str
    country: str
    priority: int
    enabled: bool
    timeout: int
    retry: int
    time_shift_hours: int = 0
    multi_country: bool = False
    user_agent: Optional[str] = None
    # Explicit per-channel extra shifts; take precedence over patterns.
    per_channel_shifts: Dict[str, int] = field(default_factory=dict)
    # (compiled regex, extra hours) — first full match wins.
    shift_patterns: List[Tuple[Pattern, int]] = field(default_factory=list)
    # Position in sources.yaml; breaks priority ties deterministically.
    order: int = 0
    # URL came from an env secret: redact its host as well as its query.
    sensitive: bool = False

    def extra_shift(self, channel_id: str) -> int:
        if channel_id in self.per_channel_shifts:
            return self.per_channel_shifts[channel_id]
        for pattern, hours in self.shift_patterns:
            if pattern.fullmatch(channel_id):
                return hours
        return 0

    def total_shift(self, channel_id: str) -> int:
        return self.time_shift_hours + self.extra_shift(channel_id)


@dataclass
class OutputConfig:
    combined_filename: str
    per_country: bool
    compress: bool
    compress_level: int
    slim_filename: Optional[str] = None


@dataclass
class CacheConfig:
    dir: str
    use_conditional_get: bool


@dataclass
class Settings:
    parallel_downloads: int
    retry_delay_seconds: int
    user_agent: str
    prune_ended_hours: float = 6
    min_combined_programmes: int = 0
    max_programme_drop: float = 0.5
    offset_checks: List[dict] = field(default_factory=list)


@dataclass
class ChannelData:
    id: str
    display_names: List[Tuple[str, str]]  # (name, lang)
    icon_src: Optional[str]
    source_id: str
    country: str


@dataclass
class SourceResult:
    source: SourceConfig
    file_path: Optional[Path]
    cached: bool
    status: str  # 'ok' | 'cached' | 'failed'
    channel_count: int = 0
    programme_count: int = 0
    error: str = ""
    channel_ids: Set[str] = field(default_factory=set)


# ── Redaction ──────────────────────────────────────────────────────────────────

def redact(text: str, source: Optional[SourceConfig] = None) -> str:
    """Strip credentials from text bound for logs, metadata.json or the status page.

    The repo, its Actions logs and the Pages site are public, and requests'
    exception messages embed the request URL (``...with url: /xmltv.php?
    username=..&password=..``). GitHub's secret masking only matches the whole
    secret, not such fragments, so remove query strings and userinfo here — and
    the host too for sources whose URL came from a secret.
    """
    if not text:
        return text
    text = re.sub(r"(?i)(https?://)[^/\s@'\"]+@", r"\1", text)
    text = re.sub(r"\?[^\s'\")]+", "?<redacted>", text)
    if source is not None and source.sensitive:
        host = urlsplit(source.url).hostname
        if host:
            text = text.replace(host, "<redacted-host>")
    return text


# ── Config loading ─────────────────────────────────────────────────────────────

def _expand_env_url(url: str) -> Tuple[str, List[str]]:
    """Substitute ${VAR} placeholders from the environment.

    Returns the expanded URL plus a list of any referenced variables that were
    unset or empty — used to disable sources whose secrets aren't configured
    (e.g. the provider EPG when XTREAM_EPG_URL isn't set), so the build never
    leaks credentials into the repo and degrades gracefully without them.
    """
    missing: List[str] = []

    def repl(m):
        val = os.environ.get(m.group(1))
        if not val:
            missing.append(m.group(1))
            return ""
        return val

    return re.sub(r"\$\{(\w+)\}", repl, url), missing


def load_config(
    config_path: str,
    channel_map_path: str,
) -> Tuple[List[SourceConfig], OutputConfig, CacheConfig, Settings, Dict[str, str]]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    sources: List[SourceConfig] = []
    for order, s in enumerate(cfg.get("sources", [])):
        if not s.get("enabled", True):
            continue
        url, missing = _expand_env_url(s["url"])
        if missing:
            LOG.warning(
                "[%s] disabled — required env var(s) not set: %s",
                s["id"], ", ".join(missing),
            )
            continue
        sources.append(SourceConfig(
            id=s["id"],
            name=s.get("name", s["id"]),
            url=url,
            country=s["country"].upper(),
            priority=s.get("priority", 99),
            enabled=True,
            timeout=s.get("timeout", 120),
            retry=s.get("retry", 3),
            time_shift_hours=s.get("time_shift_hours", 0),
            multi_country=s.get("multi_country", False),
            user_agent=s.get("user_agent"),
            per_channel_shifts={
                str(k): int(v)
                for k, v in (s.get("per_channel_shifts") or {}).items()
            },
            shift_patterns=[
                (re.compile(p["pattern"]), int(p["hours"]))
                for p in (s.get("per_channel_shift_patterns") or [])
            ],
            order=order,
            sensitive="${" in s["url"],
        ))

    out = cfg.get("output", {})
    output = OutputConfig(
        combined_filename=out.get("combined_filename", "epg.xml.gz"),
        per_country=out.get("per_country", True),
        compress=out.get("compress", True),
        compress_level=out.get("compress_level", 9),
        slim_filename=out.get("slim_filename"),
    )

    cache_cfg = cfg.get("cache", {})
    cache = CacheConfig(
        dir=cache_cfg.get("dir", "scripts/.cache"),
        use_conditional_get=cache_cfg.get("use_conditional_get", True),
    )

    s = cfg.get("settings", {})
    settings = Settings(
        parallel_downloads=s.get("parallel_downloads", 3),
        retry_delay_seconds=s.get("retry_delay_seconds", 30),
        user_agent=s.get("user_agent", "EPG-Aggregator/1.0"),
        prune_ended_hours=s.get("prune_ended_hours", 6),
        min_combined_programmes=s.get("min_combined_programmes", 0),
        max_programme_drop=s.get("max_programme_drop", 0.5),
        offset_checks=cfg.get("offset_checks") or [],
    )

    with open(channel_map_path) as f:
        cm = yaml.safe_load(f) or {}
    alias_map: Dict[str, str] = {
        str(k): str(v) for k, v in (cm.get("mappings") or {}).items()
    }

    return sources, output, cache, settings, alias_map


# ── Cache management ───────────────────────────────────────────────────────────

class CacheManager:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def meta_path(self, source_id: str) -> Path:
        return self.cache_dir / f"{source_id}.meta.json"

    def xml_path(self, source_id: str) -> Path:
        return self.cache_dir / f"{source_id}.xml"

    def get_meta(self, source_id: str) -> dict:
        p = self.meta_path(source_id)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}

    def save_meta(self, source_id: str, etag: str, last_modified: str) -> None:
        self.meta_path(source_id).write_text(
            json.dumps({"etag": etag, "last_modified": last_modified})
        )

    def get_cached_file(self, source_id: str) -> Optional[Path]:
        p = self.xml_path(source_id)
        return p if p.exists() and p.stat().st_size > 0 else None


# ── Download ───────────────────────────────────────────────────────────────────

def download_source(
    source: SourceConfig,
    cache: CacheManager,
    session: requests.Session,
    use_conditional_get: bool,
    retry_delay: int,
    force: bool = False,
) -> SourceResult:
    xml_path = cache.xml_path(source.id)
    meta = cache.get_meta(source.id) if (use_conditional_get and not force) else {}

    headers: Dict[str, str] = {}
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    # Some providers (e.g. Xtream panels) gate their EPG endpoint on
    # User-Agent and return an empty 200 body to unrecognised clients, so
    # allow a per-source override that mimics a media player.
    if source.user_agent:
        headers["User-Agent"] = source.user_agent

    last_error = ""
    for attempt in range(source.retry + 1):
        if attempt > 0:
            delay = retry_delay * (2 ** (attempt - 1))
            LOG.warning("[%s] retry %d/%d in %ds", source.id, attempt, source.retry, delay)
            time.sleep(delay)
        try:
            resp = session.get(
                source.url,
                headers=headers,
                timeout=source.timeout,
                stream=True,
            )

            if resp.status_code == 304:
                cached_path = cache.get_cached_file(source.id)
                if cached_path:
                    LOG.info("[%s] 304 Not Modified — using cache", source.id)
                    return SourceResult(source=source, file_path=cached_path, cached=True, status="cached")
                LOG.warning("[%s] 304 but no cached file; forcing re-download", source.id)
                headers = {k: v for k, v in headers.items() if k == "User-Agent"}
                continue

            if resp.status_code == 404:
                LOG.warning("[%s] 404 — source not available", source.id)
                return SourceResult(source=source, file_path=None, cached=False, status="failed", error="HTTP 404")

            resp.raise_for_status()

            # Stream to disk
            tmp_path = xml_path.with_suffix(".tmp")
            content_encoding = resp.headers.get("Content-Encoding", "")
            LOG.info("[%s] downloading (encoding=%s)...", source.id, content_encoding or "none")
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)

            # If the server sent gzip-encoded content that requests didn't auto-decode,
            # attempt to detect and decompress manually.
            if _is_gzip(tmp_path):
                LOG.debug("[%s] decompressing gzip content", source.id)
                with gzip.open(tmp_path, "rb") as gz_in:
                    xml_path.write_bytes(gz_in.read())
                tmp_path.unlink()
            else:
                tmp_path.rename(xml_path)

            # Validate it's actually XML (not an HTML error page served as HTTP 200)
            if not _is_xml(xml_path):
                size = xml_path.stat().st_size if xml_path.exists() else 0
                if size == 0:
                    LOG.warning(
                        "[%s] empty 200 response — server likely blocked this "
                        "User-Agent or IP", source.id,
                    )
                    last_error = "empty response (0 bytes)"
                else:
                    LOG.warning("[%s] downloaded content is not XML", source.id)
                    last_error = "response was not valid XML"
                xml_path.unlink(missing_ok=True)
                continue

            # Only record validators once the body is known-good, so a bad
            # response can't pin a later 304 to a missing/invalid cache file.
            etag = resp.headers.get("ETag", "")
            last_mod = resp.headers.get("Last-Modified", "")
            if etag or last_mod:
                cache.save_meta(source.id, etag, last_mod)

            LOG.info("[%s] downloaded %.1fMB", source.id, xml_path.stat().st_size / 1e6)
            return SourceResult(source=source, file_path=xml_path, cached=False, status="ok")

        except requests.RequestException as e:
            last_error = redact(str(e), source)
            LOG.warning("[%s] attempt %d failed: %s", source.id, attempt + 1, last_error)

    # All attempts failed — try to use stale cache
    stale = cache.get_cached_file(source.id)
    if stale:
        LOG.warning("[%s] all attempts failed; using stale cache", source.id)
        return SourceResult(source=source, file_path=stale, cached=True, status="cached", error=last_error)

    return SourceResult(source=source, file_path=None, cached=False, status="failed", error=last_error)


def _is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def _is_xml(path: Path) -> bool:
    """Check that the file starts with an XML declaration or a known XMLTV root tag."""
    try:
        with open(path, "rb") as f:
            header = f.read(256).lstrip()
        return header.startswith(b"<?xml") or header.startswith(b"<tv")
    except Exception:
        return False


# ── XMLTV parsing ──────────────────────────────────────────────────────────────

# Map dotted-channel-ID suffixes (as used by the strong8k M3U / provider EPG)
# to the ISO country codes our per-country output files use.
_SUFFIX_COUNTRY = {
    "au": "AU", "uk": "GB", "gb": "GB", "ca": "CA", "us": "US",
    "nz": "NZ", "ie": "IE",
}


def country_for_channel(channel_id: str, default: str) -> str:
    """Derive a channel's country from its dotted ID suffix when present.

    Provider/M3U IDs encode country in the suffix (e.g. ``ABC.au``, ``bbc1.uk``),
    so multi-country sources still land in the correct per-country file. Numeric
    epg.pw IDs have no suffix and fall back to the source's configured country.
    """
    if "." in channel_id:
        suffix = channel_id.rsplit(".", 1)[1].lower()
        if suffix.isalpha() and len(suffix) <= 3:
            return _SUFFIX_COUNTRY.get(suffix, suffix.upper())
    return default


def collect_channels(file_path: Path, source: SourceConfig) -> Dict[str, ChannelData]:
    channels: Dict[str, ChannelData] = {}
    try:
        context = ET.iterparse(str(file_path), events=("end",))
        for event, elem in context:
            if elem.tag == "channel":
                cid = elem.get("id", "").strip()
                if not cid:
                    elem.clear()
                    continue
                names: List[Tuple[str, str]] = [
                    (dn.text or "", dn.get("lang", ""))
                    for dn in elem.findall("display-name")
                ]
                icon = elem.find("icon")
                channels[cid] = ChannelData(
                    id=cid,
                    display_names=names,
                    icon_src=icon.get("src") if icon is not None else None,
                    source_id=source.id,
                    country=country_for_channel(cid, source.country),
                )
                elem.clear()
            elif elem.tag == "programme":
                # Channels always precede programmes in XMLTV; stop early.
                elem.clear()
                break
    except ET.ParseError as e:
        LOG.warning("[%s] XML parse error in channel pass: %s", source.id, e)
    return channels


def merge_channel_dicts(
    source_channels: List[Tuple[SourceConfig, Dict[str, ChannelData]]]
) -> Dict[str, ChannelData]:
    # Lower priority number wins; config order breaks ties so the result never
    # depends on which download happened to finish first.
    ordered = sorted(source_channels, key=lambda x: (x[0].priority, x[0].order))
    merged: Dict[str, ChannelData] = {}
    for _source, channels in ordered:
        for cid, ch in channels.items():
            if cid not in merged:
                merged[cid] = ch
    return merged


def apply_channel_aliases(
    channels: Dict[str, ChannelData],
    alias_map: Dict[str, str],
) -> Dict[str, ChannelData]:
    """Add synthetic duplicate entries for alternate channel IDs."""
    expanded = dict(channels)
    for alt_id, canonical_id in alias_map.items():
        if canonical_id in channels and alt_id not in expanded:
            original = channels[canonical_id]
            expanded[alt_id] = ChannelData(
                id=alt_id,
                display_names=original.display_names,
                icon_src=original.icon_src,
                source_id=original.source_id,
                country=country_for_channel(alt_id, original.country),
            )
    return expanded


# ── XMLTV output ───────────────────────────────────────────────────────────────

def _channel_xml(ch: ChannelData) -> str:
    parts = [f'  <channel id="{_esc(ch.id)}">']
    for name, lang in ch.display_names:
        if lang:
            parts.append(f'    <display-name lang="{_esc(lang)}">{_esc(name)}</display-name>')
        else:
            parts.append(f'    <display-name>{_esc(name)}</display-name>')
    if ch.icon_src:
        parts.append(f'    <icon src="{_esc(ch.icon_src)}" />')
    parts.append("  </channel>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _shift_xmltv_time(ts: str, hours: int) -> str:
    """Shift an XMLTV timestamp string (YYYYMMDDHHmmss +HHMM) by whole hours."""
    if not ts or not hours:
        return ts
    ts = ts.strip()
    dt_part = ts[:14]
    tz_part = ts[14:].strip() or "+0000"
    try:
        dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S") + timedelta(hours=hours)
        return dt.strftime("%Y%m%d%H%M%S") + " " + tz_part
    except ValueError:
        return ts


def parse_xmltv_time(ts: str) -> Optional[datetime]:
    """Parse an XMLTV timestamp into an aware datetime (missing offset = UTC)."""
    ts = (ts or "").strip()
    try:
        dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", ts[14:].strip())
    offset = timedelta(0)
    if m:
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        if m.group(1) == "-":
            offset = -offset
    return dt.replace(tzinfo=timezone(offset))


def stream_programmes_to_file(
    source_path: Path,
    out,
    source: SourceConfig,
    valid_channel_ids: Set[str],
    seen_set: Set[Tuple[str, str]],
    alias_reverse: Dict[str, List[str]],
    claimed_channels: Optional[Set[str]] = None,
    newly_claimed: Optional[Set[str]] = None,
    prune_before: Optional[datetime] = None,
) -> int:
    """Stream <programme> elements from source_path to out for valid channels.

    A programme is written under its own channel ID and/or each alias of it,
    each independently subject to that ID being in ``valid_channel_ids`` — so a
    per-country or slim file gets an alias's listings even when the canonical
    epg.pw ID belongs to another country (or is excluded entirely).

    Channel-level ownership: an ID already in ``claimed_channels`` (written by a
    higher-priority source) is skipped, so a channel's listings come wholly
    from one source rather than mixing two schedules. IDs this source writes
    are recorded in ``newly_claimed`` for the caller to fold in.

    Programmes that ended before ``prune_before`` are dropped.
    """
    claimed = claimed_channels or set()
    written = newly_claimed if newly_claimed is not None else set()
    shift_cache: Dict[str, int] = {}
    count = 0
    try:
        # Use start+end events so we can grab the root and clear it after each
        # programme. We must NOT clear child elements (title/desc/etc) on their
        # own "end" event, or their text would be wiped before the parent
        # <programme> is serialised.
        context = ET.iterparse(str(source_path), events=("start", "end"))
        _, root = next(context)
        for event, elem in context:
            if event != "end" or elem.tag != "programme":
                continue

            channel_id = elem.get("channel", "").strip()
            write_canonical = channel_id in valid_channel_ids and channel_id not in claimed
            alt_ids = [
                a for a in alias_reverse.get(channel_id, ())
                if a in valid_channel_ids and a not in claimed
            ]
            if not write_canonical and not alt_ids:
                root.clear()
                continue

            # Apply the time shift before dedup/pruning so both use actual
            # broadcast time. Explicit/pattern per-channel shifts stack on the
            # source-level shift (e.g. epg.pw's 12xxx batch needs −16 h total).
            shift = shift_cache.get(channel_id)
            if shift is None:
                shift = shift_cache[channel_id] = source.total_shift(channel_id)
            start = elem.get("start", "").strip()
            stop = elem.get("stop", "").strip()
            if shift:
                start = _shift_xmltv_time(start, shift)
                elem.set("start", start)
                if stop:
                    stop = _shift_xmltv_time(stop, shift)
                    elem.set("stop", stop)

            if prune_before is not None:
                end = parse_xmltv_time(stop) if stop else parse_xmltv_time(start)
                if end is not None and end < prune_before:
                    root.clear()
                    continue

            prog_xml = ET.tostring(elem, encoding="unicode").strip()

            if write_canonical and (channel_id, start) not in seen_set:
                seen_set.add((channel_id, start))
                out.write("  " + prog_xml + "\n")
                written.add(channel_id)
                count += 1

            for alt_id in alt_ids:
                if (alt_id, start) in seen_set:
                    continue
                seen_set.add((alt_id, start))
                alt_xml = prog_xml.replace(
                    f'channel="{_esc(channel_id)}"',
                    f'channel="{_esc(alt_id)}"',
                    1,
                )
                out.write("  " + alt_xml + "\n")
                written.add(alt_id)
                count += 1

            # Drop all accumulated elements from the root to keep memory flat
            root.clear()
    except ET.ParseError as e:
        LOG.warning("[%s] XML parse error in programme pass: %s", source.id, e)
    return count


def write_xmltv_output(
    output_path: Path,
    channels: Dict[str, ChannelData],
    source_results: List[SourceResult],
    compress: bool,
    compress_level: int,
    alias_map: Dict[str, str],
    include: Optional[Set[str]] = None,
    prune_before: Optional[datetime] = None,
    record_stats: bool = False,
) -> Tuple[int, int]:
    """Write a single XMLTV output file. Returns (channel_count, programme_count).

    ``include`` limits the file to those channel IDs (None = every channel).
    ``record_stats`` adds programme counts to each SourceResult — set it for
    one pass only (the combined file) so metadata isn't multiplied per file.
    """
    filtered_channels = (
        channels if include is None
        else {cid: ch for cid, ch in channels.items() if cid in include}
    )
    valid_ids: Set[str] = set(filtered_channels)

    # Reverse alias map: canonical_id -> [alt_ids]
    alias_reverse: Dict[str, List[str]] = {}
    for alt_id, canonical_id in alias_map.items():
        alias_reverse.setdefault(canonical_id, []).append(alt_id)

    # Canonical IDs whose programmes feed this file (directly or via an alias);
    # a source carrying none of them is skipped without parsing it.
    needed = set(valid_ids)
    for canonical_id, alts in alias_reverse.items():
        if any(a in valid_ids for a in alts):
            needed.add(canonical_id)

    seen_set: Set[Tuple[str, str]] = set()
    claimed_channels: Set[str] = set()
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    open_fn = (
        gzip.open(output_path, "wt", encoding="utf-8", compresslevel=compress_level)
        if compress else open(output_path, "w", encoding="utf-8")
    )

    total_programmes = 0
    with open_fn as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(f'<tv date="{now_str}" generator-info-name="EPG-Aggregator" generator-info-url="https://github.com/gjhayes/EPG">\n')

        for cid in sorted(filtered_channels):
            out.write(_channel_xml(filtered_channels[cid]) + "\n")

        # Sources in (priority, config order) so higher-priority programmes win
        # ownership/dedup, deterministically.
        sorted_results = sorted(
            [r for r in source_results if r.file_path and r.status != "failed"],
            key=lambda r: (r.source.priority, r.source.order),
        )
        for result in sorted_results:
            if result.channel_ids and not (result.channel_ids & needed):
                continue
            source_written: Set[str] = set()
            n = stream_programmes_to_file(
                result.file_path, out, result.source, valid_ids, seen_set,
                alias_reverse,
                claimed_channels=claimed_channels,
                newly_claimed=source_written,
                prune_before=prune_before,
            )
            claimed_channels |= source_written
            if record_stats:
                result.programme_count += n
            total_programmes += n

        out.write("</tv>\n")

    return len(filtered_channels), total_programmes


# ── Offset sanity checks ───────────────────────────────────────────────────────

def run_offset_checks(
    source_results: List[SourceResult],
    checks: List[dict],
) -> List[dict]:
    """Verify time offsets against programmes with a fixed local start time.

    Each check names a source, a raw channel ID, a title substring and the
    local wall-clock time (in an IANA timezone) at which that programme always
    starts. The check passes if any matching programme lands on that time after
    the source's configured shifts — so a change in epg.pw's offsets (or a DST
    edge we've mis-modelled) shows up as a warning instead of silently
    shifting everyone's guide.
    """
    from zoneinfo import ZoneInfo

    by_id = {r.source.id: r for r in source_results if r.file_path}
    by_source: Dict[str, List[dict]] = {}
    for c in checks:
        by_source.setdefault(c["source"], []).append(c)

    results: List[dict] = []
    for source_id, group in by_source.items():
        r = by_id.get(source_id)
        if r is None:
            for c in group:
                results.append({"name": c["name"], "status": "skipped",
                                "detail": f"source {source_id} unavailable"})
            continue
        wanted = {str(c["channel"]) for c in group}
        seen: Dict[str, List[Tuple[datetime, str]]] = {}
        try:
            for _ev, elem in ET.iterparse(str(r.file_path), events=("end",)):
                if elem.tag == "programme":
                    cid = elem.get("channel", "")
                    if cid in wanted:
                        start = parse_xmltv_time(
                            _shift_xmltv_time(elem.get("start", ""), r.source.total_shift(cid)))
                        if start:
                            seen.setdefault(cid, []).append((start, elem.findtext("title") or ""))
                    elem.clear()
                elif elem.tag == "channel":
                    elem.clear()
        except ET.ParseError as e:
            LOG.warning("[%s] parse error during offset checks: %s", source_id, e)

        for c in group:
            tz = ZoneInfo(c["timezone"])
            want_hm = c["local_time"]
            needle = c["title_contains"].lower()
            cands = [(s, t) for s, t in seen.get(str(c["channel"]), []) if needle in t.lower()]
            if not cands:
                results.append({"name": c["name"], "status": "skipped",
                                "detail": "no matching programmes in feed window"})
                continue
            local = sorted({s.astimezone(tz).strftime("%H:%M") for s, _ in cands})
            ok = want_hm in local
            results.append({
                "name": c["name"],
                "status": "pass" if ok else "fail",
                "detail": f"expected {want_hm} {c['timezone']}; saw {', '.join(local[:8])}",
            })
    return results


# ── Output sanity gate ─────────────────────────────────────────────────────────

def sanity_problem(
    combined_programmes: int,
    settings: Settings,
    previous_metadata: Optional[Path],
) -> Optional[str]:
    """Return a reason not to deploy this build, or None if it looks healthy."""
    if combined_programmes < settings.min_combined_programmes:
        return (f"combined feed has {combined_programmes} programmes, below the "
                f"floor of {settings.min_combined_programmes}")
    if previous_metadata and previous_metadata.exists():
        try:
            prev = json.loads(previous_metadata.read_text())
            prev_files = {f["filename"]: f for f in prev.get("output_files", [])}
            prev_n = next(
                (f["programmes"] for name, f in prev_files.items()
                 if name.startswith("epg.xml")), None)
        except Exception as e:
            LOG.warning("could not read previous metadata (%s); skipping drop check", e)
            prev_n = None
        if prev_n:
            floor = int(prev_n * (1 - settings.max_programme_drop))
            if combined_programmes < floor:
                return (f"combined feed dropped to {combined_programmes} programmes from "
                        f"{prev_n} last deploy (more than {settings.max_programme_drop:.0%})")
    return None


# ── HTML status page ───────────────────────────────────────────────────────────

def generate_status_html(
    metadata: dict,
    base_url: str,
    output_path: Path,
) -> None:
    e = html.escape
    files = metadata.get("output_files", [])
    sources = metadata.get("sources", [])
    checks = metadata.get("offset_checks", [])
    generated_at = metadata.get("generated_at", "")
    base = base_url.rstrip("/")

    rows_files = ""
    for f in files:
        rows_files += (
            f'<tr><td><a href="{e(base)}/{e(f["filename"])}">{e(f["filename"])}</a></td>'
            f'<td>{f["channels"]:,}</td>'
            f'<td>{f["programmes"]:,}</td>'
            f'<td>{f["size_mb"]:.1f} MB</td></tr>\n'
        )

    rows_sources = ""
    for s in sources:
        status_cls = "ok" if s["status"] in ("ok", "cached") else "fail"
        rows_sources += (
            f'<tr class="{status_cls}"><td>{e(s["id"])}</td>'
            f'<td>{e(s["country"])}</td>'
            f'<td>{e(s["status"])}</td>'
            f'<td>{s["channels"]:,}</td>'
            f'<td>{e(s.get("error", ""))}</td></tr>\n'
        )

    rows_checks = ""
    for c in checks:
        cls = {"pass": "ok", "fail": "fail"}.get(c["status"], "")
        rows_checks += (
            f'<tr class="{cls}"><td>{e(c["name"])}</td><td>{e(c["status"])}</td>'
            f'<td>{e(c["detail"])}</td></tr>\n'
        )

    slim = next((f["filename"] for f in files if "slim" in f["filename"]), None)
    combined = next((f["filename"] for f in files if f["filename"].startswith("epg.xml")), "epg.xml.gz")
    recommend = f"{base}/{slim or combined}"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EPG Status</title>
<style>
body{{font-family:sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
h1{{color:#333}}
table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}
th,td{{border:1px solid #ddd;padding:.5rem .75rem;text-align:left}}
th{{background:#f5f5f5}}
tr.ok td{{color:#2d7d2d}}
tr.fail td{{color:#c0392b}}
.url-box{{background:#f0f0f0;padding:.75rem 1rem;border-radius:4px;font-family:monospace;word-break:break-all}}
</style>
</head>
<body>
<h1>EPG Aggregator</h1>
<p>Last updated: <strong>{e(generated_at)}</strong></p>

<h2>TiviMate Setup</h2>
<p>In TiviMate: <em>Settings → EPG Sources → Add</em> and paste this URL:</p>
<div class="url-box">{e(recommend)}</div>
<p>The slim feed contains only channels mapped to the strong8k playlist. Use
<code>{e(combined)}</code> for every channel, or a per-country file below.</p>

<h2>Output Files</h2>
<table>
<tr><th>File</th><th>Channels</th><th>Programmes</th><th>Size</th></tr>
{rows_files}
</table>

<h2>Time-offset Checks</h2>
<table>
<tr><th>Check</th><th>Result</th><th>Detail</th></tr>
{rows_checks}
</table>

<h2>Source Health</h2>
<table>
<tr><th>Source</th><th>Country</th><th>Status</th><th>Channels</th><th>Error</th></tr>
{rows_sources}
</table>
</body>
</html>
"""
    output_path.write_text(page, encoding="utf-8")


def build_metadata(
    source_results: List[SourceResult],
    output_files: list,
    offset_checks: List[dict],
    generated_at: str,
) -> dict:
    return {
        "generated_at": generated_at,
        "sources": [
            {
                "id": r.source.id,
                "name": r.source.name,
                "country": r.source.country,
                "status": r.status,
                "channels": r.channel_count,
                "programmes": r.programme_count,
                "cached": r.cached,
                "error": redact(r.error, r.source),
            }
            for r in source_results
        ],
        "output_files": output_files,
        "offset_checks": offset_checks,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Merge EPG sources into XMLTV files")
    parser.add_argument("--config", default="config/sources.yaml")
    parser.add_argument("--channel-map", default="config/channel_map.yaml")
    parser.add_argument("--manual-epg", default="config/manual_epg.xml",
                        help="Optional local XMLTV file merged verbatim into every output "
                             "(no time-shift) for events no upstream source labels")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--cache-dir", default=None, help="Override cache dir from config")
    parser.add_argument("--countries", default=None, help="Comma-separated country codes to process")
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--previous-metadata", default=None,
                        help="metadata.json from the last deploy; refuse to finish if the "
                             "combined programme count collapsed (see settings.max_programme_drop)")
    parser.add_argument("--allow-drop", action="store_true",
                        help="Skip the programme-count sanity gate")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # urllib3 logs full request URLs (incl. credentials) at DEBUG.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    force = args.force_download or os.environ.get("FORCE_DOWNLOAD", "").lower() == "true"
    allow_drop = args.allow_drop or os.environ.get("ALLOW_DROP", "").lower() == "true"

    sources, output_cfg, cache_cfg, settings, alias_map = load_config(
        args.config, args.channel_map
    )

    cache_dir = Path(args.cache_dir) if args.cache_dir else Path(cache_cfg.dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    country_filter_set = None
    if args.countries:
        country_filter_set = {c.strip().upper() for c in args.countries.split(",")}
        sources = [
            s for s in sources
            if s.multi_country or s.country in country_filter_set
        ]

    cache = CacheManager(cache_dir)

    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent, "Accept-Encoding": "gzip"})

    # ── Download all sources in parallel ──────────────────────────────────────
    LOG.info("Downloading %d sources (parallel=%d)...", len(sources), settings.parallel_downloads)
    source_results: List[SourceResult] = []
    with ThreadPoolExecutor(max_workers=settings.parallel_downloads) as pool:
        futures = {
            pool.submit(
                download_source,
                src, cache, session,
                cache_cfg.use_conditional_get,
                settings.retry_delay_seconds,
                force,
            ): src
            for src in sources
        }
        for future in as_completed(futures):
            result = future.result()
            source_results.append(result)
            LOG.info(
                "[%s] status=%s cached=%s error=%s",
                result.source.id, result.status, result.cached, result.error or "-"
            )

    # ── Manual EPG overlay ─────────────────────────────────────────────────────
    # A local XMLTV file merged verbatim (no time-shift) as a highest-priority
    # pseudo-source, so its channels win ownership and its programmes are written
    # first. Used for events no upstream feed labels (e.g. a provider stream with
    # no tvg-id / no guide). Its channels carry dotted country suffixes so they
    # route into the correct per-country files via the normal pipeline.
    manual_path = Path(args.manual_epg) if args.manual_epg else None
    if manual_path and manual_path.exists() and _is_xml(manual_path):
        manual_src = SourceConfig(
            id=MANUAL_SOURCE_ID, name="Manual EPG overlay", url=str(manual_path),
            country="ALL", priority=0, enabled=True, timeout=0, retry=0,
            time_shift_hours=0, multi_country=True, order=-1,
        )
        source_results.append(SourceResult(
            source=manual_src, file_path=manual_path, cached=False, status="ok",
        ))
        LOG.info("[%s] merging overlay from %s", MANUAL_SOURCE_ID, manual_path)
    elif args.manual_epg:
        LOG.info("[%s] no overlay file at %s (skipping)", MANUAL_SOURCE_ID, args.manual_epg)

    # Deterministic order regardless of download completion order.
    source_results.sort(key=lambda r: (r.source.priority, r.source.order))

    # ── Collect channels from all successful sources ───────────────────────────
    source_channels: List[Tuple[SourceConfig, Dict[str, ChannelData]]] = []
    for result in source_results:
        if result.file_path:
            ch = collect_channels(result.file_path, result.source)
            result.channel_count = len(ch)
            result.channel_ids = set(ch)
            LOG.info("[%s] found %d channels", result.source.id, len(ch))
            source_channels.append((result.source, ch))

    all_channels = merge_channel_dicts(source_channels)
    all_channels = apply_channel_aliases(all_channels, alias_map)
    LOG.info("Total merged channels: %d", len(all_channels))

    missing_targets = sorted({c for c in alias_map.values() if c not in all_channels})
    if missing_targets:
        LOG.warning("%d channel_map targets not present in any source: %s",
                    len(missing_targets), ", ".join(missing_targets[:20]))

    prune_before = (
        datetime.now(timezone.utc) - timedelta(hours=settings.prune_ended_hours)
        if settings.prune_ended_hours is not None else None
    )

    # ── Write output files ─────────────────────────────────────────────────────
    compress = output_cfg.compress and not args.no_compress
    ext = ".gz" if compress else ""
    output_files = []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def write(path: Path, include: Optional[Set[str]], label: str, stats: bool = False):
        LOG.info("Writing %s -> %s", label, path)
        ch_count, prog_count = write_xmltv_output(
            path, all_channels, source_results, compress, output_cfg.compress_level,
            alias_map=alias_map, include=include, prune_before=prune_before,
            record_stats=stats,
        )
        size_mb = path.stat().st_size / 1e6
        output_files.append({
            "filename": path.name,
            "channels": ch_count,
            "programmes": prog_count,
            "size_mb": round(size_mb, 2),
        })
        LOG.info("%s: %d channels, %d programmes, %.1fMB", label, ch_count, prog_count, size_mb)
        return prog_count

    combined_name = output_cfg.combined_filename
    if not compress and combined_name.endswith(".gz"):
        combined_name = combined_name[:-3]
    combined_programmes = write(output_dir / combined_name, None, "combined", stats=True)

    # Slim feed: only the IDs the strong8k playlist uses (channel_map aliases)
    # plus manual-overlay channels — a fraction of the size for TiviMate.
    if output_cfg.slim_filename:
        slim_ids = {a for a in alias_map if a in all_channels}
        slim_ids |= {cid for cid, ch in all_channels.items() if ch.source_id == MANUAL_SOURCE_ID}
        slim_name = output_cfg.slim_filename
        if not compress and slim_name.endswith(".gz"):
            slim_name = slim_name[:-3]
        write(output_dir / slim_name, slim_ids, "slim")

    # Per-country outputs — derive the country set from the actual channels
    # (their dotted-ID suffix / source country) rather than source.country, so
    # multi-country sources like the provider EPG don't create an "epg_ALL" file.
    if output_cfg.per_country:
        single_country = {s.country for s in sources if not s.multi_country}
        channel_countries = {ch.country for ch in all_channels.values()}
        countries = sorted(single_country & channel_countries)
        if country_filter_set:
            countries = [c for c in countries if c in country_filter_set]
        for country in countries:
            ids = {cid for cid, ch in all_channels.items() if ch.country == country}
            write(output_dir / f"epg_{country}.xml{ext}", ids, country)

    # ── Offset checks ──────────────────────────────────────────────────────────
    checks = run_offset_checks(source_results, settings.offset_checks)
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for c in checks:
        LOG.info("offset check [%s] %s — %s", c["status"], c["name"], c["detail"])
        if c["status"] == "fail" and in_actions:
            print(f"::warning title=EPG offset check failed::{c['name']}: {c['detail']}")

    # ── Status page and metadata ───────────────────────────────────────────────
    base_url = "https://gjhayes.github.io/EPG"
    metadata = build_metadata(source_results, output_files, checks, generated_at)
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    generate_status_html(metadata, base_url, output_dir / "index.html")

    failed = [r for r in source_results if r.status == "failed"]
    if failed:
        LOG.warning("Failed sources (skipped): %s", [r.source.id for r in failed])

    if not all_channels or combined_programmes == 0:
        LOG.error("Combined output is empty — all sources failed")
        return 1

    problem = None if allow_drop else sanity_problem(
        combined_programmes, settings,
        Path(args.previous_metadata) if args.previous_metadata else None,
    )
    if problem:
        LOG.error("Refusing to publish: %s (re-run with --allow-drop / ALLOW_DROP=true "
                  "if this is expected)", problem)
        return 3

    LOG.info("Done. Output written to %s/", output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
