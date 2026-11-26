#!/usr/bin/env python3
"""
FossID SARIF Exporter

Converts FossID license-scan JSON into SARIF 2.1.0 for GitHub Code Scanning.
Used by the FossID diffscan GitHub Actions workflow, shared across rdkcentral repos.

Usage: python3 format_fossid.py <fossid_raw.json> --sarif <output.sarif>

"""
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlsplit

MATCH_TYPE_LABELS = {
    "file": "Full File Match",
    "partial": "Partial Match",
    "component": "Component Match",
}

# match_types with no local highlight - anchored to line 1 instead.
WHOLE_SCOPE_MATCH_TYPES = {"file", "component"}


def get_pr_changed_lines(base_ref: str, file_path: str) -> Optional[Set[int]]:
    """
    Lines added/modified in this PR for one file, via `git diff` against base_ref.
    Returns None (= don't filter, include everything) if base_ref/file_path is
    missing or git fails - we'd rather show a stale finding than hide a real one.
    """
    if not base_ref or not file_path:
        return None

    try:
        # -U0: no context lines, so hunk headers map exactly to changed lines.
        # Equivalent shell command: git diff -U0 <base_ref>...HEAD -- <file_path>
        cmd = ["git", "diff", "-U0", f"{base_ref}...HEAD", "--", file_path]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)

        changed_lines: Set[int] = set()
        for line in res.stdout.splitlines():
            # Hunk header: @@ -old_start,old_count +new_start,new_count @@
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if not match:
                    # Unparseable hunk header - can't trust changed_lines for
                    # this file, so fail open rather than risk silently
                    # excluding a real finding.
                    return None
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                if count > 0:  # count == 0 -> pure deletion, nothing added
                    changed_lines.update(range(start, start + count))
        return changed_lines
    except Exception:
        # New/untracked file, base_ref not fetched, git error, etc. -> fail open.
        return None


def is_github_url(url: str) -> bool:
    """True only for github.com URLs - only those support #L<start>-L<end> anchors."""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return host == "github.com" or host.endswith(".github.com")
    except ValueError:
        return False


