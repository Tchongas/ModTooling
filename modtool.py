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


def default_targets(kind):
    targets = {a: {'modrinth': {'project_id': None, 'dependencies': []},
                   'curseforge': {'project_id': None, 'relations': []}}
               for a in ('fabric', 'neoforge')}
    if kind == 'wrapper':
        targets['pack'] = {'modrinth': {'project_id': None, 'dependencies': []},
                           'curseforge': {'project_id': None, 'relations': []}}
    return targets


def _mc_range(mc):
    parts = str(mc).split('.')
    try:
        return f'[{mc}, {parts[0]}.{int(parts[1]) + 1})'
    except (IndexError, ValueError):
        return f'[{mc},)'


def expand(entry, toolchain=None):
    """Expand a compact project config (modsdev.json schema 2) into the full build config."""
    toolchain = dict(toolchain or {})
    if 'build' in entry:  # already expanded (e.g. generated .devin/mod.json)
        return entry
    kind = entry.get('kind', 'wrapper')
    mc = str(entry['minecraft_version'])
    build = {
        'version': entry['version'], 'group': entry.get('group', f'com.example.{entry["mod_id"]}'),
        'java_version': entry.get('java_version', toolchain.get('java_version', 25)),
        'minecraft_version': mc,
        'mod_name': entry['mod_name'], 'mod_author': entry.get('mod_author', ''),
        'mod_id': entry['mod_id'], 'license': entry.get('license', 'MIT'),
        'credits': entry.get('credits', ''), 'description': entry.get('description', ''),
        'minecraft_version_range': entry.get('minecraft_version_range', _mc_range(mc)),
    }
    for key, value in toolchain.items():
        build.setdefault(key, value)
    pack = dict(entry.get('pack', {}))
    pack.setdefault('enabled', kind == 'wrapper')
    return {
        'schema': 1, 'tooling_version': toolchain.get('tooling_version', entry.get('tooling_version', VERSION)),
        'kind': kind, 'build': build,
        'game_versions': entry.get('game_versions', [mc]),
        'loaders': entry.get('loaders', {'fabric': 'fabric', 'neoforge': 'neoforge'}),
        'resources': entry.get('resources', 'fabric/src/main/resources'),
        'pack': pack,
        'release': {
            'channel': entry.get('channel', 'release'),
            'changelog': entry.get('changelog', 'CHANGELOG.md'),
            'targets': entry.get('targets', default_targets(kind)),
        },
    }


def load_config(root):
    workspace_file = root.parent / 'modsdev.json'
    if workspace_file.is_file():
        workspace = read_json(workspace_file)
        if root.name in workspace.get('projects', {}):
            return expand(workspace['projects'][root.name], workspace.get('toolchain')), workspace_file
    local = root / '.devin/mod.json'
    if local.is_file():
        return expand(read_json(local)), local
    raise ToolError(f'No config for {root.name!r}: expected {workspace_file} entry or .devin/mod.json')


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
    artifacts = [a for a in manifest['artifacts'] if a['kind'] in data['loaders']]
    state_path = root / '.devin/qa-state.local.json'
    state = read_json(state_path) if state_path.is_file() else {}
    prev_raw = state.get(str(instance), {})
    previous = prev_raw.get('files') or ({loader: prev_raw} if 'file' in prev_raw else {})
    installed = {}
    for artifact in artifacts:
        target = mods / artifact['file']
        require(not target.is_symlink(), 'Refusing symlink mod destination')
        prev = previous.get(artifact['kind'], {})
        old_name = prev.get('file', artifact['file'])
        require(Path(old_name).name == old_name, 'Invalid previous QA filename')
        old = mods / old_name
        if artifact['kind'] == 'fabric':
            for candidate in mods.glob('*.jar'):
                if candidate in (target, old) or not zipfile.is_zipfile(candidate):
                    continue
                with zipfile.ZipFile(candidate) as archive:
                    if 'fabric.mod.json' in archive.namelist():
                        metadata = json.loads(archive.read('fabric.mod.json'))
                        require(metadata.get('id') != data['build']['mod_id'], f'Another version of this mod is already installed; move it manually first: {candidate}')
        conflicts = [p for p in {target, old} if p.exists()]
        if target.exists() and digest(target) == artifact['sha256'] and old == target:
            print(f'Already installed: {target}')
            installed[artifact['kind']] = {'file': artifact['file'], 'sha256': artifact['sha256']}
            continue
        for path in conflicts:
            require(not path.is_symlink(), 'Refusing symlink replacement')
            require(prev.get('file') == path.name and prev.get('sha256') == digest(path), f'Untracked or modified mod; move it manually first: {path}')
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
        installed[artifact['kind']] = {'file': artifact['file'], 'sha256': artifact['sha256']}
        print(f'Installed ({artifact["kind"]}): {target}' + ('  [not this instance\'s loader — enable in launcher when testing it]' if artifact['kind'] != loader else ''))
    state[str(instance)] = {'files': installed}
    write_json(state_path, state)
    print('Launch Minecraft manually; installation is not a gameplay QA pass.')


