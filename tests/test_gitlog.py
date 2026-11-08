import pytest

from agent_harness.gitlog import (
    DEFAULT_TEST_PATTERNS,
    FileChange,
    changes,
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
    assert (attribution.ticket, attribution.found_in) == ("SHOP-412", "the commit message")


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
    assert (attribution.ticket, attribution.found_in) == ("SHOP-412", "the merge commit")


def test_no_ticket_anywhere(git_repo):
    sha = git_repo.commit("fix rounding", {"a": "a"})
    assert ticket_of(git_repo.path, sha) is None


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
