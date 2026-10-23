# EPG Aggregator

Aggregates XMLTV guide data for TiviMate with a strong8k IPTV subscription.
Pulls epg.pw for Australia, UK, US and Canada, corrects epg.pw's time offsets,
maps the provider's channel IDs onto epg.pw's, and publishes to GitHub Pages
every 6 hours.

Status page (source health, offset checks, file sizes): **https://gjhayes.github.io/EPG/**

## TiviMate setup

Use two EPG sources, in this priority order:

1. **Your provider's EPG** (strong8k Xtream `xmltv.php` URL) — accurate and has
   live sports (Fox Sports, F1, AFL…). Add it in TiviMate directly; it can't be
   fetched from GitHub because the provider blocks GitHub's IPs.
2. **This feed** — fills the channels the provider EPG leaves empty:

```
https://gjhayes.github.io/EPG/epg_slim.xml.gz
```

`epg_slim` contains only channels mapped to the strong8k playlist (~0.8 MB),
so it refreshes quickly. Set refresh to 12–24 hours.

Other files:

| File | Contents |
|---|---|
| `epg_slim.xml.gz` | Mapped strong8k channels + manual overlay (recommended) |
| `epg.xml.gz` | Every channel from every source (~13 MB) |
| `epg_AU` / `epg_GB` (`epg_UK`) / `epg_US` / `epg_CA` `.xml.gz` | Per country |

Plain `.xml` copies are published for files under GitHub's 100 MB limit.

## How times are corrected

epg.pw labels every programme `+0000`, but the times are really UTC+8 (China
time, no DST), so each epg.pw source has `time_shift_hours: -8`. Its 5-digit
`12xxx` channel IDs (GB feed only: Sky, E4, More4, BBC regions, CNBC…) are
converted twice and need a further −8 h, applied by the
`per_channel_shift_patterns` rule on `epg_pw_gb`.

Every build re-verifies this against programmes with fixed local start times
(`offset_checks` in `config/sources.yaml`, e.g. BBC News at 18:00 UK, ABC News
at 19:00 Sydney). A failure shows as a warning on the Actions run and on the
status page.

## Channel mapping

`config/channel_map.yaml` maps the playlist's dotted IDs (`SkyAtlantic.uk`) to
epg.pw numeric IDs (`12366`). Each mapping duplicates the channel and its
programmes under the playlist ID. The build log lists any mapping whose target
has disappeared from epg.pw ("channel_map targets not present").

Some channels are deliberately unmapped (e.g. Fox Sports AU) because epg.pw only
has placeholder listings for them — mapping them would mask the provider EPG.

To add a mapping: find the channel's `tvg-id` in the M3U, find the matching
`<channel id="...">` in `epg.xml.gz`, add `"tvg-id": "epg.pw id"`, and push.

## Manual overlay (events no source lists)

`config/manual_epg.xml` is a hand-written XMLTV file merged into every output
with top priority and no time shift — for a stream with no guide anywhere
(e.g. an overflow sports channel during a tournament). Its channels are always
in the slim feed. In TiviMate, long-press the stream → Edit → EPG → pick the
overlay channel. Ended programmes are pruned automatically; delete finished
events so the channel disappears. See the comment in the file for the format.

## Deploy safety

The workflow:

- runs the test suite first;
- refuses to publish if the combined feed is below `min_combined_programmes`
  or dropped by more than `max_programme_drop` versus the last deploy (dispatch
  manually with `allow_drop=true` if a drop is expected);
- redacts URLs/credentials from errors written to logs, `metadata.json` and the
  status page;
- deploys gh-pages as a single orphan commit, so history doesn't grow.

The schedule runs on the **default branch (`main`)** — changes on other
branches only go live until the next scheduled run, so merge to `main`.

## Provider EPG via GitHub (disabled)

`xtream_provider` in `config/sources.yaml` can pull the provider EPG from the
`XTREAM_EPG_URL` repository secret, but the provider blocks GitHub-hosted
runners, so it's `enabled: false`. It would work on a self-hosted runner.

## Local run

```bash
pip install -r scripts/requirements-dev.txt
python -m pytest -q tests
python scripts/merge_epg.py --output-dir ./output --log-level INFO
# options: --countries AU,GB  --no-compress  --force-download
#          --previous-metadata metadata.json  --allow-drop
```

## Adding a source

```yaml
- id: my_source
  name: "My Source"
  url: "https://example.com/epg.xml"
  country: AU
  priority: 3          # lower number wins channel ownership
  enabled: true
  timeout: 120
  retry: 3
  time_shift_hours: 0
```

A source that 404s, times out or serves non-XML is skipped (falling back to its
last cached copy if one exists); the rest still publish.
