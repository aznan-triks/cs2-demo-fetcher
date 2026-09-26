"""Tests for cs2_demo_core — run with:  python -m unittest discover -s tests"""

import bz2
import json
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cs2_demo_core as core  # noqa: E402

SAMPLE_GC = ROOT / "tests" / "fixtures" / "gc_recent_matches.bin"
NOISE = random.Random(1).randbytes(4000)   # incompressible demo body
SETTINGS = dict(core.DEFAULT_SETTINGS, retry_backoff=0, boiler_delay=0,
                api_rate_delay=0)


class ShareCodeTests(unittest.TestCase):
    def test_decode_known_code(self):
        self.assertEqual(core.decode_share_code("CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK"),
                         (3230642215713767580, 3230647599455273103, 55788))

    def test_encode_roundtrip(self):
        self.assertEqual(core.encode_share_code(3230642215713767580, 3230647599455273103, 55788),
                         "CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK")

    def test_invalid_codes(self):
        for bad in ["", "CSGO-abc", "CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2x0", None, 123]:
            self.assertFalse(core.valid_share_code(bad), bad)

    def test_selftest(self):
        core.selftest()

    def test_name_format_unchanged(self):
        # GUI naming kept (1000+ existing files use it)
        oid = 3230647599455273103
        self.assertEqual(core.csdm_name(3230642215713767580, oid, 55788),
                         f"match730_003230642215713767580_{oid & 0xFFFFFFFF:010d}_55788")


class GcParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matches = core.parse_match_list(SAMPLE_GC.read_bytes())

    def test_all_matches_read(self):
        self.assertEqual(len(self.matches), 8)
        for m in self.matches:
            self.assertTrue(m["url"].startswith("http://replay"))
            self.assertTrue(m["url"].endswith(".dem.bz2"))

    def test_first_match_fields(self):
        m = self.matches[0]
        self.assertEqual(m["matchid"], 3809357928926806424)
        self.assertEqual(m["reservationid"], 3809360954731266299)
        self.assertEqual(m["tv_port"], 1188481574)
        self.assertEqual(m["game_type"], 16392)
        self.assertEqual(m["players"], 10)
        self.assertEqual(m["rounds"], 16)
        self.assertEqual(m["mode"], "5v5")
        self.assertEqual(m["map"], "de_vertigo")
        # URL embeds reservationId + tv_port → consistent with our fields
        self.assertIn(f"{m['reservationid']:021d}_{m['tv_port']:010d}", m["url"])

    def test_mode_uses_player_count_not_game_type_byte(self):
        # 16394 has the low byte CS:DM calls "wingman", but it is a 10-player match
        m = next(x for x in self.matches if x["game_type"] == 16394)
        self.assertEqual(m["mode"], "5v5")

    def test_modes_by_players(self):
        self.assertEqual(core.match_mode(6), "Rush")
        self.assertEqual(core.match_mode(4), "Wingman")
        self.assertEqual(core.match_mode(10), "5v5")
        self.assertEqual(core.match_mode(7), "Unknown")

    def test_rush_synthetic(self):
        # Build a minimal 3v3 match message: matchid, watchable(tv_port), last round
        def varint(n):
            out = bytearray()
            while True:
                b = n & 0x7F; n >>= 7
                out.append(b | (0x80 if n else 0))
                if not n: return bytes(out)
        def fld(num, wt, payload):
            key = varint((num << 3) | wt)
            return key + (varint(len(payload)) + payload if wt == 2 else payload)
        resv = b"".join(fld(1, 0, varint(1000 + i)) for i in range(6)) + fld(2, 0, varint(9999))
        last = (fld(1, 0, varint(222)) + fld(2, 2, resv)
                + fld(3, 2, b"http://replay1.valve.net/730/x.dem.bz2"))
        watch = fld(2, 0, varint(77))
        match = fld(1, 0, varint(111)) + fld(3, 2, watch) + fld(5, 2, last)
        ms = core.parse_match_list(fld(4, 2, match))
        self.assertEqual(len(ms), 1)
        m = ms[0]
        self.assertEqual((m["matchid"], m["reservationid"], m["tv_port"]), (111, 222, 77))
        self.assertEqual(m["mode"], "Rush")
        self.assertEqual(m["map"], "rush_001")
        self.assertIn("Rush 3v3", core.describe_match(m))
        self.assertIn("game_type=9999", core.describe_match(m))

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            core.parse_match_list(b"\x22\xff\x01abc")

    def test_boiler_match_uses_protobuf_then_regex_fallback(self):
        data = SAMPLE_GC.read_bytes()
        with mock.patch.object(core, "_boiler_call", return_value=(0, data)):
            url, rc, info = core.boiler_match(Path("x"), 3809242271899976095, 1, 1, SETTINGS,
                                              log=lambda *_: None)
        self.assertEqual(rc, 0)
        self.assertIn("003809245662776655952", url)
        self.assertEqual(info["matchid"], 3809242271899976095)
        garbage = b"\x00\xffxx http://replay9.valve.net/730/0001_2.dem.bz2 \x00"
        with mock.patch.object(core, "_boiler_call", return_value=(0, garbage)):
            url, rc, info = core.boiler_match(Path("x"), 1, 1, 1, SETTINGS, log=lambda *_: None)
        self.assertEqual(url, "http://replay9.valve.net/730/0001_2.dem.bz2")


