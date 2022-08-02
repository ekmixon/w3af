"""
common_directories.py

Copyright 2008 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""


def get_common_directories(os=None):
    """
    :param os: The operating system for which we want the common directories.
    If no os is specified, all directories are returned.

    :return: A list of common directories
    """
    directories = []

    if os == 'linux' or os is None:
        directories.extend(
            (
                "/bin/",
                "/boot/",
                "/cdrom/",
                "/dev/",
                "/etc/",
                "/home/",
                "/initrd/",
                "/lib/",
                "/media/",
                "/mnt/",
                "/opt/",
                "/proc/",
                "/root/",
                "/sbin/",
                "/sys/",
                "/srv/",
                "/tmp/",
                "/usr/",
                "/var/",
                "/htdocs/",
            )
        )

    if os == 'windows' or os is None:
        directories.extend(
            (
                r"C:\\",
                r"D:\\",
                r"E:\\",
                r"Z:\\",
                r"C:\\windows\\",
                r"C:\\winnt\\",
                r"C:\\win32\\",
                r"C:\\win\\system\\",
                r"C:\\windows\\system\\",
                r"C:\\winnt\\system\\",
                r"C:\\win32\\system\\",
                r"C:\\Program Files\\",
                r"C:\\Documents and Settings\\",
            )
        )

    return directories
