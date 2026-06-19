"""
entry.py - main entry point for the Backup Project Fusion 360 add-in.

This version keeps the original "backup active project" workflow, but adds
manifest-based resume support, post-export verification, safer path handling,
and optional STEP export for Fusion design documents.
"""

import datetime
import json
import os
import re
import time

import adsk.core
import adsk.fusion

from ... import config
from ...lib import fusionAddInUtils as futil

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_backup_project'
CMD_NAME = 'Backup Project'
CMD_DESCRIPTION = 'Backup all files from the active project'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'BackupPanel'
PANEL_NAME = 'Backup'
PANEL_AFTER = 'Archive'

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

SUPPORTED_EXTENSIONS = {'f3d', 'f3z'}
MANIFEST_FILENAME = 'backup_manifest.json'

local_handlers = []


def _app():
    return adsk.core.Application.get()


def _ui():
    return _app().userInterface


def _now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def sanitize_name(name):
    """Replace path-invalid characters and trim Windows-unsafe suffixes."""
    cleaned = re.sub(r'[\\/:;?!<>"|*]', '_', name)
    cleaned = cleaned.rstrip('. ')
    return cleaned or '_'


def project_key(project):
    """Prefer a stable project identifier over display name."""
    return getattr(project, 'id', None) or project.name


def _project_name(project):
    try:
        return project.name
    except Exception:
        return '<unknown project>'


def _project_from_folder(folder):
    current_folder = folder
    while current_folder:
        try:
            project = current_folder.parentProject
            if project:
                return project
        except Exception:
            pass

        try:
            if current_folder.isRoot:
                break
        except Exception:
            break

        try:
            current_folder = current_folder.parentFolder
        except Exception:
            break

    return None


def get_active_project_safe(app):
    """Resolve project without trusting app.data.activeProject blindly."""
    try:
        project = app.data.activeProject
        if project:
            futil.log(f'Active project from app.data.activeProject: {_project_name(project)}')
            return project
    except Exception as ex:
        futil.log(f'app.data.activeProject failed: {ex}')

    try:
        document = app.activeDocument
    except Exception as ex:
        futil.log(f'app.activeDocument failed: {ex}')
        document = None

    if not document:
        return None

    try:
        data_file = document.dataFile
    except Exception as ex:
        futil.log(f'activeDocument.dataFile failed: {ex}')
        data_file = None

    if not data_file:
        return None

    try:
        project = data_file.parentProject
        if project:
            futil.log(f'Active project from active document: {_project_name(project)}')
            return project
    except Exception as ex:
        futil.log(f'activeDocument.dataFile.parentProject failed: {ex}')

    try:
        project = _project_from_folder(data_file.parentFolder)
        if project:
            futil.log(f'Active project from active document folder: {_project_name(project)}')
            return project
    except Exception as ex:
        futil.log(f'activeDocument.dataFile.parentFolder failed: {ex}')

    return None


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
        panel = workspace.toolbarPanels.add(PANEL_ID, PANEL_NAME, PANEL_AFTER, False)
    return panel


def _select_backup_folder():
    ui = _ui()
    folder_dialog = ui.createFolderDialog()
    folder_dialog.title = 'Select a folder to save the backup'
    if folder_dialog.showDialog() != adsk.core.DialogResults.DialogOK:
        futil.log('Folder selection canceled.')
        return None
    backup_folder = folder_dialog.folder
    if not os.path.exists(backup_folder):
        os.makedirs(backup_folder)
    return backup_folder


