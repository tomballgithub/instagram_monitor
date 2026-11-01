"""Offline workflow tests for paginated follower and following fetches."""

import json
import os
from types import SimpleNamespace

import pytest


# Builds a generator of fake Instaloader username objects
def _fake_accounts(usernames):
    return iter(SimpleNamespace(username=username) for username in usernames)


class TestFetchUsernamesPaginated:
    # Basic fetching drains the generator when advanced batching is disabled
    def test_basic_fetch_drains_generator(self, im_module):
        result = im_module.fetch_usernames_paginated(None, lambda: _fake_accounts(["a", "b", "c"]), max_per_batch=1, total_limit=1, fetch_delay=0, advanced_fetch=False, estimated_limit=0, user="target")

        assert result == ["a", "b", "c"]
        assert result.complete is True

    # Advanced fetching stops exactly at total_limit even when the batch size is larger
    def test_advanced_fetch_respects_total_limit(self, im_module, monkeypatch):
        logs = []
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: logs.append((args, kwargs)))

        result = im_module.fetch_usernames_paginated(None, lambda: _fake_accounts(["a", "b", "c", "d", "e"]), max_per_batch=50, total_limit=3, fetch_delay=0, advanced_fetch=True, estimated_limit=5, user="target")

        assert result == ["a", "b", "c"]
        assert result.complete is False
        assert logs[0][0] == ("Fetching 5 accounts in batches of 50 accounts with a 0 second delay",)
        assert logs[0][1] == {"user": "target"}

    # Stop events set before fetching return an empty partial result
    def test_stop_event_before_fetch_returns_empty(self, im_module):
        stop_event = SimpleNamespace(is_set=lambda: True)

        result = im_module.fetch_usernames_paginated(None, lambda: _fake_accounts(["a", "b"]), max_per_batch=1, total_limit=0, fetch_delay=0, advanced_fetch=True, estimated_limit=2, user="target", stop_event=stop_event)

        assert result == []
        assert result.complete is False

    # Stop events set during the inter-batch wait return the already fetched batch
    def test_stop_event_during_fetch_delay_returns_partial(self, im_module, monkeypatch):
        logs = []

        class StopAfterWait:
            # Tracks wait calls and flips to stopped after the first wait
            def __init__(self):
                self.stopped = False
                self.waits = []

            # Returns whether the event has been set
            def is_set(self):
                return self.stopped

            # Records the wait interval and sets the event
            def wait(self, seconds):
                self.waits.append(seconds)
                self.stopped = True

        stop_event = StopAfterWait()
        monkeypatch.setattr(im_module, "log_activity", lambda *args, **kwargs: logs.append((args, kwargs)))

        result = im_module.fetch_usernames_paginated(None, lambda: _fake_accounts(["a", "b", "c", "d"]), max_per_batch=2, total_limit=0, fetch_delay=5, advanced_fetch=True, estimated_limit=4, user="target", stop_event=stop_event)

        assert result == ["a", "b"]
        assert result.complete is False
        assert stop_event.waits == [1]
        assert len(logs) == 1

    # Baseline safety rejects partial lists and contradictory empty results
    def test_baseline_requires_complete_plausible_result(self, im_module):
        partial = im_module.PaginatedUsernameResult(["a"])
        partial.complete = False
        complete = im_module.PaginatedUsernameResult(["a"])
        complete.complete = True
        contradictory_empty = im_module.PaginatedUsernameResult()
        contradictory_empty.complete = True

        assert im_module.is_complete_username_baseline(partial, 1) is False
        assert im_module.is_complete_username_baseline(complete, 1) is True
        assert im_module.is_complete_username_baseline(contradictory_empty, 1) is False
        assert im_module.is_complete_username_baseline(contradictory_empty, 0) is True

    # A stalled browser dialog and a real unfollow both come back short. Instagram's own count is what
    # separates them, and the saved list is the second witness
    def test_a_list_short_of_the_count_may_not_shrink_the_saved_one(self, im_module, capsys):
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(90)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 100, [f"user{index}" for index in range(100)], "followers", "target")

        assert result.complete is False
        assert im_module.is_complete_username_baseline(result, 100) is False
        assert "came back with 90 of the 100 Instagram reports, fewer than the 100 in the saved list" in capsys.readouterr().out

    # The rejected list is kept beside the baseline, in its format, so it can be compared with the saved list
    def test_a_rejected_list_is_written_to_a_side_file(self, im_module, tmp_path, capsys):
        baseline = tmp_path / "instagram_target_followers.json"
        saved = [f"user{index}" for index in range(100)]
        baseline.write_text(json.dumps([100, saved]))
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(90)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 100, saved, "followers", "target", str(baseline))

        side = tmp_path / "instagram_target_followers.rejected.json"
        assert json.loads(side.read_text()) == [100, [f"user{index}" for index in range(90)]]
        assert json.loads(baseline.read_text()) == [100, saved], "the saved baseline is never touched"
        output = capsys.readouterr().out
        assert "instagram_target_followers.rejected.json" in output
        assert "which has 10 names that are not in it" in output

    # A scan that is not rejected writes nothing, so the side file only ever describes a rejection
    @pytest.mark.parametrize("reported, previous_size", [(80, 100), (100, 90)])
    def test_an_accepted_list_writes_no_side_file(self, im_module, tmp_path, reported, previous_size):
        baseline = tmp_path / "instagram_target_followers.json"
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(80 if reported == 80 else 95)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, reported, [f"user{index}" for index in range(previous_size)], "followers", "target", str(baseline))

        assert not (tmp_path / "instagram_target_followers.rejected.json").exists()

    # The next rejected scan replaces the side file, so it always holds the latest scan
    def test_the_next_rejection_replaces_the_side_file(self, im_module, tmp_path):
        baseline = str(tmp_path / "instagram_target_followings.json")
        saved = [f"user{index}" for index in range(100)]

        for size in (90, 85):
            result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(size)])
            result.complete = True
            im_module.reject_shrinking_username_baseline(result, 100, saved, "followings", "target", baseline)

        assert len(json.loads((tmp_path / "instagram_target_followings.rejected.json").read_text())[1]) == 85

    # A side file that cannot be written is reported, and the rejection still stands
    def test_a_write_failure_does_not_break_the_rejection(self, im_module, tmp_path, monkeypatch, capsys):
        # Fails the way a read-only folder does
        def refuse(*args, **kwargs):
            raise PermissionError("read-only folder")

        monkeypatch.setattr(im_module, "save_username_baseline", refuse)
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(90)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 100, [f"user{index}" for index in range(100)], "followers", "target", str(tmp_path / "instagram_target_followers.json"))

        assert result.complete is False
        assert "could not be saved" in capsys.readouterr().out

    # The setting ships off, so a short list is discarded as it always was unless the user asks for more
    def test_short_additions_ship_disabled(self, im_module):
        parsed = im_module.parse_config_content(im_module.CONFIG_BLOCK, "<built-in>")

        assert parsed["FOLLOW_LIST_SHORT_ADDITIONS"] is False

    # With the setting on, a short list becomes the saved list plus the names it adds, in that order, marked usable
    def test_a_short_list_becomes_the_saved_list_plus_additions(self, im_module, monkeypatch, capsys):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(10)]
        result = im_module.PaginatedUsernameResult(["user3", "new_b", "user1", "new_a"])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 12, saved, "followers", "target")

        assert list(result) == saved + ["new_b", "new_a"]
        assert result.complete is True
        assert isinstance(result, im_module.PaginatedUsernameResult)
        assert im_module.is_complete_username_baseline(result, 12) is True
        output = capsys.readouterr().out
        assert "Using the short followers list only to add names: 2 new, none removed" in output
        assert "so the saved list is kept" not in output

    # A short list that adds nothing leaves the saved list exactly as it was
    def test_a_short_list_with_no_additions_changes_nothing(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(10)]
        result = im_module.PaginatedUsernameResult(saved[:6])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 12, saved, "followings", "target")

        assert list(result) == saved and result.complete is True

    # A list that was not rejected is untouched by the setting
    def test_the_setting_leaves_an_accepted_list_alone(self, im_module, monkeypatch):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(80)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 80, [f"user{index}" for index in range(100)], "followers", "target")

        assert len(result) == 80

    # The side file still holds exactly what the scan read, not the merged list
    def test_the_side_file_keeps_the_scan_as_read(self, im_module, monkeypatch, tmp_path):
        monkeypatch.setattr(im_module, "FOLLOW_LIST_SHORT_ADDITIONS", True)
        saved = [f"user{index}" for index in range(10)]
        read = ["user1", "user2", "new_a"]
        result = im_module.PaginatedUsernameResult(read)
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 12, saved, "followers", "target", str(tmp_path / "instagram_target_followers.json"))

        assert json.loads((tmp_path / "instagram_target_followers.rejected.json").read_text()) == [12, read]
        assert list(result) == saved + ["new_a"]

    # The side file name keeps the folder and the extension of the baseline
    def test_the_side_file_name(self, im_module):
        assert im_module.rejected_username_file(os.path.join("data", "instagram_KK_followings.json")) == os.path.join("data", "instagram_KK_followings.rejected.json")

    # Accounts that really went away move the reported count too, so the smaller list is the truth and is saved
    def test_a_real_unfollow_the_count_agrees_with_is_saved(self, im_module):
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(80)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 80, [f"user{index}" for index in range(100)], "followers", "target")

        assert result.complete is True

    # A count that lags behind a growing list is not a shortfall worth keeping the old baseline for
    def test_a_growing_list_behind_a_stale_count_is_saved(self, im_module):
        result = im_module.PaginatedUsernameResult([f"user{index}" for index in range(95)])
        result.complete = True

        im_module.reject_shrinking_username_baseline(result, 100, [f"user{index}" for index in range(90)], "followers", "target")

        assert result.complete is True

    # The first run has nothing to lose, and a list that was already unusable is left as it was
    def test_a_first_run_and_an_incomplete_list_are_left_alone(self, im_module):
        first_run = im_module.PaginatedUsernameResult(["a"])
        first_run.complete = True
        already_partial = im_module.PaginatedUsernameResult(["a"])
        already_partial.complete = False

        im_module.reject_shrinking_username_baseline(first_run, 100, [], "followers", "target")
        im_module.reject_shrinking_username_baseline(already_partial, 100, ["a", "b"], "followers", "target")

        assert first_run.complete is True
        assert already_partial.complete is False

    # Baseline writes replace the final JSON file without leaving a temporary sibling
    def test_save_username_baseline_is_atomic(self, im_module):
        local_dir = os.path.join(os.path.dirname(os.path.abspath(im_module.__file__)), "local")
        os.makedirs(local_dir, exist_ok=True)
        baseline_file = os.path.join(local_dir, "test_followers_baseline.json")
        try:
            im_module.save_username_baseline(baseline_file, 2, ["a", "b"])

            with open(baseline_file, encoding="utf-8") as handle:
                assert json.load(handle) == [2, ["a", "b"]]
            assert not [name for name in os.listdir(local_dir) if name.startswith(".test_followers_baseline.json.")]
        finally:
            if os.path.isfile(baseline_file):
                os.remove(baseline_file)
