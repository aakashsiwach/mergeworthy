"""Host git, used only for submission, and only in a fresh clean clone made for that purpose.

Never point these functions at the sandbox working copy: it is untrusted, including its ``.git``
(docs/architecture.md, "Rule: the working copy is untrusted"). The clean clone is created here from the
upstream repository, so its ``.git`` holds nothing the project or the agent wrote.

Every command runs with hooks pointed at an empty directory, fsmonitor off, no pager, no terminal prompts and
no inherited credential helpers. The push token is passed to git through an environment variable read by a
one-off credential helper, never on the command line or in a URL, and is removed from any output.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
)
TOKEN_ENV = "MERGEWORTHY_PUSH_TOKEN"
_HELPER = f'!f() {{ test "$1" = get && echo username=x-access-token && echo "password=${TOKEN_ENV}"; }}; f'


class GitError(Exception):
    pass


BRANCH_PREFIX = "mergeworthy/"
_BRANCH = re.compile(r"^mergeworthy/(\d+)-[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_LIMIT = 40


def branch_name(issue_number: int, title: str) -> str:
    """``mergeworthy/<issue-number>-<slug>``, the only branch names mergeworthy ever pushes."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > SLUG_LIMIT:
            break
        slug = candidate
    return f"{BRANCH_PREFIX}{int(issue_number)}-{slug or 'change'}"


def check_branch(branch: str, *, issue_number: int, default_branch: str) -> None:
    """Refuse anything but a ``mergeworthy/<issue-number>-<slug>`` branch for this issue."""
    match = _BRANCH.fullmatch(branch)
    if not match:
        raise GitError(
            f"refusing branch {branch!r}: only mergeworthy/<issue-number>-<slug> branches are pushed"
        )
    if int(match.group(1)) != int(issue_number):
        raise GitError(f"refusing branch {branch!r}: it is not for issue #{issue_number}")
    if branch == default_branch:
        raise GitError(f"refusing branch {branch!r}: it is the default branch")


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


class Git:
    """Runs host git. ``url_for`` builds clone and push URLs; tests point it at local repositories."""

    def __init__(self, *, executable: str = "git", base_url: str = "https://github.com") -> None:
        self.executable = executable
        self.base_url = base_url.rstrip("/")
        self._hooks = Path(tempfile.mkdtemp(prefix="mergeworthy-nohooks-"))

    def url_for(self, full_name: str) -> str:
        return f"{self.base_url}/{full_name}.git"

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        token: str | None = None,
        timeout: float = 600,
    ) -> str:
        env: dict[str, str] = {
            k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in {TOKEN_ENV}
        }
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
        command = [self.executable, *SAFE_CONFIG, "-c", f"core.hooksPath={self._hooks}"]
        if token is not None:
            env[TOKEN_ENV] = token
            command += ["-c", f"credential.helper={_HELPER}"]
        command += args
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        output = (proc.stdout or "") + (proc.stderr or "")
        if token:
            output = output.replace(token, "***")
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed ({proc.returncode}): {output.strip()[-500:]}")
        return output

    # operations ------------------------------------------------------------------------------------------

    def identity(self) -> Identity:
        """The local git identity (``git config user.name`` / ``user.email``), read outside any repository."""
        home = Path.home()
        try:
            name = self.run(["config", "--global", "--get", "user.name"], cwd=home).strip()
            email = self.run(["config", "--global", "--get", "user.email"], cwd=home).strip()
        except GitError as exc:
            raise GitError("set your git identity first: git config --global user.name / user.email") from exc
        if not name or not email:
            raise GitError("set your git identity first: git config --global user.name / user.email")
        return Identity(name, email)

    def fresh_clone(self, full_name: str, commit: str, branch: str, dest: Path) -> None:
        if dest.exists() and any(dest.iterdir()):
            raise GitError(f"{dest} is not empty")
        self.run(
            [
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-recurse-submodules",
                self.url_for(full_name),
                str(dest),
            ]
        )
        self.run(["checkout", "--quiet", "-b", branch, commit], cwd=dest)

    def apply(self, diff: str, repo: Path) -> None:
        self.run(["apply", "--check", "--whitespace=nowarn", "-"], cwd=repo, input_text=diff)
        self.run(["apply", "--whitespace=nowarn", "-"], cwd=repo, input_text=diff)

    def changed_files(self, repo: Path) -> list[str]:
        out = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo)
        files = []
        entries = out.split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            files.append(path)
            if "R" in status or "C" in status:  # renames carry the original path next
                files.append(entries[i])
                i += 1
        return sorted(set(files))

    def commit(self, repo: Path, files: list[str], identity: Identity, message: str) -> str:
        self.run(["add", "--all", "--", *files], cwd=repo)
        self.run(
            [
                "-c",
                f"user.name={identity.name}",
                "-c",
                f"user.email={identity.email}",
                "commit",
                "--quiet",
                "-F",
                "-",
            ],
            cwd=repo,
            input_text=message,
        )
        return self.run(["rev-parse", "HEAD"], cwd=repo).strip()

    def remote_branch_exists(self, repo: Path, full_name: str, branch: str, token: str | None) -> bool:
        out = self.run(
            ["ls-remote", "--heads", self.url_for(full_name), f"refs/heads/{branch}"], cwd=repo, token=token
        )
        return bool(out.strip())

    def push(self, repo: Path, full_name: str, branch: str, token: str | None) -> None:
        """Create ``refs/heads/<branch>`` on the remote. Never updates an existing branch: the lease with an
        empty expected value makes the remote refuse if the branch already exists (atomic, no race)."""
        if not _BRANCH.fullmatch(branch):
            raise GitError(f"refusing to push {branch!r}: not a mergeworthy/<issue-number>-<slug> branch")
        ref = f"refs/heads/{branch}"
        self.run(
            ["push", "--quiet", f"--force-with-lease={ref}:", self.url_for(full_name), f"HEAD:{ref}"],
            cwd=repo,
            token=token,
        )
