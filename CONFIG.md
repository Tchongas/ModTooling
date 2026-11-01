# Project configuration (schema 1)

Location: edit `projects.<RepoName>` in `modsdev.json` at the workspace root. `modsdev.py sync` copies it to `.devin/mod.json` inside each repo (commit the generated file — standalone checkouts and CI read it). Local builds also resolve root `modsdev.json` directly when the local file is absent. This is a ModTooling file, not Devin's `config.json`. MusicalInstrumentsPack is the working example. Do not copy its identity or platform dependencies into unrelated mods.

| Key | Meaning |
|---|---|
| `schema` | `1` |
| `tooling_version` | Exact version in ModTooling `VERSION`; CI additionally pins a commit SHA |
| `kind` | `wrapper` or `mod`; only wrappers may export packs |
| `build` | Gradle properties injected before project evaluation; metadata and dependencies live here, not duplicated in `gradle.properties` |
| `game_versions` | Explicit tested compatibility list, including `build.minecraft_version`; does not build a version matrix |
| `loaders` | Loader-to-Gradle-module map, e.g. `{"fabric":"fabric"}`; allowed names Fabric/NeoForge, lowercase |
| `resources` | Project-relative source resource directory used by validation |
| `pack.enabled` | Export one combined `pack.mcmeta` + `data/` + `assets/` ZIP; no split outputs |
| `pack.tab_name`, `pack.icon_custom_model_data` | Creative tab title/icon. The tab's `items` list is **generated at build time** from every recipe containing `minecraft:item_name` + `minecraft:custom_model_data` — `creative_tab_config.json` must not be hand-edited or committed (source files are removed; the file is written into each loader's processed resources before `jar`). |
| `release.channel` | `release`, `beta`, or `alpha` |
| `release.changelog` | Relative UTF-8 release-notes path; must exist and be nonempty before planning/publishing |
| `release.targets` | Artifact kind (`fabric`, `neoforge`, `pack`) to platform settings |

## Build properties

Pilot keys: `version`, `group`, `java_version`, `minecraft_version`, `mod_name`, `mod_author`, `mod_id`, `license`, `credits`, `description`, `minecraft_version_range`, `fabric_version`, `fabric_loader_version`, `fabric_loom_version`. Loader modules consume these properties using normal Gradle conventions. Keep JVM performance flags in `gradle.properties`.

Changing versions still requires reviewing Java API changes, pack formats, resource schemas, and dependency compatibility. `game_versions` is an assertion of tested compatibility, not an automatic backport. Plugin versions, wrapper version, and resource formats must be intentionally updated together when needed. The current pilot only claims 26.3.

## Targets

Each enabled artifact needs at least one configured platform for publishing. Missing/null IDs block planning/publishing but not building. Delete a platform entry to opt out. Use platform **project IDs**, not arbitrary display names; CurseForge IDs are numeric.

```json
{
  "fabric": {
    "modrinth": {"project_id": null, "dependencies": []},
    "curseforge": {"project_id": null, "relations": []}
  },
  "pack": {
    "modrinth": {"project_id": null, "dependencies": []}
  }
}
```

Modrinth dependencies use its API objects (`project_id`, `dependency_type` such as `required`); CurseForge relations use (`slug`, `type` such as `requiredDependency`). Keep dependencies artifact-specific: a standalone pack must not require Fabric API.

Modrinth versions are named `<version>-<artifact-kind>` and use loader tags `fabric`, `neoforge`, or `datapack`. The single combined ZIP is published as a datapack, not as an extra standalone resource-pack release. CurseForge uses supported game-version names plus a loader label for JARs. Platform acceptance and project categories must be verified on the first live release.

## Consumer Gradle integration

- In `settings.gradle`, apply ModTooling `settings.gradle` after plugin-management configuration. It loads configuration (`.devin/mod.json`, else the repo's entry in workspace `modsdev.json`), checks the tooling version, includes configured loader modules, and injects properties.
- In root `build.gradle`, apply ModTooling `packaging.gradle` after loader plugins.
- Each loader module must provide `jar`, `check`, and `processResources`; `jar` must be its production artifact. Remapping-based older Minecraft targets will need an explicit adapter; do not publish a development JAR.
- Pack contents come from the first configured loader module's processed resources. Those must already contain the complete shared pack and expanded metadata.

NeoForge names and normal-mod packaging are supported by the shared artifact interface and synthetic tests. Actual NeoForge module/template integration is still pending; do not enable it in the pilot until it compiles and passes runtime QA. Empty pack-only projects without loader modules are not supported yet.
