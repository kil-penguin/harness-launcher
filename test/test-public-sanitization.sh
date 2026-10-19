#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1])
files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z']).decode().split('\0')
aliases = ['k' + 'h', 'g' + 'p', 'g' + 'd']
private_names = ['쥬' + '비스', '넥' + '슨', '한화' + '생명']
public_release_pattern = re.compile(
    r'(?<![\w.-])' + re.escape('18.11.3-kh.w07.1') + r'(?![\w.-])')
findings = []
for relative in filter(None, files):
    path = root / relative
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        continue
    public_alias_spans = {(match.start() + 8, match.start() + 10)
                          for match in public_release_pattern.finditer(text)}
    for alias in aliases:
        if any(match.span() not in public_alias_spans for match in re.finditer(
                rf'(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])',
                text, re.IGNORECASE)):
            findings.append(f'{relative}: private profile alias')
    for name in private_names:
        if name.casefold() in text.casefold():
            findings.append(f'{relative}: private company name')
if findings:
    print('\n'.join(sorted(set(findings))))
    raise SystemExit(1)
print('PASS: tracked public files contain no private profile aliases or company names')
PY
python3 -I -S -B "$ROOT/test/test_public_sanitization.py"
