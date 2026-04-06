"""
entry.py — Backup Project add-in for Fusion 360.

Exports supported files (f3d, f3z) from selected projects to a local folder.
Preserves project folder hierarchy. Tracks progress via JSON manifest.
Supports resume after interruption and post-export verification.
"""

import os
import re
import json
import time
import datetime
import adsk.core
import adsk.fusion
from ...lib import fusionAddInUtils as futil
from ... import config

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_backup_project'
CMD_NAME = 'Backup Project'
CMD_DESCRIPTION = 'Backup files from selected Fusion 360 projects'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'BackupPanel'
PANEL_NAME = 'Backup'
PANEL_AFTER = 'Archive'

ICON_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'resources', ''
)

SUPPORTED_EXTENSIONS = {'f3d', 'f3z'}
MANIFEST_FILENAME = 'backup_manifest.json'

local_handlers = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app():
    return adsk.core.Application.get()


def _ui():
    return _app().userInterface


def _now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def sanitize_name(name):
    """Replace characters invalid in Windows paths with underscores.

    Also strips trailing dots and spaces — Windows silently removes them
    when creating directories, causing path mismatches.
    """
    cleaned = re.sub(r'[\\/:;?!<>"|*]', '_', name)
    cleaned = cleaned.rstrip('. ')
    return cleaned or '_'


