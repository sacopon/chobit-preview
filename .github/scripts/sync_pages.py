#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


SOURCE_REPOSITORY = "sacopon/chobit"
SOURCE_WORKFLOW = ".github/workflows/build-web-artifact.yml"
API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
STATE_FILENAME = ".preview-state.json"

MIB = 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * MIB
MAX_EXPANDED_BYTES = 128 * MIB
MAX_SITE_BYTES = 512 * MIB
MAX_FILE_COUNT = 16
MAX_PREVIEW_COUNT = 20
MAX_COMPRESSION_RATIO = 200

FILE_LIMITS = {
    "index.html": 1 * MIB,
    "index.js": 4 * MIB,
    "index.wasm": 64 * MIB,
    "index.pck": 64 * MIB,
    "index.png": 5 * MIB,
    "index.icon.png": 5 * MIB,
    "index.apple-touch-icon.png": 5 * MIB,
    "index.audio.worklet.js": 1 * MIB,
    "index.audio.position.worklet.js": 1 * MIB,
}
REQUIRED_FILES = {"index.html", "index.js", "index.wasm", "index.pck"}
ROOT_FILES = {"index.html", STATE_FILENAME}

PR_DIRECTORY_PATTERN = re.compile(r"pr-([1-9][0-9]*)\Z")
SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")


class SyncError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def api_url(
    path: str,
    query: dict[str, str] | None = None,
) -> str:
    url = f"{API_ROOT}/repos/{SOURCE_REPOSITORY}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return url


def api_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "chobit-preview-sync",
        "X-GitHub-Api-Version": API_VERSION,
    }


