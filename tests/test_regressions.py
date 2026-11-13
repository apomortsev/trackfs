import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from trackfs.albuminfo import AlbumInfo
from trackfs.cuesheet import Time, Track
from trackfs.flactracks import TrackInfo, TrackManager
from trackfs.fusepath import Factory, FusePath
try:
    import fuse  # noqa: F401
except EnvironmentError:
    fuse_stub = types.ModuleType("fuse")
    fuse_stub.Operations = type("Operations", (), {})
    fuse_stub.FuseOSError = type("FuseOSError", (Exception,), {})
    sys.modules["fuse"] = fuse_stub

from trackfs.fuseops import TrackFSOps


class TimeTests(unittest.TestCase):
    def test_cue_frames_are_seventy_five_per_second(self):
        self.assertAlmostEqual(Time(0, 0, 75).seconds(), 1.0)


class IntegerTimeTests(unittest.TestCase):
    def test_integer_duration_is_interpreted_as_seconds(self):
        self.assertEqual(Time.create(61), Time(1, 1, 0))


class AlbumInfoTests(unittest.TestCase):
    def test_external_cue_is_decoded_as_windows_1251(self):
        with tempfile.TemporaryDirectory() as directory:
            flac_path = os.path.join(directory, "album.flac")
            cue_path = os.path.join(directory, "album.cue")
            open(flac_path, "wb").close()
            with open(cue_path, "wb") as cue_file:
                cue_file.write(
                    'TITLE "Концерт"\nFILE "album.flac" WAVE\nTRACK 01 AUDIO\nTITLE "Первая"\nINDEX 01 00:00:00\n'.encode("cp1251")
                )
            cue = AlbumInfo(flac_path)._cue_from_external_file()
            self.assertIn("Концерт", cue)


class FusePathTests(unittest.TestCase):
    def test_unicode_title_is_preserved_in_virtual_path(self):
        path = FusePath("album", ".flac", True, 1, "Beyoncé 日本語")
        self.assertIn("Beyoncé 日本語", path.vpath)

    def test_cp1251_title_is_preserved_in_virtual_path(self):
        path = FusePath("album", ".flac", True, 1, "Концерт".encode("cp1251").decode("cp1251"))
        self.assertIn("Концерт", path.vpath)

    def test_separator_and_extension_are_regex_escaped(self):
        factory = Factory(track_separator=".+", track_extension=".flac")
        path = factory.from_vpath("album.flac.+001.Title.flac")
        self.assertTrue(path.is_track)
        self.assertEqual(path.num, 1)

    def test_dots_in_title_are_regex_matched(self):
        factory = Factory(track_separator=".#-#.", track_extension=".flac")
        path = factory.from_vpath("album.flac.#-#.001.01.Title.Subtitle.flac")
        self.assertTrue(path.is_track)
        self.assertEqual(path.num, 1)

class TrackFSOpsTests(unittest.TestCase):
    def test_mount_root_symlink_is_not_dereferenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source") # /tmp/source
            os.mkdir(source) # /tmp/source
            mount_root_symlink = os.path.join(tmp, "src") # /tmp/src
            os.symlink(source, mount_root_symlink) # /tmp/src -> /tmp/source

            ops = TrackFSOps(mount_root_symlink)

            # /tmp/src should be reported as the mount root, not /tmp/source.
            self.assertEqual(ops.root, mount_root_symlink)

    def test_root_directory_symlink_is_reported_as_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source") # /tmp/source
            os.mkdir(source) # /tmp/source
            mount_root = os.path.join(tmp, "link") # /tmp/link
            os.symlink(source, mount_root, target_is_directory=True) # /tmp/link -> /tmp/source/src

            ops = TrackFSOps(mount_root) # /tmp/llink
            result = ops.getattr(mount_root)

            # /tmp/link should be reported as a directory.
            self.assertTrue(os.path.isdir(mount_root))
            self.assertTrue(result["st_mode"] & 0o170000 == 0o040000)

    def test_absolute_source_symlink_is_relative_to_fuse_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = os.path.join(tmp, "src") # /tmp/src
            os.mkdir(mount_root) # /tmp/src
            album = os.path.join(mount_root, "album.flac") # /tmp/src/album.flac
            open(album, "wb").close()
            link = os.path.join(mount_root, "link.flac") # /tmp/src/link.flac
            os.symlink(album, link) # /tmp/src/link.flac -> /tmp/src/album.flac

            ops = TrackFSOps(mount_root)

            self.assertEqual(ops.readlink(link), "album.flac")

