# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

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
