from utils.output import KeyValueOutput


SQLITE_MAX_REACHED = 'The SQLiteExecutor.in_queue length has reached its max'


def get_dbms_queue_size_exceeded(scan_log_filename, scan):
    scan.seek(0)
    error_count = sum(SQLITE_MAX_REACHED in line for line in scan)

    return KeyValueOutput('sqlite_limit_reached',
                          'SQLite queue limit reached',
                          error_count)
