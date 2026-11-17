import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from mergeworthy.sandbox import (
    BuildPlan,
    Sandbox,
    SandboxError,
    SandboxSettings,
    detect_build,
    maven_fetch_script,
    maven_offline_missing,
    trim,
)

SETTINGS = SandboxSettings()


class FakeDocker:
    def __init__(self, results: list[tuple[int, str, bool]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.results = results or []

    def __call__(self, args: Sequence[str], timeout: float) -> tuple[int, str, bool]:
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        return self.results.pop(0) if self.results else (0, "ok", False)


def sandbox(tmp_path: Path, docker: FakeDocker | None = None) -> Sandbox:
    box = Sandbox(7, SandboxSettings(work_root=tmp_path), runner=docker or FakeDocker())
    box.workdir.mkdir(parents=True)
    return box


def no_read(_: str) -> str | None:
    return None


# --- detection --------------------------------------------------------------------------------------------


def test_detects_maven_and_prefers_wrapper() -> None:
    plan = detect_build({"pom.xml"}, no_read, SETTINGS)
    assert plan is not None
    assert plan.ecosystem == "maven"
    assert plan.image == SETTINGS.maven_image
    assert plan.test.startswith("mvn ") and " -o test" in plan.test
    wrapped = detect_build({"pom.xml", "mvnw"}, no_read, SETTINGS)
    assert wrapped is not None
    assert wrapped.test.startswith("./mvnw ")


@pytest.mark.parametrize("marker", ["build.gradle", "build.gradle.kts"])
def test_detects_gradle(marker: str) -> None:
    plan = detect_build({marker, "gradlew"}, no_read, SETTINGS)
    assert plan is not None
    assert plan.ecosystem == "gradle"
    assert "./gradlew" in plan.test and "--offline test" in plan.test


@pytest.mark.parametrize(
    "marker", ["pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "noxfile.py"]
)
def test_detects_python(marker: str) -> None:
    plan = detect_build({marker}, no_read, SETTINGS)
    assert plan is not None
    assert plan.ecosystem == "python"
    assert plan.test.endswith("python -m pytest -q")


def test_python_install_uses_test_extras_and_requirements() -> None:
    pyproject = (
        '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest"]\ntest = ["h"]\nlocal = []\n'
    )
    plan = detect_build(
        {"pyproject.toml", "requirements-dev.txt"},
        lambda p: pyproject if p == "pyproject.toml" else None,
        SETTINGS,
    )
    assert plan is not None
    assert "pip install --quiet -e '.[test,dev]'" in plan.install
    assert "-r requirements-dev.txt" in plan.install
    assert "local" not in plan.install


def test_invalid_pyproject_is_tolerated() -> None:
    plan = detect_build({"pyproject.toml"}, lambda _: "not [toml", SETTINGS)
    assert plan is not None
    assert "-e '.'" in plan.install


def test_documented_test_command_wins() -> None:
    plan = detect_build({"pom.xml"}, no_read, SETTINGS, test_command="mvn -B verify")
    assert plan is not None
    assert plan.test == "mvn -B verify"


def test_maven_wins_over_python_and_nothing_found_returns_none() -> None:
    plan = detect_build({"pom.xml", "pyproject.toml"}, no_read, SETTINGS)
    assert plan is not None
    assert plan.ecosystem == "maven"
    assert detect_build({"README.md", "package.json"}, no_read, SETTINGS) is None


# --- command lines ----------------------------------------------------------------------------------------


def test_run_args_are_isolated(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    args = box.docker_run_args("img", "echo hi", network=False, container="c1")
    assert args[:3] == ["docker", "run", "--rm"]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in args
    # Exactly one bind mount (the working copy) and one named volume; nothing else from the host.
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == [
        f"type=bind,source={box.workdir},target=/work/repo",
        f"type=volume,source={box.volume},target=/work/deps",
    ]
    assert "-v" not in args and "--volume" not in args
    # No host environment: every --env has an explicit value, and there is no env file.
    envs = [args[i + 1] for i, a in enumerate(args) if a in ("--env", "-e")]
    assert envs == ["HOME=/work/deps/home"]
    assert "--env-file" not in args
    assert args[-3:] == ["img", "-c", "echo hi"]


def test_run_args_never_include_host_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "FAKE_TOKEN_FOR_TESTS")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE_TOKEN_FOR_TESTS")
    args = sandbox(tmp_path).docker_run_args("img", "env", network=True, container="c1")
    assert not any("FAKE_TOKEN_FOR_TESTS" in a or "GITHUB_TOKEN" in a or "ANTHROPIC" in a for a in args)
    assert args[args.index("--network") + 1] == "bridge"


def test_tests_run_without_network_and_install_with(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(tmp_path, docker)
    plan = BuildPlan("python", "py", install="pip install", test="pytest")
    box.install(plan)
    box.run_tests(plan, timeout_minutes=15)
    box.run(plan, "ls")
    networks = [call[call.index("--network") + 1] for call in docker.calls]
    assert networks == ["bridge", "none", "none"]
    assert docker.timeouts[1] == 15 * 60
    assert docker.calls[2][-1].endswith("\nls")  # the venv is activated first


def test_timeout_kills_container_and_trims_output(tmp_path: Path) -> None:
    docker = FakeDocker([(-1, "x" * 50_000, True), (0, "", False)])
    box = sandbox(tmp_path, docker)
    result = box.run_tests(BuildPlan("maven", "mvn", "i", "t"), timeout_minutes=1)
    assert result.timed_out
    assert not result.ok
    assert docker.calls[1][:3] == ["docker", "rm", "--force"]
    assert docker.calls[1][3] == docker.calls[0][docker.calls[0].index("--name") + 1]
    assert result.output.startswith("[... 30000 characters omitted ...]")


def test_clone_validates_inputs_and_runs_in_container(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(tmp_path, docker)
    with pytest.raises(SandboxError, match="repository name"):
        box.clone("bad name", "abcdef1")
    with pytest.raises(SandboxError, match="commit"):
        box.clone("owner/repo", "main; rm -rf /")
    box.clone("owner/repo", "abcdef1234567")
    script = docker.calls[0][-1]
    assert "https://github.com/owner/repo.git" in script
    assert "core.symlinks=false" in script
    assert "checkout --quiet abcdef1234567" in script
    assert docker.calls[0][docker.calls[0].index("--network") + 1] == "bridge"


def test_clone_refuses_non_empty_working_copy(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    (box.workdir / "x").write_text("x")
    with pytest.raises(SandboxError, match="not empty"):
        box.clone("owner/repo", "abcdef1")


def test_diff_is_not_trimmed_and_runs_in_container(tmp_path: Path) -> None:
    big = "+" * 50_000
    docker = FakeDocker([(0, big, False)])
    box = sandbox(tmp_path, docker)
    assert box.diff() == big
    assert docker.calls[0][docker.calls[0].index("--network") + 1] == "none"
    assert "--no-ext-diff" in docker.calls[0][-1]


def test_diff_failure_raises(tmp_path: Path) -> None:
    box = sandbox(tmp_path, FakeDocker([(128, "fatal: not a git repository", False)]))
    with pytest.raises(SandboxError, match="git diff failed"):
        box.diff()


def test_prepare_creates_volume(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = Sandbox(3, SandboxSettings(work_root=tmp_path), runner=docker)
    box.prepare()
    assert box.workdir.is_dir()
    assert docker.calls[0] == ["docker", "volume", "create", "mergeworthy-attempt-3-deps"]
    assert docker.calls[1][docker.calls[1].index("--network") + 1] == "none"


def test_prepare_failure_raises(tmp_path: Path) -> None:
    box = Sandbox(3, SandboxSettings(work_root=tmp_path), runner=FakeDocker([(1, "daemon down", False)]))
    with pytest.raises(SandboxError, match="could not create volume"):
        box.prepare()


def test_destroy_removes_volume_and_working_copy(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(tmp_path, docker)
    (box.workdir / "file.txt").write_text("x")
    box.destroy()
    assert docker.calls[0] == ["docker", "volume", "rm", "--force", box.volume]
    assert not box.workdir.exists()


# --- host-side safety -------------------------------------------------------------------------------------


def test_resolve_refuses_escapes(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    assert box.resolve("src/a.py") == box.workdir / "src" / "a.py"
    for bad in ("../outside", "../../etc/passwd", str(tmp_path / "elsewhere")):
        with pytest.raises(SandboxError, match="escapes"):
            box.resolve(bad)


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "probe").symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        return False
    (tmp_path / "probe").unlink()
    return True


def test_symlinks_created_in_working_copy_are_removed(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("creating symbolic links is not permitted here")
    box = sandbox(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("FAKE_TOKEN_FOR_TESTS")
    (box.workdir / "sub").mkdir()
    (box.workdir / "sub" / "link.txt").symlink_to(secret)
    (box.workdir / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (box.workdir / "real.txt").write_text("ok")
    removed = box.remove_symlinks()
    assert sorted(removed) == ["dirlink", "sub/link.txt"]
    assert box.read_text("real.txt") == "ok"
    assert box.read_text("sub/link.txt") is None
    assert secret.read_text() == "FAKE_TOKEN_FOR_TESTS"


def test_every_run_removes_symlinks(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("creating symbolic links is not permitted here")
    box = sandbox(tmp_path)
    (box.workdir / "link").symlink_to(tmp_path / "x")
    box.run(BuildPlan("maven", "m", "i", "t"), "true")
    assert not os.path.lexists(box.workdir / "link")


def test_load_local_copies_project(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pom.xml").write_text("<project/>")
    (source / "pkg" / "A.java").write_text("class A {}")
    box = Sandbox(1, SandboxSettings(work_root=tmp_path / "work"), runner=FakeDocker())
    box.workdir.mkdir(parents=True)
    box.load_local(source)
    assert box.root_files() == {"pom.xml", "pkg"}
    assert box.detect().ecosystem == "maven"


def test_detect_without_build_tool_raises(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    (box.workdir / "README.md").write_text("hi")
    with pytest.raises(SandboxError, match="no supported build tool"):
        box.detect()


def test_trim() -> None:
    assert trim("short") == "short"
    assert trim("abcdef", limit=3) == "[... 3 characters omitted ...]\ndef"


MISSING = (
    "[ERROR] Failed to execute goal on project x: The following artifacts could not be resolved: "
    "org.apache.maven.surefire:surefire-junit-platform:jar:3.5.2 (absent), "
    "org.junit.platform:junit-platform-launcher:jar:1.11.4 (absent): "
    "Cannot access central (https://repo.maven.apache.org/maven2) in offline mode and the artifact"
)


def test_maven_offline_missing_parses_coordinates() -> None:
    assert maven_offline_missing(MISSING) == [
        "org.apache.maven.surefire:surefire-junit-platform:3.5.2:jar",
        "org.junit.platform:junit-platform-launcher:1.11.4:jar",
    ]
    assert maven_offline_missing("BUILD FAILURE: tests failed") == []
    assert maven_offline_missing(MISSING.replace("3.5.2", "3.5.2;rm -rf /")) == [
        "org.junit.platform:junit-platform-launcher:1.11.4:jar"
    ]


def test_maven_fetches_missing_artifacts_then_reruns_offline(tmp_path: Path) -> None:
    docker = FakeDocker([(1, MISSING, False), (0, "", False), (0, "Tests run: 1", False)])
    box = sandbox(tmp_path, docker)
    result = box.run_tests(BuildPlan("maven", "m", "i", "mvn -o test"), timeout_minutes=5)
    assert result.ok
    networks = [call[call.index("--network") + 1] for call in docker.calls]
    assert networks == ["none", "bridge", "none"]
    fetch = docker.calls[1][-1]
    assert "maven-dependency-plugin:3.11.0:get" in fetch and "surefire-junit-platform:3.5.2:jar" in fetch
    assert "mvn -o test" not in fetch  # the fetch step runs no tests


def test_ordinary_test_failure_is_not_retried(tmp_path: Path) -> None:
    docker = FakeDocker([(1, "Tests run: 3, Failures: 1", False)])
    box = sandbox(tmp_path, docker)
    assert not box.run_tests(BuildPlan("maven", "m", "i", "t"), timeout_minutes=5).ok
    assert len(docker.calls) == 1


def test_fetch_rounds_are_bounded(tmp_path: Path) -> None:
    docker = FakeDocker([(1, MISSING, False), (0, "", False)] * 5)
    box = sandbox(tmp_path, docker)
    assert not box.run_tests(BuildPlan("maven", "m", "i", "t"), timeout_minutes=5).ok
    assert len(docker.calls) == 1 + 2 * 3


def test_maven_fetch_never_loads_the_project(tmp_path: Path) -> None:
    script = maven_fetch_script(["org.apache.maven.surefire:surefire-junit-platform:3.5.2:jar"])
    assert "cd /tmp" in script
    assert "./mvnw" not in script and "/work/repo" not in script
    assert "export MAVEN_SKIP_RC=1 HOME=/tmp/mergeworthy-home" in script
    assert "-s /tmp/mergeworthy-settings.xml -gs /tmp/mergeworthy-settings.xml" in script
    assert "org.apache.maven.plugins:maven-dependency-plugin:3.11.0:get" in script
    assert "-DremoteRepositories=central::default::https://repo.maven.apache.org/maven2 " in script
    assert "-Dmaven.repo.local=/tmp/mergeworthy-m2 " in script  # fresh repository, not the shared one
    assert script.rstrip().endswith("cp -R -n /tmp/mergeworthy-m2/. /work/deps/m2/")
    assert "-Dartifact=org.apache.maven.surefire:surefire-junit-platform:3.5.2:jar" in script


def test_maven_fetch_rejects_odd_coordinates() -> None:
    with pytest.raises(SandboxError, match="not a Maven coordinate"):
        maven_fetch_script(["g:a:1.0:jar; curl evil"])


def test_maven_fetch_container_has_no_repository_mount(tmp_path: Path) -> None:
    docker = FakeDocker([(1, MISSING, False), (0, "", False), (0, "ok", False)])
    box = sandbox(tmp_path, docker)
    (box.workdir / ".git").mkdir()
    box.run_tests(BuildPlan("maven", "m", "i", "mvn -o test"), timeout_minutes=5)
    fetch = docker.calls[1]
    assert fetch[fetch.index("--network") + 1] == "bridge"
    assert fetch[fetch.index("--workdir") + 1] == "/tmp"
    mounts = [fetch[i + 1] for i, a in enumerate(fetch) if a == "--mount"]
    assert mounts == [f"type=volume,source={box.volume},target=/work/deps"]
    tests_run = docker.calls[0]
    assert any("target=/work/repo/.git,readonly" in a for a in tests_run)


def test_clone_excludes_build_output_from_diffs(tmp_path: Path) -> None:
    docker = FakeDocker()
    box = sandbox(tmp_path, docker)
    box.clone("owner/repo", "abcdef1")
    script = docker.calls[0][-1]
    assert ">> .git/info/exclude" in script
    for pattern in ("'target/'", "'build/'", "'__pycache__/'", "'*.egg-info/'", "'.gradle/'"):
        assert pattern in script
