#!/usr/bin/env python3
"""Create a deterministic application release; never package credentials/data."""
import argparse
import gzip
import hashlib
import io
import json
import pathlib
import re
import tarfile


def package(project, output, commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected a full Git commit SHA')
    project, output = pathlib.Path(project), pathlib.Path(output)
    files = {}
    for source in sorted((project / 'dist').rglob('*')):
        if source.is_symlink():
            raise ValueError('Build output contains a symlink')
        if source.is_file():
            relative = source.relative_to(project / 'dist').as_posix()
            if relative.startswith('.') or '/.' in relative:
                raise ValueError('Build output contains an unexpected hidden file')
            files[relative] = source.read_bytes()
    if 'index.html' not in files:
        raise ValueError('dist/index.html is missing')
    version = json.loads((project / 'package.json').read_text(encoding='utf-8'))['version']
    live = (project / 'backend/server.py').is_file()
    kind = 'monitoring-server' if live else 'frontend-prototype'
    metadata = {'schema': 1, 'kind': kind, 'commit': commit, 'version': version}
    if live:
        metadata['dataSchema'] = 2
    files['release.json'] = (json.dumps(metadata, sort_keys=True) + '\n').encode()
    files['.deploy/manage.py'] = (project / 'scripts/deploy/manage.py').read_bytes()
    files['.deploy/console.py'] = (project / 'scripts/deploy/console.py').read_bytes()
    if live:
        files['.deploy/runtime.py'] = (project / 'scripts/deploy/runtime.py').read_bytes()
        for source in sorted((project / 'backend').rglob('*.py')):
            if source.is_symlink():
                raise ValueError('Backend source contains a symlink')
            if '__pycache__' not in source.parts:
                files['.backend/' + source.relative_to(project / 'backend').as_posix()] = source.read_bytes()
        for name in ('agent.py', 'install.sh'):
            source = project / 'agent' / name
            if source.is_symlink() or not source.is_file():
                raise ValueError('Agent source is missing or unsafe')
            files['.agent/' + name] = source.read_bytes()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'xiaowork-watch-web.tar.gz'
    with archive.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as tar:
                for name, content in sorted(files.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(content), 0o644, 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ''
                    tar.addfile(entry, io.BytesIO(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
    print('Packaged ' + kind + ' ' + version + ' at ' + commit)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', default='.')
    parser.add_argument('--output', default='release-assets')
    parser.add_argument('--commit', required=True)
    arguments = parser.parse_args()
    package(arguments.project, arguments.output, arguments.commit)
