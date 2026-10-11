"""Tests for DISPATCHARR_DVR_FINALIZE and DISPATCHARR_DVR_AUDIO.

The hls modes keep a finished recording's HLS playlist at the URL a viewer
opened while it was live, closing it with #EXT-X-ENDLIST instead of
replacing it with an MKV.
"""
import os
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase


def _cmd():
    from apps.channels.tasks import _dvr_build_ffmpeg_cmd

    return _dvr_build_ffmpeg_cmd(
        "http://127.0.0.1:5656/proxy/ts/stream/uuid",
        71,
        "/data/recordings/.dvr_71_hls/index.m3u8",
        "/data/recordings/.dvr_71_hls/seg_%05d.ts",
        0,
    )


class FinalizeModeTests(SimpleTestCase):
    def test_default_is_upstream_mkv(self):
        from apps.channels.tasks import _dvr_finalize_mode, _dvr_keep_hls

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPATCHARR_DVR_FINALIZE", None)
            self.assertEqual(_dvr_finalize_mode(), "mkv")
            self.assertFalse(_dvr_keep_hls())

    def test_hls_modes(self):
        from apps.channels.tasks import _dvr_finalize_mode, _dvr_keep_hls

        for value, expected in (("hls", "hls"), ("HLS+MKV", "hls+mkv"), (" hls ", "hls")):
            with patch.dict(os.environ, {"DISPATCHARR_DVR_FINALIZE": value}):
                self.assertEqual(_dvr_finalize_mode(), expected)
                self.assertTrue(_dvr_keep_hls())

    def test_unknown_value_falls_back_to_mkv(self):
        from apps.channels.tasks import _dvr_finalize_mode

        with patch.dict(os.environ, {"DISPATCHARR_DVR_FINALIZE": "mp4"}):
            self.assertEqual(_dvr_finalize_mode(), "mkv")


class FfmpegCommandTests(SimpleTestCase):
    def test_default_command_is_unchanged(self):
        with patch.dict(os.environ, {"DISPATCHARR_DVR_FINALIZE": "mkv", "DISPATCHARR_DVR_AUDIO": "copy"}):
            cmd = _cmd()
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("-hls_playlist_type", cmd)
        self.assertNotIn("-c:a", cmd)

    def test_hls_mode_writes_event_playlist_without_endlist(self):
        with patch.dict(os.environ, {"DISPATCHARR_DVR_FINALIZE": "hls"}):
            cmd = _cmd()
        self.assertEqual(cmd[cmd.index("-hls_playlist_type") + 1], "event")
        # ENDLIST is written by run_recording, not FFmpeg, so a restarted
        # FFmpeg can keep appending to the same playlist.
        self.assertIn("omit_endlist", cmd[cmd.index("-hls_flags") + 1])
        self.assertLess(cmd.index("-hls_playlist_type"), cmd.index("/data/recordings/.dvr_71_hls/index.m3u8"))

    def test_aac_audio_copies_video_and_reencodes_audio(self):
        with patch.dict(os.environ, {"DISPATCHARR_DVR_AUDIO": "aac"}):
            cmd = _cmd()
        self.assertNotIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-af") + 1], "aresample=async=1")
        self.assertLess(cmd.index("-i"), cmd.index("-c:a"))


class WriteEndlistTests(SimpleTestCase):
    def _playlist(self, tmp, body):
        path = os.path.join(tmp, "index.m3u8")
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_appends_endlist_once(self):
        from apps.channels.tasks import _dvr_hls_write_endlist

        with tempfile.TemporaryDirectory() as tmp:
            path = self._playlist(tmp, "#EXTM3U\n#EXTINF:4.0,\nseg_00000.ts\n")
            self.assertTrue(_dvr_hls_write_endlist(path))
            self.assertTrue(_dvr_hls_write_endlist(path))
            body = open(path).read()
            self.assertEqual(body.count("#EXT-X-ENDLIST"), 1)
            self.assertTrue(body.endswith("seg_00000.ts\n#EXT-X-ENDLIST\n"))
            self.assertEqual(sorted(os.listdir(tmp)), ["index.m3u8"])

    def test_handles_missing_trailing_newline(self):
        from apps.channels.tasks import _dvr_hls_write_endlist

        with tempfile.TemporaryDirectory() as tmp:
            path = self._playlist(tmp, "#EXTM3U\nseg_00000.ts")
            _dvr_hls_write_endlist(path)
            self.assertEqual(open(path).read(), "#EXTM3U\nseg_00000.ts\n#EXT-X-ENDLIST\n")

    def test_missing_playlist_returns_false(self):
        from apps.channels.tasks import _dvr_hls_write_endlist

        self.assertFalse(_dvr_hls_write_endlist(None))
        self.assertFalse(_dvr_hls_write_endlist("/nonexistent/index.m3u8"))


class RunRecordingFinalizeTests(SimpleTestCase):
    """Source checks on run_recording, in the style of ConcatFlagsTests."""

    def test_run_recording_gates_concat_and_cleanup_on_mode(self):
        import inspect
        from apps.channels.tasks import run_recording

        src = inspect.getsource(run_recording)
        self.assertIn('if segments and finalize_mode != "hls":', src)
        self.assertIn("if not keep_hls:", src)
        # ENDLIST is written before the MKV concat starts.
        self.assertLess(src.index("_dvr_hls_write_endlist(hls_m3u8)"), src.index("_dvr_build_hls_concat_cmd("))
        # The upstream viewer wait and rmtree sit behind the keep_hls guard.
        self.assertLess(src.index("if not keep_hls:"), src.index("shutil.rmtree(hls_dir)\n                        logger.debug"))

    def test_recovery_keeps_hls_in_hls_modes(self):
        import inspect
        from apps.channels.tasks import recover_recordings_on_startup

        src = inspect.getsource(recover_recordings_on_startup)
        self.assertIn("_dvr_hls_write_endlist(", src)
        self.assertIn('if _finalize_mode == "mkv":', src)
