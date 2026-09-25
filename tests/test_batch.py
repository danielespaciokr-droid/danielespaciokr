import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ps_remover import api, batch, shapes
from ps_remover.photoshop import PhotoshopError, PhotoshopNotFound, ScriptFailed

AREA = shapes.Area([shapes.rect(0, 0, 10, 10)], (100, 100), "anchor")


class FakeRemove:
    """Stands in for api.remove_area: records the call and writes the result file."""

    def __init__(self, errors=None):
        self.calls = []
        self.errors = dict(errors or {})  # photo name -> list of exceptions to raise, in order

    def __call__(self, photo, area, output, options, overwrite=False, photoshop=None, timeout=None):
        self.calls.append({"photo": photo.name, "area": area, "output": output, "options": options,
                           "overwrite": overwrite})
        pending = self.errors.get(photo.name)
        if pending:
            raise pending.pop(0)
        output.write_bytes(b"result")
        return {"ok": True, "output": str(output), "warnings": []}


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "사진"
        self.dir.mkdir()
        poll = mock.patch.object(batch, "SETTLE_POLL_SECONDS", 0.01)  # the second look comes quickly
        poll.start()
        self.addCleanup(poll.stop)

    def photo(self, name, age=10.0):
        path = self.dir / name
        path.write_bytes(b"photo")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def job(self, **kwargs):
        return batch.BatchJob(self.dir, AREA, **kwargs)

    @staticmethod
    def run_job(job, remove, **kwargs):
        events = []
        runner = batch.BatchRunner(job, events.append, remove=remove)
        runner.run(**kwargs)
        return runner, events