def _select_backup_folder():
    ui = _ui()
    dlg = ui.createFolderDialog()
    dlg.title = 'Select backup destination folder'
    if dlg.showDialog() != adsk.core.DialogResults.DialogOK:
        futil.log('Folder selection canceled.')
        return None
    folder = dlg.folder
    if not os.path.exists(folder):
        os.makedirs(folder)
    return folder


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class Manifest:
    """JSON file that tracks export status of every file in the backup.

    Saved after each file export so that interrupted runs can be resumed.
    """

    def __init__(self, path):
        self.path = path
        self.data = {
            'version': 1,
            'created': _now_iso(),
            'updated': _now_iso(),
            'projects': {}
        }

    @classmethod
    def load(cls, path):
        inst = cls(path)
        with open(path, 'r', encoding='utf-8') as fh:
            inst.data = json.load(fh)
        return inst

    def save(self):
        self.data['updated'] = _now_iso()
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        # Atomic replace — prevents corrupt manifest if Fusion crashes
        # mid-write.  os.replace is atomic on Windows NTFS.
        os.replace(tmp, self.path)

    # -- mutators ----------------------------------------------------------

    def ensure_project(self, project_name, dir_name):
        """Register a project with its unique directory name."""
        proj = self.data['projects'].setdefault(
            project_name,
            {'status': 'in_progress', 'dir_name': dir_name, 'files': {}}
        )
        # Always keep dir_name in sync (fresh start may reassign)
        proj['dir_name'] = dir_name

    def ensure_file(self, project_name, rel_path):
        """Register a file in the manifest if not already present."""
        proj = self.data['projects'].get(project_name)
        if not proj:
            raise RuntimeError(
                f'Project not registered in manifest: {project_name}'
            )
        if rel_path not in proj['files']:
            proj['files'][rel_path] = {
                'status': 'pending',
                'size': None,
                'error': None,
                'exported_at': None,
                'step': False
            }

    def mark_exported(self, project_name, rel_path, step_ok=False):
        """Mark a file as exported (awaiting disk verification)."""
        entry = self.data['projects'][project_name]['files'][rel_path]
        entry['status'] = 'exported'
        entry['error'] = None
        entry['exported_at'] = _now_iso()
        entry['step'] = step_ok

    def mark_done(self, project_name, rel_path, size):
        entry = self.data['projects'][project_name]['files'][rel_path]
        entry['status'] = 'done'
        entry['size'] = size
        entry['error'] = None
        entry['exported_at'] = entry.get('exported_at') or _now_iso()

    def mark_step(self, project_name, rel_path, step_ok):
        """Update STEP export status for a file."""
        entry = self.data['projects'][project_name]['files'][rel_path]
        entry['step'] = step_ok

    def mark_failed(self, project_name, rel_path, error):
        entry = self.data['projects'][project_name]['files'][rel_path]
        entry['status'] = 'failed'
        entry['error'] = str(error)
        entry['exported_at'] = entry.get('exported_at') or _now_iso()

    def finalize_project(self, project_name):
        """Set project-level status based on its files."""
        proj = self.data['projects'].get(project_name)
        if not proj:
            return
        statuses = {f['status'] for f in proj['files'].values()}
        proj['status'] = 'done' if (not statuses or statuses == {'done'}) else 'partial'

    # -- queries -----------------------------------------------------------

    def file_status(self, project_name, rel_path):
        proj = self.data['projects'].get(project_name)
        if not proj:
            return None
        entry = proj['files'].get(rel_path)
        return entry['status'] if entry else None

    def project_dir(self, project_name):
        """Return the directory name assigned to a project."""
        proj = self.data['projects'].get(project_name)
        if proj and 'dir_name' in proj:
            return proj['dir_name']
        return sanitize_name(project_name)

    def count_incomplete(self):
        pending = failed = 0
        for proj in self.data['projects'].values():
            for f in proj['files'].values():
                if f['status'] in ('pending', 'exported'):
                    pending += 1
                elif f['status'] == 'failed':
                    failed += 1
        return pending, failed

    def count_done(self):
        total = 0
        for proj in self.data['projects'].values():
            for f in proj['files'].values():
                if f['status'] == 'done':
                    total += 1
        return total

    def count_step_missing(self):
        """Count 'done' files that still need STEP export."""
        total = 0
        for proj in self.data['projects'].values():
            for f in proj['files'].values():
                if f['status'] == 'done' and not f.get('step'):
                    total += 1
        return total

    def verify(self, backup_root):
        """Scan disk and match actual files to manifest entries.

        For each project:
        1. Collect all .f3d/.f3z files on disk under the project dir.
        2. For each manifest entry with status 'exported':
           - Try exact relative path match.
           - If not found, try matching by filename only (handles
             slashes-in-name creating subdirectories, etc.).
           - If matched with non-zero size → mark 'done'.
           - If not matched → mark 'failed'.

        Returns (verified_count, failed_count).
        """
        verified = 0
        failed = 0

        for proj_name, proj in self.data['projects'].items():
            proj_dir = proj.get('dir_name', sanitize_name(proj_name))
            proj_path = os.path.join(backup_root, proj_dir)

            # Collect actual files on disk: {relative_path: abs_path}
            disk_map = {}       # rel_path → (abs_path, size)
            disk_by_name = {}   # filename → (abs_path, size)
            if os.path.exists(proj_path):
                for root, _dirs, files in os.walk(proj_path):
                    for f in files:
                        if not f.lower().endswith(('.f3d', '.f3z')):
                            continue
                        abs_p = os.path.join(root, f)
                        rel_p = os.path.relpath(abs_p, proj_path)
                        rel_p = rel_p.replace('\\', '/')
                        sz = os.path.getsize(abs_p)
                        disk_map[rel_p] = (abs_p, sz)
                        disk_by_name[f] = (abs_p, sz)

            for rel_path, entry in proj['files'].items():
                if entry['status'] != 'exported':
                    continue

                # Try 1: exact relative path
                match = disk_map.get(rel_path)

                # Try 2: filename only (last component of rel_path)
                if not match:
                    fname = rel_path.rsplit('/', 1)[-1]
                    match = disk_by_name.get(fname)

                if match:
                    _abs, sz = match
                    if sz > 0:
                        entry['status'] = 'done'
                        entry['size'] = sz
                        entry['error'] = None
                        verified += 1
                        continue

                entry['status'] = 'failed'
                entry['error'] = 'not_found_on_disk_after_export'
                failed += 1
                futil.log(f'VERIFY FAIL: {proj_name}/{rel_path}')

        return verified, failed


# ---------------------------------------------------------------------------
# UI lifecycle
# ---------------------------------------------------------------------------

def _ensure_command_definition():
    ui = _ui()
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if not cmd_def:
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_DESCRIPTION, ICON_FOLDER
        )
    return cmd_def


def _ensure_panel():
    ui = _ui()
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    if not panel:
        panel = workspace.toolbarPanels.add(
            PANEL_ID, PANEL_NAME, PANEL_AFTER, False
        )
    return panel


def start():
    """Create the command definition and add the toolbar button."""
    cmd_def = _ensure_command_definition()
    futil.add_handler(cmd_def.commandCreated, command_created)
    panel = _ensure_panel()
    ctrl = panel.controls.itemById(CMD_ID)
    if not ctrl:
        ctrl = panel.controls.addCommand(cmd_def)
    ctrl.isPromoted = IS_PROMOTED
    futil.log('Backup add-in started.')


def stop():
    """Remove the command UI elements and clear handler references."""
    ui = _ui()
    ws = ui.workspaces.itemById(WORKSPACE_ID)
    panel = ws.toolbarPanels.itemById(PANEL_ID)
    if panel:
        ctrl = panel.controls.itemById(CMD_ID)
        if ctrl:
            ctrl.deleteMe()
        panel.deleteMe()
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()
    global local_handlers
    local_handlers = []
    futil.log('Backup add-in stopped.')