class TrackManagerTests(unittest.TestCase):
    @staticmethod
    def _preload_fixture():
        first = Track(1, "First", start=Time(0, 0, 0), end=Time(0, 1, 0))
        second = Track(2, "Second", start=Time(0, 1, 0), end=Time(0, 2, 0))

        class Album:
            def tracks(self):
                return [first, second]

        manager = TrackManager()
        fp = FusePath("/album", ".flac", True, first.num, first.title)
        manager.registry[fp.vpath] = TrackInfo(Path("/tmp/current"), 60)
        return manager, fp, Album(), second

    def test_preload_reference_is_released_after_parent_and_child_close(self):
        manager, fp, album, next_track = self._preload_fixture()
        next_fp = fp.for_other_track(
            next_track.num, next_track.title, next_track.start, next_track.end
        )

        def prepare_track(path, _fp):
            manager.registry[path] = TrackInfo(Path("/tmp/next"), 60)
            return Path("/tmp/next")

        with patch("trackfs.flactracks.albuminfo.get", return_value=album), \
                patch("trackfs.flactracks.os.stat", return_value=types.SimpleNamespace(st_size=100)), \
                patch.object(manager, "prepare_track", side_effect=prepare_track):
            manager._do_check_next_track(fp.vpath, fp, 50)
            self.assertEqual(manager[next_fp.vpath].ref_count, 1)

            manager._change_usage(next_fp.vpath, +1)
            self.assertEqual(manager[next_fp.vpath].ref_count, 2)

            manager.release_track(fp.vpath, fp)
            self.assertEqual(manager[next_fp.vpath].ref_count, 1)

            manager.release_track(next_fp.vpath, next_fp)
            self.assertEqual(manager[next_fp.vpath].ref_count, 0)

    def test_releasing_parent_during_preload_does_not_leak_reference(self):
        manager, fp, album, next_track = self._preload_fixture()
        next_fp = fp.for_other_track(
            next_track.num, next_track.title, next_track.start, next_track.end
        )
        preparing = threading.Event()
        finish_preparing = threading.Event()

        def prepare_track(path, _fp):
            preparing.set()
            self.assertTrue(finish_preparing.wait(timeout=2))
            manager.registry[path] = TrackInfo(Path("/tmp/next"), 60)
            return Path("/tmp/next")

        with patch("trackfs.flactracks.albuminfo.get", return_value=album), \
                patch("trackfs.flactracks.os.stat", return_value=types.SimpleNamespace(st_size=100)), \
                patch.object(manager, "prepare_track", side_effect=prepare_track):
            worker = threading.Thread(
                target=manager._do_check_next_track, args=(fp.vpath, fp, 50)
            )
            worker.start()
            self.assertTrue(preparing.wait(timeout=2))

            manager.release_track(fp.vpath, fp)
            finish_preparing.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(manager[next_fp.vpath].ref_count, 0)
        self.assertNotIn(fp.vpath, manager.preloaded_next_tracks)

    def test_preload_is_skipped_if_parent_closes_before_preload_is_registered(self):
        manager, fp, album, next_track = self._preload_fixture()
        next_fp = fp.for_other_track(
            next_track.num, next_track.title, next_track.start, next_track.end
        )
        resolving_album = threading.Event()
        finish_resolving = threading.Event()

        def get_album(_source):
            resolving_album.set()
            self.assertTrue(finish_resolving.wait(timeout=2))
            return album

        with patch("trackfs.flactracks.albuminfo.get", side_effect=get_album), \
                patch("trackfs.flactracks.os.stat", return_value=types.SimpleNamespace(st_size=100)):
            worker = threading.Thread(
                target=manager._do_check_next_track, args=(fp.vpath, fp, 50)
            )
            worker.start()
            self.assertTrue(resolving_album.wait(timeout=2))

            manager.release_track(fp.vpath, fp)
            finish_resolving.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertNotIn(next_fp.vpath, manager.registry)
        self.assertNotIn(fp.vpath, manager.preloaded_next_tracks)

    def test_next_track_uses_position_not_track_number_as_index(self):
        first = Track(10, "A", start=Time(0, 0, 0), end=Time(0, 1, 0))
        second = Track(20, "A", start=Time(0, 1, 0), end=Time(0, 2, 0))

        class Album:
            def tracks(self):
                return [first, second]

        current, following = TrackManager._find_this_and_next_track(Album(), 10)
        self.assertIs(current, first)
        self.assertIs(following, second)

    def test_duration_is_compared_as_seconds(self):
        track = Track(1, "A", start=Time(0, 0, 0), end=Time(0, 0, 75))
        self.assertEqual(track.duration.seconds(), 1.0)

    def test_time_subtraction_normalizes_frame_boundary(self):
        self.assertEqual(Time(1, 0, 0) - Time(0, 0, 75), Time(0, 59, 0))


if __name__ == "__main__":
    unittest.main()
