import hashlib
import importlib.util
import json
import pathlib
import tarfile
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / 'package_release.py'
if not SCRIPT.exists():
    SCRIPT = HERE.parent / 'scripts/deploy/package_release.py'
SPEC = importlib.util.spec_from_file_location('package_release', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / 'dist/assets').mkdir(parents=True)
        (self.root / 'scripts/deploy').mkdir(parents=True)
        (self.root / 'dist/index.html').write_text('<html>prototype</html>')
        (self.root / 'dist/assets/index-a1.js').write_text('console.log(1)')
        (self.root / 'scripts/deploy/manage.py').write_text('# verified manager\n')
        (self.root / 'package.json').write_text(json.dumps({'version': '0.2.1'}))
        (self.root / '.env').write_text('SECRET=never-package-this')
        self.sha = 'a' * 40

    def tearDown(self):
        self.temporary.cleanup()

    def test_repeatable_package_and_metadata(self):
        MODULE.package(self.root, self.root / 'one', self.sha)
        MODULE.package(self.root, self.root / 'two', self.sha)
        first = self.root / 'one/xiaowork-watch-web.tar.gz'
        self.assertEqual(first.read_bytes(), (self.root / 'two/xiaowork-watch-web.tar.gz').read_bytes())
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        self.assertTrue(first.with_name(first.name + '.sha256').read_text().startswith(digest + '  '))
        with tarfile.open(first) as archive:
            self.assertEqual(set(archive.getnames()), {'index.html', 'assets/index-a1.js', 'release.json', '.deploy/manage.py'})
            metadata = json.load(archive.extractfile('release.json'))
            self.assertEqual(metadata['commit'], self.sha)
            self.assertEqual(metadata['kind'], 'frontend-prototype')
            self.assertEqual(metadata['version'], '0.2.1')
            self.assertTrue(all(member.isfile() and member.mode == 0o644 for member in archive))

    def test_hidden_build_file_rejected(self):
        (self.root / 'dist/.env').write_text('private')
        with self.assertRaises(ValueError):
            MODULE.package(self.root, self.root / 'out', self.sha)

    def test_invalid_commit_and_missing_index_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.package(self.root, self.root / 'out', 'main')
        (self.root / 'dist/index.html').unlink()
        with self.assertRaises(ValueError):
            MODULE.package(self.root, self.root / 'out', self.sha)


if __name__ == '__main__':
    unittest.main()