# ---------------------------------------------------------------------------
# Command events
# ---------------------------------------------------------------------------

def command_created(args: adsk.core.CommandCreatedEventArgs):
    """Build the project-selection dialog."""
    futil.log(f'{CMD_NAME}: command_created')
    cmd = args.command
    inputs = cmd.commandInputs

    # «Select All» toggle
    inputs.addBoolValueInput('select_all', 'Select All', True, '', False)

    # Per-project checkboxes — active project pre-selected
    app = _app()
    projects = app.data.dataProjects
    active = app.data.activeProject
    active_id = active.id if active else None

    for i in range(projects.count):
        proj = projects.item(i)
        inputs.addBoolValueInput(
            f'project_{i}', proj.name, True, '',
            proj.id == active_id
        )

    futil.add_handler(
        cmd.inputChanged, command_input_changed,
        local_handlers=local_handlers
    )
    futil.add_handler(
        cmd.execute, command_execute,
        local_handlers=local_handlers
    )
    futil.add_handler(
        cmd.destroy, command_destroy,
        local_handlers=local_handlers
    )


def command_input_changed(args: adsk.core.InputChangedEventArgs):
    """Toggle all project checkboxes when 'Select All' changes."""
    if args.input.id != 'select_all':
        return
    value = args.input.value
    inputs = args.inputs
    for i in range(inputs.count):
        inp = inputs.item(i)
        if inp.id.startswith('project_'):
            inp.value = value


def command_execute(args: adsk.core.CommandEventArgs):
    """Run the backup workflow after the user confirms."""
    try:
        app = _app()
        ui = app.userInterface
        inputs = args.command.commandInputs

        # Collect selected projects
        all_projects = app.data.dataProjects
        selected = []
        for i in range(all_projects.count):
            inp = inputs.itemById(f'project_{i}')
            if inp and inp.value:
                selected.append(all_projects.item(i))

        if not selected:
            ui.messageBox('No projects selected.')
            return

        # Destination folder
        backup_folder = _select_backup_folder()
        if not backup_folder:
            return

        # Check for existing manifest
        manifest_path = os.path.join(backup_folder, MANIFEST_FILENAME)
        manifest = None
        resume = False

        if os.path.exists(manifest_path):
            try:
                manifest = Manifest.load(manifest_path)
            except Exception as ex:
                futil.log(f'Manifest read error: {ex}')
                manifest = None

        if manifest:
            pending, failed = manifest.count_incomplete()
            done = manifest.count_done()
            step_missing = manifest.count_step_missing()

            if pending + failed + step_missing > 0:
                btn = ui.messageBox(
                    f'Existing backup found.\n\n'
                    f'Done: {done}\n'
                    f'Pending: {pending}\n'
                    f'Failed: {failed}\n'
                    f'STEP missing: {step_missing}\n\n'
                    f'Yes \u2014 resume incomplete\n'
                    f'No \u2014 start fresh\n'
                    f'Cancel \u2014 abort',
                    'Existing Backup',
                    adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType
                )
                if btn == adsk.core.DialogResults.DialogYes:
                    resume = True
                elif btn == adsk.core.DialogResults.DialogNo:
                    manifest = None
                else:
                    return
            else:
                manifest = None

        processor = BackupProcessor(
            app, selected, backup_folder, manifest, resume
        )
        processor.run()

    except Exception:
        futil.handle_error('command_execute', show_message_box=True)


def command_destroy(args: adsk.core.CommandEventArgs):
    """Release handler references when the command terminates."""
    futil.log(f'{CMD_NAME}: command_destroy')
    global local_handlers
    local_handlers = []


# ---------------------------------------------------------------------------
# Backup processor
# ---------------------------------------------------------------------------

