"""
entry.py — main entry point for the Backup Project Fusion 360 add-in.

This file is responsible for registering the Backup Project command, creating
the UI panel and button in Fusion 360, and managing the lifecycle of the add-in.
It initializes the command, handles its execution trigger, and launches the
backup workflow for the active project.
"""

import os
import re
import adsk.core
from ...lib import fusionAddInUtils as futil
from ... import config

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

local_handlers = []


def _app():
    return adsk.core.Application.get()


def _ui():
    return _app().userInterface


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

        active_project = app.data.activeProject
        if not active_project:
            ui.messageBox('Active project not found.')
            return

        futil.log(f'Active project identified: {active_project.name}')

        backup_folder = _select_backup_folder()
        if not backup_folder:
            return

        futil.log(f'Backup folder selected: {backup_folder}')

        backup_processor = BackupProcessor(app, active_project, backup_folder)
        backup_processor.run()

        ui.messageBox(
            f"Backup of project '{active_project.name}' completed. "
            f'Files saved to: {backup_folder}'
        )
    except Exception:
        futil.handle_error('command_execute', show_message_box=True)


def command_destroy(args: adsk.core.CommandEventArgs):
    """Release handler references when the command terminates."""
    futil.log(f'{CMD_NAME} Command Destroy Event')

    global local_handlers
    local_handlers = []


class BackupProcessor:
    def __init__(self, app, project, backup_path):
        self.app = app
        self.ui = app.userInterface
        self.documents = app.documents
        self.project = project
        self.backup_path = backup_path
        self.failed_files = []
        self.num_issues = 0

    def run(self):
        """Iterate project files and export supported data to the backup folder."""
        try:
            progress_dialog = self.ui.createProgressDialog()
            progress_dialog.show(
                f"Backing up project '{self.project.name}'", '', 0, 1, 1
            )

            files_to_backup = self._collect_files(self.project.rootFolder)
            progress_dialog.maximumValue = len(files_to_backup)
            progress_dialog.reset()

            for idx, data_file in enumerate(files_to_backup):
                sanitized_name = sanitize_file_name(data_file.name)
                progress_dialog.message = (
                    f'Backing up file {idx + 1} of {len(files_to_backup)}: '
                    f'{sanitized_name}'
                )
                progress_dialog.progressValue = idx + 1

                if progress_dialog.wasCancelled:
                    self.ui.messageBox('Backup operation canceled by user.')
                    break

                try:
                    self._backup_file(data_file)
                except Exception as ex:
                    self.ui.messageBox(
                        f'Error backing up file {sanitized_name}:\n{str(ex)}'
                    )
                    self.num_issues += 1

            progress_dialog.hide()
            if self.num_issues == 0:
                self.ui.messageBox('Backup completed successfully with no issues.')
            else:
                self.ui.messageBox(
                    f'Backup completed with {self.num_issues} issues.'
                )
                if self.failed_files:
                    futil.log(f"Failed files: {','.join(self.failed_files)}")

        except Exception:
            futil.handle_error('BackupProcessor.run', show_message_box=True)

    def _collect_files(self, folder):
        """Recursively gather all data files from the given folder."""
        files = list(folder.dataFiles)
        for subfolder in folder.dataFolders:
            files.extend(self._collect_files(subfolder))
        return files

    def _backup_file(self, data_file):
        """Open a data file, export it, and close the document."""
        sanitized_name = sanitize_file_name(data_file.name)
        file_ext = data_file.fileExtension.lower()
        if file_ext not in SUPPORTED_EXTENSIONS:
            futil.log(f'Skipping unsupported file type: {sanitized_name}')
            return

        relative_path = self._generate_backup_path(data_file, sanitized_name)
        target_path = os.path.join(self.backup_path, relative_path)
        target_dir = os.path.dirname(target_path)

        if os.path.exists(target_path):
            futil.log(f'Skipping file: {sanitized_name}')
            return

        futil.log(f'Starting backup for file: {sanitized_name}')

        document = None
        try:
            document = self.documents.open(data_file, False)
            if not document:
                raise Exception(f'Failed to open file: {sanitized_name}')

            document.activate()
            if not document.dataFile:
                raise Exception(
                    f'No data file URL for document: {document.name or "Untitled"}'
                )
            self.app.executeTextCommand('Document.RemoveLinks')
            futil.log('Links removed from document.')

            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
                futil.log(f'Target directory created: {target_dir}')

            self._export_file(file_ext, target_dir)

            futil.log(f'File exported to: {target_dir}')
            futil.log(f'Backup completed successfully for file: {sanitized_name}')

        except Exception as ex:
            futil.log(f'Error during backup for file {sanitized_name}: {str(ex)}')
            self.failed_files.append(sanitized_name)
            raise

        finally:
            if document and document.isActive:
                document.close(False)
                futil.log(f'Document closed for file: {sanitized_name}')

    def _export_file(self, file_ext, target_dir):
        """Run the Fusion export command for the given extension."""
        if file_ext == 'f3d':
            self.app.executeTextCommand(f'data.fileExport f3d "{target_dir}"')
        elif file_ext == 'f3z':
            self.app.executeTextCommand(f'data.fileExport f3z "{target_dir}"')

    def _generate_backup_path(self, data_file, sanitized_name):
        """Build a relative backup path from the project folder tree."""
        current_folder = data_file.parentFolder
        folder_path = []

        while current_folder and not current_folder.isRoot:
            folder_path.insert(0, current_folder.name)
            current_folder = current_folder.parentFolder

        directory_path = os.path.join(*folder_path) if folder_path else ''
        full_path = os.path.join(
            directory_path, f'{sanitized_name}.{data_file.fileExtension}'
        ).replace('\\', '/')

        futil.log(f'Generated path: {full_path}')
        return full_path


def sanitize_file_name(file_name):
    """Replace invalid path characters with underscores."""
    return re.sub(r'[\\/:;?!<>"|*]', '_', file_name)