class Manifest:
    """Tracks export progress to support resume and post-run verification."""

    def __init__(self, path):
        self.path = path
        now = _now_iso()
        self.data = {
            'version': 1,
            'created': now,
            'updated': now,
            'projects': {},
        }

    @classmethod
    def load(cls, path):
        inst = cls(path)
        with open(path, 'r', encoding='utf-8') as fh:
            inst.data = json.load(fh)
        return inst

    def save(self):
        self.data['updated'] = _now_iso()
        tmp_path = self.path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def ensure_project(self, key, name, dir_name):
        project = self.data['projects'].setdefault(
            key,
            {
                'name': name,
                'dir_name': dir_name,
                'status': 'in_progress',
                'files': {},
            },
        )
        project['name'] = name
        project['dir_name'] = dir_name
        return project

    def get_project(self, key):
        return self.data['projects'].get(key)

    def ensure_file(self, key, rel_path):
        project = self.data['projects'].get(key)
        if not project:
            raise RuntimeError(f'Project not registered in manifest: {key}')
        project['files'].setdefault(
            rel_path,
            {
                'status': 'pending',
                'size': None,
                'error': None,
                'exported_at': None,
                'step': False,
            },
        )
        return project['files'][rel_path]

    def file_entry(self, key, rel_path):
        project = self.data['projects'].get(key)
        if not project:
            return None
        return project['files'].get(rel_path)

    def file_status(self, key, rel_path):
        entry = self.file_entry(key, rel_path)
        return entry['status'] if entry else None

    def mark_exported(self, key, rel_path, step_ok=False):
        entry = self.data['projects'][key]['files'][rel_path]
        entry['status'] = 'exported'
        entry['size'] = None
        entry['error'] = None
        entry['exported_at'] = _now_iso()
        entry['step'] = step_ok

    def mark_done(self, key, rel_path, size, step_ok=None):
        entry = self.data['projects'][key]['files'][rel_path]
        entry['status'] = 'done'
        entry['size'] = size
        entry['error'] = None
        entry['exported_at'] = entry.get('exported_at') or _now_iso()
        if step_ok is not None:
            entry['step'] = step_ok

    def mark_step(self, key, rel_path, step_ok):
        entry = self.data['projects'][key]['files'][rel_path]
        entry['step'] = step_ok

    def mark_failed(self, key, rel_path, error):
        entry = self.data['projects'][key]['files'][rel_path]
        entry['status'] = 'failed'
        entry['error'] = str(error)
        entry['exported_at'] = entry.get('exported_at') or _now_iso()

    def finalize_project(self, key):
        project = self.data['projects'].get(key)
        if not project:
            return
        statuses = {item['status'] for item in project['files'].values()}
        project['status'] = 'done' if statuses <= {'done'} else 'partial'

    def project_summary(self, key):
        project = self.data['projects'].get(key)
        summary = {'done': 0, 'pending': 0, 'exported': 0, 'failed': 0, 'step_missing': 0}
        if not project:
            return summary
        for entry in project['files'].values():
            status = entry['status']
            if status in summary:
                summary[status] += 1
            if status == 'done' and not entry.get('step'):
                summary['step_missing'] += 1
        return summary

    def count_exported(self, key):
        project = self.data['projects'].get(key)
        if not project:
            return 0
        return sum(1 for entry in project['files'].values() if entry['status'] == 'exported')

    def verify_project(self, key, backup_root):
        """Match exported entries against files on disk and mark them done/failed."""
        project = self.data['projects'].get(key)
        if not project:
            return 0, 0

        verified = 0
        failed = 0
        project_path = os.path.join(backup_root, project['dir_name'])

        disk_map = {}
        disk_by_name = {}
        if os.path.exists(project_path):
            for root, _dirs, files in os.walk(project_path):
                for filename in files:
                    if not filename.lower().endswith(('.f3d', '.f3z')):
                        continue
                    abs_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(abs_path, project_path).replace('\\', '/')
                    size = os.path.getsize(abs_path)
                    disk_map[rel_path] = size
                    disk_by_name[filename] = size

        for rel_path, entry in project['files'].items():
            if entry['status'] != 'exported':
                continue

            size = disk_map.get(rel_path)
            if size is None:
                size = disk_by_name.get(rel_path.rsplit('/', 1)[-1])

            if size and size > 0:
                self.mark_done(key, rel_path, size)
                verified += 1
            else:
                self.mark_failed(key, rel_path, 'not_found_on_disk_after_export')
                failed += 1
                futil.log(f'VERIFY FAIL: {project["name"]}/{rel_path}')

        return verified, failed


def start():
    """Create the command definition and add the toolbar button."""
    cmd_def = _ensure_command_definition()
    futil.add_handler(cmd_def.commandCreated, command_created)

    panel = _ensure_panel()
    control = panel.controls.itemById(CMD_ID)
    if not control:
        control = panel.controls.addCommand(cmd_def)
    control.isPromoted = IS_PROMOTED

    futil.log('Backup add-in started. Button created.')


