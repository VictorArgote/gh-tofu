"""Exercise scripts/install.sh and the gh-tofu release asset contract without network access."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / 'scripts' / 'install.sh'
ACTION = ROOT / 'action.yml'
RELEASE = ROOT / '.github' / 'workflows' / 'release.yml'
VERSION = '1.2.3'
# Published targets as (GOOS, GOARCH, extension). Keep in sync with release.yml.
TARGETS = [
    ('linux', 'amd64', ''),
    ('linux', 'arm64', ''),
    ('darwin', 'amd64', ''),
    ('darwin', 'arm64', ''),
    ('windows', 'amd64', '.exe'),
]
FAKE_UNAME = '''#!/bin/sh
case "$1" in
  -s) echo "$FAKE_UNAME_S" ;;
  -m) echo "$FAKE_UNAME_M" ;;
  *) exit 2 ;;
esac
'''


def asset_name(goos, goarch, ext):
    return f'gh-tofu-{goos}-{goarch}{ext}'


def snapshot(path):
    """Everything under path (or path itself), so a test can prove nothing changed."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        return {'.': ('link', os.readlink(path))}
    if os.path.isfile(path):
        return {'.': ('file', Path(path).read_bytes(), stat.S_IMODE(os.stat(path).st_mode))}
    state = {'.': ('dir',)}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path)
            if os.path.islink(full):
                state[rel] = ('link', os.readlink(full))
            elif os.path.isdir(full):
                state[rel] = ('dir',)
            else:
                state[rel] = ('file', Path(full).read_bytes(), stat.S_IMODE(os.stat(full).st_mode))
    return state


class Release:
    """A local release: latest-release metadata, asset bytes, and sha256sums.txt."""

    def __init__(self, root):
        self.api_url = (root / 'api').as_uri()
        self.release_url = (root / 'download').as_uri()
        self.dir = root / 'download' / f'v{VERSION}'
        self.dir.mkdir(parents=True)
        latest = root / 'api' / 'releases' / 'latest'
        latest.parent.mkdir(parents=True)
        latest.write_text(json.dumps({'tag_name': f'v{VERSION}'}))
        self.assets = {}
        for goos, goarch, ext in TARGETS:
            name = asset_name(goos, goarch, ext)
            self.assets[name] = f'{name} bytes\n'.encode()
            (self.dir / name).write_bytes(self.assets[name])
        self.write_sums()

    def write_sums(self, exclude=None, duplicate=None, hash_override=None, star=False):
        lines = []
        for name, data in self.assets.items():
            if name == exclude:
                continue
            digest = hashlib.sha256(data).hexdigest()
            if name == hash_override:
                digest = 'Z' * 64
            lines.append(f'{digest}  {"*" if star else ""}{name}\n')
            if name == duplicate:
                lines.append(f'{digest}  {name}\n')
        (self.dir / 'sha256sums.txt').write_text(''.join(lines))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = Release(self.root)
        self.fake_bin = self.root / 'fakebin'
        self.fake_bin.mkdir()
        uname = self.fake_bin / 'uname'
        uname.write_text(FAKE_UNAME)
        uname.chmod(0o755)
        self.install_dir = self.root / 'install'

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, system='Linux', machine='x86_64', version=None, install_dir=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ('VERSION', 'INSTALL_DIR', 'GH_TOFU_REPOSITORY')}
        env.update({
            'PATH': f'{self.fake_bin}{os.pathsep}{os.environ.get("PATH", "")}',
            'FAKE_UNAME_S': system,
            'FAKE_UNAME_M': machine,
            'GH_TOFU_API_URL': self.release.api_url,
            'GH_TOFU_RELEASE_URL': self.release.release_url,
            'INSTALL_DIR': str(install_dir or self.install_dir),
        })
        if version is not None:
            env['VERSION'] = version
        return subprocess.run(['sh', str(INSTALLER)], env=env, capture_output=True,
                              text=True, timeout=120)

    def dest(self):
        return self.install_dir / 'gh-tofu'

    def test_installs_latest_verified_binary(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest().read_bytes(), self.release.assets['gh-tofu-linux-amd64'])
        self.assertEqual(stat.S_IMODE(self.dest().stat().st_mode), 0o755)
        self.assertEqual(sorted(os.listdir(self.install_dir)), ['gh-tofu'])

    def test_selects_asset_for_each_supported_platform(self):
        cases = [
            ('Linux', 'x86_64', 'gh-tofu-linux-amd64'),
            ('Linux', 'aarch64', 'gh-tofu-linux-arm64'),
            ('Darwin', 'x86_64', 'gh-tofu-darwin-amd64'),
            ('Darwin', 'arm64', 'gh-tofu-darwin-arm64'),
        ]
        for system, machine, asset in cases:
            with self.subTest(system=system, machine=machine):
                install_dir = self.root / f'bin-{system}-{machine}'
                result = self.run_installer(system, machine, install_dir=install_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((install_dir / 'gh-tofu').read_bytes(),
                                 self.release.assets[asset])

    def test_unsupported_platforms_fail_before_writing(self):
        cases = [
            ('Linux', 'i686', 'unsupported architecture'),
            ('FreeBSD', 'x86_64', 'unsupported operating system'),
            ('Windows_NT', 'x86_64', 'unsupported operating system'),
            ('Linux', 'riscv64', 'unsupported architecture'),
        ]
        for system, machine, message in cases:
            with self.subTest(system=system, machine=machine):
                result = self.run_installer(system, machine)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.install_dir.exists())

    def test_pinned_version_accepts_v_prefix(self):
        result = self.run_installer(version=f'v{VERSION}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dest().is_file())

    def test_rejects_non_stable_versions_before_writing(self):
        for bad in ('1.2', '1.2.3-rc.1', '1.2.x', '1..2'):
            with self.subTest(version=bad):
                result = self.run_installer(version=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('stable semantic version', result.stderr)
                self.assertFalse(self.install_dir.exists())

    def test_checksum_mismatch_keeps_existing_binary(self):
        self.install_dir.mkdir()
        self.dest().write_bytes(b'previous')
        (self.release.dir / 'gh-tofu-linux-amd64').write_bytes(b'tampered')
        before = snapshot(self.install_dir)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch for gh-tofu-linux-amd64', result.stderr)
        self.assertEqual(snapshot(self.install_dir), before)

    def test_missing_checksum_entry_writes_nothing(self):
        self.release.write_sums(exclude='gh-tofu-linux-amd64')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must list gh-tofu-linux-amd64 exactly once (found 0)', result.stderr)
        self.assertFalse(self.install_dir.exists())

    def test_duplicate_checksum_entry_fails_closed(self):
        self.install_dir.mkdir()
        self.dest().write_bytes(b'previous')
        self.release.write_sums(duplicate='gh-tofu-linux-amd64')
        before = snapshot(self.install_dir)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exactly once (found 2)', result.stderr)
        self.assertEqual(snapshot(self.install_dir), before)

    def test_malformed_checksum_fails_closed(self):
        self.release.write_sums(hash_override='gh-tofu-linux-amd64')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid checksum for gh-tofu-linux-amd64', result.stderr)
        self.assertFalse(self.install_dir.exists())

    def test_binary_mode_checksum_marker_is_accepted(self):
        self.release.write_sums(star=True)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dest().is_file())

    def test_failed_download_writes_nothing(self):
        (self.release.dir / 'gh-tofu-linux-amd64').unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.install_dir.exists())

    def test_existing_regular_file_is_replaced_after_verification(self):
        self.install_dir.mkdir()
        self.dest().write_bytes(b'previous')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest().read_bytes(), self.release.assets['gh-tofu-linux-amd64'])
        self.assertEqual(sorted(os.listdir(self.install_dir)), ['gh-tofu'])

    def test_directory_destination_is_not_touched(self):
        self.dest().mkdir(parents=True)
        (self.dest() / 'keep').write_text('keep')
        before = snapshot(self.install_dir)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a regular file', result.stderr)
        self.assertEqual(snapshot(self.install_dir), before)

    def test_symlink_destination_is_not_followed(self):
        target = self.root / 'outside'
        target.write_bytes(b'keep')
        self.install_dir.mkdir()
        os.symlink(target, self.dest())
        before = snapshot(self.install_dir)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a regular file', result.stderr)
        self.assertEqual(target.read_bytes(), b'keep')
        self.assertEqual(snapshot(self.install_dir), before)

    def test_dangling_symlink_destination_is_not_followed(self):
        missing = self.root / 'missing'
        self.install_dir.mkdir()
        os.symlink(missing, self.dest())
        before = snapshot(self.install_dir)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.lexists(missing))
        self.assertEqual(snapshot(self.install_dir), before)

    def test_install_dir_that_is_a_file_is_not_touched(self):
        self.install_dir.write_bytes(b'file')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot create', result.stderr)
        self.assertEqual(self.install_dir.read_bytes(), b'file')


