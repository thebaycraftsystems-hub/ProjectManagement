#!/usr/bin/env python3
"""
Compare two application project folders (config, lib, startup/shutdown scripts).

Reads paths from config.ini. Supports local paths or SSH/SFTP to a Linux server.
Writes reports under logs/ and lib_changes/.
"""

from __future__ import annotations

import configparser
import difflib
import hashlib
import logging
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

# Activity logger (console + file); configured in setup_activity_logging()
log = logging.getLogger("compare_projects")

# ---------------------------------------------------------------------------
# Backup / skip rules
# ---------------------------------------------------------------------------

SKIP_DIR_NAMES = frozenset({"logs", "log"})


def is_backup_name(name: str) -> bool:
    """Skip files/folders whose name suggests a backup copy."""
    lower = name.lower()
    if "backup" in lower:
        return True
    for suffix in (".bak", ".old", "~"):
        if lower.endswith(suffix):
            return True
    for prefix in ("backup_", "backup.", "bak_"):
        if lower.startswith(prefix):
            return True
    return False


def should_skip_dir(dir_name: str) -> bool:
    return dir_name.lower() in SKIP_DIR_NAMES or is_backup_name(dir_name)


# ---------------------------------------------------------------------------
# Activity logging (console + file)
# ---------------------------------------------------------------------------


