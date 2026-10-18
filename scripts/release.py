#!/usr/bin/env python3
"""Generate and verify the public release manifest.

    python scripts/release.py manifest   # rewrite release-manifest.json from git ls-files
    python scripts/release.py check      # manifest == tracked files, path policy, privacy scan

只输出类别与位置，不输出命中的具体值。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts')]

import privacy_scan

MANIFEST = 'release-manifest.json'
MAX_FILE_BYTES = 2 * 1024 * 1024

# 本机专用、永不发布的路径（与 .gitignore 保持一致，防止 git add -f）。
LOCAL_ONLY = {
    'AGENTS.local.md',
    'scripts/assemble_profiles.py',
    'scripts/cross_mentions.py',
    'scripts/profile_lint.py',
    'scripts/split_speakers.py',
    'scripts/verify_cross_profiles.py',
    'scripts/repair_evidence.py',
}
LOCAL_ONLY_PREFIXES = ('docs/private/', 'experiments/', 'native/wecom_probe')


def is_local_only(name):
    return name in LOCAL_ONLY or any(name.startswith(prefix) for prefix in LOCAL_ONLY_PREFIXES)



def tracked():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    return sorted(name for name in result.stdout.decode().split('\0') if name)


def manifest_path():
    return ROOT / MANIFEST


def load_manifest():
    value = json.loads(manifest_path().read_text())
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError('release-manifest.json must be a JSON list of paths')
    return value


def write_manifest(_args=None):
    names = tracked()
    if not names:
        raise ValueError('No tracked files; git add the release files first')
    if MANIFEST not in names:
        names = sorted(names + [MANIFEST])
    if len(names) != len(set(names)):
        raise ValueError('Duplicate tracked paths')
    for name in names:
        path = ROOT / name
        if is_local_only(name) or not privacy_scan.allowed_path(name):
            raise ValueError('Private or unsafe tracked path: ' + name)
        if name == MANIFEST:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing, symlinked or non-regular release file: ' + name)
    manifest_path().write_text(json.dumps(names, ensure_ascii=False, indent=2) + '\n')
    print('release-manifest.json: %d files' % len(names))
    return 0


def check_paths(names):
    problems = []
    for name in names:
        if is_local_only(name) or not privacy_scan.allowed_path(name):
            problems.append(dict(path=name, category='private_path'))
            continue
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            problems.append(dict(path=name, category='unsafe_path'))
            continue
        if not path.is_file():
            problems.append(dict(path=name, category='missing_file'))
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            problems.append(dict(path=name, category='file_too_large'))
    return problems


def check(_args=None):
    names = tracked()
    manifest = load_manifest()
    problems = []
    if sorted(manifest) != names:
        missing = sorted(set(names) - set(manifest))
        stale = sorted(set(manifest) - set(names))
        problems.append(dict(category='manifest_mismatch',
                             missing=missing[:20], stale=stale[:20],
                             missing_count=len(missing), stale_count=len(stale)))
    problems += check_paths(names)

    deny_file = ROOT / 'keys' / 'privacy-denylist.json'
    deny = privacy_scan.load_deny(deny_file if deny_file.exists() else None)
    findings = privacy_scan.scan_directory(ROOT, deny)
    findings += privacy_scan.scan_git(ROOT, deny)
    if findings:
        problems.append(dict(category='privacy_findings', count=len(findings),
                             findings=findings[:40]))

    if problems:
        print(json.dumps(dict(ok=False, problems=problems), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(dict(ok=True, files=len(names), privacy_findings=0), ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('manifest', help='rewrite the manifest from git ls-files')
    p.set_defaults(func=write_manifest)
    p = commands.add_parser('check', help='verify manifest, paths and privacy scan')
    p.set_defaults(func=check)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
        else:
            print('release check failed: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
