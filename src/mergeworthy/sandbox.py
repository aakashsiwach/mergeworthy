"""Docker sandbox: one working copy and one dependency volume per attempt.

Repository code only ever runs inside containers started here. Containers get:

- no environment variables from the host (only the fixed ``HOME`` below), so no tokens or keys;
- one bind mount, the attempt's working copy at ``/work/repo``, and one named volume for dependencies at
  ``/work/deps``; nothing else from the host filesystem;
- ``--network none`` for test runs and agent commands (dependency installation and cloning use the network);
- dropped capabilities, ``no-new-privileges``, and CPU, memory and process limits.

The host reads files in the working copy (the coding agent's file tools need that) but never runs ``git``
or any repository code there: the clone and the diff happen inside containers. Symbolic links are checked out
as plain files, and any symbolic link a container creates is removed after every run, so host-side reads
cannot be redirected outside the working copy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO_DIR = "/work/repo"
DEPS_DIR = "/work/deps"
CONTAINER_HOME = f"{DEPS_DIR}/home"
VENV = f"{DEPS_DIR}/venv"
M2 = f"{DEPS_DIR}/m2"
GRADLE_HOME = f"{DEPS_DIR}/gradle"

OUTPUT_LIMIT = 20_000
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
GIT = "git -c safe.directory='*'"
PYTHON_TEST_EXTRAS = ("test", "tests", "testing", "dev")
MAX_FETCH_ROUNDS = 3

# Maven reports artifacts it needed but could not download in offline mode, e.g. surefire's test provider,
# which is resolved only when tests run.
_MAVEN_OFFLINE_MISSING = re.compile(
    r"The following artifacts could not be resolved: (.+?): Cannot access \S+ \(\S+\) in offline mode"
)
_MAVEN_COORDINATE = re.compile(r"^[\w.-]+:[\w.-]+:[\w.-]+(?::[\w.-]+)?:[\w.-]+$")


def maven_offline_missing(output: str) -> list[str]:
    """Coordinates (``group:artifact:version:packaging``) Maven could not resolve offline."""
    found: list[str] = []
    for match in _MAVEN_OFFLINE_MISSING.finditer(output):
        for item in match.group(1).split(","):
            coordinate = item.strip().split(" ")[0]  # drop " (absent)"
            parts = coordinate.split(":")
            if not _MAVEN_COORDINATE.match(coordinate) or len(parts) not in (4, 5):
                continue
            group, artifact, packaging, *rest = parts
            version = rest[-1]
            classifier = rest[0] if len(rest) == 2 else None
            spec = f"{group}:{artifact}:{version}:{packaging}" + (f":{classifier}" if classifier else "")
            if spec not in found:
                found.append(spec)
    return found


class SandboxSettings(BaseModel):
    """``[sandbox]`` in ``mergeworthy.toml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_root: Path = Path.home() / ".mergeworthy" / "work"
    git_image: str = "buildpack-deps:bookworm-scm"
    python_image: str = "python:3.12"
    maven_image: str = "maven:3.9-eclipse-temurin-21"
    gradle_image: str = "gradle:8-jdk21"
    install_timeout_minutes: int = Field(default=20, ge=1)
    command_timeout_minutes: int = Field(default=10, ge=1)
    cpus: float = Field(default=2.0, gt=0)
    memory: str = Field(default="4g", pattern=r"^\d+[kmg]$")
    pids_limit: int = Field(default=512, ge=64)


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    output: str
    timed_out: bool
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class BuildPlan:
    ecosystem: str
    image: str
    install: str  # shell script run with network access
    test: str  # shell script run without network access


