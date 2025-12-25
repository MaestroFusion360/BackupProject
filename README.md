# BackupProject

BackupProject is a Fusion 360 add-in that exports all files from the active
project into a user-selected folder while preserving the original folder
structure. It supports Fusion data files with `f3d` and `f3z` extensions.

---

- [BackupProject](#backupproject)
  - [Install](#install)
  - [Usage](#usage)
  - [Features](#features)
  - [Notes](#notes)
  - [Configuration](#configuration)
  - [Version](#version)
  - [License](#license)

---

## Install

1. Copy this folder to your Fusion 360 add-ins directory.
2. In Fusion 360, open `Scripts and Add-Ins`.
3. Find `BackupProject` and click `Run` or enable `Run on Startup`.

---

## Usage

1. Open the Design workspace in Fusion 360.
2. Click the `Backup Project` button on the `Backup` panel.
3. Select a destination folder.
4. Wait for the export to finish.

---

## Features

- One-click backup of the active project
- Preserves project folder hierarchy
- Skips unsupported file types
- Progress dialog with cancel support

---

## Notes

- Exports run through Fusion commands, so cloud access and permissions apply.
- Existing files at the target path are skipped.
- Files without a cloud data URL (for example, untitled documents) are reported
  as issues and skipped.

---

## Configuration

Global settings are in `config.py`. The command ID uses `COMPANY_NAME` and
`ADDIN_NAME` from that file.

---

## Version

Current version (see `BackupProject.manifest`).

---

## License

See [LICENSE.md](LICENSE.md)

---
<!-- markdownlint-disable MD033 -->
<p align="center">
  <a href="https://github.com/MaestroFusion360/BackupProject/issues">
    <img src="https://img.shields.io/github/issues/MaestroFusion360/BackupProject" alt="Issues" />
  </a>
  <a href="https://github.com/MaestroFusion360/BackupProject/stargazers">
    <img src="https://img.shields.io/github/stars/MaestroFusion360/BackupProject" alt="Stars" />
  </a>
</p>

<p align="center">
  <img src="https://komarev.com/ghpvc/?username=MaestroFusion360-BackupProject&label=Project+Views&color=blue" alt="Project Views" />
</p>
