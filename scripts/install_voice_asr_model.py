#!/usr/bin/env python3
"""Download a fixed public Qwen model; verify length, Git blobs and LFS SHA256."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT / 'scripts')]
from mysql_store.files import hashes
from voice_asr_core import CONFIG, MODEL, STATE


def download(url, target):
    subprocess.run(['curl', '--noproxy', '*', '-fL', '--retry', '3', '--retry-delay', '2',
                    '--connect-timeout', '10', '--max-time', '1800', '-sS', url, '-o', str(target)], check=True)


def download_weight(url, target, size):
    """Verified byte-range download; restartable parts, no unchecked concatenation."""
    folder = target.parent / 'download-parts'
    folder.mkdir(exist_ok=True)
    chunk = 8 * 1024 * 1024
    ranges = [(offset, min(size, offset + chunk)-1) for offset in range(0, size, chunk)]
    def part(bounds):
        start, end = bounds
        path = folder / str(start)
        if path.exists() and path.stat().st_size == end-start+1:
            return path
        temp, headers = folder / (str(start)+'.tmp'), folder / (str(start)+'.headers')
        subprocess.run(['curl', '--noproxy', '*', '-fL', '--retry', '4', '--connect-timeout', '10',
                        '--max-time', '1800', '-sS', '--range', f'{start}-{end}', '-D', str(headers),
                        url, '-o', str(temp)], check=True)
        if temp.stat().st_size != end-start+1 or f'content-range: bytes {start}-{end}/{size}' not in headers.read_text().lower():
            raise RuntimeError('invalid_download_range')
        os.replace(temp, path)
        print(json.dumps(dict(downloaded_part=start, bytes=end-start+1)), flush=True)
        return path
    with ThreadPoolExecutor(max_workers=16) as pool:
        parts = list(pool.map(part, ranges))
    with target.open('wb') as out:
        for path in parts:
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    out.write(block)
        out.flush()
        os.fsync(out.fileno())


def main():
    os.umask(0o077)
    MODEL.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    host = 'https://hf-mirror.com'
    meta_path = STATE / 'model-meta.json'
    download(f"{host}/api/models/{CONFIG['model']}?blobs=true", meta_path)
    metadata = json.loads(meta_path.read_text())
    if metadata['sha'] != CONFIG['revision']:
        raise RuntimeError('model_revision_changed')
    verified = []
    for entry in metadata['siblings']:
        name = entry['rfilename']
        if name in ('.gitattributes', 'README.md'):
            continue
        if Path(name).name != name:
            raise RuntimeError('unexpected_model_path')
        target = MODEL / name
        part = MODEL / (name + '.partial')
        if not target.exists():
            url = f"{host}/{CONFIG['model']}/resolve/{CONFIG['revision']}/{name}"
            if entry.get('lfs'):
                download_weight(url, part, entry['size'])
            else:
                download(url, part)
        candidate = target if target.exists() else part
        info = hashes(candidate)
        if info['bytes'] != entry['size']:
            raise RuntimeError('model_length_mismatch:' + name)
        if entry.get('lfs'):
            if info['sha256'] != entry['lfs']['sha256']:
                raise RuntimeError('model_checksum_mismatch:' + name)
        else:
            h = hashlib.sha1(f"blob {info['bytes']}\0".encode())
            with candidate.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != entry['blobId']:
                raise RuntimeError('model_git_blob_mismatch:' + name)
        if candidate == part:
            with part.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(part, target)
        verified.append(dict(name=name, bytes=info['bytes'], sha256=info['sha256']))
        print(json.dumps(dict(file=name, bytes=info['bytes'], verified=True)), flush=True)
    data = dict(repo=CONFIG['model'], revision=CONFIG['revision'], download_endpoint=host, files=verified)
    part = MODEL / 'verified-model.json.partial'
    part.write_text(json.dumps(data, indent=2))
    os.replace(part, MODEL / 'verified-model.json')


if __name__ == '__main__':
    main()