def stop():
    """Remove the command UI elements and clear handler references."""
    ui = _ui()
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    if panel:
        control = panel.controls.itemById(CMD_ID)
        if control:
            control.deleteMe()
        panel.deleteMe()

    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()

    global local_handlers
    local_handlers = []

    futil.log('Backup add-in stopped. Button removed.')


def command_created(args: adsk.core.CommandCreatedEventArgs):
    """Attach execute and destroy handlers when the command is created."""
    futil.log(f'{CMD_NAME} Command Created Event')
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)


def command_execute(args: adsk.core.CommandEventArgs):
    """Run the backup workflow after the user confirms the command."""
    try:
        app = _app()
        ui = app.userInterface

        active_project = get_active_project_safe(app)
        if not active_project:
            ui.messageBox(
                'Active project not found.\n\n'
                'Open any saved design from the target project, wait until it loads, '
                'then run Backup Project again.\n\n'
                'Fusion failed to provide a valid Data Project context.'
            )
            return

        backup_folder = _select_backup_folder()
        if not backup_folder:
            return

        manifest_path = os.path.join(backup_folder, MANIFEST_FILENAME)
        manifest = None
        resume = False
        key = project_key(active_project)

        if os.path.exists(manifest_path):
            try:
                manifest = Manifest.load(manifest_path)
            except Exception as ex:
                futil.log(f'Manifest read error: {ex}')
                manifest = None

        if manifest and manifest.get_project(key):
            summary = manifest.project_summary(key)
            incomplete = summary['pending'] + summary['exported'] + summary['failed'] + summary['step_missing']
            if incomplete > 0:
                btn = ui.messageBox(
                    f"Existing backup found for '{active_project.name}'.\n\n"
                    f"Done: {summary['done']}\n"
                    f"Pending: {summary['pending']}\n"
                    f"Exported not verified: {summary['exported']}\n"
                    f"Failed: {summary['failed']}\n"
                    f"STEP missing: {summary['step_missing']}\n\n"
                    f'Yes - resume incomplete\n'
                    f'No - start fresh for this project\n'
                    f'Cancel - abort',
                    'Existing Backup',
                    adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType,
                )
                if btn == adsk.core.DialogResults.DialogYes:
                    resume = True
                elif btn == adsk.core.DialogResults.DialogNo:
                    manifest = None
                else:
                    return
            else:
                manifest = None

        futil.log(f'Active project identified: {active_project.name}')
        futil.log(f'Backup folder selected: {backup_folder}')

        backup_processor = BackupProcessor(app, active_project, backup_folder, manifest, resume)
        backup_processor.run()
    except Exception:
        futil.handle_error('command_execute', show_message_box=True)


def command_destroy(args: adsk.core.CommandEventArgs):
    """Release handler references when the command terminates."""
    futil.log(f'{CMD_NAME} Command Destroy Event')

    global local_handlers
    local_handlers = []


