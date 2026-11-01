# ModTooling

Shared Minecraft packaging, local QA installation, and publishing. Python 3.11+ standard library; Gradle builds use each project's Java toolchain. Current pilot: MusicalInstrumentsPack (Fabric / Minecraft 26.3 / Java 25).

## Layout and commands

Keep independent checkouts side by side:

```text
modsdev/
  ModTooling/
  MusicalInstrumentsPack/
  CustomTotems/
```

From a configured mod root:

```powershell
python ../ModTooling/modtool.py validate
python ../ModTooling/modtool.py build
python ../ModTooling/modtool.py verify
python ../ModTooling/modtool.py plan
python ../ModTooling/modtool.py install
```

- `validate`: configuration, JSON syntax, expected resource directories and loader metadata. Not Minecraft schema validation.
- `build`: Gradle checks + production JARs + optional combined ZIP. Outputs `output/<version>/`, including a SHA-256 manifest. Existing older outputs are retained, never selected by glob.
- `verify`: exact artifact set, checksums, archive paths, metadata, unresolved placeholders, pack layout.
- `plan`: offline upload payload preview; requires project IDs and release notes, never credentials.
- `install`: builds and installs one configured loader JAR into a local instance. Does not launch Minecraft, install dependencies, or count as gameplay QA.

Direct `gradlew build` and `gradlew :fabric:build` also package. Failed checks prevent packaging. Python can be selected with `MOD_TOOLING_PYTHON`; tooling location with `MOD_TOOLING_HOME` or `-PmodToolingDir=<absolute path>`.

## Local Minecraft QA

Create ignored `.devin/qa.local.json` in the mod repository when an instance is available:

```json
{"instance":"C:/Minecraft/Test/.minecraft","loader":"fabric","minecraft_version":"26.3"}
```

Point to the game directory containing an existing `mods/`, not its launcher parent. Close Minecraft first. Prism/MultiMC `mmc-pack.json` is inspected for Minecraft and loader identity. Other launchers require explicit `install --trust-instance` after manually checking versions. Dependency availability and minimum loader version are not checked yet.

The installer tracks only its own files in `.devin/qa-state.local.json`. Updates require `--replace`; the previous tracked JAR is moved into `.modtooling-backups/` before installation. Untracked or modified JARs are never replaced, even with that flag. Unrelated mods remain untouched. `--no-build` explicitly reuses the last verified build.

Manual checklist: startup without related errors; creative tab/items; recipes and functions; models/textures. Separately test the combined ZIP without the wrapper, installed in both world datapacks and resource packs. Inspect logs after reload. Server smoke tests and automatic world/pack installation are not implemented yet.

## GitHub publishing

The pilot's `.github/workflows/release.yml` is the thin CI adapter; shared behavior lives here. No repository URLs or platform IDs are inferred.

1. Host ModTooling in its own GitHub repository; configure repository variable `MOD_TOOLING_REPOSITORY` as `owner/repo` and `MOD_TOOLING_REF` as its full 40-character commit SHA. The checkout currently assumes this tooling repository is publicly readable.
2. Fill `.devin/mod.json` targets and write the configured changelog. Remove unwanted platform entries rather than supplying dummy IDs. Wrapper and pack can target different existing projects; check their platform project types.
3. Create GitHub environment **publishing**, configure **required reviewers** and appropriate deployment branch restrictions. An environment name alone does NOT require approval.
4. Store `MODRINTH_TOKEN` (version creation permission) and `CURSEFORGE_TOKEN` (author upload token, not the read-only Core API key) as environment secrets. Never put tokens in project files.
5. Dispatch **Validated release**, confirming exact version, Minecraft QA, and no existing platform uploads. Review candidate artifacts, manifest, release plan, and source commit before approving.

The publish job downloads the same validated candidate; it does not rebuild. Uploads use the Modrinth v2 version API and CurseForge author upload API. No uploads happen on push or during local build/plan/install. Secrets are only passed to the publish step.

Uploads across platforms are not transactional. Receipts record each successful or uncertain upload and are saved as workflow artifacts, including on failure. **Do not blindly rerun a failed publishing job or dispatch the same release again.** Inspect both platforms and reconcile partial results first; receipts only deduplicate when present in the current workspace. New runners do not automatically restore them. Use a new release version for changed published bytes. First live uploads still require verification.

## Development

```powershell
python -m unittest -v
```

Tests use temporary projects/instances and mocked network calls. They never upload or touch real instances. See `CONFIG.md` for configuration and `AGENTS.md` for AI context. Version changes affecting consumers require updating `VERSION` and each consumer's tooling pin.