class BatchJobTests(BatchTestCase):
    def test_defaults(self):
        job = self.job(options=api.RemoveOptions(keep_open=True))
        self.assertEqual(job.output_dir, self.dir / "지운 사진")
        self.assertFalse(job.options.keep_open)  # never pile up open documents
        self.assertEqual(job.output_for(self.dir / "a.heic"), self.dir / "지운 사진" / "a_removed.jpg")

    def test_validation(self):
        with self.assertRaises(api.JobError):
            batch.BatchJob(self.dir / "missing", AREA)
        with self.assertRaises(api.JobError):
            batch.BatchJob(self.dir, shapes.Area([]))
        with self.assertRaises(api.JobError):
            self.job(output_dir=self.dir, suffix="")  # results would look like new photos
        batch.BatchJob(self.dir, shapes.Area([]), options=api.RemoveOptions(subject=True))

    def test_candidates_are_new_photos_only(self):
        for name in ("b.PNG", "a.jpg", "raw.CR2"):
            self.photo(name)
        for name in ("notes.txt", ".a.jpg", "c_removed.jpg", "c_removed_2.jpg"):
            self.photo(name)
        (self.dir / "folder.jpg").mkdir()
        job = self.job(output_dir=self.dir)
        found = job.candidates()
        self.assertEqual([p.name for p, _ in found], ["a.jpg", "b.PNG", "raw.CR2"])
        self.assertEqual(found[0][1][0], len(b"photo"))  # (size, mtime) snapshot

    def test_candidates_skip_done_photos(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        job = self.job()
        job.output_dir.mkdir()
        (job.output_dir / "A_REMOVED.JPG").write_bytes(b"done")  # file systems here ignore case
        self.assertEqual([p.name for p, _ in job.candidates()], ["b.jpg"])
        self.assertEqual([p.name for p, _ in job.candidates(skip={self.dir / "b.jpg"})], [])
        job.skip_done = False
        self.assertEqual([p.name for p, _ in job.candidates()], ["a.jpg", "b.jpg"])

    def test_file_in_use_is_a_windows_check(self):
        path = self.photo("a.jpg")
        with mock.patch.object(batch.sys, "platform", "linux"):
            self.assertFalse(batch.file_in_use(path))


class BatchRunnerTests(BatchTestCase):
    def test_processes_every_photo_once(self):
        self.photo("a.jpg")
        self.photo("b.png")
        remove = FakeRemove()
        runner, events = self.run_job(self.job(), remove)
        self.assertEqual([e["type"] for e in events], ["start", "done", "start", "done", "finished"])
        self.assertEqual((events[0]["index"], events[0]["count"]), (0, 2))
        self.assertEqual(events[-1], {"type": "finished", "done": 2, "failed": 0, "error": None})
        self.assertEqual(sorted(p.name for p in (self.dir / "지운 사진").iterdir()), ["a_removed.jpg", "b_removed.png"])
        call = remove.calls[0]
        self.assertIs(call["area"], AREA)
        self.assertFalse(call["overwrite"])
        self.assertFalse(call["options"].keep_open)
        # Next time only new photos are processed.
        self.photo("c.jpg")
        _, events = self.run_job(self.job(), remove)
        self.assertEqual([e["photo"].name for e in events if e["type"] == "done"], ["c.jpg"])
        _, events = self.run_job(self.job(), remove)
        self.assertEqual(events, [{"type": "finished", "done": 0, "failed": 0, "error": None}])

    def test_a_failing_photo_does_not_stop_the_run(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        remove = FakeRemove({"a.jpg": [ScriptFailed("선택 영역이 비어 있습니다.", {"ok": False})]})
        runner, events = self.run_job(self.job(), remove)
        failed = [e for e in events if e["type"] == "failed"]
        self.assertEqual((failed[0]["photo"].name, failed[0]["error"]), ("a.jpg", "선택 영역이 비어 있습니다."))
        self.assertEqual(events[-1]["done"], 1)
        self.assertEqual(events[-1]["failed"], 1)
        self.assertEqual(len(remove.calls), 2)  # a.jpg is not retried within the run

    def test_unexpected_errors_are_per_photo(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        remove = FakeRemove({"a.jpg": [OSError("disk full")]})
        _, events = self.run_job(self.job(), remove)
        self.assertEqual([e["type"] for e in events if e["type"] in ("done", "failed")], ["failed", "done"])

    def test_missing_photoshop_ends_the_run(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        remove = FakeRemove({"a.jpg": [PhotoshopNotFound("Photoshop 없음")]})
        _, events = self.run_job(self.job(), remove, watch=True, interval=0.01)
        self.assertEqual(len(remove.calls), 1)
        self.assertEqual(events[-1]["error"], "Photoshop 없음")

    def test_busy_photoshop_ends_a_single_run(self):
        self.photo("a.jpg")
        remove = FakeRemove({"a.jpg": [PhotoshopError("응답 없음")]})
        _, events = self.run_job(self.job(), remove)
        self.assertEqual([e["type"] for e in events], ["start", "error", "finished"])
        self.assertEqual(events[-1]["failed"], 0)  # the photo itself did not fail

    def test_reprocess_overwrites_each_photo_once(self):
        self.photo("a.jpg")
        job = self.job(skip_done=False)
        job.output_dir.mkdir()
        (job.output_dir / "a_removed.jpg").write_bytes(b"old")
        remove = FakeRemove()
        self.run_job(job, remove)
        self.assertEqual([(c["photo"], c["overwrite"]) for c in remove.calls], [("a.jpg", True)])

    def test_waits_for_a_photo_that_is_still_copying(self):
        self.photo("a.jpg", age=0)
        remove = FakeRemove()
        start = time.monotonic()
        with mock.patch.object(batch, "SETTLE_SECONDS", 0.2), mock.patch.object(batch, "SETTLE_POLL_SECONDS", 0.05):
            _, events = self.run_job(self.job(), remove)
        self.assertEqual([c["photo"] for c in remove.calls], ["a.jpg"])
        self.assertGreaterEqual(time.monotonic() - start, 0.15)

    def test_ready_only_when_unchanged_between_looks(self):
        runner = batch.BatchRunner(self.job(), lambda event: None, remove=FakeRemove())
        path = self.dir / "a.jpg"
        now = 1000.0
        looks = [(100, 900.0), (250, 900.5), (250, 900.5)]  # still growing, then steady
        results = [runner._check([(path, snapshot)], now, watch=True) for snapshot in looks]
        self.assertEqual(results, [([], 1), ([], 1), ([path], 0)])
        # An empty file is never ready.
        empty = self.dir / "b.jpg"
        results = [runner._check([(empty, (0, 900.0))], now, watch=True) for _ in range(2)]
        self.assertEqual(results[-1], ([], 1))

    def test_waits_while_another_program_writes(self):
        self.photo("a.jpg")
        remove = FakeRemove()
        with mock.patch.object(batch, "file_in_use", side_effect=[True, True, False]) as in_use:
            _, events = self.run_job(self.job(), remove)
        self.assertEqual(in_use.call_count, 3)  # asked again at each look until it was free
        self.assertEqual([c["photo"] for c in remove.calls], ["a.jpg"])

    def test_single_run_gives_up_on_a_busy_photo(self):
        self.photo("a.jpg")
        self.photo("b.jpg")
        remove = FakeRemove()
        busy = lambda path: path.name == "a.jpg"  # noqa: E731
        with mock.patch.object(batch, "file_in_use", side_effect=busy), \
                mock.patch.object(batch, "BUSY_GIVE_UP_SECONDS", 0.05):
            runner, events = self.run_job(self.job(), remove)
        self.assertEqual([c["photo"] for c in remove.calls], ["b.jpg"])
        failed = [e for e in events if e["type"] == "failed"]
        self.assertEqual(failed[0]["photo"].name, "a.jpg")
        self.assertIn("다른 프로그램", failed[0]["error"])

    def test_watch_mode_picks_up_new_photos(self):
        self.photo("a.jpg")
        remove = FakeRemove({"b.jpg": [PhotoshopError("잠깐 바쁨")]})  # the first try on b.jpg hits a busy Photoshop
        events = []
        runner = batch.BatchRunner(self.job(), events.append, remove=remove)
        with mock.patch.object(batch, "SETTLE_SECONDS", 0):
            thread = threading.Thread(target=runner.run, kwargs={"watch": True, "interval": 0.05})
            thread.start()
            self._wait_for(lambda: any(e["type"] == "waiting" for e in events))
            self.photo("b.jpg")
            self._wait_for(lambda: runner.done == 2)
            runner.stop()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([c["photo"] for c in remove.calls], ["a.jpg", "b.jpg", "b.jpg"])
        types = [e["type"] for e in events]
        self.assertIn("error", types)
        self.assertEqual(types[-1], "finished")
        self.assertEqual(events[-1]["failed"], 0)

    def test_stop_before_next_photo(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.photo(name)
        events = []

        def remove(photo, *args, **kwargs):
            runner.stop()  # e.g. the user pressed [멈춤] while the first photo was processed
            return FakeRemove()(photo, *args, **kwargs)

        runner = batch.BatchRunner(self.job(), events.append, remove=remove)
        runner.run(watch=True, interval=10)
        self.assertEqual(runner.done, 1)
        self.assertEqual(events[-1]["type"], "finished")

    @staticmethod
    def _wait_for(condition, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError("condition not reached")


if __name__ == "__main__":
    unittest.main()