def get_local_link(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    GitHub blob URL to the matched lines in this repo.
    Prefers GITHUB_HEAD_SHA (real PR commit) over GITHUB_SHA (for pull_request
    events this is an ephemeral merge commit, not the PR author's commit).
    GITHUB_SHA is always set by Actions, so this is added as a fallback.
    """
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_HEAD_SHA") or os.environ.get("GITHUB_SHA")

    if not (repo and ref and file_path):
        return ""

    # Encode path so spaces/'#'/etc. don't break the URL or the line anchor.
    encoded_path = quote(file_path, safe="/")
    base = f"{server_url}/{repo}/blob/{ref}/{encoded_path}"
    if start_line is not None and end_line is not None:
        return f"{base}#L{start_line}-L{end_line}"
    if start_line is not None:
        return f"{base}#L{start_line}"
    return f"{base}#L1"


def extract_license_str(container: Any) -> str:
    """Unique license IDs from a `licenses: [{"id": ...}]` list, comma-joined."""
    if not isinstance(container, dict):
        return ""
    names = [
        lic["id"].strip()
        for lic in (container.get("licenses") or [])
        if isinstance(lic, dict) and lic.get("id")
    ]
    return ", ".join(dict.fromkeys(names))


def get_rule_id(match_type: str, author: str, artifact: str, version: str = "") -> str:
    """Stable SARIF rule ID, e.g. "fossid/partial/kernel-2.6.10"."""
    author = (author or "").strip().lower()
    artifact = (artifact or "").strip().lower()
    version = (version or "").strip().lower()

    if author and artifact and artifact.startswith(author):
        clean_name = artifact  # avoid "kernel-kernel"
    elif author and artifact:
        clean_name = f"{author}-{artifact}"
    else:
        clean_name = artifact or author or "unspecified"

    if version:
        clean_name = f"{clean_name}-{version}"

    clean_name = re.sub(r"[^a-z0-9._-]", "-", clean_name).strip("-")
    match_clean = re.sub(r"[^a-z0-9._-]", "-", match_type.lower()) if match_type else "partial"
    return f"fossid/{match_clean}/{clean_name}"


def write_sarif(path: str, rules: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    """Write the SARIF 2.1.0 document. Called with empty rules/results for a clean scan too."""
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "FossID",
                        "organization": "FossID",
                        "informationUri": "https://fossid.com/",
                        "rules": list(rules.values()),
                    }
                },
                "originalUriBaseIds": {
                    "%SRCROOT%": {
                        "description": {"text": "The root of the source repository."}
                    }
                },
                "automationDetails": {
                    "id": "fossid-license-compliance/"
                },
                "results": results,
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as sarif_file:
        json.dump(sarif, sarif_file, indent=2)
        sarif_file.write("\n")


def _line_ranges(blocks: Any) -> List[Tuple[int, int]]:
    """FossID's 0-based (offset, length) blocks -> 1-based (start, end) line tuples."""
    ranges: List[Tuple[int, int]] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        lines = block.get("lines") or {}
        offset = lines.get("offset")
        length = lines.get("length")
        if offset is None or length is None:
            continue
        start = offset + 1
        ranges.append((start, start + (length - 1 if length > 0 else 0)))
    return ranges


def _cached_changed_lines(
    base_ref: str, file_path: str, diff_cache: Dict[str, Optional[Set[int]]]
) -> Optional[Set[int]]:
    """get_pr_changed_lines, memoized per file path across one script run."""
    if file_path not in diff_cache:
        diff_cache[file_path] = get_pr_changed_lines(base_ref, file_path)
    return diff_cache[file_path]


def _build_annotation_message(
    local_file: str,
    local_start: Optional[int],
    local_end: Optional[int],
    remote_file_path: str,
    remote_lines_display: str,
    remote_lic: str,
    purl: str,
    match_type_display: str,
    download_url: str,
    local_link: str,
    remote_link: str,
    multi_chunk_note: str,
) -> str:
    """Body text of the PR annotation shown in the Code Scanning UI."""
    local_line_str = (
        f" (Lines {local_start}-{local_end})"
        if local_start is not None
        else " (entire file)"
    )
    remote_lic_part = f" | License: {remote_lic}" if remote_lic else ""

    lines = [
        f"Local:       {local_file}{local_line_str}",
        f"Remote:      {remote_file_path}{remote_lines_display}{remote_lic_part}",
        f"Component:   {purl}",
        f"Match Type:  {match_type_display}",
    ]
    if download_url:
        lines.append(f"Download:    {download_url}")
    if local_link and remote_link:
        lines.append(f"Local Link:  {local_link}\nRemote Link: {remote_link}")
    elif local_link:
        lines.append(f"Local Link:  {local_link}")
    elif remote_link:
        lines.append(f"Remote Link: {remote_link}")

    if multi_chunk_note:
        lines.append(multi_chunk_note)

    return "\n".join(lines)


def _build_sarif_rule(rule_id: str, match_type_display: str, artifact: str) -> Dict[str, Any]:
    """SARIF rule entry for the tool driver's "rules" list."""
    return {
        "id": rule_id,
        "name": "FossIDLicenseMatch",
        "shortDescription": {
            "text": f"Third-party {match_type_display}: {artifact}"
        },
        "fullDescription": {
            "text": (
                f"FossID detected a {match_type_display.lower()} against the "
                f"third-party component '{artifact}'."
            )
        },
        "help": {
            "text": (
                f"Third-party {match_type_display} for component "
                f"'{artifact}'. Verify this "
                "component and its license terms comply with project "
                "open-source policy. Each alert lists the matched "
                "version, source lines, and upstream link."
            ),
            "markdown": (
                f"### FossID Third-Party {match_type_display}\n\n"
                f"- **Component:** `{artifact}`\n\n"
                "**Compliance Policy:**\n"
                "Verify that this external component and its license "
                "terms comply with project open-source guidelines. "
                "Consult repository maintainers or the compliance team "
                "if an approved exception applies.\n\n"
                "_Matched version, license, source lines, and upstream "
                "link are shown in each individual alert._"
            ),
        },
        "defaultConfiguration": {"level": "error"},
        "properties": {"tags": ["license-compliance", "fossid"]},
    }


def _build_sarif_result(
    rule_id: str,
    card_msg: str,
    local_file: str,
    local_start: Optional[int],
    local_end: Optional[int],
    purl: str,
    remote_file_path: str,
    download_url: str,
    match_type_display: str,
    remote_lic: str,
) -> Dict[str, Any]:
    """SARIF result entry - one per matched local highlight block."""
    # local_start is None for whole-file/whole-component matches.
    physical_location: Dict[str, Any] = {
        "artifactLocation": {
            "uri": local_file,
            "uriBaseId": "%SRCROOT%",
        },
        "region": {
            "startLine": local_start if local_start is not None else 1,
            "endLine": local_end if local_end is not None else 1,
        },
    }
    return {
        "ruleId": rule_id,
        "level": "error",
        "message": {"text": card_msg},
        "locations": [{"physicalLocation": physical_location}],
        "properties": {
            "component": purl,
            "remoteFile": remote_file_path,
            "downloadUrl": download_url,
            "matchType": match_type_display,
            "matchedLicense": remote_lic,
        },
    }


def _process_license_issue(
    issue: Dict[str, Any],
    base_ref: str,
    diff_cache: Dict[str, Optional[Set[int]]],
) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Turn one entry from FossID's "license_issues" array into zero or more
    (rule_id, rule_dict, result_dict) triples - zero if every matched block
    is pre-existing (outside this PR's diff) or local_file is missing, more
    than one if the issue has multiple local highlight blocks.
    """
    # Pull out the raw fields we need, defensively - FossID's JSON can have
    # nulls/missing keys where the schema implies an object.
    local_file_info = issue.get("local_file") or {}
    local_file: str = local_file_info.get("path") or ""
    local_blocks = (local_file_info.get("highlight") or {}).get("blocks") or []

    remote_file_info = issue.get("remote_file") or {}
    remote_file_path: str = remote_file_info.get("path") or ""
    remote_blocks = (remote_file_info.get("highlight") or {}).get("blocks") or []
    remote_url: str = remote_file_info.get("url") or ""

    comp = issue.get("component") or {}
    author: str = comp.get("author") or ""
    artifact: str = comp.get("artifact") or ""
    ver: str = comp.get("version") or ""
    download_url: str = comp.get("url") or ""
    fallback_purl = f"{artifact}@{ver}" if (artifact and ver) else artifact
    purl: str = comp.get("purl") or fallback_purl
    raw_match_type = issue.get("match_type")
    if not raw_match_type:
        # Required per FossID's schema; treat a missing one as malformed
        # rather than guessing, so the outer loop logs and skips this issue.
        raise ValueError("issue is missing required field 'match_type'")
    match_type_display = MATCH_TYPE_LABELS.get(
        raw_match_type.lower(), f"{raw_match_type.capitalize()} Match"
    )

    # License of the specific matched snippet - not the component's overall
    # license list, which may not apply to this exact match.
    remote_lic = extract_license_str(remote_file_info)

    valid_local_blocks: List[Tuple[Optional[int], Optional[int]]] = _line_ranges(local_blocks)
    valid_remote_blocks: List[Tuple[int, int]] = _line_ranges(remote_blocks)

    rem_range_strs = [f"{s}-{e}" for s, e in valid_remote_blocks]
    remote_lines_display = (
        f" (Lines {', '.join(rem_range_strs)})" if rem_range_strs else ""
    )

    # file/component matches: no local highlight, treat as one finding
    # covering the whole file (anchored to line 1 in _build_sarif_result).
    if not valid_local_blocks and raw_match_type.lower() in WHOLE_SCOPE_MATCH_TYPES:
        valid_local_blocks = [(None, None)]

    # Remote link: only add a line anchor if it's an actual GitHub URL.
    primary_r_start, primary_r_end = (
        valid_remote_blocks[0] if valid_remote_blocks else (None, None)
    )
    if remote_url and is_github_url(remote_url) and primary_r_start is not None:
        base_url = remote_url.split("#")[0]
        remote_link = f"{base_url}#L{primary_r_start}-L{primary_r_end}"
    else:
        remote_link = remote_url

    # A single URL can't anchor more than one chunk - note it if there's more than one.
    if len(valid_remote_blocks) > 1:
        first_chunk_str = f" (Lines {primary_r_start}-{primary_r_end})" if primary_r_start else ""
        multi_chunk_note = (
            f"Note:        Remote match spans {len(valid_remote_blocks)} separate chunks. "
            f"The Remote Link highlights the starting chunk{first_chunk_str}; "
            "please review all listed remote ranges."
        )
    else:
        multi_chunk_note = ""

    changed_lines_in_pr = _cached_changed_lines(base_ref, local_file, diff_cache)

    triples: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []

    # Build one SARIF result per local highlight block that survives the
    # PR-diff filter below.
    for local_start, local_end in valid_local_blocks:
        # Skip blocks that don't overlap any line this PR actually touched -
        # pre-existing findings shouldn't flag every PR.
        if changed_lines_in_pr is not None and local_start is not None and local_end is not None:
            block_lines = range(local_start, local_end + 1)
            if changed_lines_in_pr.isdisjoint(block_lines):
                continue

        local_link = get_local_link(local_file, local_start, local_end)

        card_msg = _build_annotation_message(
            local_file, local_start, local_end,
            remote_file_path, remote_lines_display, remote_lic,
            purl, match_type_display, download_url,
            local_link, remote_link, multi_chunk_note,
        )

        # local_file is required to attach a SARIF result to a file.
        if local_file:
            rule_id = get_rule_id(raw_match_type, author, artifact, ver)
            rule_dict = _build_sarif_rule(rule_id, match_type_display, artifact)
            result_dict = _build_sarif_result(
                rule_id, card_msg, local_file, local_start, local_end,
                purl, remote_file_path, download_url, match_type_display, remote_lic,
            )
            triples.append((rule_id, rule_dict, result_dict))

    return triples


def parse_and_generate_sarif(raw_text: str, sarif_path: str) -> None:
    """Parse FossID JSON, write SARIF to sarif_path. Always exits the process."""
    # Empty input -> 0 findings, not an error.
    if not raw_text or not raw_text.strip():
        write_sarif(sarif_path, {}, [])
        sys.exit(0)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # Not valid JSON at all - could be masking a real upstream failure,
        # so warn loudly, but still don't fail the build over it.
        sys.stderr.write(
            f"::warning::FossID formatter could not parse scan output as JSON "
            f"({exc}); emitting empty SARIF. This may hide real findings - "
            f"check the raw scanner output.\n"
        )
        write_sarif(sarif_path, {}, [])
        sys.exit(0)

    if not isinstance(data, dict):
        # Valid JSON but not an object (e.g. "null", "[]") - .get() below
        # would crash on it, so treat it like unparseable input.
        sys.stderr.write(
            f"::warning::FossID formatter expected a JSON object but got "
            f"{type(data).__name__}; emitting empty SARIF. This may hide "
            f"real findings - check the raw scanner output.\n"
        )
        write_sarif(sarif_path, {}, [])
        sys.exit(0)

    # Per FossID's docs, "license_issues" is always an array.
    issues = data.get("license_issues", [])
    if not isinstance(issues, list):
        sys.stderr.write(
            f"::warning::FossID formatter expected 'license_issues' to be a "
            f"list but got {type(issues).__name__}; emitting empty SARIF.\n"
        )
        issues = []
    if not issues:
        # 0 issues - clean scan.
        write_sarif(sarif_path, {}, [])
        sys.exit(0)

    sarif_rules: Dict[str, Any] = {}
    sarif_results: List[Dict[str, Any]] = []

    # Set by the workflow to the PR's base branch; enables PR-diff suppression below.
    base_ref = os.environ.get("BASE_REF", "")

    # Cache git-diff results per file, since one file can have multiple
    # findings and we don't want to re-run `git diff` for each of them.
    diff_cache: Dict[str, Optional[Set[int]]] = {}

    # Process every issue, accumulating SARIF rules/results as we go. Rules
    # are deduped by ID (setdefault); results are one per matched block.
    for issue_index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue

        # One bad record shouldn't take down the whole scan's results - log
        # it and move on to the next issue instead of crashing here.
        try:
            for rule_id, rule_dict, result_dict in _process_license_issue(
                issue, base_ref, diff_cache
            ):
                sarif_rules.setdefault(rule_id, rule_dict)
                sarif_results.append(result_dict)
        except Exception as exc:
            identifying_path = issue.get("local_file", {}).get("path") if isinstance(issue.get("local_file"), dict) else None
            sys.stderr.write(
                f"::warning::Skipping malformed FossID issue at index {issue_index} "
                f"(local_file='{identifying_path or 'unknown'}'): {exc}\n"
            )
            continue

    write_sarif(sarif_path, sarif_rules, sarif_results)

    sys.exit(0)


def main() -> None:
    """CLI entry: format_fossid.py <input.json> --sarif <output.sarif>"""
    input_path: Optional[str] = None
    sarif_path: Optional[str] = None
    args = iter(sys.argv[1:])
    for arg in args:
        if arg == "--sarif":
            try:
                sarif_path = next(args)
            except StopIteration:
                sys.stderr.write("Error: --sarif requires an output path\n")
                sys.exit(2)
        elif input_path is None:
            input_path = arg
        else:
            sys.stderr.write(f"Error: Unexpected argument: {arg}\n")
            sys.exit(2)

    if not input_path or not sarif_path:
        sys.stderr.write("Usage: format_fossid.py <input.json> --sarif <output.sarif>\n")
        sys.exit(2)

    # Missing file -> treat as empty input; parse_and_generate_sarif already
    # handles empty input as "0 findings".
    raw_input = ""
    if os.path.exists(input_path):
        with open(input_path, "r", encoding="utf-8") as f:
            raw_input = f.read()

    parse_and_generate_sarif(raw_input, sarif_path)


if __name__ == "__main__":
    main()
