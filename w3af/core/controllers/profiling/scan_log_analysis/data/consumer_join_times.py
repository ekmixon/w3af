import re

from utils.output import KeyValueOutput

JOIN_TIMES = re.compile('(.*?) took (.*?) seconds to join\(\)')


def get_consumer_join_times(scan_log_filename, scan):
    scan.seek(0)

    join_times = []

    for line in scan:
        if 'seconds to join' not in line:
            continue

        if match := JOIN_TIMES.search(line):
            join_times.append(match.group(0))

    return (
        KeyValueOutput(
            'consumer_join_times',
            'These consumers have been join()ed',
            join_times,
        )
        if join_times
        else KeyValueOutput(
            'consumer_join_times', 'The scan log has no calls to join()'
        )
    )
