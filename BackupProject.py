"""
Fusion 360 Add-in: Backup Project

This add-in adds a "Backup Project" command to Fusion 360, enabling users to
export and save all supported files from the currently active Fusion 360 project
to a user-defined local folder. The add-in preserves the project folder
structure, sanitizes invalid file names, displays progress feedback, and reports
any issues encountered during the backup process.
"""
from . import commands
from .lib import fusionAddInUtils as futil


def run(context):
    try:
        # This will run the start function in each of your commands as defined in commands/__init__.py
        commands.start()

    except:
        futil.handle_error('run')


def stop(context):
    try:
        # Remove all of the event handlers your app has created
        futil.clear_handlers()

        # This will run the start function in each of your commands as defined in commands/__init__.py
        commands.stop()

    except:
        futil.handle_error('stop')