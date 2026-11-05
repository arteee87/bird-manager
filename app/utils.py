import datetime

def local_now():
    """Return the machine's current time (local time zone) without tzinfo.

    Used for every timestamp in the application, so the audit log, backups
    and status always show the same server-local time.
    """
    return datetime.datetime.now().astimezone().replace(tzinfo=None)

