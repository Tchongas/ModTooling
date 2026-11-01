# Project configuration

`modsdev.json` (schema 2) is the **compact, user-edited** config at the workspace root. The tooling expands it into the full schema-1 config; `modsdev info --expanded` or `modtool.py emit-config` shows the resolved values.

## Compact fields (what you edit)

| Key | Meaning |
|---|---|
| `kind` | `wrapper` or `mod` |
| `version`, `minecraft_version` | Release and build target |
| `mod_id`, `mod_name`, `mod_author`, `group`, `description` | Identity |
| `pack` | `{tab_name, icon_custom_model_data}` (wrappers; `enabled` defaults from kind) |
| `targets` | Artifact → platform → `project_id` + dependencies/relations |

## Defaults (expanded automatically — override only when needed)

`license` = `MIT`; `game_versions` = `[minecraft_version]`; `minecraft_version_range` = `[mc, mc+0.1)`; `resources` = `fabric/src/main/resources`; `loaders` = fabric+neoforge modules; `changelog` = `CHANGELOG.md`; `channel` = `release`; `credits` = empty. Loader/plugin/JDK versions come from root `toolchain` (agent-maintained; per-project override by adding the key directly to the project entry).

`modsdev.py sync` writes the **expanded** config to `.devin/mod.json` inside each repo (commit it — standalone checkouts and CI read it). Local builds resolve workspace `modsdev.json` first, then the local file. Gradle resolves config by running `modtool.py emit-config` — Python 3.11+ is required on PATH.

## Expanded keys (for agents reading `.devin/mod.json` or `emit-config`)

| Key | Meaning |
|---|---|
| `schema` / `tooling_version` | `1` / exact version in ModTooling `VERSION`; CI also pins a commit SHA |
| `build` | Gradle properties injected before project evaluation (identity + toolchain pins); not duplicated in `gradle.properties` |
| `game_versions`, `loaders`, `resources`, `pack`, `release` | Expanded from the compact fields; `release.targets` = artifact kind (`fabric`, `neoforge`, `pack`) → platform settings |
| `pack.tab_name`, `pack.icon_custom_model_data` | Creative tab title/icon. The tab's `items` list is **generated at build time** from every recipe containing `minecraft:item_name` + `minecraft:custom_model_data` — never hand-edit `creative_tab_config.json`. |

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

- In `settings.gradle`, apply ModTooling `settings.gradle` after plugin-management configuration. It resolves config by executing `modtool.py emit-config`, checks the tooling version, includes configured loader modules, and injects properties.
- In root `build.gradle`, apply ModTooling `packaging.gradle` after loader plugins.
- Each loader module must provide `jar`, `check`, and `processResources`; `jar` must be its production artifact. Remapping-based older Minecraft targets will need an explicit adapter; do not publish a development JAR.
- Pack contents come from the first configured loader module's processed resources. Those must already contain the complete shared pack and expanded metadata.

Fabric + NeoForge both build and package on the pilot. Empty pack-only projects without loader modules are not supported yet.
