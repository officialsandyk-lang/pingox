"""Collect packaged dependency license/notice files from the locked native graph."""
from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def notice_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and
                  p.name.lower().startswith(('license', 'licence', 'copying', 'copyright', 'notice')))


def write_notices(release: Path) -> Path:
    host = next(line.split(':', 1)[1].strip() for line in
                subprocess.check_output(['rustc', '-vV'], text=True).splitlines() if line.startswith('host:'))
    metadata = json.loads(subprocess.check_output(['cargo', 'metadata', '--offline', '--locked',
                         '--format-version', '1', '--filter-platform', host,
                         '--manifest-path', str(release / 'Cargo.toml')], text=True))
    packages = [p for p in metadata['packages'] if p['source']]
    texts = ['Pingox third-party notices\nGenerated from the locked native dependency graph.\n']
    revisions = {}
    for package in packages:
        directory = Path(package['manifest_path']).parent
        vcs = directory / '.cargo_vcs_info.json'
        if vcs.exists():
            info = json.loads(vcs.read_text())
            if not info.get('path_in_vcs') and info.get('git', {}).get('sha1'):
                revisions[(package.get('repository'), info['git']['sha1'])] = directory
    missing = []
    for package in sorted(packages, key=lambda p: (p['name'], p['version'])):
        directory = Path(package['manifest_path']).parent
        files = notice_files(directory)
        declared = package.get('license_file')
        if declared:
            declared = (directory / declared).resolve()
            if declared.is_relative_to(directory.resolve()) and declared.is_file() and declared not in files:
                files.append(declared)
        if not files:
            vcs = directory / '.cargo_vcs_info.json'
            if vcs.exists():
                revision = json.loads(vcs.read_text()).get('git', {}).get('sha1')
                sibling = revisions.get((package.get('repository'), revision))
                if sibling: files = notice_files(sibling)
        if not files:
            override = ROOT / 'deploy/license-overrides' / (package['name'] + '-' + package['version'])
            if override.is_dir(): files = notice_files(override)
        if not files:
            missing.append(package['name'] + '@' + package['version'])
            continue
        texts.append(f"\n{'=' * 72}\n{package['name']} {package['version']}\nDeclared license: {package.get('license')}\nRepository: {package.get('repository')}\n")
        for file in files:
            if file.stat().st_size > 512 * 1024:
                raise ValueError('unexpectedly large license file')
            texts.append('\n--- ' + file.name + ' ---\n' + file.read_text())
    if missing:
        raise ValueError('Missing dependency notices: ' + ', '.join(missing))
    sysroot = Path(subprocess.check_output(['rustc', '--print', 'sysroot'], text=True).strip())
    rust_docs = sysroot / 'share/doc/rust'
    if rust_docs.is_dir():
        for file in notice_files(rust_docs):
            if file.stat().st_size <= 2 * 1024 * 1024:
                texts.append('\n--- Rust toolchain: ' + file.name + ' ---\n' + file.read_text())
    output = release / 'THIRD_PARTY_NOTICES.txt'
    output.write_text('\n'.join(texts))
    return output


if __name__ == '__main__':
    import sys
    write_notices(Path(sys.argv[1]).resolve())
