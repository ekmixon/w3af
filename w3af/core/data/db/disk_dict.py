"""
disk_dict.py

Copyright 2012 Andres Riancho

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
import cPickle

from w3af.core.data.misc.cpickle_dumps import cpickle_dumps
from w3af.core.data.fuzzer.utils import rand_alpha
from w3af.core.data.db.dbms import get_default_temp_db_instance


class DiskDict(object):
    """
    It's a dict that stores items in a sqlite3 database and has the following
    features:
        - Dict-like API
        - Is thread safe
        - Deletes the table when the instance object is deleted

    :author: Andres Riancho (andres.riancho@gmail.com)
    """
    def __init__(self, table_prefix=None):
        self.db = get_default_temp_db_instance()

        prefix = '' if table_prefix is None else f'{table_prefix}_'
        self.table_name = f'disk_dict_{prefix}{rand_alpha(30)}'

        # Create table
        # DO NOT add the AUTOINCREMENT flag to the table creation since that
        # will break __getitem__ when an item is removed, see:
        #     http://www.sqlite.org/faq.html#q1
        columns = [('index_', 'INTEGER'),
                   ('key', 'BLOB'),
                   ('value', 'BLOB')]
        pks = ['index_']

        self.db.create_table(self.table_name, columns, pks)
        self.db.create_index(self.table_name, ['key'])
        self.db.commit()

    def cleanup(self):
        self.db.drop_table(self.table_name)

    def keys(self):
        pickled_keys = self.db.select(f'SELECT key FROM {self.table_name}')
        return [cPickle.loads(r[0]) for r in pickled_keys]

    def iterkeys(self):
        pickled_keys = self.db.select(f'SELECT key FROM {self.table_name}')

        for r in pickled_keys:
            yield cPickle.loads(r[0])

    def iteritems(self):
        pickled_keys = self.db.select(f'SELECT key, value FROM {self.table_name}')

        for r in pickled_keys:
            yield cPickle.loads(r[0]), cPickle.loads(r[1])

    def __contains__(self, key):
        """
        :return: True if the value is in keys
        """
        # Adding the "limit 1" to the query makes it faster, as it won't
        # have to scan through all the table/index, it just stops on the
        # first match.
        query = f'SELECT count(*) FROM {self.table_name} WHERE key=? limit 1'
        r = self.db.select_one(query, (cpickle_dumps(key),))
        return bool(r[0])

    def __delitem__(self, key):
        """
        Delete the key from the dict

        :param key: The key to delete
        :return: None
        """
        query = f'DELETE FROM {self.table_name} WHERE key = ?'
        self.db.execute(query, (cpickle_dumps(key),))

    def __setitem__(self, key, value):
        # Test if it is already in the DB:
        if key in self:
            query = f'UPDATE {self.table_name} SET value = ? WHERE key=?'
            self.db.execute(query, (cpickle_dumps(value),
                                    cpickle_dumps(key)))
        else:
            query = f"INSERT INTO {self.table_name} VALUES (NULL, ?, ?)"
            self.db.execute(query, (cpickle_dumps(key),
                                    cpickle_dumps(value)))

    def __getitem__(self, key):
        query = f'SELECT value FROM {self.table_name} WHERE key=? limit 1'
        r = self.db.select(query, (cpickle_dumps(key),))

        if not r:
            args = (key, self.table_name)
            raise KeyError('%s not in %s.' % args)

        return cPickle.loads(r[0][0])

    def __len__(self):
        query = f'SELECT count(*) FROM {self.table_name}'
        r = self.db.select_one(query)
        return r[0]

    def get(self, key, default=-456):
        try:
            return self[key]
        except KeyError:
            if default is not -456:
                return default

        raise KeyError()

    def pop(self, key, default=-456):
        value = self.get(key, default=default)
        del self[key]
        return value
