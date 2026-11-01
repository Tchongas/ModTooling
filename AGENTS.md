# ModTooling context

- Python 3.11+ standard library. `modtool.py`: config, JSON/archive QA, packaging, local install, release plan/upload. `settings.gradle`: properties/modules. `packaging.gradle`: success-gated Gradle tasks. No external Python dependencies.
- Verify: `python -m unittest -v` here; Java 25 + `gradlew build` and `python ../ModTooling/modtool.py verify` in MusicalInstrumentsPack. On this Windows machine Java 25 is at `C:/Program Files/Eclipse Adoptium/jdk-25.0.3.9-hotspot`; default Java is 17.
- Consumer config: edit `modsdev.json` at workspace root; `modsdev.py sync` generates repo `.devin/mod.json` (committed for standalone/CI). Machine paths/state: ignored `.devin/*.local.json`. Pin tooling version and CI commit SHA. Never store tokens in files.
- Runtime pilot: Fabric + NeoForge on MC 26.3, both build. NeoForge uses ModDevGradle, shares Constants/ModCreativeTabs/resources from fabric module via sourceSet includes, AT file shared. Server smoke tests, version matrices, pack-only builds, templates remain pending.
- ThinMultiloaderTemplate is failed/ignored, not a source template.
- Safety: no publishing during tests/local commands; mocked network only. Real uploads require approved CI setup. Never overwrite untracked/modified instance mods; tracked replacements require explicit flag and backup. Do not delete stale user outputs.
- Docs: README = commands/QA/CI setup; CONFIG = fields/constraints. Read only the relevant section. Avoid duplicate explanations in consumer docs.
