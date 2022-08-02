"""
webroot.py

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
from w3af.core.data.parsers.doc.url import URL
import w3af.core.data.kb.knowledge_base as kb


def get_webroot_dirs(domain=None):
    """
    :return: A list of strings with possible webroots. This function also 
             analyzed the contents of the knowledgeBase and tries to use
             that information in order to guess.
    """
    result = []

    if obtained_webroot := kb.kb.raw_read('path_disclosure', 'webroot'):
        result.append(obtained_webroot)

    if domain:
        root_domain = URL(f'http://{domain}').get_root_domain()

        result.extend(
            (
                f'/var/www/{domain}',
                f'/var/www/{domain}/www/',
                f'/var/www/{domain}/html/',
                f'/var/www/{domain}/htdocs/',
                f'/home/{domain}',
                f'/home/{domain}/www/',
                f'/home/{domain}/html/',
                f'/home/{domain}/htdocs/',
            )
        )

        if domain != root_domain:
            result.extend(
                (
                    f'/home/{root_domain}',
                    f'/home/{root_domain}/www/',
                    f'/home/{root_domain}/html/',
                    f'/home/{root_domain}/htdocs/',
                    f'/var/www/{root_domain}',
                    f'/var/www/{root_domain}/www/',
                    f'/var/www/{root_domain}/html/',
                    f'/var/www/{root_domain}/htdocs/',
                )
            )

    result.extend(('/var/www/', '/var/www/html/', '/var/www/htdocs/'))
    return result