def api_json(
    token: str,
    path: str,
    query: dict[str, str] | None = None,
) -> Any:
    request = urllib.request.Request(
        api_url(path, query),
        headers=api_headers(token),
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SyncError(
            f"GitHub API request failed for {path}: HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise SyncError(
            f"GitHub API request failed for {path}"
        ) from error


def paged_list(
    token: str,
    path: str,
    query: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1

    while True:
        page_query = dict(query or {})
        page_query.update({
            "per_page": "100",
            "page": str(page),
        })

        batch = api_json(token, path, page_query)
        if not isinstance(batch, list):
            raise SyncError(f"Unexpected list response for {path}")

        results.extend(batch)

        if len(batch) < 100:
            return results

        page += 1


def named_artifacts(
    token: str,
    name: str,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    page = 1

    while True:
        response = api_json(
            token,
            "/actions/artifacts",
            {
                "name": name,
                "per_page": "100",
                "page": str(page),
            },
        )

        batch = (
            response.get("artifacts")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(batch, list):
            raise SyncError("Unexpected artifact list response")

        artifacts.extend(batch)

        if len(batch) < 100:
            return artifacts

        page += 1


def trusted_artifact(
    token: str,
    artifact: dict[str, Any],
    *,
    event: str,
    head_sha: str | None = None,
    head_branch: str | None = None,
) -> bool:
    if artifact.get("expired") is not False:
        return False

    size = artifact.get("size_in_bytes")
    if (
        not isinstance(size, int)
        or size <= 0
        or size > MAX_ARCHIVE_BYTES
    ):
        return False

    digest = artifact.get("digest")
    if (
        not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
    ):
        return False

    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        return False

    run_id = workflow_run.get("id")
    if not isinstance(run_id, int):
        return False

    run = api_json(token, f"/actions/runs/{run_id}")
    if not isinstance(run, dict):
        return False

    if run.get("path") != SOURCE_WORKFLOW:
        return False

    if run.get("event") != event:
        return False

    if run.get("conclusion") != "success":
        return False

    if head_sha is not None and run.get("head_sha") != head_sha:
        return False

    if (
        head_branch is not None
        and run.get("head_branch") != head_branch
    ):
        return False

    return True


def select_artifact(
    token: str,
    name: str,
    *,
    event: str,
    head_sha: str | None = None,
    head_branch: str | None = None,
) -> dict[str, Any] | None:
    candidates = sorted(
        named_artifacts(token, name),
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )

    for artifact in candidates:
        if trusted_artifact(
            token,
            artifact,
            event=event,
            head_sha=head_sha,
            head_branch=head_branch,
        ):
            return artifact

    return None


def artifact_redirect_url(
    token: str,
    artifact_id: int,
) -> str:
    request = urllib.request.Request(
        api_url(f"/actions/artifacts/{artifact_id}/zip"),
        headers=api_headers(token),
    )
    opener = urllib.request.build_opener(NoRedirect())

    try:
        opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise SyncError(
                f"Artifact download request failed: HTTP {error.code}"
            ) from error

        location = error.headers.get("Location")
        if not location:
            raise SyncError("Artifact download redirect is missing")

        parsed = urllib.parse.urlparse(location)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SyncError(
                "Artifact download redirect is not HTTPS"
            )

        return location

    raise SyncError(
        "Artifact download did not return a redirect"
    )


def download_artifact(
    token: str,
    artifact: dict[str, Any],
    destination: Path,
) -> None:
    artifact_id = artifact.get("id")
    expected_size = artifact.get("size_in_bytes")
    digest = artifact.get("digest")

    if not isinstance(artifact_id, int):
        raise SyncError("Artifact ID is invalid")

    if not isinstance(expected_size, int):
        raise SyncError("Artifact size is invalid")

    if not isinstance(digest, str):
        raise SyncError("Artifact digest is invalid")

    match = SHA256_PATTERN.fullmatch(digest)
    if match is None:
        raise SyncError("Artifact digest is invalid")

    expected_digest = match.group(1)
    redirect_url = artifact_redirect_url(token, artifact_id)

    # 認証ヘッダーはGitHub APIにだけ送る。
    # リダイレクト先のストレージには送らない。
    request = urllib.request.Request(
        redirect_url,
        headers={"User-Agent": "chobit-preview-sync"},
    )

    actual_size = 0
    hasher = hashlib.sha256()

    try:
        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(MIB)
                    if not chunk:
                        break

                    actual_size += len(chunk)
                    if actual_size > MAX_ARCHIVE_BYTES:
                        raise SyncError(
                            "Artifact archive exceeds the size limit"
                        )

                    hasher.update(chunk)
                    output.write(chunk)
    except urllib.error.URLError as error:
        raise SyncError(
            "Artifact archive download failed"
        ) from error

    if actual_size != expected_size:
        raise SyncError(
            "Artifact archive size mismatch: "
            f"{actual_size} != {expected_size}"
        )

    if hasher.hexdigest() != expected_digest:
        raise SyncError(
            "Artifact archive SHA-256 mismatch"
        )


def validate_and_extract(
    archive_path: Path,
    output_dir: Path,
) -> None:
    output_dir.mkdir()

    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()

            if len(entries) > MAX_FILE_COUNT:
                raise SyncError(
                    "Artifact contains too many files"
                )

            names = [entry.filename for entry in entries]

            if len(names) != len(set(names)):
                raise SyncError(
                    "Artifact contains duplicate paths"
                )

            if not REQUIRED_FILES.issubset(names):
                raise SyncError(
                    "Artifact is missing required Web files"
                )

            expanded_size = 0

            for entry in entries:
                name = entry.filename
                path = Path(name)

                if (
                    entry.is_dir()
                    or path.is_absolute()
                    or len(path.parts) != 1
                    or "\\" in name
                    or name not in FILE_LIMITS
                ):
                    raise SyncError(
                        f"Artifact path is not allowed: {name}"
                    )

                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise SyncError(
                        "Artifact contains a symbolic link: "
                        f"{name}"
                    )

                if entry.flag_bits & 0x1:
                    raise SyncError(
                        "Artifact contains an encrypted file: "
                        f"{name}"
                    )

                if (
                    entry.file_size <= 0
                    or entry.file_size > FILE_LIMITS[name]
                ):
                    raise SyncError(
                        "Artifact file size is not allowed: "
                        f"{name}"
                    )

                expanded_size += entry.file_size
                if expanded_size > MAX_EXPANDED_BYTES:
                    raise SyncError(
                        "Expanded Artifact exceeds the size limit"
                    )

                if (
                    entry.compress_size > 0
                    and entry.file_size / entry.compress_size
                    > MAX_COMPRESSION_RATIO
                ):
                    raise SyncError(
                        "Artifact compression ratio is not allowed: "
                        f"{name}"
                    )

            # extractallは使わず、検査済みの直下ファイルだけを書く。
            for entry in entries:
                destination = output_dir / entry.filename

                with archive.open(entry) as source:
                    with destination.open("xb") as output:
                        shutil.copyfileobj(source, output)

                destination.chmod(0o644)

    except zipfile.BadZipFile as error:
        raise SyncError(
            "Artifact is not a valid ZIP archive"
        ) from error


def install_artifact(
    token: str,
    artifact: dict[str, Any],
    target: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="chobit-preview-",
        dir=target.parent,
    ) as temporary_directory:
        temporary_root = Path(temporary_directory)
        archive_path = temporary_root / "artifact.zip"
        extracted_path = temporary_root / "content"

        download_artifact(token, artifact, archive_path)
        validate_and_extract(archive_path, extracted_path)

        if target.exists():
            shutil.rmtree(target)

        shutil.move(str(extracted_path), str(target))


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "main": None,
        "pull_requests": {},
    }


def load_state(site_dir: Path) -> dict[str, Any]:
    state_path = site_dir / STATE_FILENAME

    if not state_path.exists():
        return empty_state()

    try:
        state = json.loads(
            state_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(
            "Preview state file is invalid"
        ) from error

    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(
            state.get("pull_requests"),
            dict,
        )
    ):
        raise SyncError(
            "Preview state structure is invalid"
        )

    return state


def artifact_state(
    artifact: dict[str, Any],
) -> dict[str, Any]:
    workflow_run = artifact["workflow_run"]

    return {
        "artifact_id": artifact["id"],
        "digest": artifact["digest"],
        "head_sha": workflow_run["head_sha"],
        "run_id": workflow_run["id"],
    }


def remove_directory(
    path: Path,
    site_dir: Path,
) -> None:
    if path.parent != site_dir:
        raise SyncError(
            "Refusing to remove a path outside the site root"
        )

    if (
        path.name != "main"
        and PR_DIRECTORY_PATTERN.fullmatch(path.name) is None
    ):
        raise SyncError(
            f"Refusing to remove an unexpected path: {path.name}"
        )

    if path.exists():
        shutil.rmtree(path)


def tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()

        if path.is_symlink():
            raise SyncError(
                "Site state contains a symbolic link: "
                f"{relative}"
            )

        if not path.is_file():
            continue

        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")

        with path.open("rb") as source:
            while True:
                chunk = source.read(MIB)
                if not chunk:
                    break
                hasher.update(chunk)

    return hasher.hexdigest()


def validate_preview_directory(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        raise SyncError(
            f"Invalid preview directory: {path.name}"
        )

    entries = list(path.iterdir())
    if len(entries) > MAX_FILE_COUNT:
        raise SyncError(
            f"Too many files in preview: {path.name}"
        )

    names: set[str] = set()
    total_size = 0

    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise SyncError(
                f"Invalid preview path: {entry.name}"
            )

        if entry.name not in FILE_LIMITS:
            raise SyncError(
                f"Unexpected preview file: {entry.name}"
            )

        size = entry.stat().st_size
        if size <= 0 or size > FILE_LIMITS[entry.name]:
            raise SyncError(
                f"Invalid preview file size: {entry.name}"
            )

        names.add(entry.name)
        total_size += size

    if not REQUIRED_FILES.issubset(names):
        raise SyncError(
            f"Required files are missing from {path.name}"
        )

    return total_size


def validate_site(site_dir: Path) -> None:
    preview_count = 0
    total_size = 0
    main_found = False

    for entry in site_dir.iterdir():
        if entry.is_symlink():
            raise SyncError(
                f"Site contains a symbolic link: {entry.name}"
            )

        if entry.is_file():
            if entry.name not in ROOT_FILES:
                raise SyncError(
                    f"Unexpected site root file: {entry.name}"
                )

            size = entry.stat().st_size
            if size > MIB:
                raise SyncError(
                    f"Site root file is too large: {entry.name}"
                )

            total_size += size
            continue

        if not entry.is_dir():
            raise SyncError(
                f"Unexpected site root path: {entry.name}"
            )

        if entry.name == "main":
            main_found = True
        elif PR_DIRECTORY_PATTERN.fullmatch(entry.name):
            preview_count += 1
        else:
            raise SyncError(
                f"Unexpected site directory: {entry.name}"
            )

        total_size += validate_preview_directory(entry)

    if not main_found:
        raise SyncError(
            "The site does not contain a main preview"
        )

    if preview_count > MAX_PREVIEW_COUNT:
        raise SyncError(
            "The site contains too many PR previews"
        )

    if total_size > MAX_SITE_BYTES:
        raise SyncError(
            "The complete Pages site exceeds the size limit"
        )


def write_root_index(site_dir: Path) -> None:
    links: list[str] = []

    if (site_dir / "main" / "index.html").is_file():
        links.append(
            '<li><a href="./main/">main</a></li>'
        )

    preview_numbers: list[int] = []

    for child in site_dir.iterdir():
        match = PR_DIRECTORY_PATTERN.fullmatch(child.name)

        if (
            match
            and child.is_dir()
            and (child / "index.html").is_file()
        ):
            preview_numbers.append(int(match.group(1)))

    for number in sorted(preview_numbers):
        label = html.escape(f"PR #{number}")
        links.append(
            f'<li><a href="./pr-{number}/">{label}</a></li>'
        )

    document = (
        "<!doctype html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" '
        'content="width=device-width, initial-scale=1">\n'
        "  <title>chobit previews</title>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>chobit previews</h1>\n"
        "  <ul>\n"
        + "".join(
            f"    {link}\n"
            for link in links
        )
        + "  </ul>\n"
        "</body>\n"
        "</html>\n"
    )

    (site_dir / "index.html").write_text(
        document,
        encoding="utf-8",
    )


def synchronize(
    token: str,
    site_dir: Path,
) -> bool:
    site_dir.mkdir(parents=True, exist_ok=True)

    before = tree_digest(site_dir)
    state = load_state(site_dir)

    main_artifact = select_artifact(
        token,
        "chobit-web-main",
        event="push",
        head_branch="main",
    )
    main_target = site_dir / "main"

    if main_artifact is not None:
        new_main_state = artifact_state(main_artifact)

        if (
            state.get("main") != new_main_state
            or not main_target.is_dir()
        ):
            install_artifact(
                token,
                main_artifact,
                main_target,
            )
            state["main"] = new_main_state

    elif not main_target.is_dir():
        raise SyncError(
            "No trusted main Artifact or saved main preview "
            "is available"
        )

    open_pull_requests = paged_list(
        token,
        "/pulls",
        {"state": "open"},
    )

    ready_pull_requests: dict[int, dict[str, Any]] = {}

    for pull_request in open_pull_requests:
        number = pull_request.get("number")
        head = pull_request.get("head")

        if (
            isinstance(number, int)
            and pull_request.get("draft") is False
            and isinstance(head, dict)
            and isinstance(head.get("sha"), str)
        ):
            ready_pull_requests[number] = pull_request

    pull_request_state = state["pull_requests"]
    ready_keys = {
        str(number)
        for number in ready_pull_requests
    }

    # Closed、Merged、DraftになったPRを削除する。
    for key in list(pull_request_state):
        if key not in ready_keys:
            if key.isdigit():
                remove_directory(
                    site_dir / f"pr-{key}",
                    site_dir,
                )

            del pull_request_state[key]

    # 状態ファイルにない余分なPRディレクトリも削除する。
    for child in list(site_dir.iterdir()):
        match = PR_DIRECTORY_PATTERN.fullmatch(child.name)

        if match and match.group(1) not in ready_keys:
            remove_directory(child, site_dir)

    for number, pull_request in sorted(
        ready_pull_requests.items()
    ):
        head_sha = pull_request["head"]["sha"]

        artifact = select_artifact(
            token,
            f"chobit-web-pr-{number}",
            event="pull_request",
            head_sha=head_sha,
        )

        target = site_dir / f"pr-{number}"
        key = str(number)

        # 新しいHEAD用Artifactがまだなければ、
        # 公開済みの最後の成功版を維持する。
        if artifact is None:
            continue

        new_state = artifact_state(artifact)

        if (
            pull_request_state.get(key) != new_state
            or not target.is_dir()
        ):
            install_artifact(
                token,
                artifact,
                target,
            )
            pull_request_state[key] = new_state

    write_root_index(site_dir)

    state_path = site_dir / STATE_FILENAME
    state_path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    validate_site(site_dir)

    after = tree_digest(site_dir)
    return before != after


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--site-dir",
        required=True,
        type=Path,
    )
    args = parser.parse_args()

    token = os.environ.get("SOURCE_ARTIFACT_TOKEN")
    if not token:
        raise SyncError(
            "SOURCE_ARTIFACT_TOKEN is not set"
        )

    changed = synchronize(
        token,
        args.site_dir.resolve(),
    )

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(
            output_path,
            "a",
            encoding="utf-8",
        ) as output:
            output.write(
                f"changed={'true' if changed else 'false'}\n"
            )

    print(
        "Preview state changed: "
        f"{'yes' if changed else 'no'}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(
            f"Sync failed: {error}",
            file=os.sys.stderr,
        )
        raise SystemExit(1)