class BackupProcessor:
    def __init__(self, app, project, backup_path, manifest, resume):
        self.app = app
        self.ui = app.userInterface
        self.documents = app.documents
        self.project = project
        self.project_key = project_key(project)
        self.backup_path = backup_path
        self.resume = resume
        self.manifest = manifest or Manifest(os.path.join(backup_path, MANIFEST_FILENAME))
        self.failed_files = []
        self.num_issues = 0
        self.skipped = 0
        self.project_dir = self._resolve_project_dir()

    def _resolve_project_dir(self):
        existing = self.manifest.get_project(self.project_key)
        if self.resume and existing and existing.get('dir_name'):
            return existing['dir_name']
        return sanitize_name(self.project.name)

    def run(self):
        """Iterate project files and export supported data to the backup folder."""
        try:
            progress_dialog = self.ui.createProgressDialog()
            progress_dialog.show(
                f"Backing up project '{self.project.name}'", '', 0, 1, 1
            )

            tasks = self._build_task_list()
            self.manifest.save()

            if not tasks and self.manifest.count_exported(self.project_key) == 0:
                progress_dialog.hide()
                self.ui.messageBox('No files to process.')
                return

            progress_dialog.maximumValue = max(len(tasks), 1)
            progress_dialog.reset()

            step_exported = 0
            for idx, task in enumerate(tasks):
                sanitized_name = sanitize_name(task['data_file'].name)
                mode = 'STEP' if task.get('step_only') else 'native+STEP'
                progress_dialog.message = (
                    f'{idx + 1} of {len(tasks)} ({mode}): {sanitized_name}'
                )
                progress_dialog.progressValue = idx + 1

                if progress_dialog.wasCancelled:
                    self.ui.messageBox('Backup operation canceled by user.')
                    break

                if task.get('step_only'):
                    step_ok = self._export_step_only(task['data_file'], task['target_dir'])
                    self.manifest.mark_step(self.project_key, task['rel_path'], step_ok)
                    if step_ok:
                        step_exported += 1
                else:
                    try:
                        self._backup_file(task)
                        entry = self.manifest.file_entry(self.project_key, task['rel_path'])
                        if entry and entry.get('step'):
                            step_exported += 1
                    except Exception as ex:
                        self.num_issues += 1
                        self.failed_files.append(sanitized_name)
                        self.ui.messageBox(
                            f'Error backing up file {sanitized_name}:\n{str(ex)}'
                        )
                    finally:
                        self.manifest.save()

            verified = 0
            verify_failed = 0
            if self.manifest.count_exported(self.project_key) > 0:
                progress_dialog.message = 'Waiting for exports to finish...'
                progress_dialog.progressValue = progress_dialog.maximumValue
                time.sleep(15)
                verified, verify_failed = self.manifest.verify_project(
                    self.project_key, self.backup_path
                )
                self.manifest.save()

            self.manifest.finalize_project(self.project_key)
            self.manifest.save()
            progress_dialog.hide()

            if self.num_issues == 0 and verify_failed == 0:
                self.ui.messageBox(
                    f"Backup of project '{self.project.name}' completed.\n\n"
                    f'Verified on disk: {verified}\n'
                    f'STEP exported: {step_exported}\n'
                    f'Skipped: {self.skipped}\n'
                    f'Manifest: {self.manifest.path}'
                )
            else:
                self.ui.messageBox(
                    f"Backup of project '{self.project.name}' completed with issues.\n\n"
                    f'Export errors: {self.num_issues}\n'
                    f'Not found on disk: {verify_failed}\n'
                    f'Verified on disk: {verified}\n'
                    f'STEP exported: {step_exported}\n'
                    f'Skipped: {self.skipped}\n'
                    f'Manifest: {self.manifest.path}'
                )
                if self.failed_files:
                    futil.log(f"Failed files: {','.join(self.failed_files)}")

        except Exception:
            futil.handle_error('BackupProcessor.run', show_message_box=True)

    def _build_task_list(self):
        self.manifest.ensure_project(self.project_key, self.project.name, self.project_dir)

        tasks = []
        try:
            root_folder = self.project.rootFolder
        except Exception as ex:
            raise RuntimeError(f'Failed to access project root folder: {ex}') from ex

        data_files = self._collect_files(root_folder)
        for data_file in data_files:
            file_ext = data_file.fileExtension.lower()
            if file_ext not in SUPPORTED_EXTENSIONS:
                continue

            rel_path = self._generate_backup_path(data_file)
            target_path = os.path.join(self.backup_path, self.project_dir, rel_path)
            target_dir = os.path.dirname(target_path)

            self.manifest.ensure_file(self.project_key, rel_path)
            entry = self.manifest.file_entry(self.project_key, rel_path)
            status = entry['status']

            if self.resume:
                if status == 'done':
                    if not entry.get('step'):
                        tasks.append(
                            {
                                'data_file': data_file,
                                'rel_path': rel_path,
                                'target_dir': target_dir,
                                'step_only': True,
                            }
                        )
                    else:
                        self.skipped += 1
                    continue
                if status == 'exported':
                    continue

            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                self.manifest.mark_done(
                    self.project_key,
                    rel_path,
                    os.path.getsize(target_path),
                    step_ok=entry.get('step', False),
                )
                self.skipped += 1
                continue

            tasks.append(
                {
                    'data_file': data_file,
                    'rel_path': rel_path,
                    'target_dir': target_dir,
                    'step_only': False,
                }
            )

        return tasks

    def _collect_files(self, folder):
        """Recursively gather all data files from the given folder."""
        files = list(folder.dataFiles)
        for subfolder in folder.dataFolders:
            files.extend(self._collect_files(subfolder))
        return files

    def _backup_file(self, task):
        """Open a data file, export it, and close the document."""
        data_file = task['data_file']
        sanitized_name = sanitize_name(data_file.name)
        file_ext = data_file.fileExtension.lower()
        target_dir = task['target_dir']

        futil.log(f'Starting backup for file: {sanitized_name}')

        document = None
        try:
            document = self.documents.open(data_file, False)
            if not document:
                raise RuntimeError(f'Failed to open file: {sanitized_name}')

            document.activate()
            if not document.dataFile:
                raise RuntimeError(
                    f'No data file URL for document: {document.name or "Untitled"}'
                )

            self.app.executeTextCommand('Document.RemoveLinks')
            futil.log('Links removed from document.')

            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
                futil.log(f'Target directory created: {target_dir}')

            self._export_file(file_ext, target_dir)
            step_ok = self._try_step_export(document, file_ext, target_dir, sanitized_name)

            self.manifest.mark_exported(self.project_key, task['rel_path'], step_ok)

            futil.log(f'File exported to: {target_dir}')
            futil.log(f'Backup completed successfully for file: {sanitized_name}')

        except Exception as ex:
            futil.log(f'Error during backup for file {sanitized_name}: {str(ex)}')
            self.manifest.mark_failed(self.project_key, task['rel_path'], str(ex))
            raise

        finally:
            if document:
                try:
                    document.close(False)
                    futil.log(f'Document closed for file: {sanitized_name}')
                except Exception:
                    pass

    def _export_file(self, file_ext, target_dir):
        """Run the Fusion export command for the given extension."""
        if file_ext == 'f3d':
            self.app.executeTextCommand(f'data.fileExport f3d "{target_dir}"')
        elif file_ext == 'f3z':
            self.app.executeTextCommand(f'data.fileExport f3z "{target_dir}"')

    def _export_step_only(self, data_file, target_dir):
        """Open a data file and export STEP without re-exporting native format."""
        sanitized_name = sanitize_name(data_file.name)
        document = None
        try:
            document = self.documents.open(data_file, False)
            if not document:
                futil.log(f'STEP-only open failed: {sanitized_name}')
                return False
            document.activate()
            os.makedirs(target_dir, exist_ok=True)
            return self._try_step_export(
                document,
                data_file.fileExtension.lower(),
                target_dir,
                sanitized_name,
            )
        except Exception as ex:
            futil.log(f'STEP-only export failed for {sanitized_name}: {ex}')
            return False
        finally:
            if document:
                try:
                    document.close(False)
                except Exception:
                    pass

    def _try_step_export(self, document, file_ext, target_dir, display_name):
        """Export STEP if the opened document exposes a Fusion design."""
        try:
            try:
                product = self.app.activeProduct
            except Exception as ex:
                futil.log(f'activeProduct failed for {display_name}: {ex}')
                return False

            design = adsk.fusion.Design.cast(product)
            if not design:
                futil.log(f'Not a Design document, STEP skipped: {display_name}')
                return True

            step_base = sanitize_name(document.name)
            if step_base.lower().endswith(f'.{file_ext}'):
                step_base = step_base[:-(len(file_ext) + 1)]
            step_path = os.path.join(target_dir, f'{step_base}.step')

            options = design.exportManager.createSTEPExportOptions(step_path)
            design.exportManager.execute(options)
            futil.log(f'STEP exported: {step_path}')
            return True
        except Exception as ex:
            futil.log(f'STEP export failed for {display_name}: {ex}')
            return False

    def _generate_backup_path(self, data_file):
        """Build a relative backup path from the project folder tree."""
        folder_path = []
        current_folder = data_file.parentFolder

        while current_folder and not current_folder.isRoot:
            folder_path.insert(0, sanitize_name(current_folder.name))
            current_folder = current_folder.parentFolder

        sanitized_name = sanitize_name(data_file.name)
        file_ext = data_file.fileExtension.lower()
        if sanitized_name.lower().endswith(f'.{file_ext}'):
            file_name = sanitized_name
        else:
            file_name = f'{sanitized_name}.{file_ext}'

        directory_path = os.path.join(*folder_path) if folder_path else ''
        full_path = os.path.join(directory_path, file_name).replace('\\', '/')

        futil.log(f'Generated path: {full_path}')
        return full_path


def sanitize_file_name(file_name):
    """Backward-compatible alias for older call sites."""
    return sanitize_name(file_name)