class CursorTests(unittest.TestCase):
    def test_stops_at_first_hole(self):
        tl = [("a", True), ("b", 1), ("c", False), ("d", 2)]
        self.assertEqual(core.advance_cursor(tl, {1, 2}), "b")

    def test_pending_download_blocks(self):
        tl = [("a", True), ("b", 1), ("c", 2)]
        self.assertEqual(core.advance_cursor(tl, {1}), "b")
        self.assertEqual(core.advance_cursor(tl, {1, 2}), "c")

    def test_empty(self):
        self.assertIsNone(core.advance_cursor([], set()))
        self.assertIsNone(core.advance_cursor([("a", False)], set()))


class DedupTests(unittest.TestCase):
    def test_scan_and_presence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "sub").mkdir()
            (p / "match730_003230642215713767580_0000000137_55788.dem").write_bytes(b"x")
            (p / "sub" / "003809360954731266299_1188481574.dem").write_bytes(b"x")
            known = core.scan_folder(p)
            self.assertTrue(core.is_present(known, 3230642215713767580))
            # file named by reservationId (other tools) is detected via oid
            self.assertTrue(core.is_present(known, 42, 3809360954731266299))
            self.assertFalse(core.is_present(known, 42, 43))
            self.assertTrue(core.on_disk(p, 42, 3809360954731266299))
            self.assertFalse(core.on_disk(p, 42, 43))

    def test_cleanup_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "_tmp_a.dem").write_bytes(b"x")
            (p / "keep.dem").write_bytes(b"x")
            self.assertEqual(core.cleanup_tmp(p), 1)
            self.assertTrue((p / "keep.dem").exists())


class ConfigTests(unittest.TestCase):
    def test_missing_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = core.load_config(Path(d) / "config.json")
            self.assertEqual(cfg["players"], [])
            self.assertEqual(cfg["settings"]["download_workers"], 4)

    def test_corrupt_raises_and_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config.json"
            f.write_text('{"players": [ broken', encoding="utf-8")
            with self.assertRaises(core.ConfigError):
                core.load_config(f)
            self.assertEqual(f.read_text(encoding="utf-8"), '{"players": [ broken')

    def test_unknown_keys_preserved_and_settings_merged(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config.json"
            f.write_text(json.dumps({"download_path": "X", "players": [{"all_codes": [1]}],
                                     "settings": {"download_workers": 2}}), encoding="utf-8")
            cfg = core.load_config(f)
            self.assertEqual(cfg["players"][0]["all_codes"], [1])
            self.assertEqual(cfg["settings"]["download_workers"], 2)
            self.assertEqual(cfg["settings"]["boiler_delay"], 4)
            core.save_config(cfg, f)
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["players"][0]["all_codes"], [1])


