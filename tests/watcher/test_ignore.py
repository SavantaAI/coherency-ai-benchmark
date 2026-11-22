import pytest

from cohbench.watcher.ignore import IgnoreRules, compile_pattern


@pytest.mark.parametrize(
    "pattern,path,want",
    [
        (".git/**", ".git", True),
        (".git/**", ".git/objects/ab/cd", True),
        (".git/**", ".gitignore", False),
        (".git/**", "sub/.git/HEAD", False),
        ("**/.git/**", "sub/.git/HEAD", True),
        ("*.pyc", "a.pyc", True),
        ("*.pyc", "pkg/a.pyc", False),
        ("**/*.pyc", "pkg/sub/a.pyc", True),
        ("**/*.pyc", "a.pyc", True),
        ("build", "build/x/y.o", True),
        ("build", "builder", False),
        ("src/*/gen", "src/a/gen/x", True),
        ("src/*/gen", "src/a/b/gen", False),
        ("src/**/gen", "src/a/b/gen", True),
        ("src/**/gen", "src/gen", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file10.txt", False),
        ("[ab].txt", "a.txt", True),
        ("[!ab].txt", "a.txt", False),
        ("[!ab].txt", "c.txt", True),
        ("**", "anything/at/all", True),
        ("node_modules/**", "node_modules", True),
        ("a*", "a/b", True),
    ],
)
def test_patterns(pattern, path, want):
    assert IgnoreRules([pattern]).ignored(path) is want


def test_default_ignores_nothing():
    r = IgnoreRules()
    assert not r.ignored(".git/HEAD")
    assert not r.ignored("x")


def test_star_does_not_cross_segments():
    rx = compile_pattern("a/*")
    assert rx.match("a/b")
    assert not rx.match("a/b/c")


@pytest.mark.parametrize("bad", ["", "/abs/path", "   "])
def test_bad_patterns_raise(bad):
    with pytest.raises(ValueError):
        IgnoreRules([bad])
