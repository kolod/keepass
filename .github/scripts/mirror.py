#!/usr/bin/env python3
"""Mirror KeePass 2.x source releases from SourceForge into a git repository.

The highest 'v<version>' tag marks the last mirrored release. Every newer
release found in https://sourceforge.net/projects/keepass/files/KeePass%202.x/
is downloaded (sha256-verified), extracted to the repository root, committed
with the change list from Docs/History.txt and the SourceForge upload date,
and tagged - oldest first. Files listed in KEEP (.git, .github) are preserved.

Usage:
    python .github/scripts/mirror.py [--repo DIR] [--dry-run] [--tag-prefix v]
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT = "keepass"
ROOT = "KeePass 2.x"
FILES_URL = f"https://sourceforge.net/projects/{PROJECT}/files/"
DL_URL = f"https://downloads.sourceforge.net/project/{PROJECT}/"

# Cloudflare rejects fake browser user agents; honest client names pass.
UA = "keepass-mirror/1.0 (Python-urllib)"

# repository entries that are not part of the KeePass sources
KEEP = {".git", ".github"}

# Section headers in Docs/History.txt:
#   '07/03/17 - 2.00 Alpha'   (2.00 - 2.05)
#   '2008-11-01: 2.06 Beta'   (2.06 and later)
HISTORY_HEADER = re.compile(r"^(?:\d\d/\d\d/\d\d - |\d{4}-\d\d-\d\d: )(\S+)")
# placeholder lines like '... (a lot of versions here) ...'
HISTORY_ELLIPSIS = re.compile(r"^\.\.\. \(.*\) \.\.\.\s*$")


# --- SourceForge ------------------------------------------------------------

def fetch(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


def version_key(v):
    return [int(p) if p.isdigit() else p for p in re.split(r"[.\s]+", v)]


def list_folder(path):
    """Entries of a SourceForge file browser folder: {name: {...}}.

    Entries come from the JSON embedded in the page (net.sf.files); each is
    extended with 'date' taken from the listing table.
    """
    url = FILES_URL + urllib.parse.quote(path.strip("/")) + "/"
    page = fetch(url).decode("utf-8", "replace")
    m = re.search(r"net\.sf\.files\s*=\s*(\{.*?\});\s*\n", page, re.S)
    if not m:
        raise ValueError(f"no file list found on {url}")
    entries = json.loads(m.group(1))
    for name, e in entries.items():
        row = re.search(r'<tr\s+title="%s".*?<abbr title="([^"]+)"' % re.escape(name),
                        page, re.S)
        e["date"] = row.group(1) if row else None
    return entries


def list_versions():
    return [n for n, e in list_folder(ROOT).items() if e.get("type") == "d"]


def find_source_file(version):
    """Folder entry of the source zip for a version, or None."""
    for name, e in list_folder(f"{ROOT}/{version}").items():
        if e.get("type") == "f" and re.search(r"source", name, re.I) \
                and name.lower().endswith(".zip"):
            return e
    return None


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def download(entry):
    data = fetch(DL_URL + urllib.parse.quote(entry["full_path"]))
    if entry.get("sha256") and sha256(data) != entry["sha256"]:
        raise ValueError(f"sha256 mismatch for {entry['name']}")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError(f"{entry['name']}: not a zip (got {len(data)} bytes)")
    return data


# --- commit message ---------------------------------------------------------

def history(src, version):
    """Change list for `version` from Docs/History.txt, or None."""
    path = src / "Docs" / "History.txt"
    if not path.is_file():
        return None
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    body = None
    for line in text.splitlines():
        m = HISTORY_HEADER.match(line)
        if m:
            if body is not None:
                break
            if m.group(1) == version:
                body = []
        elif body is not None and not HISTORY_ELLIPSIS.match(line):
            body.append(line.rstrip())
    return "\n".join(body).strip() if body else None


def label(version, name):
    """Release label from the archive name, e.g. '2.00-Alpha' or '2.61.1'."""
    m = re.fullmatch(r"(?:KeePass-)?(.+?)(?:-Source)?\.zip", name, re.I)
    return m.group(1) if m else version


def message(version, entry, changes=None):
    url = FILES_URL + urllib.parse.quote(f"{ROOT}/{version}") + "/"
    return (f"KeePass {label(version, entry['name'])}\n\n"
            + (f"{changes}\n\n" if changes else "")
            + f"Source: {entry['name']}\n"
            f"SHA-256: {entry.get('sha256') or '-'}\n"
            f"Released: {entry['date']}\n"
            f"{url}\n")


# --- git --------------------------------------------------------------------

def git(repo, *args, env=None, capture=False, input=None):
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       env={**os.environ, **(env or {})}, input=input,
                       stdout=subprocess.PIPE if capture else None)
    return r.stdout.decode("utf-8").strip() if capture else None


def git_date(date):
    """'2007-03-17 15:21:34 UTC' -> '2007-03-17T15:21:34+00:00'"""
    return datetime.strptime(date, "%Y-%m-%d %H:%M:%S UTC").strftime("%Y-%m-%dT%H:%M:%S+00:00")


def current_version(repo, prefix):
    """Highest mirrored version according to tags, or None."""
    versions = [t[len(prefix):] for t in git(repo, "tag", "--list", capture=True).split()
                if t.startswith(prefix) and re.fullmatch(r"\d+(\.\d+)*", t[len(prefix):])]
    return max(versions, key=version_key, default=None)


def replace_worktree(repo, src):
    for p in repo.iterdir():
        if p.name in KEEP:
            continue
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    for p in src.iterdir():
        if p.name in KEEP:
            raise ValueError(f"source archive contains reserved entry {p.name}")
        if p.is_dir():
            shutil.copytree(p, repo / p.name)
        else:
            shutil.copy2(p, repo / p.name)


def commit_version(repo, version, entry, src, prefix):
    replace_worktree(repo, src)
    git(repo, "read-tree", "--empty")  # rebuild index from disk (case renames)
    git(repo, "add", "-A")
    date = git_date(entry["date"])
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    git(repo, "commit", "-q", "--allow-empty", "-F", "-", env=env,
        input=message(version, entry, history(src, version)).encode("utf-8"))
    git(repo, "tag", "-a", prefix + version, "-m",
        f"KeePass {label(version, entry['name'])}", env=env)


# --- main -------------------------------------------------------------------

def summary(lines):
    """Append to the GitHub Actions job summary, if running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="git repository (default: current directory)")
    ap.add_argument("--tag-prefix", default="v", help="tag prefix (default: v)")
    ap.add_argument("--dry-run", action="store_true",
                    help="download and show commit messages, but do not commit")
    args = ap.parse_args()

    repo, prefix = Path(args.repo).resolve(), args.tag_prefix
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repository", file=sys.stderr)
        return 1
    if not args.dry_run:
        if git(repo, "status", "--porcelain", capture=True):
            print("working tree is not clean, aborting", file=sys.stderr)
            return 1
        # store files exactly as in the zips, track case-only renames
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "config", "core.ignorecase", "false")

    current = current_version(repo, prefix)
    available = sorted(list_versions(), key=version_key)
    new = [v for v in available if current is None or version_key(v) > version_key(current)]
    print(f"mirrored: {current or 'nothing'}, latest on SourceForge: "
          f"{available[-1] if available else '-'}, new: {' '.join(new) or 'none'}")

    done = []
    prev_date = None
    with tempfile.TemporaryDirectory() as tmp:
        for v in new:
            entry = find_source_file(v)
            if not entry:
                print(f"{v}: no source zip, skipped")
                continue
            if prev_date and entry["date"] < prev_date:
                print(f"warning: {v} was uploaded before the previous version")
            prev_date = entry["date"]

            src = Path(tmp) / v
            with zipfile.ZipFile(io.BytesIO(download(entry))) as z:
                z.extractall(src)
            if not history(src, v):
                print(f"warning: no Docs/History.txt entry for {v}")

            if args.dry_run:
                print(f"--- {prefix}{v} ({entry['date']})\n"
                      f"{message(v, entry, history(src, v))}")
            else:
                commit_version(repo, v, entry, src, prefix)
                print(f"committed {prefix}{v} ({entry['date']})")
            done.append(v)
            shutil.rmtree(src)

    verb = "would add" if args.dry_run else "added"
    print(f"done: {verb} {len(done)} version(s)")
    summary([f"### KeePass mirror{' (dry run)' if args.dry_run else ''}",
             f"Previously mirrored: `{current or 'nothing'}`", "",
             f"{verb.capitalize()}: " + (", ".join(f"`{prefix}{v}`" for v in done) or "nothing")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
