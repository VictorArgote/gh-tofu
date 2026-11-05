"""Check the release pin policy in .github/workflows/release.yml (verify job, validate step).

Runs the real `run:` block with stub curl and git commands, so no network or GitHub state is
touched. Covers: default rejection of an older pin, explicit opt-in acceptance, rejection of a
pin newer than latest stable (even when opted in), and the tag/commit safeguards.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'release.yml'
STEP = '- name: Validate pinned version and release type'
SOURCE_SHA = 'a' * 40
COMMIT = '0123456789abcdef0123456789abcdef01234567'

CURL_STUB = '''#!/bin/sh
cat "$STUB_RELEASES"
'''
GIT_STUB = '''#!/bin/sh
case "$1" in
  rev-parse) echo "$STUB_SOURCE_SHA" ;;
  ls-remote)
    case "$*" in
      *--exit-code*) [ "$STUB_TAG_EXISTS" = 1 ] && exit 0; exit 2 ;;
      *) printf '%s\\trefs/tags/x\\n' "$STUB_UPSTREAM_COMMIT" ;;
    esac ;;
  *) echo "unexpected git $*" >&2; exit 2 ;;
esac
'''

# Latest stable is 1.13.8. The draft 1.14.0 and prerelease 1.14.0-rc.1 must be ignored.
RELEASES = [
    {'tag_name': 'v1.14.0', 'draft': True, 'prerelease': False},
    {'tag_name': 'v1.14.0-rc.1', 'draft': False, 'prerelease': True},
    {'tag_name': 'v1.13.8', 'draft': False, 'prerelease': False},
    {'tag_name': 'v1.13.10-backport', 'draft': False, 'prerelease': False},
    {'tag_name': 'v1.13.2', 'draft': False, 'prerelease': False},
    {'tag_name': 'v1.13.7', 'draft': False, 'prerelease': False},
]
LATEST = '1.13.8'


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


class ReleasePinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        for name, text in (('curl', CURL_STUB), ('git', GIT_STUB)):
            stub = self.bin / name
            stub.write_text(text)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.releases = self.work / 'releases.json'
        self.releases.write_text(json.dumps(RELEASES))
        self.script = self.work / 'step.sh'
        self.script.write_text(run_block())

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, version, ref=None, commit=COMMIT, upstream=COMMIT, opt_in=None,
                 tag_exists=False, release_type='stable'):
        (self.work / 'gh-tofu.env').write_text(
            f'GH_TOFU_VERSION={version}\n'
            f'OPENTOFU_REF={ref or "v" + version}\n'
            f'OPENTOFU_COMMIT={commit}\n')
        out = self.work / 'github_output'
        summary = self.work / 'step_summary'
        out.write_text('')
        summary.write_text('')
        env = dict(os.environ)
        env.update({
            'PATH': f'{self.bin}{os.pathsep}{env["PATH"]}',
            'STUB_RELEASES': str(self.releases),
            'STUB_UPSTREAM_COMMIT': upstream,
            'STUB_SOURCE_SHA': SOURCE_SHA,
            'STUB_TAG_EXISTS': '1' if tag_exists else '0',
            'GITHUB_OUTPUT': str(out),
            'GITHUB_STEP_SUMMARY': str(summary),
            'RELEASE_TYPE': release_type,
            'PRERELEASE_SUFFIX': 'rc.1',
            'ALLOW_OLDER_UPSTREAM_PIN': 'true' if opt_in else 'false',
        })
        result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', str(self.script)],
                                cwd=self.work, env=env, capture_output=True, text=True, timeout=60)
        return result, out.read_text(), summary.read_text()

    def test_latest_stable_pin_passes_without_opt_in(self):
        result, out, summary = self.run_step(LATEST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('version=1.13.8\n', out)
        self.assertNotIn('exception', result.stdout)
        self.assertEqual(summary, '')

    def test_older_pin_rejected_by_default(self):
        result, out, _ = self.run_step('1.13.2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pinned=1.13.2, latest=1.13.8', result.stdout)
        self.assertIn('allow_older_upstream_pin', result.stdout)
        self.assertEqual(out, '')

    def test_older_pin_rejected_when_opt_in_is_not_true(self):
        result, _, _ = self.run_step('1.13.2', opt_in=False)
        self.assertNotEqual(result.returncode, 0)

    def test_older_pin_accepted_with_opt_in_and_surfaced(self):
        result, out, summary = self.run_step('1.13.2', opt_in=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('::warning::Older upstream pin exception', result.stdout)
        self.assertIn('Older upstream pin exception', summary)
        self.assertIn('`1.13.2`', summary)
        self.assertIn('version=1.13.2\n', out)
        self.assertIn('tag=v1.13.2\n', out)
        self.assertIn('prerelease=false\n', out)
        self.assertIn(f'source_sha={SOURCE_SHA}\n', out)

    def test_older_prerelease_accepted_with_opt_in(self):
        result, out, _ = self.run_step('1.13.2', opt_in=True, release_type='prerelease')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('tag=v1.13.2-rc.1\n', out)
        self.assertIn('prerelease=true\n', out)

    def test_pin_newer_than_latest_rejected_by_default(self):
        result, out, _ = self.run_step('1.14.0', opt_in=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('newer than OpenTofu', result.stdout)
        self.assertEqual(out, '')

    def test_pin_newer_than_latest_rejected_even_with_opt_in(self):
        result, out, _ = self.run_step('1.14.0', opt_in=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('newer than OpenTofu', result.stdout)
        self.assertNotIn('exception', result.stdout)
        self.assertEqual(out, '')

    def test_ref_mismatch_rejected_with_opt_in(self):
        result, _, _ = self.run_step('1.13.2', ref='v1.13.3', opt_in=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must match OPENTOFU_REF', result.stdout)

    def test_commit_mismatch_rejected_with_opt_in(self):
        result, out, _ = self.run_step('1.13.2', upstream='f' * 40, opt_in=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match v1.13.2', result.stdout)
        self.assertEqual(out, '')

    def test_existing_tag_rejected_with_opt_in(self):
        result, _, _ = self.run_step('1.13.2', opt_in=True, tag_exists=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Tag v1.13.2 already exists', result.stdout)


if __name__ == '__main__':
    unittest.main()
