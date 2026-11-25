# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [1.2.2] - 2026-11-25
### Fixed
- Designs with external references were saved as `name.f3z.f3d`. Fusion's API always writes a standard `.f3d` archive and appended `.f3d` to the `.f3z` name it was given. They are now saved as `name.f3d`.
- When Fusion's `execute()` returns False but has written a valid, non-empty file, the export is recorded as successful (with a log note) instead of partial. Seen on large assemblies.
- An export that reports success but writes no file is now recorded as a failure.

### Removed
- The `.f3d` → `.f3z` retry from 1.1.1. It never produced a different file.

## [1.2.1] - 2026-11-25
### Fixed
- Settings dialog: wider default size so the output path is readable, the Browse button no longer repeats its label, and the description is no longer cut off.

## [1.2.0] - 2026-11-25
### Added
- Non-Fusion files (DXF, PDF, CAM/Nest templates, etc.) are downloaded as-is to `Originals/` (setting: "Download non-Fusion files", on by default). Files that older versions skipped are picked up on the next run.

### Fixed
- Uploaded `.dxf`/`.dwg` files are no longer opened as designs. That always failed with "not a valid zip package".

## [1.1.1] - 2026-11-25
### Fixed
- When `.f3d` export returns False (assemblies that reference other designs and aren't detected), retry as `.f3z`.

## [1.1.0] - 2026-11-25
### Added
- A settings dialog before every run: output folder, export formats (Fusion archive, STEP, IGES, SAT, SMT, STL, 3MF, drawing PDF), defer drawings, separate Imported folder, always-mine and always-imported patterns, and projects to skip. Settings are saved to `settings.json`.
- STEP alternatives (IGES, SAT, SMT) and mesh formats (STL, 3MF).
- Option to keep imported models under `Designs/`.
- Adapter tests against a stub Fusion API, and CI on Linux, macOS and Windows.

### Changed
- Defaults no longer include any user-specific name patterns.
- `defer_drawings` now defaults to off.
- Legacy `archiver_config.json` is still read when no `settings.json` exists.

## [1.0.3] - 2026-11-25
### Added
- `defer_drawings` option, and timing for each step of a drawing export in the log.

## [1.0.2] - 2026-11-25
### Fixed
- Drawing PDF export failing with "failed to find product".

## [1.0.1] - 2026-11-25
### Added
- The progress window now opens at the start of a run, including the project-listing phase, and Stop works while listing.

## [1.0.0] - 2026-11-24
### Added
- First release. Exports the latest version of each document. Classifies designs vs. imported models. Writes an inventory. Can stop and resume.
