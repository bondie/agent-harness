import numpy as np
import pytest

from agent_harness.gitlog import (
    DEFAULT_TEST_PATTERNS,
    FileChange,
    changes,
    changes_by_term,
    diff,
    is_test,
    mentions,
    new_path,
    resolve,
    ticket_of,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/a.py", "src/a.py"),
        ("old.py => new.py", "new.py"),
        ("src/{old => new}/a.py", "src/new/a.py"),
        ("src/{ => sub}/a.py", "src/sub/a.py"),
        ("src/{sub => }/a.py", "src/a.py"),
        ("src/{a.py => b.py}", "src/b.py"),
    ],
)
def test_new_path_after_a_rename(raw, expected):
    assert new_path(raw) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_x.py", True),
        ("src/Bundle/tests/Unit/X.php", True),
        ("PriceBundle/spec/PriceSpec.php", True),
        ("src/PriceTest.php", True),
        ("pkg/price_test.py", True),
        ("client/price.spec.ts", True),
        ("src/latests/x.py", False),
        ("src/Price.php", False),
        ("src/test_price.py", False),
    ],
)
def test_default_test_patterns(path, expected):
    assert is_test(path, DEFAULT_TEST_PATTERNS) is expected


def test_test_patterns_with_a_slash_match_the_whole_path():
    assert is_test("client/e2e/cart.ts", ["client/e2e/*"])
    assert not is_test("server/e2e/cart.ts", ["client/e2e/*"])


def test_mentions_is_a_whole_word():
    assert mentions("fix(cart): keep it - SHOP-412", "SHOP-412")
    assert mentions("SHOP-412: fix", "SHOP-412")
    assert not mentions("SHOP-4120 fix", "SHOP-412")
    assert not mentions("XSHOP-412 fix", "SHOP-412")


def test_diff_sums_lines_and_counts_test_lines():
    found = [
        FileChange("src/a.py", 10, 2),
        FileChange("tests/test_a.py", 5, 0),
        FileChange("src/a.py", 1, 1),
    ]
    result = diff(found)
    assert result.as_json() == {
        "files": ["src/a.py", "tests/test_a.py"],
        "added": 16,
        "removed": 3,
        "test_added": 5,
    }


def test_changes_of_a_ticket_across_branches(git_repo):
    git_repo.commit("init", {"src/a.py": "a\n"})
    git_repo.git("branch", "release")
    git_repo.commit("feat: price - SHOP-412", {"src/a.py": "a\nb\nc\n", "tests/t.py": "x\n"})
    git_repo.commit("feat: other - SHOP-4120", {"src/other.py": "o\n"})
    git_repo.git("checkout", "-q", "release")
    git_repo.git("cherry-pick", "main~1")
    git_repo.git("checkout", "-q", "main")
    git_repo.git("mv", "src/a.py", "src/b.py")
    git_repo.commit("refactor: rename for SHOP-412")
    git_repo.commit("chore: binary SHOP-412", {"img.bin": "\x00\x01"})

    found = changes(git_repo.path, "SHOP-412")
    result = diff(found)

    assert result.files == ("img.bin", "src/a.py", "src/b.py", "tests/t.py")
    assert result.added == 3
    assert result.test_added == 1


def test_no_commits_name_the_key(git_repo):
    git_repo.commit("init", {"a": "a"})
    assert changes(git_repo.path, "SHOP-412") == []


def test_ticket_from_the_commit_message(git_repo):
    sha = git_repo.commit("fix: rounding - SHOP-412", {"a": "a"})
    attribution = ticket_of(git_repo.path, sha)
    assert attribution is not None
    assert (attribution.candidates, attribution.found_in) == (("SHOP-412",), "the commit message")


def test_ticket_from_the_first_merge_that_brought_the_commit_in(git_repo):
    git_repo.commit("init", {"a": "a"})
    git_repo.git("checkout", "-q", "-b", "feature")
    sha = git_repo.commit("fix rounding", {"b": "b"})
    git_repo.git("checkout", "-q", "main")
    git_repo.commit("other", {"c": "c"})
    merge = "Merge branch 'feature' into 'main'\n\nSHOP-412 Fix rounding\n\nSee merge request !7"
    git_repo.git("merge", "-q", "--no-ff", "feature", "-m", merge)
    git_repo.git("checkout", "-q", "-b", "release")
    git_repo.commit("release", {"d": "d"})
    git_repo.git("checkout", "-q", "main")
    git_repo.git("merge", "-q", "--no-ff", "release", "-m", "Merge release PROJ-1")

    attribution = ticket_of(git_repo.path, sha)

    assert attribution is not None
    assert (attribution.candidates, attribution.found_in) == (("SHOP-412",), "the merge commit")


def test_no_ticket_anywhere(git_repo):
    sha = git_repo.commit("fix rounding", {"a": "a"})
    assert ticket_of(git_repo.path, sha) is None


@pytest.mark.parametrize("branched_after_the_hotfix", [False, True])
def test_a_commit_made_on_the_target_branch_is_not_blamed_on_a_later_merge(
    git_repo, branched_after_the_hotfix
):
    git_repo.commit("init", {"a": "a"})
    git_repo.git("checkout", "-q", "-b", "dev")
    if not branched_after_the_hotfix:
        git_repo.git("branch", "feature/SHOP-999")
    hotfix = git_repo.commit("hotfix rounding", {"a": "b"})
    if branched_after_the_hotfix:
        git_repo.git("branch", "feature/SHOP-999")
    git_repo.git("checkout", "-q", "feature/SHOP-999")
    git_repo.commit("feature work", {"f": "f"})
    git_repo.git("checkout", "-q", "dev")
    git_repo.git("merge", "-q", "--no-ff", "feature/SHOP-999", "-m", "Merge feature/SHOP-999")

    assert ticket_of(git_repo.path, hotfix) is None


