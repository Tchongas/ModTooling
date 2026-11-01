import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import modtool as tool


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.resources = self.root / 'fabric/src/main/resources'
        self.resources.mkdir(parents=True)
        (self.root / 'fabric/build.gradle').write_text('')
        self.data = {
            'schema': 1, 'tooling_version': tool.VERSION, 'kind': 'wrapper',
            'build': {'mod_id': 'testmod', 'version': '1.0.0', 'minecraft_version': '26.3', 'java_version': 25, 'mod_name': 'Test'},
            'game_versions': ['26.3'], 'loaders': {'fabric': 'fabric'},
            'resources': 'fabric/src/main/resources', 'pack': {'enabled': True},
            'release': {'channel': 'release', 'changelog': 'CHANGELOG.md', 'targets': {
                kind: {'modrinth': {'project_id': 'test'}, 'curseforge': {'project_id': 123}}
                for kind in ('fabric', 'pack')
            }}
        }
        self.save()
        tool.write_json(self.resources / 'pack.mcmeta', {'pack': {'description': 'Test', 'min_format': [97, 1], 'max_format': [121, 0]}})
        tool.write_json(self.resources / 'fabric.mod.json', {'id': 'testmod', 'version': '1.0.0'})
        tool.write_json(self.resources / 'data/test/recipe/test.json', {'type': 'minecraft:crafting_shapeless'})
        tool.write_json(self.resources / 'assets/test/models/item/test.json', {'parent': 'minecraft:item/generated'})
        (self.root / 'CHANGELOG.md').write_text('Test release', encoding='utf-8')
        self.jar = self.root / 'test-fabric.jar'
        self.make_jar()
        self.args = argparse.Namespace(artifact=[f'fabric={self.jar}'], resources=str(self.resources))
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def save(self):
        tool.write_json(self.root / '.devin/mod.json', self.data)

    def make_jar(self):
        with zipfile.ZipFile(self.jar, 'w') as archive:
            archive.writestr('fabric.mod.json', json.dumps({'id': 'testmod', 'version': self.data['build']['version']}))
            archive.writestr('Test.class', b'class-bytes')

    def package(self):
        tool.package(self.root, self.data, self.args)
        return tool.verified_artifacts(self.root, self.data)

    def setup_instance(self):
        instance = self.root / 'instance/.minecraft'
        (instance / 'mods').mkdir(parents=True)
        tool.write_json(instance.parent / 'mmc-pack.json', {'components': [
            {'uid': 'net.minecraft', 'version': '26.3'},
            {'uid': 'net.fabricmc.fabric-loader', 'version': '0.19.5'}]})
        tool.write_json(self.root / '.devin/qa.local.json', {'instance': str(instance), 'loader': 'fabric', 'minecraft_version': '26.3'})
        return instance

    def install(self, replace=False, trust=False):
        tool.install(self.root, self.data, argparse.Namespace(no_build=True, replace=replace, trust_instance=trust))

    def test_configuration(self):
        self.assertEqual(tool.config(self.root), self.data)

    def test_tooling_version_must_match(self):
        self.data['tooling_version'] = 'other'
        self.save()
        with self.assertRaisesRegex(tool.ToolError, 'required'):
            tool.config(self.root)

    def test_normal_mod_cannot_export_pack(self):
        self.data['kind'] = 'mod'
        self.save()
        with self.assertRaisesRegex(tool.ToolError, 'cannot export'):
            tool.config(self.root)

    def test_paths_cannot_escape_project(self):
        with self.assertRaises(tool.ToolError):
            tool.inside(self.root, '../escape')

    def test_malformed_resource_rejected(self):
        (self.resources / 'bad.json').write_text('{')
        with self.assertRaisesRegex(tool.ToolError, 'bad.json'):
            tool.validate(self.root, self.data)

    def test_packaging_manifest_and_content(self):
        manifest = self.package()
        self.assertEqual({a['kind'] for a in manifest['artifacts']}, {'fabric', 'pack'})
        pack = next(a for a in manifest['artifacts'] if a['kind'] == 'pack')
        with zipfile.ZipFile(tool.output_dir(self.root, self.data) / pack['file']) as archive:
            self.assertNotIn('fabric.mod.json', archive.namelist())
            self.assertIn('pack.mcmeta', archive.namelist())

    def test_pack_is_reproducible(self):
        first = self.package()['artifacts']
        second = self.package()['artifacts']
        self.assertEqual(first, second)

    def test_normal_mod_has_no_pack(self):
        self.data.update(kind='mod', pack={'enabled': False})
        self.save()
        self.assertEqual(len(self.package()['artifacts']), 1)

    def test_neoforge_artifact_interface(self):
        self.data['loaders'] = {'neoforge': 'neoforge'}
        module = self.root / 'neoforge'
        (module / 'src/main/resources/META-INF').mkdir(parents=True)
        (module / 'build.gradle').write_text('')
        metadata = '[[mods]]\nmodId = "testmod"\nversion = "1.0.0"\n'
        (module / 'src/main/resources/META-INF/neoforge.mods.toml').write_text(metadata)
        self.save()
        self.assertEqual(tool.config(self.root)['loaders'], {'neoforge': 'neoforge'})
        with zipfile.ZipFile(self.jar, 'w') as archive:
            archive.writestr('META-INF/neoforge.mods.toml', metadata)
            archive.writestr('Test.class', b'class-bytes')
        self.args.artifact = [f'neoforge={self.jar}']
        self.assertEqual({a['kind'] for a in self.package()['artifacts']}, {'neoforge', 'pack'})

    def test_tampered_artifact_rejected(self):
        manifest = self.package()
        (tool.output_dir(self.root, self.data) / manifest['artifacts'][0]['file']).write_bytes(b'tampered')
        with self.assertRaisesRegex(tool.ToolError, 'checksum mismatch'):
            tool.verified_artifacts(self.root, self.data)

    def test_changed_config_rejected(self):
        self.package()
        self.data['game_versions'].append('26.2')
        self.save()
        with self.assertRaisesRegex(tool.ToolError, 'Configuration changed'):
            tool.verified_artifacts(self.root, self.data)

    def test_manifest_traversal_rejected(self):
        self.package()
        path = tool.output_dir(self.root, self.data) / 'manifest.json'
        manifest = tool.read_json(path)
        manifest['artifacts'][0]['file'] = '../outside.jar'
        tool.write_json(path, manifest)
        with self.assertRaisesRegex(tool.ToolError, 'filename'):
            tool.verified_artifacts(self.root, self.data)

    def test_unexpanded_metadata_rejected(self):
        with zipfile.ZipFile(self.jar, 'a') as archive:
            archive.writestr('extra.json', '{"version":"${version}"}')
        with self.assertRaisesRegex(tool.ToolError, 'Unexpanded'):
            self.package()

    def test_install_leaves_unrelated_mods_alone(self):
        self.package()
        instance = self.setup_instance()
        unrelated = instance / 'mods/unrelated.jar'
        unrelated.write_bytes(b'unrelated')
        self.install()
        self.assertEqual(unrelated.read_bytes(), b'unrelated')
        self.assertTrue((instance / 'mods' / self.jar.name).is_file())
        self.install()

    def test_untracked_file_never_overwritten(self):
        self.package()
        instance = self.setup_instance()
        target = instance / 'mods' / self.jar.name
        target.write_bytes(b'untracked')
        with self.assertRaisesRegex(tool.ToolError, 'Untracked'):
            self.install(replace=True)
        self.assertEqual(target.read_bytes(), b'untracked')

    def test_tracked_replacement_requires_permission_and_backup(self):
        self.package()
        instance = self.setup_instance()
        self.install()
        original = self.jar.read_bytes()
        with zipfile.ZipFile(self.jar, 'a') as archive:
            archive.writestr('changed.txt', 'changed')
        self.package()
        with self.assertRaisesRegex(tool.ToolError, '--replace'):
            self.install()
        self.install(replace=True)
        backups = list((instance / '.modtooling-backups').rglob('*.jar'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_modified_tracked_file_rejected(self):
        self.package()
        instance = self.setup_instance()
        self.install()
        (instance / 'mods' / self.jar.name).write_bytes(b'user-changes')
        with self.assertRaisesRegex(tool.ToolError, 'Untracked or modified'):
            self.install(replace=True)

    def test_launcher_mismatch_rejected(self):
        self.package()
        instance = self.setup_instance()
        tool.write_json(instance.parent / 'mmc-pack.json', {'components': [{'uid': 'net.minecraft', 'version': 'wrong'}]})
        with self.assertRaisesRegex(tool.ToolError, 'version mismatch'):
            self.install()

    def test_unknown_launcher_requires_explicit_trust(self):
        self.package()
        instance = self.setup_instance()
        (instance.parent / 'mmc-pack.json').unlink()
        with self.assertRaisesRegex(tool.ToolError, '--trust-instance'):
            self.install()
        self.install(trust=True)

    def test_release_plan_is_offline(self):
        self.package()
        with patch.object(tool, 'request', side_effect=AssertionError('Network used')):
            plans = tool.release_plan(self.root, self.data)
        self.assertEqual(len(plans), 4)
        fabric = next(p for p in plans if p['platform'] == 'modrinth' and p['kind'] == 'fabric')
        self.assertEqual(fabric['metadata']['loaders'], ['fabric'])
        pack = next(p for p in plans if p['platform'] == 'modrinth' and p['kind'] == 'pack')
        self.assertEqual(pack['metadata']['loaders'], ['datapack'])

    def test_missing_project_id_blocks_release(self):
        self.data['release']['targets']['fabric']['modrinth']['project_id'] = None
        self.save()
        self.package()
        with self.assertRaisesRegex(tool.ToolError, 'project_id'):
            tool.release_plan(self.root, self.data)

    def test_publish_requires_confirmation(self):
        self.package()
        with self.assertRaisesRegex(tool.ToolError, '--confirm'):
            tool.publish(self.root, self.data, argparse.Namespace(confirm='wrong'))

    def test_publish_restricted_to_ci(self):
        self.package()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': ''}):
            with self.assertRaisesRegex(tool.ToolError, 'restricted'):
                tool.publish(self.root, self.data, argparse.Namespace(confirm='1.0.0'))

    def test_publish_receipts_prevent_repeat_uploads(self):
        self.package()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'MODRINTH_TOKEN': 'fake', 'CURSEFORGE_TOKEN': 'fake'}):
            with patch.object(tool, 'request', side_effect=lambda url, *a: [] if '/project/' in url else {'id': 42}) as request:
                tool.publish(self.root, self.data, argparse.Namespace(confirm='1.0.0'))
                count = request.call_count
                tool.publish(self.root, self.data, argparse.Namespace(confirm='1.0.0'))
                self.assertEqual(request.call_count, count)

    def test_duplicate_untracked_mod_id_rejected(self):
        self.package()
        instance = self.setup_instance()
        duplicate = instance / 'mods/old-name.jar'
        duplicate.write_bytes(self.jar.read_bytes())
        with self.assertRaisesRegex(tool.ToolError, 'Another version'):
            self.install(replace=True)
        self.assertTrue(duplicate.exists())

    def test_redirects_never_forward_credentials(self):
        with self.assertRaisesRegex(tool.ToolError, 'refusing to forward'):
            tool.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid')

    def test_uncertain_upload_blocks_retry(self):
        self.package()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'MODRINTH_TOKEN': 'fake', 'CURSEFORGE_TOKEN': 'fake'}):
            def fail(url, *args):
                if '/project/' in url:
                    return []
                raise tool.ToolError('timeout')
            with patch.object(tool, 'request', side_effect=fail):
                with self.assertRaisesRegex(tool.ToolError, 'timeout'):
                    tool.publish(self.root, self.data, argparse.Namespace(confirm='1.0.0'))
            with patch.object(tool, 'request') as request:
                with self.assertRaisesRegex(tool.ToolError, 'Uncertain'):
                    tool.publish(self.root, self.data, argparse.Namespace(confirm='1.0.0'))
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
