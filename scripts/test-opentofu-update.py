"""Check the OpenTofu update PR body produced by .github/workflows/opentofu-update.yml.

Runs the real `run:` block of the "Open update pull request" step with stub git and gh
commands, so no GitHub state is touched. Backticks in the body must stay literal, and
values must never be executed.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'opentofu-update.yml'
STEP = '- name: Open update pull request'
PR_BODY_PATH = '/tmp/pr-body.md'

GIT_STUB = '''#!/bin/sh
echo "git $*" >> "$STUB_LOG"
exit 0
'''
GH_STUB = '''#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "pr list") exit 0 ;;
  "pr create")
    while [ $# -gt 0 ]; do
      if [ "$1" = "--body-file" ]; then cp "$2" "$STUB_BODY"; fi
      shift
    done
    echo "https://example.test/pull/1" ;;
  "pr edit"|"workflow run") exit 0 ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
'''


def run_block():
    """Return the step's `run:` script, dedented as GitHub Actions receives it."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == STEP)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == 'run: |')
    run_indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if not line.strip():
            body.append('')
            continue
        if len(line) - len(line.lstrip()) <= run_indent:
            break
        body.append(line)
    block_indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return '\n'.join(line[block_indent:] for line in body) + '\n'


class PrBodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        for name, text in (('git', GIT_STUB), ('gh', GH_STUB)):
            stub = self.bin / name
            stub.write_text(text)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = self.work / 'calls.log'
        self.capture = self.work / 'captured-body.md'
        # The workflow writes a fixed /tmp path; redirect it into the fixture directory.
        self.script = run_block().replace(PR_BODY_PATH, str(self.work / 'pr-body.md'))

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, commit):
        env = dict(os.environ)
        env.update({
            'PATH': f'{self.bin}{os.pathsep}{env["PATH"]}',
            'STUB_LOG': str(self.log),
            'STUB_BODY': str(self.capture),
            'GITHUB_REPOSITORY': 'owner/gh-tofu',
            'CURRENT_VERSION': '1.13.2',
            'VERSION': '1.14.0',
            'TAG': 'v1.14.0',
            'COMMIT': commit,
        })
        return subprocess.run(['bash', '-c', self.script], cwd=self.work, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_body_has_literal_commit_and_no_stray_output(self):
        commit = '0123456789abcdef0123456789abcdef01234567'
        result = self.run_step(commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('command not found', result.stderr)
        body = self.capture.read_text()
        self.assertIn(f'\n- Pinned release commit: `{commit}`\n', body)
        self.assertIn('Updates the OpenTofu base from **1.13.2** to **1.14.0**.\n', body)
        self.assertIn('- Updates `GH_TOFU_VERSION`, `OPENTOFU_REF`, and `OPENTOFU_COMMIT` atomically\n', body)
        self.assertIn('Upstream release: https://github.com/opentofu/opentofu/releases/tag/v1.14.0\n', body)
        self.assertFalse(any(line[:1].isspace() for line in body.splitlines() if line),
                         'body lines must not carry indentation')
        self.assertIn('pr create', self.log.read_text())

    def test_commit_value_is_never_executed(self):
        marker = self.work / 'EXECUTED'
        result = self.run_step(f'touch {marker}')
        self.assertFalse(marker.exists(), 'a value from the environment was executed as a command')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'- Pinned release commit: `touch {marker}`', self.capture.read_text())


if __name__ == '__main__':
    unittest.main()