class FileTests(unittest.TestCase):
    def _dem_bytes(self):
        return b"PBDEMS2\x00" + NOISE

    def test_bz2_ok(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "a.bz2", Path(d) / "a.dem"
            src.write_bytes(bz2.compress(self._dem_bytes()))
            self.assertEqual(core.decompress_bz2(src, dst), (True, ""))
            self.assertEqual(dst.read_bytes(), self._dem_bytes())

    def test_bz2_truncated_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "a.bz2", Path(d) / "a.dem"
            data = bz2.compress(self._dem_bytes())
            src.write_bytes(data[: len(data) // 2])
            ok, err = core.decompress_bz2(src, dst)
            self.assertFalse(ok)

    def test_validate(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x"
            f.write_bytes(b"<!DOCTYPE html>" + b" " * 200)
            self.assertIn("HTML", core.validate_file(f, True))
            f.write_bytes(bz2.compress(self._dem_bytes()))
            self.assertIsNone(core.validate_file(f, True))
            f.write_bytes(self._dem_bytes())
            self.assertIsNone(core.validate_file(f, False))
            f.write_bytes(b"garbage" * 50)
            self.assertIsNotNone(core.validate_file(f, False))


class DownloadTests(unittest.TestCase):
    def _task(self):
        return {"mid": 5, "oid": 6, "name": core.csdm_name(5, 6, 7),
                "url": "http://replay1.valve.net/730/x.dem.bz2", "info": None}

    def test_download_one_success(self):
        payload = bz2.compress(b"PBDEMS2\x00" + NOISE)

        def fake(url, dest, settings, on_progress=None, cancel=None):
            dest.write_bytes(payload); return True, "", False
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(core, "http_download", side_effect=fake):
            p = Path(d)
            known = core.SafeSet()
            ok, reason, perm = core.download_one(self._task(), p, known, SETTINGS)
            self.assertEqual((ok, reason, perm), (True, "", False))
            files = sorted(x.name for x in p.iterdir())
            self.assertEqual(files, [core.csdm_name(5, 6, 7) + ".dem"])
            # second time → skipped, not re-downloaded
            ok, reason, _ = core.download_one(self._task(), p, known, SETTINGS)
            self.assertEqual(reason, core.SKIPPED)

    def test_download_one_404_is_permanent(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(core, "http_download", return_value=(False, "HTTP 404", True)):
            ok, reason, perm = core.download_one(self._task(), Path(d), core.SafeSet(), SETTINGS)
            self.assertFalse(ok); self.assertTrue(perm)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_truncated_bz2_leaves_no_file(self):
        payload = bz2.compress(b"PBDEMS2\x00" + NOISE)

        def fake(url, dest, settings, on_progress=None, cancel=None):
            dest.write_bytes(payload[: len(payload) // 2]); return True, "", False
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(core, "http_download", side_effect=fake):
            ok, reason, perm = core.download_one(self._task(), Path(d), core.SafeSet(), SETTINGS)
            self.assertFalse(ok); self.assertFalse(perm)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_http_download_no_retry_on_404(self):
        calls = []

        def once(*a, **k):
            calls.append(1); raise core.HttpError(404)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(core, "_download_once", side_effect=once):
            ok, err, perm = core.http_download("u", Path(d) / "f", SETTINGS)
        self.assertEqual((ok, perm, len(calls)), (False, True, 1))

    def test_http_download_incomplete_is_retried(self):
        calls = []

        def once(url, dest, timeout, on_progress, cancel):
            calls.append(1); dest.write_bytes(b"x"); return 1, 10
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(core, "_download_once", side_effect=once):
            ok, err, perm = core.http_download("u", Path(d) / "f", SETTINGS)
        self.assertFalse(ok); self.assertIn("incomplete", err)
        self.assertEqual(len(calls), SETTINGS["max_retries"])

    def test_cancel_stops_download(self):
        ev = threading.Event(); ev.set()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(core.ScanCancelled):
                core.http_download("u", Path(d) / "f", SETTINGS, cancel=ev)


class ScanTests(unittest.TestCase):
    """End-to-end run_scan with Steam API, boiler and HTTP mocked."""

    def test_cli_regression_scan_really_downloads_and_moves_cursor(self):
        mids = [(1000 + i, 2000 + i, 30 + i) for i in range(3)]
        codes = [core.encode_share_code(*m) for m in mids]
        chain = {codes[0]: codes[1], codes[1]: codes[2]}

        def fake_next(player, known, settings):
            return (chain.get(known), True, 200) if known in chain else (None, True, 202)

        def fake_boiler(boiler, mid, oid, token, settings, log=print, debug=False):
            if mid == 1002:
                return None, 4, None                                   # transient GC error
            return f"http://replay1.valve.net/730/{oid:021d}_1.dem.bz2", 0, {
                "mode": "Rush", "map": "rush_001", "game_type": 1, "players": 6}

        def fake_http(url, dest, settings, on_progress=None, cancel=None):
            dest.write_bytes(bz2.compress(b"PBDEMS2\x00" + NOISE)); return True, "", False

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            cfg_file = p / "config.json"
            cfg = core.default_config()
            cfg["settings"].update(SETTINGS, fetch_recent_gc_matches=False)
            cfg["players"].append(core.new_player("A", "7" * 17, "k", "a", codes[0]))
            dl = p / "demos"; dl.mkdir()
            logs = []
            with mock.patch.object(core, "next_code", side_effect=fake_next), \
                    mock.patch.object(core, "ensure_boiler", return_value=Path("b")), \
                    mock.patch.object(core, "boiler_match", side_effect=fake_boiler), \
                    mock.patch.object(core, "http_download", side_effect=fake_http):
                res = core.run_scan(cfg, dl, log=logs.append, config_path=cfg_file)
            self.assertEqual(res["downloaded"], 2)
            names = sorted(x.name for x in dl.iterdir())
            self.assertEqual(names, sorted(core.csdm_name(*m) + ".dem" for m in mids[:2]))
            # cursor stops before the match that failed → retried next run
            self.assertEqual(cfg["players"][0]["last_known_code"], codes[1])
            self.assertEqual(json.loads(cfg_file.read_text(encoding="utf-8"))
                             ["players"][0]["last_known_code"], codes[1])
            self.assertTrue(any("Rush 3v3" in l for l in logs))

    def test_missing_folder_aborts_without_touching_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "config.json"
            cfg = core.default_config()
            cfg["players"].append(core.new_player("A", "7" * 17, "k", "a",
                                                  core.encode_share_code(1, 2, 3)))
            res = core.run_scan(cfg, Path(d) / "nope", log=lambda *_: None,
                                config_path=cfg_file)
            self.assertIsNone(res)
            self.assertFalse(cfg_file.exists())

    def test_gc_recent_matches_added(self):
        data = SAMPLE_GC.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with mock.patch.object(core, "_boiler_call", return_value=(0, data)):
                tasks = core.recent_gc_tasks(Path("b"), core.SafeSet(), p, SETTINGS, {},
                                             log=lambda *_: None)
            self.assertEqual(len(tasks), 8)
            t = tasks[0]
            self.assertEqual(t["name"], core.csdm_name(3809357928926806424,
                                                       3809360954731266299,
                                                       1188481574 & 0xFFFF))
            self.assertEqual(core.decode_share_code(t["code"]),
                             (3809357928926806424, 3809360954731266299, 1188481574 & 0xFFFF))


class NextCodeTests(unittest.TestCase):
    """The urllib fallback must report 403/412/202 like the requests path."""

    def _run_urllib(self, status, body=b""):
        import urllib.error
        player = {"api_key": "k", "steam_id": "1", "auth_code": "a"}

        class Resp:
            def __init__(s): s.status = status
            def read(s): return body
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def fake_urlopen(req, timeout=None):
            if status >= 400:
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, None)
            return Resp()
        with mock.patch.object(core, "HAS_REQUESTS", False), \
                mock.patch.object(core.urllib.request, "urlopen", side_effect=fake_urlopen):
            return core.next_code(player, "CODE", SETTINGS)

    def test_statuses(self):
        self.assertEqual(self._run_urllib(403), (None, False, 403))
        self.assertEqual(self._run_urllib(412), (None, False, 412))
        self.assertEqual(self._run_urllib(202), (None, True, 202))
        self.assertEqual(self._run_urllib(200, b'{"result":{"nextcode":"n/a"}}'), (None, True, 200))
        self.assertEqual(self._run_urllib(200, b'{"result":{"nextcode":"CSGO-x"}}'),
                         ("CSGO-x", True, 200))


if __name__ == "__main__":
    unittest.main()