def multipart(field, metadata, files):
    """files: list of (field_name, Path)."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\nContent-Type: application/json\r\n\r\n'.encode() + json.dumps(metadata).encode())
    for fname, path in files:
        body += f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{fname}"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        body += path.read_bytes()
    body += f'\r\n--{boundary}--\r\n'.encode()
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
    """Grouped releases: 'mod' = all loader JARs together (Modrinth multi-file, CurseForge per-loader);
    'pack' = datapack ZIP on Modrinth only — never CurseForge."""
    manifest = verified_artifacts(root, data)
    release = data['release']
    require(release['channel'] in ('release', 'beta', 'alpha'), 'Invalid release channel')
    changelog = inside(root, release['changelog'])
    require(changelog.is_file() and changelog.read_text(encoding='utf-8').strip(), 'Write release notes in the configured changelog file first')
    notes = changelog.read_text(encoding='utf-8')
    version = data['build']['version']
    name = data['build']['mod_name']
    targets = release.get('targets', {})
    out = output_dir(root, data)
    plans = []
    by_kind = {a['kind']: a for a in manifest['artifacts']}
    loader_kinds = [k for k in data['loaders'] if k in by_kind]

    if loader_kinds and 'mod' in targets:
        for platform, target in targets['mod'].items():
            pid = _require_pid(target, 'mod', platform)
            if platform == 'modrinth':
                files = [f'file{i}' for i in range(len(loader_kinds))]
                metadata = {'project_id': str(pid), 'name': f'{name} {version} Mod',
                            'version_number': f'{version}+mod', 'changelog': notes,
                            'dependencies': target.get('dependencies', []),
                            'game_versions': data['game_versions'], 'version_type': release['channel'],
                            'loaders': loader_kinds, 'featured': False,
                            'file_parts': files, 'primary_file': files[0]}
                plans.append({'platform': 'modrinth', 'group': 'mod',
                              'files': [by_kind[k]['file'] for k in loader_kinds],
                              'sha256': [by_kind[k]['sha256'] for k in loader_kinds],
                              'url': f'{MODRINTH}/version', 'metadata': metadata, 'file_fields': files})
            elif platform == 'curseforge':
                for k in loader_kinds:
                    metadata = {'displayName': f'{name} {version} [{k}]', 'changelog': notes,
                                'changelogType': 'markdown', 'releaseType': release['channel'],
                                'gameVersionNames': data['game_versions'] + [{'fabric': 'Fabric', 'neoforge': 'NeoForge'}[k]],
                                'relations': {'projects': target.get('relations', [])}}
                    plans.append({'platform': 'curseforge', 'group': 'mod', 'loader': k,
                                  'files': [by_kind[k]['file']], 'sha256': [by_kind[k]['sha256']],
                                  'url': f'{CURSEFORGE}/projects/{pid}/upload-file',
                                  'pid': str(pid), 'metadata': metadata, 'file_fields': ['file']})
            else:
                raise ToolError(f'Unknown platform: {platform}')

    if 'pack' in by_kind:
        target = (targets.get('pack') or {}).get('modrinth')
        if target:
            pid = _require_pid(target, 'pack', 'modrinth')
            metadata = {'project_id': str(pid), 'name': f'{name} {version} Datapack',
                        'version_number': f'{version}+datapack', 'changelog': notes,
                        'dependencies': target.get('dependencies', []),
                        'game_versions': data['game_versions'], 'version_type': release['channel'],
                        'loaders': ['datapack'], 'featured': False,
                        'file_parts': ['file'], 'primary_file': 'file'}
            plans.append({'platform': 'modrinth', 'group': 'pack',
                          'files': [by_kind['pack']['file']], 'sha256': [by_kind['pack']['sha256']],
                          'url': f'{MODRINTH}/version', 'metadata': metadata, 'file_fields': ['file']})
        if (targets.get('pack') or {}).get('curseforge'):
            raise ToolError('Datapack releases are never uploaded to CurseForge — remove pack.curseforge from targets.')
    require(plans, 'No release targets configured')
    return plans


def _require_pid(target, group, platform):
    pid = target.get('project_id')
    require(pid and re.fullmatch(r'[A-Za-z0-9_-]+', str(pid)), f'Set {group}.{platform}.project_id before publishing')
    if platform == 'curseforge':
        require(str(pid).isdigit(), 'CurseForge requires a numeric project ID')
    return pid


def publish(root, data, args):
    for env_file in (root.parent / '.env.local', root.parent / '.env'):
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, _, value = line.partition('=')
                    os.environ.setdefault(key.strip(), value.strip())
            break
    plans = release_plan(root, data)
    only = {k.strip() for k in getattr(args, 'only', '') .split(',') if k.strip()} if getattr(args, 'only', '') else None
    if only:
        plans = [p for p in plans if f'{p["platform"]}:{p["group"]}:{p.get("loader", "")}' in only]
        require(plans, f'No releases matched --only ({sorted(only)})')
    require(args.confirm == data['build']['version'], 'Pass --confirm with the exact release version to authorize uploads')
    for platform in {p['platform'] for p in plans}:
        require(os.environ.get(f'{platform.upper()}_TOKEN'), f'Missing {platform.upper()}_TOKEN')
    receipt_path = output_dir(root, data) / 'publish-receipts.json'
    receipts = read_json(receipt_path) if receipt_path.is_file() else {}
    for plan in plans:
        key = f'{plan["platform"]}:{plan["group"]}:{plan.get("loader", "")}:{plan["url"]}'
        joined = '|'.join(plan['sha256'])
        if key in receipts:
            require(receipts[key]['sha256'] == joined, 'Already published different bytes under this release; use a new version')
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
        else:
            try:
                existing = request(f'{CURSEFORGE}/projects/{plan["pid"]}/files', header, token)
            except ToolError:
                existing = None
            if existing is not None:
                files = existing if isinstance(existing, list) else existing.get('data', [])
                if any(f.get('fileName') == plan['files'][0] for f in files):
                    raise ToolError(f'CurseForge file already exists: {plan["files"][0]}. Reconcile manually; no duplicate upload.')
        files = [(fname, output_dir(root, data) / filename) for fname, filename in zip(plan['file_fields'], plan['files'])]
        body, content_type = multipart('data' if platform == 'modrinth' else 'metadata', plan['metadata'], files)
        receipts[key] = {'sha256': joined, 'status': 'pending'}
        write_json(receipt_path, receipts)
        result = request(plan['url'], header, token, body, content_type)
        receipts[key].update(status='uploaded', id=result['id'])
        write_json(receipt_path, receipts)
        print(f'Uploaded {platform}/{plan["group"]}{("-" + plan["loader"]) if plan.get("loader") else ""}: {result["id"]}')


def main():
    parser = argparse.ArgumentParser(description='Shared Minecraft packaging, QA, and release tooling')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'build', 'verify', 'plan', 'emit-config'):
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
    publish_parser.add_argument('--only', help='Comma-separated platform:group:loader filters, e.g. modrinth:pack:,curseforge:mod:fabric')
    args = parser.parse_args()
    root = args.project.resolve()
    data = config(root)
    if args.command == 'emit-config':
        print(json.dumps(data))
    elif args.command == 'validate':
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