def setup_activity_logging(logs_dir: Path, prefix: str = "activity") -> Path:
    """Log all run activities to console and logs/activity_<timestamp>.log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"{prefix}_{stamp}.log"

    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    log.addHandler(console)
    log.addHandler(file_handler)
    return log_file


# ---------------------------------------------------------------------------
# File system backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    rel_path: str  # posix-style relative path
    is_dir: bool


class ProjectFS(ABC):
    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def is_dir(self, path: str) -> bool: ...

    @abstractmethod
    def read_text(self, path: str) -> str: ...

    @abstractmethod
    def file_digest(self, path: str) -> str: ...

    @abstractmethod
    def list_entries(self, dir_path: str) -> list[FileEntry]: ...

    def walk_files(self, dir_path: str, prefix: str = "") -> Iterator[str]:
        """Yield relative file paths under dir_path (posix)."""
        if not self.exists(dir_path) or not self.is_dir(dir_path):
            return
        for entry in sorted(self.list_entries(dir_path), key=lambda e: e.rel_path):
            name = PurePosixPath(entry.rel_path).name
            if is_backup_name(name):
                log.debug("Skipping backup item: %s", entry.rel_path)
                continue
            full = f"{dir_path.rstrip('/')}/{entry.rel_path}".replace("//", "/")
            rel = f"{prefix}/{entry.rel_path}".lstrip("/") if prefix else entry.rel_path
            if entry.is_dir:
                if should_skip_dir(name):
                    log.debug("Skipping directory: %s", name)
                    continue
                yield from self.walk_files(full, rel)
            else:
                yield rel.replace("\\", "/")


class LocalFS(ProjectFS):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _full(self, path: str) -> Path:
        p = path.lstrip("/").replace("/", os.sep)
        return self.root / p if p else self.root

    def exists(self, path: str) -> bool:
        return self._full(path).exists()

    def is_dir(self, path: str) -> bool:
        return self._full(path).is_dir()

    def read_text(self, path: str) -> str:
        return self._full(path).read_text(encoding="utf-8", errors="replace")

    def file_digest(self, path: str) -> str:
        h = hashlib.sha256()
        with open(self._full(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def list_entries(self, dir_path: str) -> list[FileEntry]:
        base = self._full(dir_path)
        if not base.is_dir():
            return []
        out: list[FileEntry] = []
        for child in base.iterdir():
            out.append(FileEntry(child.name, child.is_dir()))
        return out


class SftpFS(ProjectFS):
    def __init__(self, sftp, root: str) -> None:
        self.sftp = sftp
        self.root = root.rstrip("/")

    def _full(self, path: str) -> str:
        p = path.lstrip("/")
        return f"{self.root}/{p}" if p else self.root

    def exists(self, path: str) -> bool:
        try:
            self.sftp.stat(self._full(path))
            return True
        except OSError:
            return False

    def is_dir(self, path: str) -> bool:
        import stat

        try:
            mode = self.sftp.stat(self._full(path)).st_mode
            return stat.S_ISDIR(mode)
        except OSError:
            return False

    def read_text(self, path: str) -> str:
        with self.sftp.open(self._full(path), "r") as f:
            data = f.read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return data

    def file_digest(self, path: str) -> str:
        h = hashlib.sha256()
        with self.sftp.open(self._full(path), "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def list_entries(self, dir_path: str) -> list[FileEntry]:
        full = self._full(dir_path)
        try:
            names = self.sftp.listdir(full)
        except OSError:
            return []
        out: list[FileEntry] = []
        import stat

        for name in names:
            try:
                mode = self.sftp.stat(f"{full}/{name}").st_mode
                out.append(FileEntry(name, stat.S_ISDIR(mode)))
            except OSError:
                continue
        return out


def connect_sftp(host: str, port: int, username: str, password: str):
    import paramiko

    log.info("Connecting via SSH to %s:%s as %s ...", host, port, username)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, port=port, username=username, password=password, timeout=30)
    log.info("SSH connection established")
    return client, client.open_sftp()


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def unified_diff_short(
    left_lines: list[str],
    right_lines: list[str],
    left_label: str,
    right_label: str,
    context: int = 3,
) -> str:
    diff = list(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile=left_label,
            tofile=right_label,
            lineterm="",
            n=context,
        )
    )
    if not diff:
        return "(identical)\n"
    return "\n".join(diff) + "\n"


def _extract_xml_tag(line: str) -> str | None:
    """Return inner text of a simple <Tag>value</Tag> line, else None."""
    stripped = line.strip()
    if not stripped.startswith("<") or stripped.count("<") < 2:
        return None
    start = stripped.find(">") + 1
    end = stripped.rfind("<")
    if start > 0 and end > start:
        tag_end = stripped.find(">")
        tag_name = stripped[1:tag_end].split()[0] if tag_end > 1 else ""
        value = stripped[start:end].strip()
        if tag_name and value:
            return f"{tag_name} = {value!r}"
    return None


def _describe_line_change(old_line: str, new_line: str) -> str:
    """One-line hint for what changed between two similar lines."""
    old_hint = _extract_xml_tag(old_line)
    new_hint = _extract_xml_tag(new_line)
    if old_hint and new_hint and old_hint != new_hint:
        return f"XML setting changed: {old_hint}  -->  {new_hint}"
    if old_line.strip() == new_line.strip():
        return "Whitespace or invisible characters differ"
    return "Line text was modified"


def build_plain_summary(
    left_lines: list[str],
    right_lines: list[str],
    label1: str,
    label2: str,
) -> str:
    """Human-readable summary: show both sides and what changed."""
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines)
    blocks: list[str] = []
    change_num = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        change_num += 1
        prod_lines = left_lines[i1:i2]
        uat_lines = right_lines[j1:j2]
        prod_ln = (i1 + 1) if prod_lines else "-"
        uat_ln = (j1 + 1) if uat_lines else "-"

        block = [f"Change #{change_num} (Prod line {prod_ln}, UAT line {uat_ln})", ""]

        if tag == "replace":
            block.append(f"Type: VALUE CHANGED (same location, different content)")
            block.append("")
            block.append(f"  {label1}:")
            for ln, text in zip(range(i1 + 1, i2 + 1), prod_lines, strict=False):
                block.append(f"    Line {ln}: {text}")
            block.append("")
            block.append(f"  {label2}:")
            for ln, text in zip(range(j1 + 1, j2 + 1), uat_lines, strict=False):
                block.append(f"    Line {ln}: {text}")
            if len(prod_lines) == 1 and len(uat_lines) == 1:
                block.append("")
                block.append(f"  In plain words: {_describe_line_change(prod_lines[0], uat_lines[0])}")
        elif tag == "delete":
            block.append(f"Type: REMOVED from UAT (exists only in {label1})")
            block.append("")
            block.append(f"  {label1} — present:")
            for ln, text in zip(range(i1 + 1, i2 + 1), prod_lines, strict=False):
                block.append(f"    Line {ln}: {text}")
            block.append(f"  {label2} — missing (not in file)")
        elif tag == "insert":
            block.append(f"Type: ADDED in UAT (not in {label1})")
            block.append("")
            block.append(f"  {label1} — missing (not in file)")
            block.append(f"  {label2} — present:")
            for ln, text in zip(range(j1 + 1, j2 + 1), uat_lines, strict=False):
                block.append(f"    Line {ln}: {text}")

        blocks.append("\n".join(block))

    if not blocks:
        return ""

    header = [
        "PLAIN ENGLISH SUMMARY",
        "=" * 60,
        f"Total differences: {change_num} change block(s)",
        "",
        "How to read unified diff below (optional):",
        f"  Lines starting with '-' = only in {label1} (Prod)",
        f"  Lines starting with '+' = only in {label2} (UAT)",
        "  Lines with no prefix = same in both (shown for context)",
        "",
        "-" * 60,
        "",
    ]
    return "\n".join(header) + "\n\n".join(blocks) + "\n\n"


def format_comparison_body(
    left_lines: list[str],
    right_lines: list[str],
    label1: str,
    label2: str,
    rel_path: str,
) -> str:
    """Full report: plain summary first, then technical unified diff."""
    plain = build_plain_summary(left_lines, right_lines, label1, label2)
    technical = unified_diff_short(
        left_lines, right_lines, f"{label1}:{rel_path}", f"{label2}:{rel_path}"
    )
    parts = [plain] if plain else []
    parts.append("TECHNICAL DIFF (vimdiff style)")
    parts.append("=" * 60)
    parts.append(technical.rstrip())
    return "\n".join(parts) + "\n"


def find_first_existing(fs: ProjectFS, candidates: Iterable[str]) -> str | None:
    for name in candidates:
        if fs.exists(name) and not fs.is_dir(name):
            return name
    return None


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


@dataclass
class CompareResult:
    category: str
    name: str
    status: str  # identical | different | only_p1 | only_p2 | missing
    detail_path: str | None = None


def compare_text_files(
    fs1: ProjectFS,
    fs2: ProjectFS,
    rel_path: str,
    label1: str,
    label2: str,
) -> tuple[str, str]:
    """Return (status, diff_body)."""
    p1, p2 = fs1.exists(rel_path), fs2.exists(rel_path)
    if p1 and not p2:
        return "only_p1", f"Present only in {label1}: {rel_path}\n"
    if p2 and not p1:
        return "only_p2", f"Present only in {label2}: {rel_path}\n"
    if not p1 and not p2:
        return "missing", f"Missing in both: {rel_path}\n"

    t1 = fs1.read_text(rel_path).splitlines()
    t2 = fs2.read_text(rel_path).splitlines()
    if t1 == t2:
        return "identical", "(identical)\n"
    body = format_comparison_body(t1, t2, label1, label2, rel_path)
    return "different", body


def compare_config_folders(
    fs1: ProjectFS,
    fs2: ProjectFS,
    config_subdir: str,
    label1: str,
    label2: str,
    out_dir: Path,
) -> list[CompareResult]:
    results: list[CompareResult] = []
    dir1 = config_subdir
    dir2 = config_subdir

    log.info("--- Comparing config folder: %s/ ---", config_subdir)
    files1 = set()
    if fs1.exists(dir1) and fs1.is_dir(dir1):
        files1 = {PurePosixPath(p).name for p in fs1.walk_files(dir1)}
        log.info("%s: found %d config file(s)", label1, len(files1))
    else:
        log.warning("%s: config folder '%s' not found", label1, dir1)

    files2 = set()
    if fs2.exists(dir2) and fs2.is_dir(dir2):
        files2 = {PurePosixPath(p).name for p in fs2.walk_files(dir2)}
        log.info("%s: found %d config file(s)", label2, len(files2))
    else:
        log.warning("%s: config folder '%s' not found", label2, dir2)

    all_names = sorted(files1 | files2)
    log.info("Comparing %d config file name(s): %s", len(all_names), ", ".join(all_names) or "(none)")
    config_out = out_dir / "config"
    config_out.mkdir(parents=True, exist_ok=True)

    for name in all_names:
        if is_backup_name(name):
            log.info("Skipping backup config file: %s", name)
            continue
        rel = f"{config_subdir}/{name}"
        log.info("Comparing config file: %s", rel)
        status, body = compare_text_files(fs1, fs2, rel, label1, label2)
        safe_name = name.replace(os.sep, "_")
        detail_file = config_out / f"{safe_name}.diff.txt"
        header = (
            f"=== {label1} vs {label2} ===\n"
            f"File: {rel}\n"
            f"Status: {status}\n"
            f"{'=' * 60}\n\n"
        )
        detail_file.write_text(header + body, encoding="utf-8")
        if status == "different" and body.startswith("PLAIN ENGLISH"):
            for line in body.splitlines():
                if line.startswith("Change #") or line.startswith("  In plain words:"):
                    log.info("  %s", line.strip())
        log.info("  -> %s | report: %s", status.upper(), detail_file)
        results.append(
            CompareResult("config", name, status, str(detail_file.relative_to(out_dir.parent)))
        )
    log.info("Config comparison finished (%d file(s))", len(results))
    return results


def compare_lib_folders(
    fs1: ProjectFS,
    fs2: ProjectFS,
    lib_subdir: str,
    label1: str,
    label2: str,
    lib_out: Path,
) -> list[CompareResult]:
    results: list[CompareResult] = []
    libs1: dict[str, str] = {}
    libs2: dict[str, str] = {}

    log.info("--- Comparing lib folder: %s/ ---", lib_subdir)
    if fs1.exists(lib_subdir) and fs1.is_dir(lib_subdir):
        for rel in fs1.walk_files(lib_subdir):
            base = PurePosixPath(rel).name
            if is_backup_name(base):
                log.info("Skipping backup library (%s): %s", label1, base)
                continue
            path = f"{lib_subdir}/{rel}"
            log.info("Hashing %s: %s", label1, path)
            libs1[base] = fs1.file_digest(path)
            log.debug("  %s SHA256: %s", base, libs1[base])
    else:
        log.warning("%s: lib folder '%s' not found", label1, lib_subdir)

    if fs2.exists(lib_subdir) and fs2.is_dir(lib_subdir):
        for rel in fs2.walk_files(lib_subdir):
            base = PurePosixPath(rel).name
            if is_backup_name(base):
                log.info("Skipping backup library (%s): %s", label2, base)
                continue
            path = f"{lib_subdir}/{rel}"
            log.info("Hashing %s: %s", label2, path)
            libs2[base] = fs2.file_digest(path)
            log.debug("  %s SHA256: %s", base, libs2[base])
    else:
        log.warning("%s: lib folder '%s' not found", label2, lib_subdir)

    all_libs = sorted(set(libs1) | set(libs2))
    rows: list[tuple[str, str, str, str, str]] = []

    for lib in all_libs:
        d1 = libs1.get(lib, "-")
        d2 = libs2.get(lib, "-")
        if lib not in libs1:
            status = "only_p2"
            match = "Unmatched"
        elif lib not in libs2:
            status = "only_p1"
            match = "Unmatched"
        elif d1 == d2:
            status = "identical"
            match = "Matched"
        else:
            status = "different"
            match = "Unmatched"
        rows.append((match, lib, d1[:16], d2[:16], status))
        results.append(CompareResult("lib", lib, status))
        log.info("Library %-10s %s | %s: %s | %s: %s", match, lib, label1, d1[:16], label2, d2[:16])

    rows.sort(key=lambda r: (0 if r[0] == "Unmatched" else 1, r[1].lower()))
    unmatched = sum(1 for r in rows if r[0] == "Unmatched")
    matched = len(rows) - unmatched
    log.info("Lib summary: %d Matched, %d Unmatched (of %d)", matched, unmatched, len(rows))

    col_match, col_lib = 10, 40
    col_sum = 18
    lines = [
        f"Library comparison: {label1} vs {label2}",
        f"Folder: {lib_subdir}/",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"{'Status':<{col_match}} {'Library':<{col_lib}} {label1 + ' SHA256':<{col_sum}} {label2 + ' SHA256':<{col_sum}} Note",
        "-" * (col_match + col_lib + col_sum * 2 + 10),
    ]
    for match, lib, s1, s2, status in rows:
        note = status.replace("_", " ")
        lines.append(
            f"{match:<{col_match}} {lib:<{col_lib}} {s1:<{col_sum}} {s2:<{col_sum}} {note}"
        )

    lib_out.mkdir(parents=True, exist_ok=True)
    report = lib_out / "lib_comparison.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Lib report written: %s", report)

    for match, lib, s1, s2, status in rows:
        if match == "Matched":
            continue
        side_path = lib_out / f"{lib}.txt"
        log.info("Lib detail written: %s", side_path)
        body = [
            f"Library: {lib}",
            f"Status: {match} ({status})",
            "",
            f"{label1} ({'present' if lib in libs1 else 'missing'}):",
            f"  SHA256: {libs1.get(lib, 'N/A')}",
            "",
            f"{label2} ({'present' if lib in libs2 else 'missing'}):",
            f"  SHA256: {libs2.get(lib, 'N/A')}",
        ]
        side_path.write_text("\n".join(body) + "\n", encoding="utf-8")

    return results


def compare_scripts(
    fs1: ProjectFS,
    fs2: ProjectFS,
    candidates: list[str],
    category: str,
    label1: str,
    label2: str,
    out_dir: Path,
) -> list[CompareResult]:
    results: list[CompareResult] = []
    script_out = out_dir / "scripts"
    script_out.mkdir(parents=True, exist_ok=True)

    log.info("--- Comparing %s script (%s) ---", category, " / ".join(candidates))
    p1 = find_first_existing(fs1, candidates)
    p2 = find_first_existing(fs2, candidates)
    display = p1 or p2 or candidates[0]
    log.info("%s script: %s", label1, p1 or "NOT FOUND")
    log.info("%s script: %s", label2, p2 or "NOT FOUND")

    if not p1 and not p2:
        log.warning("No %s script found in either project", category)
        results.append(CompareResult(category, display, "missing"))
        return results

    if not p1:
        status, body = "only_p2", f"Present only in {label2}: {p2}\n"
    elif not p2:
        status, body = "only_p1", f"Present only in {label1}: {p1}\n"
    else:
        t1 = fs1.read_text(p1).splitlines()
        t2 = fs2.read_text(p2).splitlines()
        if t1 == t2:
            status, body = "identical", "(identical)\n"
        else:
            status = "different"
            body = format_comparison_body(t1, t2, label1, label2, p1)
        if p1 != p2:
            body = f"Note: {label1} uses '{p1}', {label2} uses '{p2}'\n\n" + body

    detail = script_out / f"{category}.diff.txt"
    header = (
        f"=== {category} ({' / '.join(candidates)}) ===\n"
        f"{label1}: {p1 or 'NOT FOUND'}\n"
        f"{label2}: {p2 or 'NOT FOUND'}\n"
        f"Status: {status}\n"
        f"{'=' * 60}\n\n"
    )
    detail.write_text(header + body, encoding="utf-8")
    log.info("%s script result: %s | report: %s", category, status.upper(), detail)
    results.append(
        CompareResult(category, display, status, str(detail.relative_to(out_dir.parent)))
    )
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.is_file():
        sys.stderr.write(f"Config not found: {path}\n")
        sys.stderr.write("Copy config.ini.example to config.ini and edit paths.\n")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg


def build_filesystems(cfg: configparser.ConfigParser) -> tuple[ProjectFS, ProjectFS, object | None]:
    p1 = cfg.get("projects", "project1_path")
    p2 = cfg.get("projects", "project2_path")
    ssh_enabled = cfg.getboolean("ssh", "enabled", fallback=False)
    client = None

    if ssh_enabled:
        host = cfg.get("ssh", "host")
        port = cfg.getint("ssh", "port", fallback=22)
        user = cfg.get("ssh", "username")
        password = cfg.get("ssh", "password")
        client, sftp = connect_sftp(host, port, user, password)
        log.info("Project roots over SSH: [%s] %s", cfg.get("projects", "project1_name"), p1)
        log.info("Project roots over SSH: [%s] %s", cfg.get("projects", "project2_name"), p2)
        return SftpFS(sftp, p1), SftpFS(sftp, p2), client

    log.info("Using local filesystem")
    log.info("Project 1 path: %s", p1)
    log.info("Project 2 path: %s", p2)
    return LocalFS(p1), LocalFS(p2), None


def write_summary(
    out_dir: Path,
    label1: str,
    label2: str,
    path1: str,
    path2: str,
    all_results: list[CompareResult],
) -> None:
    lines = [
        "PROJECT COMPARISON SUMMARY",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"{label1}: {path1}",
        f"{label2}: {path2}",
        "",
        f"{'Category':<12} {'Name':<40} {'Status':<12} Detail",
        "-" * 90,
    ]
    counts: dict[str, int] = {}
    for r in all_results:
        counts[r.status] = counts.get(r.status, 0) + 1
        detail = r.detail_path or ""
        lines.append(f"{r.category:<12} {r.name:<40} {r.status:<12} {detail}")

    lines.extend(
        [
            "",
            "Totals:",
            f"  identical : {counts.get('identical', 0)}",
            f"  different : {counts.get('different', 0)}",
            f"  only_p1   : {counts.get('only_p1', 0)}",
            f"  only_p2   : {counts.get('only_p2', 0)}",
            f"  missing   : {counts.get('missing', 0)}",
        ]
    )
    summary_path = out_dir / "summary.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Summary report written: %s", summary_path)


def main() -> None:
    base = Path(__file__).resolve().parent
    cfg_path = base / "config.ini"
    if len(sys.argv) > 1:
        cfg_path = Path(sys.argv[1]).resolve()

    cfg = load_config(cfg_path)
    label1 = cfg.get("projects", "project1_name", fallback="Project1")
    label2 = cfg.get("projects", "project2_name", fallback="Project2")
    path1 = cfg.get("projects", "project1_path")
    path2 = cfg.get("projects", "project2_path")
    config_folder = cfg.get("compare", "config_folder", fallback="config")
    lib_folder = cfg.get("compare", "lib_folder", fallback="lib")
    logs_dir = base / cfg.get("output", "logs_dir", fallback="logs")
    lib_changes = logs_dir / cfg.get("output", "lib_changes_dir", fallback="lib_changes")
    activity_prefix = cfg.get("output", "activity_log_prefix", fallback="activity")

    logs_dir.mkdir(parents=True, exist_ok=True)
    activity_log = setup_activity_logging(logs_dir, activity_prefix)

    log.info("=" * 60)
    log.info("Project comparison started")
    log.info("Config file: %s", cfg_path)
    log.info("Activity log file: %s", activity_log)
    log.info("Comparing %s vs %s", label1, label2)
    log.info("Skipping project folders: %s", ", ".join(sorted(SKIP_DIR_NAMES)))
    log.info("Excluding backup names (backup, .bak, .old, etc.)")

    client = None
    try:
        fs1, fs2, client = build_filesystems(cfg)

        all_results: list[CompareResult] = []
        all_results.extend(
            compare_config_folders(fs1, fs2, config_folder, label1, label2, logs_dir)
        )
        all_results.extend(
            compare_lib_folders(fs1, fs2, lib_folder, label1, label2, lib_changes)
        )
        all_results.extend(
            compare_scripts(
                fs1,
                fs2,
                ["start.sh", "startup.sh"],
                "start",
                label1,
                label2,
                logs_dir,
            )
        )
        all_results.extend(
            compare_scripts(
                fs1,
                fs2,
                ["stop.sh", "shutdown.sh"],
                "stop",
                label1,
                label2,
                logs_dir,
            )
        )
        write_summary(logs_dir, label1, label2, path1, path2, all_results)

        counts: dict[str, int] = {}
        for r in all_results:
            counts[r.status] = counts.get(r.status, 0) + 1

        log.info("=" * 60)
        log.info("Comparison complete")
        log.info(
            "Totals: identical=%d different=%d only_%s=%d only_%s=%d missing=%d",
            counts.get("identical", 0),
            counts.get("different", 0),
            label1,
            counts.get("only_p1", 0),
            label2,
            counts.get("only_p2", 0),
            counts.get("missing", 0),
        )
        log.info("Reports directory: %s", logs_dir)
        log.info("  summary.txt")
        log.info("  config/*.diff.txt")
        log.info("  scripts/*.diff.txt")
        log.info("  %s/lib_comparison.txt", lib_changes)
        log.info("Activity log saved: %s", activity_log)
    except Exception:
        log.exception("Comparison failed with an error")
        raise
    finally:
        if client is not None:
            log.info("Closing SSH connection")
            client.close()


if __name__ == "__main__":
    main()