def trim(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Keep the end of long output (where test failures are reported), with a marker for what was cut."""
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"[... {cut} characters omitted ...]\n" + text[-limit:]


# --- build tool detection (docs/architecture.md) ----------------------------------------------------------


def _python_extras(read: Callable[[str], str | None]) -> list[str]:
    text = read("pyproject.toml")
    if not text:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    extras = data.get("project", {}).get("optional-dependencies", {})
    return [name for name in PYTHON_TEST_EXTRAS if name in extras]


def _python_install(files: set[str], read: Callable[[str], str | None]) -> str:
    steps = [
        "set -e",
        f"python -m venv {VENV}",
        f". {VENV}/bin/activate",
        "python -m pip install --quiet --upgrade pip",
    ]
    if "pyproject.toml" in files or "setup.py" in files or "setup.cfg" in files:
        extras = _python_extras(read)
        target = f".[{','.join(extras)}]" if extras else "."
        steps.append(f"python -m pip install --quiet -e '{target}'")
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements_dev.txt"):
        if name in files:
            steps.append(f"python -m pip install --quiet -r {name}")
    steps.append("python -m pip install --quiet pytest")
    return "\n".join(steps)


def detect_build(
    files: set[str],
    read: Callable[[str], str | None],
    settings: SandboxSettings,
    *,
    test_command: str | None = None,
) -> BuildPlan | None:
    """Choose install and test commands from the files at the repository root.

    ``test_command`` (for example from the contributing guide) replaces the detected test command.
    """
    if "pom.xml" in files:
        mvn = "./mvnw" if "mvnw" in files else "mvn"
        prefix = f"chmod +x mvnw 2>/dev/null || true\n{mvn}" if mvn == "./mvnw" else mvn
        flags = f"-B -q -Dmaven.repo.local={M2}"
        return BuildPlan(
            "maven",
            settings.maven_image,
            install=f"set -e\n{prefix} {flags} dependency:go-offline test-compile",
            test=test_command or f"{mvn} {flags} -o test",
        )
    if any(name in files for name in ("build.gradle", "build.gradle.kts")):
        gradle = "./gradlew" if "gradlew" in files else "gradle"
        setup = "chmod +x gradlew 2>/dev/null || true\n" if gradle == "./gradlew" else ""
        flags = f"--no-daemon --gradle-user-home {GRADLE_HOME}"
        return BuildPlan(
            "gradle",
            settings.gradle_image,
            install=f"set -e\n{setup}{gradle} {flags} testClasses",
            test=test_command or f"{gradle} {flags} --offline test",
        )
    python_markers = ("pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", "noxfile.py")
    if any(name in files for name in python_markers):
        return BuildPlan(
            "python",
            settings.python_image,
            install=_python_install(files, read),
            test=f". {VENV}/bin/activate\n" + (test_command or "python -m pytest -q"),
        )
    return None


# --- docker -----------------------------------------------------------------------------------------------

Runner = Callable[[Sequence[str], float], tuple[int, str, bool]]


def subprocess_runner(args: Sequence[str], timeout: float) -> tuple[int, str, bool]:
    """Run the docker CLI. Returns (exit code, combined output, timed out)."""
    try:
        proc = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        return -1, out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out, True
    except FileNotFoundError as exc:
        raise SandboxError("the docker CLI was not found") from exc
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), False


def docker_available(runner: Runner = subprocess_runner) -> bool:
    try:
        code, _, _ = runner(["docker", "info", "--format", "{{.OSType}}"], 20)
    except SandboxError:
        return False
    return code == 0


class Sandbox:
    """The sandbox for one attempt."""

    def __init__(
        self,
        attempt_id: int,
        settings: SandboxSettings | None = None,
        *,
        runner: Runner = subprocess_runner,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempt_id = int(attempt_id)
        self.settings = settings or SandboxSettings()
        self._runner = runner
        self._clock = clock
        self.name = f"mergeworthy-attempt-{self.attempt_id}"
        self.volume = f"{self.name}-deps"
        self.workdir = (self.settings.work_root / str(self.attempt_id) / "repo").resolve()

    # container command lines

    def _user(self) -> list[str]:
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if getuid is None or getgid is None:  # Windows: Docker Desktop maps file ownership itself
            return []
        return ["--user", f"{getuid()}:{getgid()}"]

    def docker_run_args(
        self,
        image: str,
        script: str,
        *,
        network: bool,
        container: str,
        git_writable: bool = False,
        mount_repo: bool = True,
        workdir: str = REPO_DIR,
    ) -> list[str]:
        """The full ``docker run`` command line for one script. Public for tests and dry runs.

        Project code (installs, tests, agent commands) sees ``.git`` read-only, so it cannot plant git
        configuration or hooks. Only mergeworthy's own fixed git commands get it writable.
        """
        mounts: list[str] = []
        if mount_repo:
            mounts += ["--mount", f"type=bind,source={self.workdir},target={REPO_DIR}"]
            git_dir = self.workdir / ".git"
            if not git_writable and git_dir.is_dir() and not git_dir.is_symlink():
                mounts += ["--mount", f"type=bind,source={git_dir},target={REPO_DIR}/.git,readonly"]
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            container,
            "--network",
            "bridge" if network else "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.settings.pids_limit),
            "--memory",
            self.settings.memory,
            "--cpus",
            str(self.settings.cpus),
            *self._user(),
            *mounts,
            "--mount",
            f"type=volume,source={self.volume},target={DEPS_DIR}",
            "--workdir",
            workdir,
            "--env",
            f"HOME={CONTAINER_HOME}",
            "--entrypoint",
            "sh",
            image,
            "-c",
            script,
        ]

    def _run(
        self,
        image: str,
        script: str,
        *,
        network: bool,
        timeout_seconds: float,
        limit: int | None = OUTPUT_LIMIT,
        git_writable: bool = False,
        mount_repo: bool = True,
        workdir: str = REPO_DIR,
    ) -> RunResult:
        container = f"{self.name}-{uuid.uuid4().hex[:8]}"
        args = self.docker_run_args(
            image,
            script,
            network=network,
            container=container,
            git_writable=git_writable,
            mount_repo=mount_repo,
            workdir=workdir,
        )
        start = self._clock()
        code, output, timed_out = self._exec(args, timeout_seconds)
        if timed_out:
            self._exec(["docker", "rm", "--force", container], 60)
        self.remove_symlinks()
        text = output if limit is None else trim(output, limit)
        return RunResult(code, text, timed_out, self._clock() - start)

    def _exec(self, args: Sequence[str], timeout: float) -> tuple[int, str, bool]:
        """The only way this class starts a process: the docker CLI. Never git or project code on the host."""
        if not args or args[0] != "docker":
            raise SandboxError(f"refusing to run a non-docker command on the host: {list(args)[:2]}")
        return self._runner(args, timeout)

    # lifecycle

    def prepare(self) -> None:
        """Create the working copy directory and the dependency volume."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        code, output, _ = self._exec(["docker", "volume", "create", self.volume], 60)
        if code != 0:
            raise SandboxError(f"could not create volume {self.volume}: {trim(output, 500)}")
        # The volume starts out owned by root; make it writable for the unprivileged user (fixed command,
        # no repository code).
        user = self._user()
        if user:
            uid_gid = user[1]
            script = f"mkdir -p {CONTAINER_HOME} && chown -R {uid_gid} {DEPS_DIR}"
            init = [
                "docker", "run", "--rm", "--network", "none",
                "--mount", f"type=volume,source={self.volume},target={DEPS_DIR}",
                "--entrypoint", "sh", self.settings.git_image, "-c", script,
            ]  # fmt: skip
        else:
            init = [
                "docker", "run", "--rm", "--network", "none",
                "--mount", f"type=volume,source={self.volume},target={DEPS_DIR}",
                "--entrypoint", "sh", self.settings.git_image, "-c", f"mkdir -p {CONTAINER_HOME}",
            ]  # fmt: skip
        code, output, _ = self._exec(init, 300)
        if code != 0:
            raise SandboxError(f"could not initialise volume {self.volume}: {trim(output, 500)}")

    def clone(self, full_name: str, commit: str) -> RunResult:
        """Clone ``https://github.com/<full_name>`` and check out ``commit``, inside a container."""
        if not _REPO_NAME.match(full_name):
            raise SandboxError(f"not a repository name: {full_name!r}")
        if not _COMMIT.match(commit):
            raise SandboxError(f"not a commit SHA: {commit!r}")
        if any(self.workdir.iterdir()):
            raise SandboxError(f"working copy {self.workdir} is not empty")
        url = f"https://github.com/{full_name}.git"
        script = "\n".join(
            [
                "set -e",
                "export GIT_TERMINAL_PROMPT=0",
                f"{GIT} -c core.symlinks=false clone --quiet --no-checkout {url} {REPO_DIR}",
                f"cd {REPO_DIR}",
                "git config core.symlinks false",
                f"{GIT} checkout --quiet {commit}",
            ]
        )
        timeout = self.settings.install_timeout_minutes * 60
        return self._run(
            self.settings.git_image, script, network=True, timeout_seconds=timeout, git_writable=True
        )

    def load_local(self, source: Path) -> None:
        """Copy a local project into the empty working copy (fixtures and replay cases), without symlinks."""
        if any(self.workdir.iterdir()):
            raise SandboxError(f"working copy {self.workdir} is not empty")
        shutil.copytree(source, self.workdir, dirs_exist_ok=True, symlinks=True)
        self.remove_symlinks()
        script = "\n".join(
            [
                "set -e",
                f"{GIT} init --quiet",
                "git config core.symlinks false",
                f"{GIT} add --all",
                f"{GIT} -c user.name=mergeworthy -c user.email=mergeworthy@localhost commit -q -m baseline",
            ]
        )
        result = self._run(
            self.settings.git_image, script, network=False, timeout_seconds=300, git_writable=True
        )
        if not result.ok:
            raise SandboxError(f"could not record the baseline commit: {trim(result.output, 500)}")

    def destroy(self) -> None:
        """Remove the dependency volume and the working copy."""
        self._exec(["docker", "volume", "rm", "--force", self.volume], 120)
        root = self.workdir.parent
        if root.exists() and root.is_relative_to(self.settings.work_root.resolve()):
            shutil.rmtree(root, ignore_errors=True)

    # host-side reads of the working copy

    def remove_symlinks(self) -> list[str]:
        """Delete symbolic links in the working copy so host reads cannot escape it. Returns removed paths."""
        removed: list[str] = []
        if not self.workdir.exists():
            return removed
        for dirpath, dirnames, filenames in os.walk(self.workdir, followlinks=False):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or _is_junction(path):
                    removed.append(path.relative_to(self.workdir).as_posix())
                    try:
                        path.unlink()
                    except (IsADirectoryError, PermissionError):
                        os.rmdir(path)  # a directory link on Windows
        return removed

    def resolve(self, relative: str) -> Path:
        """A path inside the working copy. Refuses absolute paths, ``..`` and links."""
        candidate = (self.workdir / relative).resolve()
        if not candidate.is_relative_to(self.workdir):
            raise SandboxError(f"path escapes the working copy: {relative!r}")
        return candidate

    def root_files(self) -> set[str]:
        return {p.name for p in self.workdir.iterdir() if not p.is_symlink()}

    def read_text(self, relative: str, limit: int = 1_000_000) -> str | None:
        path = self.resolve(relative)
        if not path.is_file() or path.is_symlink():
            return None
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")

    # build and run

    def detect(self, test_command: str | None = None) -> BuildPlan:
        plan = detect_build(self.root_files(), self.read_text, self.settings, test_command=test_command)
        if plan is None:
            raise SandboxError("no supported build tool found (Maven, Gradle or Python)")
        return plan

    def install(self, plan: BuildPlan) -> RunResult:
        timeout = self.settings.install_timeout_minutes * 60
        return self._run(plan.image, plan.install, network=True, timeout_seconds=timeout)

    def run_tests(self, plan: BuildPlan, timeout_minutes: float) -> RunResult:
        """Run the test suite without network access.

        For Maven, artifacts that are only resolved when tests run (such as surefire's provider) are fetched
        with ``dependency:get`` (network on, no project code) and the tests are run again offline.
        """
        result = self._run(plan.image, plan.test, network=False, timeout_seconds=timeout_minutes * 60)
        for _ in range(MAX_FETCH_ROUNDS):
            missing = maven_offline_missing(result.output) if plan.ecosystem == "maven" else []
            if result.ok or not missing:
                break
            mvn = "./mvnw" if (self.workdir / "mvnw").is_file() else "mvn"
            fetch = "set -e\n" + "\n".join(
                f"{mvn} -B -q -Dmaven.repo.local={M2} dependency:get -Dtransitive=true -Dartifact={spec}"
                for spec in missing
            )
            fetched = self._run(
                plan.image, fetch, network=True, timeout_seconds=self.settings.install_timeout_minutes * 60
            )
            if not fetched.ok:
                return RunResult(fetched.exit_code, fetched.output, fetched.timed_out, fetched.seconds)
            result = self._run(plan.image, plan.test, network=False, timeout_seconds=timeout_minutes * 60)
        return result

    def run(self, plan: BuildPlan, command: str, *, timeout_minutes: float | None = None) -> RunResult:
        """Run an arbitrary command without network access: the agent's ``run_in_sandbox`` tool."""
        minutes = timeout_minutes or self.settings.command_timeout_minutes
        script = command
        if plan.ecosystem == "python":
            script = f". {VENV}/bin/activate 2>/dev/null || true\n{command}"
        return self._run(plan.image, script, network=False, timeout_seconds=minutes * 60)

    def diff(self) -> str:
        """The working copy's changes against the checked-out commit, including new files, computed in a
        container so the host never runs git on untrusted repository metadata."""
        script = "\n".join(
            [
                "set -e",
                f"{GIT} add --all --intent-to-add",
                f"{GIT} -c core.pager=cat diff --no-color --no-ext-diff --no-textconv HEAD",
            ]
        )
        result = self._run(
            self.settings.git_image, script, network=False, timeout_seconds=300, limit=None, git_writable=True
        )
        if not result.ok:
            raise SandboxError(f"git diff failed: {trim(result.output, 500)}")
        return result.output


def _is_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(is_junction and is_junction(path))