def test_a_hotfix_synced_into_a_feature_branch_is_not_blamed_on_it(git_repo):
    git_repo.commit("init", {"a": "0"})
    git_repo.git("checkout", "-q", "-b", "feature/SHOP-1-a")
    feature = git_repo.commit("work on the feed", {"f": "f"})
    git_repo.git("checkout", "-q", "main")
    hotfix = git_repo.commit("hotfix typo", {"h": "h"})
    git_repo.git("checkout", "-q", "feature/SHOP-1-a")
    sync = "Merge branch 'main' into feature/SHOP-1-a"
    git_repo.git("merge", "-q", "--no-edit", "-m", sync, "main")
    git_repo.git("checkout", "-q", "main")
    merge = "Merge branch 'feature/SHOP-1-a' into 'main'\n\nSHOP-1 feed"
    git_repo.git("merge", "-q", "--no-ff", "-m", merge, "feature/SHOP-1-a")

    assert ticket_of(git_repo.path, hotfix) is None
    attribution = ticket_of(git_repo.path, feature)
    assert attribution is not None
    assert (attribution.candidates, attribution.found_in) == (("SHOP-1",), "the merge commit")

    git_repo.git("checkout", "-q", "-b", "feature/SHOP-2-b", "main~1")
    git_repo.commit("other work", {"o": "o"})
    git_repo.git("merge", "-q", "--no-edit", "-m", "Merge branch 'main' into SHOP-2-b", "main")
    git_repo.git("checkout", "-q", "main")
    git_repo.commit("later work on main", {"l": "l"})

    attribution = ticket_of(git_repo.path, feature)
    assert attribution is not None
    assert attribution.candidates == ("SHOP-1",)
    assert ticket_of(git_repo.path, hotfix) is None


def test_names_of_standards_are_not_ticket_keys(git_repo):
    sha = git_repo.commit("Read the feed as UTF-8, sign with SHA-256 per RFC-7515 - SHOP-412")
    attribution = ticket_of(git_repo.path, sha)
    assert attribution is not None
    assert attribution.candidates == ("SHOP-412",)
    plain = git_repo.commit("Format to PSR-12 and ISO-8601")
    assert ticket_of(git_repo.path, plain) is None


def test_every_key_of_the_message_is_a_candidate(git_repo):
    sha = git_repo.commit("fix: SHOP-2 regression from SHOP-1, see SHOP-2")
    attribution = ticket_of(git_repo.path, sha)
    assert attribution is not None
    assert attribution.candidates == ("SHOP-2", "SHOP-1")


def test_two_distinct_commits_with_the_same_message_and_changes_both_count(git_repo, monkeypatch):
    git_repo.commit("init", {"a.txt": "a\n"})
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-03-02T10:00:00+00:00")
    git_repo.commit("fix typo SHOP-1", {"a.txt": "b\n"})
    git_repo.commit("revert", {"a.txt": "a\n"})
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-03-03T10:00:00+00:00")
    git_repo.commit("fix typo SHOP-1", {"a.txt": "b\n"})

    assert diff(changes(git_repo.path, "SHOP-1")).added == 2


def test_rev_leaves_out_a_lingering_branch(git_repo):
    git_repo.commit("init", {"a.txt": "a\n"})
    git_repo.git("checkout", "-q", "-b", "feature/SHOP-1")
    git_repo.commit("wip SHOP-1", {"a.txt": "a\nb\n"})
    git_repo.commit("more SHOP-1", {"a.txt": "a\nb\nc\n"})
    git_repo.git("checkout", "-q", "main")
    git_repo.git("merge", "-q", "--squash", "feature/SHOP-1")
    git_repo.git("commit", "-q", "-m", "feat: price SHOP-1")

    assert diff(changes(git_repo.path, "SHOP-1")).added == 4
    assert diff(changes(git_repo.path, "SHOP-1", rev="main")).added == 2


def test_one_pass_over_the_log_finds_what_one_grep_per_key_finds(git_repo):
    rng = np.random.default_rng(5)
    keys = ["SHOP-1", "SHOP-12", "SHOP-2", "PROJ-7", "SHOP-99"]
    git_repo.commit("init", {"a.txt": "a\n"})
    for index in range(40):
        named = [keys[i] for i in rng.permutation(len(keys))[: int(rng.integers(0, 3))]]
        files = {f"f{int(rng.integers(0, 5))}.txt": f"{index}\n" * int(rng.integers(1, 4))}
        if rng.random() < 0.2:
            git_repo.git("checkout", "-q", "-B", f"side{index % 3}")
        git_repo.commit(f"change {index} {' '.join(named)}".strip(), files)
        git_repo.git("checkout", "-q", "main")

    for rev in (None, "main"):
        one_pass = changes_by_term(git_repo.path, [*keys, "SHOP-1"], rev)
        assert list(one_pass) == keys
        for key in keys:
            assert one_pass[key] == changes(git_repo.path, key, rev)
    assert changes_by_term(git_repo.path, []) == {}


def test_resolve_a_commit(git_repo):
    sha = git_repo.commit("init", {"a": "a"})
    assert resolve(git_repo.path, sha[:8]) == sha
    with pytest.raises(ValueError, match="No commit"):
        resolve(git_repo.path, "deadbeef")


def test_git_errors_become_value_errors(tmp_path, git_repo):
    with pytest.raises(ValueError, match="git log failed"):
        changes(tmp_path / "missing", "SHOP-412")


def test_missing_git(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ValueError, match="not installed"):
        changes(git_repo.path, "SHOP-412")
