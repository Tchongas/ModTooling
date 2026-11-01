import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import uuid
import zipfile

VERSION = Path(__file__).with_name('VERSION').read_text().strip()
MODRINTH = 'https://api.modrinth.com/v2'
CURSEFORGE = 'https://minecraft.curseforge.com/api'


class ToolError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ToolError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        temporary = Path(stream.name)
    temporary.replace(path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(root, relative):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), f'Expected relative path: {relative}')
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), f'Path escapes project: {relative}')
    return path


def load_config(root):
    local = root / '.devin/mod.json'
    if local.is_file():
        return read_json(local), local
    workspace_file = root.parent / 'modsdev.json'
    workspace = read_json(workspace_file)
    for name, entry in workspace.get('projects', {}).items():
        if name == root.name:
            return entry, workspace_file
    raise ToolError(f'No entry for {root.name!r} in workspace {workspace_file.name} and no .devin/mod.json')


def config(root):
    data, _source = load_config(root)
    require(data.get('schema') == 1, 'Unsupported configuration schema')
    require(data.get('tooling_version') == VERSION, f'ModTooling {data.get("tooling_version")} required; running {VERSION}')
    require(data.get('kind') in ('wrapper', 'mod'), 'kind must be wrapper or mod')
    build = data['build']
    require(re.fullmatch(r'[a-z][a-z0-9_]*', build['mod_id']), 'Invalid mod_id')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]*', build['version']), 'Invalid release version')
    require(isinstance(build['java_version'], int), 'java_version must be an integer')
    require(data['game_versions'] and build['minecraft_version'] in data['game_versions'], 'game_versions must include the build target')
    require(all(isinstance(v, str) and v for v in data['game_versions']), 'Invalid game_versions')
    loaders = data['loaders']
    require(loaders and set(loaders) <= {'fabric', 'neoforge'}, 'Configure Fabric and/or NeoForge modules')
    require(len(set(loaders.values())) == len(loaders), 'Each loader needs its own module')
    for module in loaders.values():
        require(re.fullmatch(r'[a-z][a-z0-9_-]*', module), 'Invalid Gradle module name')
        require((inside(root, module) / 'build.gradle').is_file(), f'Missing loader module: {module}')
    require(data['kind'] == 'wrapper' or not data['pack']['enabled'], 'Normal mods cannot export datapacks')
    require(inside(root, data['resources']).is_dir(), 'Missing resources directory')
    return data


def validate(root, data):
    resources = inside(root, data['resources'])
    paths = list(resources.rglob('*.json')) + list(resources.rglob('*.mcmeta'))
    for path in paths:
        try:
            read_json(path)
        except (ValueError, UnicodeError) as error:
            raise ToolError(f'{path.relative_to(root.resolve())}: {error}') from error
    if data['pack']['enabled']:
        require((resources / 'pack.mcmeta').is_file(), 'Pack needs pack.mcmeta')
        require((resources / 'data').is_dir() and (resources / 'assets').is_dir(), 'Combined pack needs data/ and assets/')
    for loader, module in data['loaders'].items():
        metadata = 'fabric.mod.json' if loader == 'fabric' else 'META-INF/neoforge.mods.toml'
        require((root / module / 'src/main/resources' / metadata).is_file(), f'Missing {loader} metadata')
    print(f'Validated configuration and {len(paths)} JSON resources. Runtime compatibility still requires Minecraft QA.')


def check_archive(path, kind, data):
    require(path.is_file(), f'Missing artifact: {path}')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(set(names)) == len(names), f'Duplicate archive entries: {path.name}')
        require(all(not n.startswith('/') and '..' not in n.split('/') and '\\' not in n for n in names), 'Unsafe archive paths')
        require(archive.testzip() is None, f'Corrupt archive: {path.name}')
        for name in names:
            if name.endswith(('.json', '.mcmeta')):
                text = archive.read(name).decode('utf-8-sig')
                require(not re.search(r'\$\{[^}]+\}', text), f'Unexpanded placeholder: {name}')
                json.loads(text)
        if kind == 'fabric':
            metadata = json.loads(archive.read('fabric.mod.json'))
            require(metadata['id'] == data['build']['mod_id'] and metadata['version'] == data['build']['version'], 'Fabric identity/version mismatch')
            require(any(n.endswith('.class') for n in names), 'Mod JAR has no compiled classes')
        elif kind == 'neoforge':
            require('META-INF/neoforge.mods.toml' in names, 'Missing NeoForge metadata')
            metadata = tomllib.loads(archive.read('META-INF/neoforge.mods.toml').decode('utf-8-sig'))
            require(any(m.get('modId') == data['build']['mod_id'] and m.get('version') == data['build']['version'] for m in metadata.get('mods', [])), 'NeoForge identity/version mismatch')
            require(any(n.endswith('.class') for n in names), 'Mod JAR has no compiled classes')
        else:
            require('pack.mcmeta' in names, 'Missing pack.mcmeta')
            require(any(n.startswith('data/') for n in names) and any(n.startswith('assets/') for n in names), 'Combined ZIP is missing content')
            require(all(n == 'pack.mcmeta' or n.startswith(('data/', 'assets/')) for n in names), 'Wrapper-only files leaked into pack')


