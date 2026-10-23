import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_epg as m  # noqa: E402


def src(**kw):
    base = dict(id="s", name="s", url="https://example.com/epg.xml", country="GB",
                priority=2, enabled=True, timeout=1, retry=0)
    base.update(kw)
    return m.SourceConfig(**base)


def xmltv(tmp_path, body, name="feed.xml"):
    p = tmp_path / name
    p.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n{body}\n</tv>\n')
    return p


def prog(channel, start, stop, title="T"):
    return (f'<programme start="{start} +0000" stop="{stop} +0000" channel="{channel}">'
            f'<title>{title}</title></programme>')


# ── time handling ──────────────────────────────────────────────────────────────

def test_shift_rolls_over_day_and_keeps_offset_label():
    assert m._shift_xmltv_time("20261023060000 +0000", -8) == "20261022220000 +0000"
    assert m._shift_xmltv_time("20261023060000", 0) == "20261023060000"


def test_parse_xmltv_time_offsets():
    assert m.parse_xmltv_time("20260726180000 +1000") == datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    assert m.parse_xmltv_time("20260101000000 -0530").utcoffset() == -timedelta(hours=5, minutes=30)
    assert m.parse_xmltv_time("garbage") is None


def test_pattern_shift_fullmatch_and_explicit_override():
    s = src(time_shift_hours=-8,
            shift_patterns=[(re.compile(r"12\d{3}"), -8)],
            per_channel_shifts={"12999": 0})
    assert s.total_shift("12385") == -16
    assert s.total_shift("486807") == -8
    assert s.total_shift("122800") == -8      # 6-digit ID must not match
    assert s.total_shift("12999") == -8       # explicit entry wins over pattern


def test_country_for_channel():
    assert m.country_for_channel("ABC.au", "GB") == "AU"
    assert m.country_for_channel("bbc1.uk", "US") == "GB"
    assert m.country_for_channel("486807", "GB") == "GB"


# ── redaction ──────────────────────────────────────────────────────────────────

def test_redact_query_userinfo_and_sensitive_host():
    s = src(url="http://panel.example.net/xmltv.php?username=u&password=p", sensitive=True)
    msg = ("HTTPConnectionPool(host='panel.example.net', port=80): Max retries exceeded "
           "with url: /xmltv.php?username=u&password=p (Caused by timeout)")
    out = m.redact(msg, s)
    assert "password" not in out and "username" not in out
    assert "panel.example.net" not in out
    assert m.redact("http://user:secret@h.com/x") == "http://h.com/x"


# ── programme streaming ────────────────────────────────────────────────────────

def stream(path, source, valid, aliases=None, claimed=None, prune_before=None):
    out = io.StringIO()
    written = set()
    rev = {}
    for alt, canon in (aliases or {}).items():
        rev.setdefault(canon, []).append(alt)
    n = m.stream_programmes_to_file(path, out, source, set(valid), set(), rev,
                                    claimed_channels=claimed or set(),
                                    newly_claimed=written, prune_before=prune_before)
    return n, out.getvalue(), written


def test_alias_written_even_when_canonical_excluded(tmp_path):
    # Slim/per-country files: alias valid, canonical numeric ID not.
    p = xmltv(tmp_path, prog("12084", "20261023180000", "20261023190000"))
    s = src(time_shift_hours=-8, shift_patterns=[(re.compile(r"12\d{3}"), -8)])
    n, text, written = stream(p, s, {"Nickelodeon.us"}, {"Nickelodeon.us": "12084"})
    assert n == 1 and written == {"Nickelodeon.us"}
    assert 'channel="Nickelodeon.us"' in text and 'channel="12084"' not in text
    assert 'start="20261023020000 +0000"' in text          # −16 h applied


def test_alias_not_written_when_not_valid(tmp_path):
    p = xmltv(tmp_path, prog("1", "20261023180000", "20261023190000"))
    n, text, _ = stream(p, src(), {"1"}, {"X.us": "1"})
    assert n == 1 and "X.us" not in text


def test_claimed_ids_skipped_independently(tmp_path):
    p = xmltv(tmp_path, prog("1", "20261023180000", "20261023190000"))
    n, text, written = stream(p, src(), {"1", "A.uk"}, {"A.uk": "1"}, claimed={"1"})
    assert n == 1 and written == {"A.uk"}


def test_prune_drops_ended_programmes(tmp_path):
    p = xmltv(tmp_path, prog("1", "20200101000000", "20200101010000", "old") + "\n"
                        + prog("1", "20991231000000", "20991231010000", "new"))
    n, text, _ = stream(p, src(), {"1"}, prune_before=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert n == 1 and "new" in text and "old" not in text


def test_write_output_slim_include(tmp_path):
    p = xmltv(tmp_path,
              '<channel id="1"><display-name>One</display-name></channel>'
              '<channel id="2"><display-name>Two</display-name></channel>'
              + prog("1", "20991231000000", "20991231010000")
              + prog("2", "20991231000000", "20991231010000"))
    s = src()
    r = m.SourceResult(source=s, file_path=p, cached=False, status="ok")
    chans = m.collect_channels(p, s)
    r.channel_ids = set(chans)
    aliases = {"One.uk": "1"}
    chans = m.apply_channel_aliases(chans, aliases)
    out = tmp_path / "slim.xml"
    ch_count, prog_count = m.write_xmltv_output(out, chans, [r], False, 0, aliases,
                                                include={"One.uk"})
    text = out.read_text()
    assert (ch_count, prog_count) == (1, 1)
    assert '<channel id="One.uk">' in text and 'channel="2"' not in text


def test_merge_channel_dicts_is_deterministic():
    a = src(id="a", order=0)
    b = src(id="b", order=1)
    ch = lambda sid: {"1": m.ChannelData("1", [], None, sid, "GB")}
    merged1 = m.merge_channel_dicts([(b, ch("b")), (a, ch("a"))])
    merged2 = m.merge_channel_dicts([(a, ch("a")), (b, ch("b"))])
    assert merged1["1"].source_id == merged2["1"].source_id == "a"


# ── gates & checks ─────────────────────────────────────────────────────────────

def test_sanity_problem(tmp_path):
    st = m.Settings(1, 1, "ua", min_combined_programmes=100, max_programme_drop=0.5)
    assert "floor" in m.sanity_problem(50, st, None)
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"output_files": [{"filename": "epg.xml.gz", "programmes": 1000}]}))
    assert "dropped" in m.sanity_problem(400, st, prev)
    assert m.sanity_problem(600, st, prev) is None
    assert m.sanity_problem(600, st, tmp_path / "missing.json") is None


def test_offset_checks_pass_and_fail(tmp_path):
    # 18:00 BST == 17:00 UTC; raw epg.pw-style stamp is UTC+8 labelled +0000.
    p = xmltv(tmp_path, prog("486807", "20261023010000", "20261023013000", "BBC News"))
    s = src(id="gb", time_shift_hours=-8)
    r = m.SourceResult(source=s, file_path=p, cached=False, status="ok")
    check = dict(name="c", source="gb", channel="486807", title_contains="bbc news",
                 timezone="Europe/London")
    ok = m.run_offset_checks([r], [dict(check, local_time="18:00")])
    bad = m.run_offset_checks([r], [dict(check, local_time="19:00")])
    missing = m.run_offset_checks([r], [dict(check, title_contains="nope", local_time="18:00")])
    assert ok[0]["status"] == "pass"
    assert bad[0]["status"] == "fail"
    assert missing[0]["status"] == "skipped"
