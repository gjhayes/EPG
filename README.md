# EPG Aggregator

Automatically aggregates XMLTV Electronic Program Guide data from multiple sources for use with TiviMate and strong8k IPTV. Runs every 6 hours via GitHub Actions and deploys to GitHub Pages.

## Coverage

| Country | Sources |
|---|---|
| Australia | epg.pw (FTA + Foxtel/pay-TV), xmltv.net |
| United Kingdom | epg.pw, xmltv.net |
| United States | epg.pw, xmltv.net |
| Canada | epg.pw, xmltv.net |
| Hungary | epg.pw, Rytec |
| Serbia | epg.pw |
| Croatia | epg.pw |
| Italy | epg.pw, xmltv.net |
| Spain | epg.pw, xmltv.net |
| Greece | epg.pw |
| Montenegro | epg.pw |

## TiviMate Setup

1. Open TiviMate → **Settings** → **EPG Sources** → **Add**
2. Paste this URL:

```
https://gjhayes.github.io/EPG/epg.xml.gz
```

3. Set the refresh interval to **12 hours** or **daily**
4. Force a refresh — TiviMate will download and index all channels

Individual country files (smaller downloads):

| Country | URL |
|---|---|
| Australia | `https://gjhayes.github.io/EPG/epg_AU.xml.gz` |
| United Kingdom | `https://gjhayes.github.io/EPG/epg_GB.xml.gz` |
| United States | `https://gjhayes.github.io/EPG/epg_US.xml.gz` |
| Canada | `https://gjhayes.github.io/EPG/epg_CA.xml.gz` |
| Hungary | `https://gjhayes.github.io/EPG/epg_HU.xml.gz` |
| Serbia | `https://gjhayes.github.io/EPG/epg_RS.xml.gz` |
| Croatia | `https://gjhayes.github.io/EPG/epg_HR.xml.gz` |
| Italy | `https://gjhayes.github.io/EPG/epg_IT.xml.gz` |
| Spain | `https://gjhayes.github.io/EPG/epg_ES.xml.gz` |
| Greece | `https://gjhayes.github.io/EPG/epg_GR.xml.gz` |
| Montenegro | `https://gjhayes.github.io/EPG/epg_ME.xml.gz` |

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
2. Go to **Actions** → **Update EPG Data** → **Run workflow** → set `force_download=true`
3. Wait ~10-15 minutes for the first run to complete and Pages to activate
4. Add the URL to TiviMate

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
