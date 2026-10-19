"""Platform-specific subprocess flags shared by every ffmpeg/ffprobe/rclone spawn."""
import subprocess
import sys

# Text-mode pipes from our tools (ffprobe JSON, rclone listings, stderr) carry file
# names and tags in UTF-8 regardless of the OS locale (e.g. cp1252 on Windows).
TEXT_KWARGS = {"text": True, "encoding": "utf-8", "errors": "replace"}


def spawn_kwargs(new_group: bool = False) -> dict:
    """Keyword arguments for subprocess.run/Popen.

    On Windows, never open a console window (the tray runs under pythonw), and run
    at below-normal priority so encodes and verify decodes yield the CPU to whatever
    the user is doing on the PC. With
    new_group, the child also gets its own process group (Windows) or session
    (POSIX), so a Ctrl-C in the parent's console isn't delivered to it: the parent
    decides how to stop it (terminate/kill) and releases the job cleanly."""
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        if new_group:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True} if new_group else {}