def generate_tab(root, data, dest):
    """Scan recipes under dest/data/**/recipe/*.json and write creative_tab_config.json into dest."""
    dest = Path(dest)
    items, seen = [], set()
    for recipe in sorted(dest.glob('data/**/recipe/*.json')):
        try:
            components = read_json(recipe).get('result', {}).get('components', {})
            cmd = components['minecraft:custom_model_data']['strings'][0]
            name = components['minecraft:item_name']
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if cmd not in seen:
            seen.add(cmd)
            items.append({'name': name, 'custom_model_data': cmd})
    pack = data.get('pack', {})
    config_tab = {'tab_name': pack.get('tab_name', data['build']['mod_name']),
                  'icon_custom_model_data': pack.get('icon_custom_model_data', items[0]['custom_model_data'] if items else '1'),
                  'items': items}
    write_json(dest / 'creative_tab_config.json', config_tab)
    print(f'Generated creative tab: {len(items)} item(s) from recipes')


def output_dir(root, data):
    return root / 'output' / data['build']['version']


def package(root, data, args):
    validate(root, data)
    artifacts = {}
    for entry in args.artifact:
        kind, path = entry.split('=', 1)
        require(kind not in artifacts, f'Duplicate artifact: {kind}')
        artifacts[kind] = Path(path).resolve()
    require(set(artifacts) == set(data['loaders']), 'Artifacts must match configured loaders')
    destination = output_dir(root, data)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination) as temporary:
        staging = Path(temporary)
        for kind, path in artifacts.items():
            check_archive(path, kind, data)
            shutil.copy2(path, staging / path.name)
        if data['pack']['enabled']:
            require(args.resources, 'Processed resources required for pack export')
            resources = Path(args.resources).resolve()
            pack = staging / f'{data["build"]["mod_id"]}-datapack-{data["build"]["version"]}.zip'
            with zipfile.ZipFile(pack, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(resources.rglob('*')):
                    relative = path.relative_to(resources)
                    if path.is_file() and (relative.as_posix() == 'pack.mcmeta' or relative.parts[0] in ('data', 'assets')):
                        entry = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        archive.writestr(entry, path.read_bytes())
            check_archive(pack, 'pack', data)
            artifacts['pack'] = pack
        manifest = {'schema': 1, 'tooling_version': VERSION, 'config_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(), 'version': data['build']['version'], 'artifacts': []}
        for kind, path in artifacts.items():
            staged = staging / path.name
            target = destination / path.name
            require(not target.is_symlink(), 'Refusing symlink output')
            shutil.copy2(staged, target)
            manifest['artifacts'].append({'kind': kind, 'file': path.name, 'sha256': digest(target)})
        write_json(destination / 'manifest.json', manifest)
    print(f'Packaged verified artifacts: {destination}')


def verified_artifacts(root, data):
    directory = output_dir(root, data)
    manifest = read_json(directory / 'manifest.json')
    require(manifest['config_sha256'] == hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(), 'Configuration changed; rebuild before using artifacts')
    require(manifest['version'] == data['build']['version'] and manifest['tooling_version'] == VERSION, 'Stale manifest')
    expected = set(data['loaders']) | ({'pack'} if data['pack']['enabled'] else set())
    require({a['kind'] for a in manifest['artifacts']} == expected and len(manifest['artifacts']) == len(expected), 'Artifact set mismatch')
    for artifact in manifest['artifacts']:
        require(Path(artifact['file']).name == artifact['file'], 'Invalid artifact filename')
        path = inside(directory, artifact['file'])
        require(digest(path) == artifact['sha256'], f'Artifact checksum mismatch: {path.name}')
        check_archive(path, artifact['kind'], data)
    return manifest


def build(root):
    wrapper = root / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
    command = [str(wrapper), 'build', f'-PmodToolingDir={Path(__file__).parent.resolve()}']
    if os.name != 'nt':
        command.insert(0, 'sh')
    subprocess.run(command, cwd=root, check=True)


def install(root, data, args):
    if not args.no_build:
        build(root)
    manifest = verified_artifacts(root, data)
    local = read_json(root / '.devin/qa.local.json')
    loader = local['loader']
    require(loader in data['loaders'], 'QA loader is not enabled')
    require(local['minecraft_version'] in data['game_versions'], 'QA Minecraft version is not supported')
    instance = Path(local['instance']).expanduser().resolve()
    require(instance.is_dir(), 'QA instance does not exist')
    launcher = instance.parent / 'mmc-pack.json'
    if launcher.is_file():
        components = {c['uid']: c['version'] for c in read_json(launcher)['components']}
        require(components.get('net.minecraft') == local['minecraft_version'], 'Launcher Minecraft version mismatch')
        uid = 'net.fabricmc.fabric-loader' if loader == 'fabric' else 'net.neoforged'
        require(uid in components, 'Launcher loader mismatch')
    else:
        require(args.trust_instance, 'Cannot inspect launcher metadata. Check loader/version and pass --trust-instance explicitly.')
    mods = instance / 'mods'
    require(mods.is_dir() and not mods.is_symlink(), 'Expected an existing, non-symlink mods directory')
    artifact = next(a for a in manifest['artifacts'] if a['kind'] == loader)
    target = mods / artifact['file']
    require(not target.is_symlink(), 'Refusing symlink mod destination')
    state_path = root / '.devin/qa-state.local.json'
    state = read_json(state_path) if state_path.is_file() else {}
    previous = state.get(str(instance), {})
    old = mods / previous.get('file', artifact['file'])
    require(old.parent == mods, 'Invalid previous QA filename')
    for candidate in mods.glob('*.jar'):
        if candidate in (target, old) or not zipfile.is_zipfile(candidate):
            continue
        with zipfile.ZipFile(candidate) as archive:
            if loader == 'fabric' and 'fabric.mod.json' in archive.namelist():
                metadata = json.loads(archive.read('fabric.mod.json'))
                require(metadata.get('id') != data['build']['mod_id'], f'Another version of this mod is already installed; move it manually first: {candidate}')
    conflicts = [p for p in {target, old} if p.exists()]
    if target.exists() and digest(target) == artifact['sha256'] and old == target:
        print(f'Already installed: {target}')
        return
    require(not conflicts or args.replace, 'Existing QA artifact found; close Minecraft and rerun with --replace to back it up before replacement')
    for path in conflicts:
        require(not path.is_symlink(), 'Refusing symlink replacement')
        require(previous.get('file') == path.name and previous.get('sha256') == digest(path), f'Untracked or modified mod; move it manually first: {path}')
    source = output_dir(root, data) / artifact['file']
    backup = instance / '.modtooling-backups' / uuid.uuid4().hex
    with tempfile.NamedTemporaryFile(dir=mods, suffix='.pending', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(source.read_bytes())
    moved = []
    try:
        for path in conflicts:
            backup.mkdir(parents=True, exist_ok=True)
            saved = backup / path.name
            path.rename(saved)
            moved.append((saved, path))
        temporary.replace(target)
    except OSError:
        for saved, original in reversed(moved):
            saved.rename(original)
        temporary.unlink(missing_ok=True)
        raise
    state[str(instance)] = {'file': artifact['file'], 'sha256': artifact['sha256']}
    write_json(state_path, state)
    print(f'Installed: {target}\nLaunch Minecraft manually; installation is not a gameplay QA pass.')


def multipart(field, metadata, file_field, path):
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\nContent-Type: application/json\r\n\r\n'.encode() + json.dumps(metadata).encode())
    body += f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    body += path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    return body, f'multipart/form-data; boundary={boundary}'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ToolError('Platform redirected the request; refusing to forward credentials. Check the documented API endpoint.')


def request(url, token_header, token, body=None, content_type=None):
    headers = {'User-Agent': f'ModTooling/{VERSION}', token_header: token}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ToolError(f'Platform request failed: HTTP {error.code}. Check credentials/configuration; no automatic upload retry.') from None
    except urllib.error.URLError:
        raise ToolError('Platform request failed. If this was an upload, check the platform before retrying; it may have succeeded.') from None


def release_plan(root, data):
    manifest = verified_artifacts(root, data)
    release = data['release']
    require(release['channel'] in ('release', 'beta', 'alpha'), 'Invalid release channel')
    changelog = inside(root, release['changelog'])
    require(changelog.is_file() and changelog.read_text(encoding='utf-8').strip(), 'Write release notes in the configured changelog file first')
    plans = []
    for artifact in manifest['artifacts']:
        kind = artifact['kind']
        targets = release['targets'].get(kind, {})
        require(targets, f'No publishing targets for {kind}')
        for platform, target in targets.items():
            require(platform in ('modrinth', 'curseforge'), f'Unknown platform: {platform}')
            project_id = target.get('project_id')
            require(project_id and re.fullmatch(r'[A-Za-z0-9_-]+', str(project_id)), f'Set {kind}.{platform}.project_id before publishing')
            if platform == 'curseforge':
                require(str(project_id).isdigit(), 'CurseForge requires a numeric project ID')
            name = f'{data["build"]["mod_name"]} {data["build"]["version"]} ({kind})'
            notes = changelog.read_text(encoding='utf-8')
            if platform == 'modrinth':
                metadata = {'project_id': str(project_id), 'name': name, 'version_number': f'{data["build"]["version"]}-{kind}', 'changelog': notes, 'dependencies': target.get('dependencies', []), 'game_versions': data['game_versions'], 'version_type': release['channel'], 'loaders': ['datapack'] if kind == 'pack' else [kind], 'featured': False, 'file_parts': ['file'], 'primary_file': 'file'}
                url = f'{MODRINTH}/version'
            else:
                metadata = {'displayName': name, 'changelog': notes, 'changelogType': 'markdown', 'releaseType': release['channel'], 'gameVersionNames': data['game_versions'] + ([] if kind == 'pack' else [{'fabric': 'Fabric', 'neoforge': 'NeoForge'}[kind]]), 'relations': {'projects': target.get('relations', [])}}
                url = f'{CURSEFORGE}/projects/{project_id}/upload-file'
            plans.append({'platform': platform, 'kind': kind, 'file': artifact['file'], 'sha256': artifact['sha256'], 'url': url, 'metadata': metadata})
    return plans


def publish(root, data, args):
    plans = release_plan(root, data)
    require(args.confirm == data['build']['version'], 'Pass --confirm with the exact release version to authorize uploads')
    require(os.environ.get('GITHUB_ACTIONS') == 'true', 'Publishing is restricted to the approval-gated GitHub Actions job')
    for platform in {p['platform'] for p in plans}:
        require(os.environ.get(f'{platform.upper()}_TOKEN'), f'Missing {platform.upper()}_TOKEN')
    receipt_path = output_dir(root, data) / 'publish-receipts.json'
    receipts = read_json(receipt_path) if receipt_path.is_file() else {}
    for plan in plans:
        key = f'{plan["platform"]}:{plan["kind"]}:{plan["metadata"]["project_id"] if plan["platform"] == "modrinth" else plan["url"]}'
        if key in receipts:
            require(receipts[key]['sha256'] == plan['sha256'], 'Already published different bytes under this release; use a new version')
            require(receipts[key]['status'] == 'uploaded', 'Uncertain prior upload: check the platform manually before retrying')
            print(f'Already uploaded: {key}')
            continue
        platform = plan['platform']
        token = os.environ[f'{platform.upper()}_TOKEN']
        header = 'Authorization' if platform == 'modrinth' else 'X-Api-Token'
        if platform == 'modrinth':
            existing = request(f'{MODRINTH}/project/{plan["metadata"]["project_id"]}/version', header, token)
            duplicates = [v for v in existing if v['version_number'] == plan['metadata']['version_number']]
            if duplicates:
                raise ToolError(f'Modrinth version already exists: {plan["metadata"]["version_number"]}. Reconcile manually; no duplicate upload.')
        path = output_dir(root, data) / plan['file']
        body, content_type = multipart('data' if platform == 'modrinth' else 'metadata', plan['metadata'], 'file', path)
        receipts[key] = {'sha256': plan['sha256'], 'status': 'pending'}
        write_json(receipt_path, receipts)
        result = request(plan['url'], header, token, body, content_type)
        receipts[key].update(status='uploaded', id=result['id'])
        write_json(receipt_path, receipts)
        print(f'Uploaded {platform}/{plan["kind"]}: {result["id"]}')


def main():
    parser = argparse.ArgumentParser(description='Shared Minecraft packaging, QA, and release tooling')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'build', 'verify', 'plan'):
        commands.add_parser(name)
    gentab = commands.add_parser('gen-tab')
    gentab.add_argument('--dest', required=True)
    package_parser = commands.add_parser('package')
    package_parser.add_argument('--artifact', action='append', default=[])
    package_parser.add_argument('--resources')
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--no-build', action='store_true')
    install_parser.add_argument('--replace', action='store_true')
    install_parser.add_argument('--trust-instance', action='store_true')
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--confirm', required=True)
    args = parser.parse_args()
    root = args.project.resolve()
    data = config(root)
    if args.command == 'validate':
        validate(root, data)
    elif args.command == 'build':
        build(root)
    elif args.command == 'package':
        package(root, data, args)
    elif args.command == 'verify':
        verified_artifacts(root, data)
        print('Artifact checksums and archive structure verified.')
    elif args.command == 'gen-tab':
        generate_tab(root, data, Path(args.dest).resolve())
    elif args.command == 'install':
        install(root, data, args)
    elif args.command == 'plan':
        print(json.dumps(release_plan(root, data), indent=2))
    else:
        publish(root, data, args)


if __name__ == '__main__':
    try:
        main()
    except (ToolError, OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
