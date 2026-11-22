"""Image recipes and reuse decisions, without a Docker daemon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cohbench.env import checks, docker, images
from cohbench.env.build_inputs import inputs_digest
from cohbench.env.environment import Environment
from cohbench.env.site import Upstream
from cohbench.env.workspace import manifest_for


def write(root: Path, name: str, text: str = "content\n", mode: int = 0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "context"
    write(root, "Dockerfile", "FROM scratch\nCOPY . /app\n")
    write(root, "app/main.py", "print('hello')\n")
    return root


def digest(root, args=None, parents=None):
    return inputs_digest(root / "Dockerfile", root, args or {}, parents)


def test_digest_has_same_known_value_on_supported_pythons(context):
    write(context, "Dockerfile", "FROM scratch\n")
    (context / "app").chmod(0o755)
    file = context / "app/main.py"
    file.write_bytes(b"hello\x00world\n")
    file.chmod(0o755)
    assert (
        digest(context, {"Z": "2", "A": "1"}, {"base": "abc"})
        == "467a2c8e31d93b1f4e4ebf582f1d33e36bea0e84ac111593c58a591455c09b96"
    )


def test_digest_independent_of_location_times_and_order(context, tmp_path):
    write(context, "z", "last")
    write(context, "a", "first")
    other = tmp_path / "other"
    shutil.copytree(context, other, copy_function=shutil.copy)
    for path in other.rglob("*"):
        os.utime(path, (123456, 123456))
    args = {"Z": "2", "A": "1"}
    parents = {"parent-b": "b", "parent-a": "a"}
    assert digest(context, args, parents) == digest(
        other, dict(reversed(list(args.items()))), dict(reversed(list(parents.items())))
    )


@pytest.mark.parametrize(
    "change",
    [
        "dockerfile",
        "content",
        "file_mode",
        "directory_mode",
        "add",
        "delete",
        "rename",
        "symlink_target",
        "file_type",
    ],
)
def test_context_changes_invalidate(context, change):
    link = context / "link"
    link.symlink_to("app/main.py")
    before = digest(context)
    file = context / "app/main.py"
    if change == "dockerfile":
        write(context, "Dockerfile", "FROM scratch\n")
    elif change == "content":
        file.write_text("print('world')\n")
    elif change == "file_mode":
        file.chmod(0o755)
    elif change == "directory_mode":
        file.parent.chmod(0o700)
    elif change == "add":
        write(context, "extra")
    elif change == "delete":
        file.unlink()
    elif change == "rename":
        file.rename(file.with_name("other.py"))
    elif change == "symlink_target":
        link.unlink()
        link.symlink_to("missing")
    elif change == "file_type":
        file.unlink()
        file.mkdir()
    assert digest(context) != before


@pytest.mark.parametrize(
    "args,parents",
    [({"VERSION": "2"}, {}), ({}, {"base": "new"}), ({}, {"other-base": "old"})],
)
def test_arguments_and_parent_digests_invalidate(context, args, parents):
    assert digest(context, {"VERSION": "1"}, {"base": "old"}) != digest(
        context, args, parents
    )


def test_symlink_does_not_follow_external_files(context, tmp_path):
    target = write(tmp_path, "external", "one")
    (context / "external").symlink_to("../external")
    before = digest(context)
    target.write_text("two")
    assert digest(context) == before


def test_specific_ignore_overrides_root_ignore_and_includes_control_files(context):
    write(context, ".dockerignore", "app/\n")
    specific = write(context, "Dockerfile.dockerignore", "*\n!app/*.py\n")
    before = digest(context)
    write(context, "ignored.txt", "ignored")
    write(context, ".dockerignore", "*\n")
    assert digest(context) == before
    write(context, "app/main.py", "changed")
    assert digest(context) != before
    before = digest(context)
    write(context, "Dockerfile", "FROM scratch\n")
    assert digest(context) != before
    before = digest(context)
    specific.write_text(specific.read_text() + "# control file changed\n")
    assert digest(context) != before


@pytest.mark.parametrize(
    "patterns,ignored,included",
    [
        ("cache/\n", "cache/deep/file", "app/other"),
        ("**/*.log\n", "app/deep/debug.log", "app/debug.txt"),
        ("**/*.log\n", "debug.log", "debug.txt"),
        ("app/?[0-9].txt\n", "app/a1.txt", "app/a10.txt"),
        ("*\n!app/*.py\napp/private.py\n", "app/private.py", "app/public.py"),
        ("*\n!app/**\napp/deep/*\n!app/deep/keep\n", "app/deep/drop", "app/deep/keep"),
        ("/cache/\n", "cache/file", "app/cache/file"),
        ("# comment\n.\n ./cache/ \n", "cache/file", "app/other"),
        ("app/[^a].txt\n", "app/b.txt", "app/a.txt"),
        ("app/\\*.txt\n", "app/*.txt", "app/b.txt"),
    ],
)
def test_ignore_matching(context, patterns, ignored, included):
    write(context, ".dockerignore", patterns)
    write(context, ignored, "before")
    write(context, included, "before")
    before = digest(context)
    write(context, ignored, "after")
    assert digest(context) == before
    write(context, included, "after")
    assert digest(context) != before


def test_ignored_tree_is_not_read(context, monkeypatch):
    write(context, ".dockerignore", "*\n!app/*.py\n")
    hidden = write(context, "private/key", "not a real key").parent
    original = Path.iterdir

    def iterdir(path):
        assert path != hidden
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    digest(context)


@pytest.mark.parametrize("pattern", ["!", "app/[", "app/\\"])
def test_invalid_ignore_rule_refused(context, pattern):
    write(context, ".dockerignore", pattern)
    with pytest.raises(ValueError):
        digest(context)


def test_special_file_refused(context):
    os.mkfifo(context / "pipe")
    with pytest.raises(ValueError, match="unsupported build context entry"):
        digest(context)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for name in ("base", "pi", "claude", "gateway", "scripted", "watcher"):
        write(root, f"docker/{name}/Dockerfile", "FROM scratch\n")
    write(root, "docker/base/entrypoint.sh", "echo ready\n", 0o755)
    monkeypatch.setattr(images, "REPO_ROOT", root)
    monkeypatch.setattr(images, "DOCKER_DIR", root / "docker")
    monkeypatch.setattr(
        checks, "network_mode", lambda: {"path": "gateway_mode_ipv4=isolated"}
    )
    state = SimpleNamespace(root=root, stored={}, builds=[])

    def build(command, **kwargs):
        assert command[:2] == ["docker", "build"]
        tag = command[command.index("-t") + 1]
        label = command[command.index("--label") + 1]
        key, value = label.split("=", 1)
        state.builds.append(tag)
        state.stored[tag] = {
            "Id": f"sha256:{len(state.builds)}",
            "Config": {"Labels": {key: value}},
        }
        return subprocess.CompletedProcess(command, 0, "", "")

    def run(command, **kwargs):
        assert command[:2] == ["image", "inspect"]
        tag = command[2]
        if tag not in state.stored:
            return subprocess.CompletedProcess(command, 1, "", "No such image")
        return subprocess.CompletedProcess(
            command, 0, json.dumps([state.stored[tag]]), ""
        )

    monkeypatch.setattr(images.subprocess, "run", build)
    monkeypatch.setattr(docker, "run", run)
    return state


@pytest.mark.parametrize("harness", ["pi", "claude"])
def test_harness_reuse_change_and_force(fake_docker, harness):
    tag, built = images.harness_image(harness)
    assert len(built) == 2
    assert images.harness_image(harness) == (tag, [])
    write(fake_docker.root, "docker/base/Dockerfile", "FROM scratch\nRUN python3\n")
    assert images.harness_image(harness) == (tag, built)
    assert images.harness_image(harness, rebuild=True) == (tag, built)


@pytest.mark.parametrize("labels", [None, {}, {images.INPUTS_LABEL: "obsolete"}])
def test_missing_or_stale_label_rebuilds(fake_docker, labels):
    tag, _ = images.harness_image("pi")
    fake_docker.stored[tag]["Config"]["Labels"] = labels
    assert images.harness_image("pi") == (tag, [tag])


def test_parent_digest_invalidates_child_even_if_context_excludes_base(fake_docker):
    root = fake_docker.root
    for name in ("pi", "claude"):
        write(root, f"docker/{name}/Dockerfile.dockerignore", f"*\n!{name}/**\n")
        images.harness_image(name)
    old_pi = fake_docker.stored[f"t3-harness-pi:{images.T3_VERSION}"]["Config"][
        "Labels"
    ][images.INPUTS_LABEL]
    write(root, "docker/base/entrypoint.sh", "echo updated\n", 0o755)
    images.harness_image("claude")
    tag, built = images.harness_image("pi")
    assert built == [tag]
    assert fake_docker.stored[tag]["Config"]["Labels"][images.INPUTS_LABEL] != old_pi


def test_build_arguments_invalidate_only_harness(fake_docker, monkeypatch):
    tag, _ = images.harness_image("pi")
    monkeypatch.setitem(images.HARNESS_BUILD_ARGS, "pi", {"PI_VERSION": "different"})
    assert images.harness_image("pi") == (tag, [tag])


def test_failed_build_is_not_recorded(fake_docker, monkeypatch):
    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "build failed")

    monkeypatch.setattr(images.subprocess, "run", fail)
    with pytest.raises(docker.DockerError, match="build failed"):
        images.harness_image("pi")
    assert not fake_docker.stored


def test_inspect_error_does_not_trigger_build(fake_docker, monkeypatch):
    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")

    monkeypatch.setattr(docker, "run", fail)
    with pytest.raises(docker.DockerError, match="daemon unavailable"):
        images.harness_image("pi")
    assert not fake_docker.builds


@pytest.mark.parametrize("role", ["gateway", "scripted", "watcher"])
def test_service_inputs_reuse_and_records(fake_docker, tmp_path, role):
    for name in ("gateway", "scripted", "watcher"):
        write(
            fake_docker.root,
            f"docker/{name}/Dockerfile.dockerignore",
            f"*\n!{name}/**\n",
        )
        write(fake_docker.root, f"{name}/source.py", "original")
    path = tmp_path / "images.json"
    imgs, first = images.provision("pi", rebuild_services=False, record_path=path)
    assert len(first["built"]) == 5
    assert first == json.loads(path.read_text())
    assert (
        set(first["ids"])
        == set(first["inputs_digests"])
        == {"harness", "gateway", "scripted", "watcher"}
    )
    assert all(len(value) == 64 for value in first["inputs_digests"].values())
    _, reused = images.provision("pi", rebuild_services=False)
    assert reused["built"] == []
    assert set(reused["reused"]) == {
        *first["images"].values(),
        f"t3-harness-base:{images.T3_VERSION}",
    }
    write(fake_docker.root, f"{role}/source.py", "modified")
    _, changed = images.provision("pi", rebuild_services=False)
    assert changed["built"] == [getattr(imgs, role)]
    assert changed["inputs_digests"][role] != first["inputs_digests"][role]
    _, forced = images.provision("pi", rebuild_services=True)
    assert set(forced["built"]) == {imgs.gateway, imgs.scripted, imgs.watcher}


def test_episode_record_keeps_starting_ids_and_digests(
    fake_docker, tmp_path, monkeypatch
):
    imgs, provision = images.provision("pi", rebuild_services=False)
    workspace = tmp_path / "workspace"
    write(workspace, "README.md", "project\n")
    env = Environment(
        imgs,
        Upstream.scripted({}),
        workspace=workspace,
        manifest=manifest_for(workspace),
        out_dir=tmp_path / "out",
        watcher=None,
    )
    # Stop before creating resources, after start has captured the image metadata.
    monkeypatch.setattr(
        "cohbench.env.environment.volume.create",
        lambda registry: (_ for _ in ()).throw(RuntimeError("stop before Docker")),
    )
    with pytest.raises(RuntimeError, match="stop before Docker"):
        env.start()
    for info in fake_docker.stored.values():
        info["Id"] = "changed-after-start"
        info["Config"]["Labels"][images.INPUTS_LABEL] = "changed-after-start"
    saved = json.loads(env.write_record().read_text())
    assert saved["image_ids"] == provision["ids"]
    assert saved["image_inputs_digests"] == provision["inputs_digests"]


def test_legacy_metadata_uses_null_digest(fake_docker):
    imgs, _ = images.provision("pi", rebuild_services=False)
    fake_docker.stored[imgs.harness]["Config"]["Labels"] = None
    assert imgs.metadata()["harness"]["inputs_digest"] is None


def test_provision_reuses_services_by_default(fake_docker):
    images.provision("pi")
    _, record = images.provision("pi")
    assert record["built"] == []
    assert len(record["reused"]) == 5