class ReleaseContractTests(unittest.TestCase):
    def test_release_matrix_matches_gh_tofu_targets(self):
        matrix = re.findall(r'- os: (\w+)\n\s+arch: (\w+)', RELEASE.read_text())
        self.assertEqual(sorted(matrix), sorted((goos, goarch) for goos, goarch, _ in TARGETS))

    def test_release_workflow_names_every_asset(self):
        text = RELEASE.read_text()
        self.assertIn('artifacts/gh-tofu-${TARGET_OS}-${TARGET_ARCH}${EXT}', text)
        for goos, goarch, ext in TARGETS:
            self.assertIn(asset_name(goos, goarch, ext), text)

    def test_installer_names_the_same_assets(self):
        self.assertIn('asset="gh-tofu-${os}-${arch}"', INSTALLER.read_text())

    def test_each_platform_matches_exactly_one_gh_binary_asset(self):
        # gh picks the first asset whose name ends with GOOS-GOARCH (plus .exe on Windows).
        names = [asset_name(g, a, e) for g, a, e in TARGETS] + ['sha256sums.txt']
        for goos, goarch, ext in TARGETS:
            with self.subTest(platform=f'{goos}-{goarch}'):
                matches = [n for n in names if n.endswith(f'{goos}-{goarch}{ext}')]
                self.assertEqual(matches, [asset_name(goos, goarch, ext)])

    def test_action_delegates_to_the_installer(self):
        text = ACTION.read_text()
        self.assertIn('using: composite', text)
        self.assertIn('sh "$GITHUB_ACTION_PATH/scripts/install.sh"', text)

    def test_removed_channels_stay_removed(self):
        forbidden = ['npm publish', 'update-apt', 'nfpm', 'package-native', 'verify-native',
                     'fetch-npm-assets', 'packaging/']
        paths = [ROOT / 'action.yml', ROOT / 'Makefile', ROOT / 'gh-tofu.env']
        paths += [p for p in (ROOT / '.github').rglob('*.yml')]
        paths += [p for p in (ROOT / 'scripts').iterdir() if p != Path(__file__).resolve()]
        for path in paths:
            if not path.is_file():
                continue
            text = path.read_text(errors='ignore')
            for token in forbidden:
                with self.subTest(path=str(path.relative_to(ROOT)), token=token):
                    self.assertNotIn(token, text)
        self.assertFalse((ROOT / 'packaging').exists())


if __name__ == '__main__':
    unittest.main()
