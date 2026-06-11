#!/usr/bin/env python3
"""
EPG aggregator: downloads XMLTV sources, merges channels and programmes,
outputs gzip-compressed XMLTV files per country and combined.
"""

import argparse
import gzip
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
import yaml

LOG = logging.getLogger("epg")


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


@dataclass
class OutputConfig:
    combined_filename: str
    per_country: bool
    compress: bool
    compress_level: int


@dataclass
class CacheConfig:
    dir: str
    use_conditional_get: bool


@dataclass
class Settings:
    parallel_downloads: int
    retry_delay_seconds: int
    user_agent: str


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


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config(
    config_path: str,
    channel_map_path: str,
) -> Tuple[List[SourceConfig], OutputConfig, CacheConfig, Settings, Dict[str, str]]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    sources = [
        SourceConfig(
            id=s["id"],
            name=s.get("name", s["id"]),
            url=s["url"],
            country=s["country"].upper(),
            priority=s.get("priority", 99),
            enabled=s.get("enabled", True),
            timeout=s.get("timeout", 120),
            retry=s.get("retry", 3),
        )
        for s in cfg.get("sources", [])
        if s.get("enabled", True)
    ]

    out = cfg.get("output", {})
    output = OutputConfig(
        combined_filename=out.get("combined_filename", "epg.xml.gz"),
        per_country=out.get("per_country", True),
        compress=out.get("compress", True),
        compress_level=out.get("compress_level", 9),
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
    )

    with open(channel_map_path) as f:
        cm = yaml.safe_load(f) or {}
    alias_map: Dict[str, str] = cm.get("mappings") or {}

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
                headers = {}
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

            etag = resp.headers.get("ETag", "")
            last_mod = resp.headers.get("Last-Modified", "")
            if etag or last_mod:
                cache.save_meta(source.id, etag, last_mod)

            # Validate it's actually XML (not an HTML error page served as HTTP 200)
            if not _is_xml(xml_path):
                snippet = xml_path.read_bytes()[:120]
                LOG.warning("[%s] downloaded content is not XML: %r", source.id, snippet)
                xml_path.unlink(missing_ok=True)
                last_error = "response was not valid XML"
                continue

            LOG.info("[%s] downloaded %.1fMB", source.id, xml_path.stat().st_size / 1e6)
            return SourceResult(source=source, file_path=xml_path, cached=False, status="ok")

        except requests.RequestException as e:
            last_error = str(e)
            LOG.warning("[%s] attempt %d failed: %s", source.id, attempt + 1, e)

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
                icon = elem.findtext("icon") or None
                icon_src = elem.find("icon").get("src") if elem.find("icon") is not None else None
                channels[cid] = ChannelData(
                    id=cid,
                    display_names=names,
                    icon_src=icon_src,
                    source_id=source.id,
                    country=source.country,
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
    # Sort by priority ascending — lower number = higher priority
    ordered = sorted(source_channels, key=lambda x: x[0].priority)
    merged: Dict[str, ChannelData] = {}
    for _source, channels in ordered:
        for cid, ch in channels.items():
            if cid not in merged:
                merged[cid] = ch  # first (highest priority) wins on collision
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
                country=original.country,
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


def stream_programmes_to_file(
    source_path: Path,
    out,
    valid_channel_ids: Set[str],
    seen_set: Set[Tuple[str, str]],
    alias_reverse: Dict[str, List[str]],
) -> int:
    """Stream <programme> elements from source_path to out for valid channels."""
    count = 0
    try:
        context = ET.iterparse(str(source_path), events=("end",))
        for event, elem in context:
            if elem.tag != "programme":
                elem.clear()
                continue

            channel_id = elem.get("channel", "").strip()
            start = elem.get("start", "").strip()

            if channel_id not in valid_channel_ids:
                elem.clear()
                continue

            key = (channel_id, start)
            if key in seen_set:
                elem.clear()
                continue
            seen_set.add(key)

            # Serialise the programme element preserving all child content
            prog_xml = ET.tostring(elem, encoding="unicode")
            out.write("  " + prog_xml + "\n")
            count += 1

            # Emit duplicate entries for alias IDs
            for alt_id in alias_reverse.get(channel_id, []):
                alt_key = (alt_id, start)
                if alt_key not in seen_set:
                    seen_set.add(alt_key)
                    # Rewrite channel attribute
                    alt_xml = prog_xml.replace(
                        f'channel="{_esc(channel_id)}"',
                        f'channel="{_esc(alt_id)}"',
                        1,
                    )
                    out.write("  " + alt_xml + "\n")
                    count += 1

            elem.clear()
    except ET.ParseError as e:
        LOG.warning("[%s] XML parse error in programme pass: %s", source_path, e)
    return count


def write_xmltv_output(
    output_path: Path,
    channels: Dict[str, ChannelData],
    source_results: List[SourceResult],
    compress: bool,
    compress_level: int,
    country_filter: Optional[str],
    alias_map: Dict[str, str],
) -> Tuple[int, int]:
    """Write a single XMLTV output file. Returns (channel_count, programme_count)."""
    # Build the set of channel IDs to include
    if country_filter:
        filtered_channels = {
            cid: ch for cid, ch in channels.items()
            if ch.country == country_filter
        }
    else:
        filtered_channels = channels

    # Build reverse alias map: canonical_id -> [alt_ids]
    alias_reverse: Dict[str, List[str]] = {}
    for alt_id, canonical_id in alias_map.items():
        alias_reverse.setdefault(canonical_id, []).append(alt_id)

    valid_ids: Set[str] = set(filtered_channels.keys())
    seen_set: Set[Tuple[str, str]] = set()
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    open_fn = gzip.open(output_path, "wt", encoding="utf-8", compresslevel=compress_level) if compress else open(output_path, "w", encoding="utf-8")

    total_programmes = 0
    with open_fn as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(f'<tv date="{now_str}" generator-info-name="EPG-Aggregator" generator-info-url="https://github.com/gjhayes/EPG">\n')

        for cid in sorted(filtered_channels.keys()):
            out.write(_channel_xml(filtered_channels[cid]) + "\n")

        # Process sources in priority order so higher-priority programmes win dedup
        sorted_results = sorted(
            [r for r in source_results if r.file_path and r.status != "failed"],
            key=lambda r: r.source.priority,
        )
        for result in sorted_results:
            if country_filter and result.source.country != country_filter:
                continue
            n = stream_programmes_to_file(
                result.file_path, out, valid_ids, seen_set, alias_reverse
            )
            result.programme_count += n
            total_programmes += n

        out.write("</tv>\n")

    return len(filtered_channels), total_programmes


# ── HTML status page ───────────────────────────────────────────────────────────

def generate_status_html(
    metadata: dict,
    base_url: str,
    output_path: Path,
) -> None:
    files = metadata.get("output_files", [])
    sources = metadata.get("sources", [])
    generated_at = metadata.get("generated_at", "")

    rows_files = ""
    for f in files:
        url = f"{base_url.rstrip('/')}/{f['filename']}"
        rows_files += (
            f'<tr><td><a href="{url}">{f["filename"]}</a></td>'
            f'<td>{f["channels"]:,}</td>'
            f'<td>{f["programmes"]:,}</td>'
            f'<td>{f["size_mb"]:.1f} MB</td></tr>\n'
        )

    rows_sources = ""
    for s in sources:
        status_cls = "ok" if s["status"] in ("ok", "cached") else "fail"
        rows_sources += (
            f'<tr class="{status_cls}"><td>{s["id"]}</td>'
            f'<td>{s["country"]}</td>'
            f'<td>{s["status"]}</td>'
            f'<td>{s["channels"]:,}</td>'
            f'<td>{s.get("error", "")}</td></tr>\n'
        )

    tivimate_url = f"{base_url.rstrip('/')}/epg.xml.gz"

    html = f"""<!DOCTYPE html>
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
<p>Last updated: <strong>{generated_at}</strong></p>

<h2>TiviMate Setup</h2>
<p>In TiviMate: <em>Settings → EPG Sources → Add</em> and paste this URL:</p>
<div class="url-box">{tivimate_url}</div>
<p>Or use individual country files below for a smaller download.</p>

<h2>Output Files</h2>
<table>
<tr><th>File</th><th>Channels</th><th>Programmes</th><th>Size</th></tr>
{rows_files}
</table>

<h2>Source Health</h2>
<table>
<tr><th>Source</th><th>Country</th><th>Status</th><th>Channels</th><th>Error</th></tr>
{rows_sources}
</table>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def write_metadata_json(
    output_dir: Path,
    source_results: List[SourceResult],
    output_files: list,
    generated_at: str,
) -> None:
    data = {
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
                "error": r.error,
            }
            for r in source_results
        ],
        "output_files": output_files,
    }
    (output_dir / "metadata.json").write_text(json.dumps(data, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Merge EPG sources into XMLTV files")
    parser.add_argument("--config", default="config/sources.yaml")
    parser.add_argument("--channel-map", default="config/channel_map.yaml")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--cache-dir", default=None, help="Override cache dir from config")
    parser.add_argument("--countries", default=None, help="Comma-separated country codes to process")
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    force = args.force_download or os.environ.get("FORCE_DOWNLOAD", "").lower() == "true"

    sources, output_cfg, cache_cfg, settings, alias_map = load_config(
        args.config, args.channel_map
    )

    cache_dir = Path(args.cache_dir) if args.cache_dir else Path(cache_cfg.dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    country_filter_set = None
    if args.countries:
        country_filter_set = {c.strip().upper() for c in args.countries.split(",")}
        sources = [s for s in sources if s.country in country_filter_set]

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

    # ── Collect channels from all successful sources ───────────────────────────
    source_channels: List[Tuple[SourceConfig, Dict[str, ChannelData]]] = []
    for result in source_results:
        if result.file_path:
            LOG.info("[%s] collecting channels...", result.source.id)
            ch = collect_channels(result.file_path, result.source)
            result.channel_count = len(ch)
            LOG.info("[%s] found %d channels", result.source.id, len(ch))
            source_channels.append((result.source, ch))

    all_channels = merge_channel_dicts(source_channels)
    all_channels = apply_channel_aliases(all_channels, alias_map)
    LOG.info("Total merged channels: %d", len(all_channels))

    # ── Write output files ─────────────────────────────────────────────────────
    compress = output_cfg.compress and not args.no_compress
    ext = ".gz" if compress else ""
    output_files = []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Combined output
    combined_path = output_dir / output_cfg.combined_filename
    LOG.info("Writing combined output -> %s", combined_path)
    ch_count, prog_count = write_xmltv_output(
        combined_path, all_channels, source_results,
        compress, output_cfg.compress_level,
        country_filter=None,
        alias_map=alias_map,
    )
    size_mb = combined_path.stat().st_size / 1e6
    output_files.append({
        "filename": combined_path.name,
        "channels": ch_count,
        "programmes": prog_count,
        "size_mb": round(size_mb, 2),
    })
    LOG.info("Combined: %d channels, %d programmes, %.1fMB", ch_count, prog_count, size_mb)

    # Per-country outputs
    if output_cfg.per_country:
        countries = sorted({s.country for s in sources})
        if country_filter_set:
            countries = [c for c in countries if c in country_filter_set]
        for country in countries:
            fname = f"epg_{country}.xml{ext}"
            country_path = output_dir / fname
            LOG.info("Writing %s -> %s", country, country_path)
            ch_count, prog_count = write_xmltv_output(
                country_path, all_channels, source_results,
                compress, output_cfg.compress_level,
                country_filter=country,
                alias_map=alias_map,
            )
            size_mb = country_path.stat().st_size / 1e6
            output_files.append({
                "filename": fname,
                "channels": ch_count,
                "programmes": prog_count,
                "size_mb": round(size_mb, 2),
            })
            LOG.info("%s: %d channels, %d programmes, %.1fMB", country, ch_count, prog_count, size_mb)

    # ── Status page and metadata ───────────────────────────────────────────────
    base_url = "https://gjhayes.github.io/EPG"
    write_metadata_json(output_dir, source_results, output_files, generated_at)
    generate_status_html(
        {"generated_at": generated_at, "sources": [
            {"id": r.source.id, "name": r.source.name, "country": r.source.country,
             "status": r.status, "channels": r.channel_count, "error": r.error}
            for r in source_results
        ], "output_files": output_files},
        base_url,
        output_dir / "index.html",
    )

    # Summary — warn about failures but only hard-fail if combined output is empty
    failed = [r for r in source_results if r.status == "failed" and not r.cached]
    if failed:
        LOG.warning("Failed sources (skipped): %s", [r.source.id for r in failed])

    combined_entry = next((f for f in output_files if f["filename"] == output_cfg.combined_filename), None)
    if combined_entry and combined_entry["channels"] == 0:
        LOG.error("Combined output has 0 channels — all sources failed")
        return 1

    LOG.info("Done. Output written to %s/", output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