class BackupProcessor:
    """Iterates selected projects, exports supported files, maintains
    the manifest, and verifies results."""

    def __init__(self, app, projects, backup_path, manifest, resume):
        self.app = app
        self.ui = app.userInterface
        self.documents = app.documents
        self.projects = projects
        self.backup_path = backup_path
        self.resume = resume
        self.manifest = manifest or Manifest(
            os.path.join(backup_path, MANIFEST_FILENAME)
        )
        self.skipped = 0
        # Unique directory name per project (handles collisions)
        self.project_dir_map = self._resolve_project_dirs()

    def _resolve_project_dirs(self):
        """Assign unique sanitized directory names to each project.

        If two projects sanitize to the same name, a numeric suffix is
        appended: ``Project``, ``Project_2``, ``Project_3``, etc.

        In resume mode, directory names already recorded in the manifest
        take priority to maintain consistency with a previous run.
        """
        mapping = {}
        used = set()

        # Preserve dirs from a previous manifest (resume)
        if self.resume:
            for proj in self.projects:
                proj_data = self.manifest.data['projects'].get(proj.name)
                if proj_data and 'dir_name' in proj_data:
                    mapping[proj.name] = proj_data['dir_name']
                    used.add(proj_data['dir_name'])

        # Assign dirs for remaining projects
        for proj in self.projects:
            if proj.name in mapping:
                continue
            base = sanitize_name(proj.name)
            candidate = base
            counter = 2
            while candidate in used:
                candidate = f'{base}_{counter}'
                counter += 1
            mapping[proj.name] = candidate
            used.add(candidate)

        return mapping

    def run(self):
        try:
            progress = self.ui.createProgressDialog()
            progress.show('Collecting files\u2026', '', 0, 1, 1)

            tasks = self._build_task_list()
            self.manifest.save()

            if not tasks:
                progress.hide()
                self.ui.messageBox('No files to process.')
                return

            # ── Phase 1: Export ───────────────────────────────────
            progress.maximumValue = len(tasks)
            progress.reset()

            export_errors = 0
            step_exported = 0
            has_full_exports = False

            for idx, task in enumerate(tasks):
                display = sanitize_name(task['data_file'].name)
                mode = 'STEP' if task.get('step_only') else 'f3d+STEP'
                progress.message = (
                    f'[{task["project_name"]}] '
                    f'{idx + 1}/{len(tasks)} ({mode}): {display}'
                )
                progress.progressValue = idx + 1

                if progress.wasCancelled:
                    self.ui.messageBox('Backup canceled by user.')
                    break

                if task.get('step_only'):
                    self._process_task(task)
                    self.manifest.save()
                    entry = self.manifest.data['projects'][
                        task['project_name']]['files'][task['rel_path']]
                    if entry.get('step'):
                        step_exported += 1
                else:
                    has_full_exports = True
                    self._process_task(task)
                    self.manifest.save()
                    if self.manifest.file_status(
                        task['project_name'], task['rel_path']
                    ) == 'failed':
                        export_errors += 1

            # ── Phase 2: Verify ──────────────────────────────────
            verified = 0
            verify_failed = 0

            if has_full_exports:
                # data.fileExport is async — files may still be writing.
                progress.message = 'Waiting for exports to finish\u2026'
                progress.progressValue = progress.maximumValue
                time.sleep(15)

                verified, verify_failed = self.manifest.verify(
                    self.backup_path
                )

            for proj in self.projects:
                self.manifest.finalize_project(proj.name)
            self.manifest.save()

            progress.hide()

            self.ui.messageBox(
                f'Backup complete.\n\n'
                f'Export errors: {export_errors}\n'
                f'Verified on disk: {verified}\n'
                f'Not found on disk: {verify_failed}\n'
                f'STEP added: {step_exported}\n'
                f'Skipped (already done): {self.skipped}\n\n'
                f'Manifest saved to:\n{self.manifest.path}'
            )

        except Exception:
            futil.handle_error('BackupProcessor.run', show_message_box=True)

    # -- task list ---------------------------------------------------------

    def _build_task_list(self):
        """Scan all selected projects, register files in the manifest,
        and return a list of tasks to execute."""
        tasks = []
        for proj in self.projects:
            proj_name = proj.name
            proj_dir = self.project_dir_map[proj_name]
            self.manifest.ensure_project(proj_name, proj_dir)
            data_files = self._collect_data_files(proj.rootFolder)

            for df in data_files:
                ext = df.fileExtension.lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue

                rel = self._relative_path(df)
                self.manifest.ensure_file(proj_name, rel)

                if self.resume:
                    status = self.manifest.file_status(proj_name, rel)

                    if status == 'exported':
                        self.skipped += 1
                        continue

                    if status == 'done':
                        # f3d is done — check if STEP is missing
                        entry = self.manifest.data['projects'][
                            proj_name]['files'][rel]
                        if not entry.get('step'):
                            tasks.append({
                                'project_name': proj_name,
                                'project_dir': proj_dir,
                                'data_file': df,
                                'rel_path': rel,
                                'step_only': True,
                            })
                        else:
                            self.skipped += 1
                        continue

                tasks.append({
                    'project_name': proj_name,
                    'project_dir': proj_dir,
                    'data_file': df,
                    'rel_path': rel,
                    'step_only': False,
                })
        return tasks

    # -- single file export ------------------------------------------------

    def _process_task(self, task):
        """Export one file. Marks 'exported' on success, 'failed' on error.

        If step_only=True, opens the document only for STEP export
        without re-exporting the native f3d/f3z.
        """
        proj_name = task['project_name']
        rel = task['rel_path']
        proj_dir = task['project_dir']
        step_only = task.get('step_only', False)

        export_dir = os.path.join(
            self.backup_path, proj_dir, os.path.dirname(rel)
        )

        if step_only:
            try:
                step_ok = self._export_step_only(task['data_file'], export_dir)
                self.manifest.mark_step(proj_name, rel, step_ok)
            except Exception as ex:
                futil.log(f'STEP-ONLY FAIL {proj_name}/{rel}: {ex}')
        else:
            try:
                step_ok = self._export_single(task['data_file'], export_dir)
                self.manifest.mark_exported(proj_name, rel, step_ok)
            except Exception as ex:
                self.manifest.mark_failed(proj_name, rel, str(ex))
                futil.log(f'FAIL {proj_name}/{rel}: {ex}')

    def _export_single(self, data_file, target_dir):
        """Open a data file in Fusion, remove links, export native + STEP,
        then close.

        Returns True if STEP export succeeded, False otherwise.
        """
        ext = data_file.fileExtension.lower()
        name = sanitize_name(data_file.name)
        step_ok = False
        doc = None
        try:
            doc = self.documents.open(data_file, False)
            if not doc:
                raise RuntimeError(f'Cannot open: {name}')

            doc.activate()
            if not doc.dataFile:
                raise RuntimeError(f'No cloud data URL: {name}')

            self.app.executeTextCommand('Document.RemoveLinks')

            os.makedirs(target_dir, exist_ok=True)

            # Native format export (f3d/f3z) — async text command
            self.app.executeTextCommand(
                f'data.fileExport {ext} "{target_dir}"'
            )

            # STEP export — synchronous via ExportManager
            step_ok = self._try_step_export(doc, ext, target_dir, name)

            futil.log(f'Exported: {name} -> {target_dir}')

        finally:
            if doc:
                try:
                    doc.close(False)
                except Exception:
                    pass
        return step_ok

    def _export_step_only(self, data_file, target_dir):
        """Open a data file in Fusion, export STEP only, close.

        Returns True if STEP export succeeded, False otherwise.
        """
        ext = data_file.fileExtension.lower()
        name = sanitize_name(data_file.name)
        step_ok = False
        doc = None
        try:
            doc = self.documents.open(data_file, False)
            if not doc:
                futil.log(f'STEP-ONLY: Cannot open: {name}')
                return False

            doc.activate()

            os.makedirs(target_dir, exist_ok=True)
            step_ok = self._try_step_export(doc, ext, target_dir, name)

        finally:
            if doc:
                try:
                    doc.close(False)
                except Exception:
                    pass
        return step_ok

    def _try_step_export(self, doc, ext, target_dir, name):
        """Attempt STEP export for the active document.

        Returns True on success, False on failure.
        """
        try:
            design = adsk.fusion.Design.cast(self.app.activeProduct)
            if not design:
                futil.log(f'Not a Design document, STEP skipped: {name}')
                # Return True — nothing to export, don't retry.
                return True

            step_base = sanitize_name(doc.name)
            if step_base.lower().endswith(f'.{ext}'):
                step_base = step_base[:-(len(ext) + 1)]
            step_path = os.path.join(target_dir, f'{step_base}.step')

            options = design.exportManager.createSTEPExportOptions(step_path)
            design.exportManager.execute(options)
            futil.log(f'STEP exported: {step_base}.step')
            return True

        except Exception as ex:
            futil.log(f'STEP export failed for {name}: {ex}')
            return False

    # -- helpers -----------------------------------------------------------

    def _collect_data_files(self, folder):
        """Recursively gather all data files from the given folder."""
        result = list(folder.dataFiles)
        for sub in folder.dataFolders:
            result.extend(self._collect_data_files(sub))
        return result

    def _relative_path(self, data_file):
        """Build a relative path from project root, with sanitized names
        for both folders and the file itself."""
        parts = []
        folder = data_file.parentFolder
        while folder and not folder.isRoot:
            parts.insert(0, sanitize_name(folder.name))
            folder = folder.parentFolder
        name = sanitize_name(data_file.name)
        ext = data_file.fileExtension.lower()
        # Avoid double extension: if name already ends with .f3d, don't
        # add .f3d again.
        if name.lower().endswith(f'.{ext}'):
            file_part = name
        else:
            file_part = f'{name}.{ext}'
        if parts:
            return os.path.join(*parts, file_part).replace('\\', '/')
        return file_part
