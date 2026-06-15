# EPG Aggregator

Automatically aggregates XMLTV Electronic Program Guide data from multiple sources for use with TiviMate and strong8k IPTV. Runs every 6 hours via GitHub Actions and deploys to GitHub Pages.

## Coverage

| Country | Sources |
|---|---|
| Australia | epg.pw (FTA + Foxtel/pay-TV) |
| United Kingdom | epg.pw |
| United States | epg.pw |
| Canada | epg.pw |

## TiviMate / IPTV App Setup

1. Open your IPTV app → **EPG Sources** → **Add**
2. Paste this URL (gzip — works in TiviMate and most apps):

```
https://gjhayes.github.io/EPG/epg.xml.gz
```

The combined feed is published gzip-only (the uncompressed file exceeds
GitHub's 100MB limit). Per-country files are also published as plain XML for
apps that can't read gzip — see the table below.

3. Set the refresh interval to **12 hours** or **daily**
4. Force a refresh — TiviMate will download and index all channels

Individual country files:

| Country | Plain XML | Gzip |
|---|---|---|
| Australia | `https://gjhayes.github.io/EPG/epg_AU.xml` | `epg_AU.xml.gz` |
| United Kingdom | `https://gjhayes.github.io/EPG/epg_GB.xml` | `epg_GB.xml.gz` |
| United States | `https://gjhayes.github.io/EPG/epg_US.xml` | `epg_US.xml.gz` |
| Canada | `https://gjhayes.github.io/EPG/epg_CA.xml` | `epg_CA.xml.gz` |

## Channel ID Troubleshooting

If channels in TiviMate show no guide data after matching:

1. Download your strong8k M3U playlist
2. Find `tvg-id=` attributes — e.g. `tvg-id="3474"` or `tvg-id="ABCNews.au"`
3. Download `epg_AU.xml.gz` and search for `<channel id="...">`
4. If they don't match, edit `config/channel_map.yaml`:

```yaml
mappings:
  "ABCNews.au": "3474"
  "FoxSports1.au": "3520"
```

5. Commit and push — the next workflow run will generate duplicate entries for both IDs.

## Status Page

Live source health and file listing: **https://gjhayes.github.io/EPG/**

## First-Time Setup

After cloning or forking this repo:

1. Go to **Settings → Pages** → Source: **Deploy from a branch** → Branch: `gh-pages` / `/ (root)`
2. *(Recommended)* Add the provider EPG secret — see below
3. Go to **Actions** → **Update EPG Data** → **Run workflow** → set `force_download=true`
4. Wait ~10-15 minutes for the first run to complete and Pages to activate
5. Add the URL to TiviMate

### Provider EPG secret (primary source)

The **primary** EPG source is your IPTV provider's own Xtream guide. It is correct
for every channel (no time-offset issues), includes live-sports listings that no
free EPG carries, and matches channels automatically (same IDs as your M3U).
epg.pw is kept only as a **fallback** for channels the provider EPG doesn't cover.

Add your provider's full XMLTV URL as a repository secret so it never appears in
the code or output:

1. **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `XTREAM_EPG_URL`
3. Value: `http://YOUR_HOST/xmltv.php?username=YOUR_USER&password=YOUR_PASS`

If the secret is **not** set, the build automatically falls back to epg.pw alone
(channel guide times for some channels may be off, and live-sports channels will
show "No listing available").

> Note: GitHub Actions runners must be able to reach your provider's host. Some
> providers block datacenter IPs; if the `xtream_provider` source shows `failed`
> on the status page, the build still works via the epg.pw fallback.

## Local Testing

```bash
pip install -r scripts/requirements.txt
python scripts/merge_epg.py \
  --config config/sources.yaml \
  --channel-map config/channel_map.yaml \
  --output-dir ./output \
  --cache-dir scripts/.cache \
  --countries AU,GB \
  --log-level DEBUG
```

Add `--force-download` to bypass the ETag cache on first run.

## Adding More Sources

Edit `config/sources.yaml` and add a new entry:

```yaml
- id: my_new_source
  name: "My Source"
  url: "https://example.com/epg.xml"
  country: AU
  priority: 3        # higher number = lower priority
  enabled: true
  timeout: 120
  retry: 3
```

The script handles 404s and network failures gracefully — a failed source is skipped and the rest still produce output.
