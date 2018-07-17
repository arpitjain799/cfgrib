#
# Copyright 2017-2018 European Centre for Medium-Range Weather Forecasts (ECMWF).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
#   Alessandro Amici - B-Open - https://bopen.eu
#

from __future__ import absolute_import, division, print_function, unicode_literals
from builtins import bytes, isinstance, str, type

import collections
import logging
import typing as T  # noqa

import attr

from . import eccodes


LOG = logging.getLogger(__name__)
_MARKER = object()


@attr.attrs()
class Message(collections.Mapping):
    codes_id = attr.attrib()
    encoding = attr.attrib(default='ascii', type=str)

    @classmethod
    def fromfile(cls, file, offset=None, **kwargs):
        if offset is not None:
            file.seek(offset)
        codes_id = eccodes.codes_new_from_file(file, eccodes.CODES_PRODUCT_GRIB)
        if codes_id is None:
            raise EOFError("end-of-file reached.")
        return cls(codes_id=codes_id, **kwargs)

    def __del__(self):
        eccodes.codes_handle_delete(self.codes_id)

    def message_get(self, item, key_type=None, size=None, length=None, default=_MARKER):
        # type: (str, int, int, int, T.Any) -> T.Any
        """Get value of a given key as its native or specified type."""
        key = item.encode(self.encoding)
        try:
            values = eccodes.codes_get_array(self.codes_id, key, key_type, size, length)
        except eccodes.EcCodesError as ex:
            if ex.code == eccodes.lib.GRIB_NOT_FOUND:
                if default is _MARKER:
                    raise KeyError(item)
                else:
                    return default
            else:
                raise
        if values and isinstance(values[0], bytes):
            values = [v.decode(self.encoding) for v in values]
        if len(values) == 1:
            return values[0]
        return values

    def message_iterkeys(self, namespace=None):
        # type: (str) -> T.Generator[str, None, None]
        if namespace is not None:
            bnamespace = namespace.encode(self.encoding)
        else:
            bnamespace = None
        iterator = eccodes.codes_keys_iterator_new(self.codes_id, namespace=bnamespace)
        while eccodes.codes_keys_iterator_next(iterator):
            yield eccodes.codes_keys_iterator_get_name(iterator).decode(self.encoding)
        eccodes.codes_keys_iterator_delete(iterator)

    def __getitem__(self, item):
        # type: (str) -> T.Any
        return self.message_get(item)

    def __iter__(self):
        # type: () -> T.Generator[str, None, None]
        for key in self.message_iterkeys():
            yield key

    def __len__(self):
        # type: () -> int
        return sum(1 for _ in self)


@attr.attrs()
class ComputedKeysMessage(Message):
    computed_keys = attr.attrib(default={}, type=T.Mapping[str, T.Callable[['Message'], T.Any]])

    def __getitem__(self, item):
        if item in self.computed_keys:
            return self.computed_keys[item](self)
        else:
            return super(ComputedKeysMessage, self).__getitem__(item)

    def __iter__(self):
        seen = set()
        for key in super(ComputedKeysMessage, self).__iter__():
            yield key
            seen.add(key)
        for key in self.computed_keys:
            if key not in seen:
                yield key


def make_message_schema(message, schema_keys, log=LOG):
    schema = collections.OrderedDict()
    for key in schema_keys:
        bkey = key.encode(message.encoding)
        try:
            key_type = eccodes.codes_get_native_type(message.codes_id, bkey)
        except eccodes.EcCodesError as ex:
            if ex.code != eccodes.lib.GRIB_NOT_FOUND:
                log.exception("key %r failed", key)
            schema[key] = ()
            continue
        size = eccodes.codes_get_size(message.codes_id, bkey)
        if key_type == eccodes.CODES_TYPE_STRING:
            length = eccodes.codes_get_length(message.codes_id, bkey)
            schema[key] = (key_type, size, length)
        else:
            schema[key] = (key_type, size)
    return schema


@attr.attrs()
class Index(collections.Mapping):
    stream = attr.attrib()
    index_keys = attr.attrib(type=T.List[str])
    offsets = attr.attrib(repr=False)

    @classmethod
    def fromstream(cls, stream, index_keys):
        schema = make_message_schema(stream.first(), index_keys)
        offsets = collections.OrderedDict()
        for message in stream:
            header_values = []
            for key, args in schema.items():
                # Note: optimisation
                # value = message.message_get(key, *args, default='undef')
                try:
                    value = message[key]
                except:
                    value = 'undef'
                header_values.append(value)
            offset = message.message_get('offset', eccodes.CODES_TYPE_LONG)
            offsets.setdefault(tuple(header_values), []).append(offset)
        return cls(stream=stream, index_keys=index_keys, offsets=offsets)

    def __iter__(self):
        return iter(self.index_keys)

    def __len__(self):
        return len(self.index_keys)

    @property
    def header_values(self):
        if not hasattr(self, '_header_values'):
            self._header_values = {}
            for header_values in self.offsets:
                for i, value in enumerate(header_values):
                    values = self._header_values.setdefault(self.index_keys[i], [])
                    if value not in values:
                        values.append(value)
        return self._header_values

    def __getitem__(self, item):
        # type: (str) -> list
        return self.header_values[item]

    def getone(self, item):
        values = self[item]
        if len(values) != 1:
            raise ValueError("not one value for %r: %r" % (item, len(values)))
        return values[0]

    def subindex(self, dict_query={}, **query):
        query.update(dict_query)
        raw_query = [(self.index_keys.index(k), v) for k, v in query.items()]
        offsets = collections.OrderedDict()
        for header_values in self.offsets:
            for idx, val in raw_query:
                if header_values[idx] != val:
                    break
            else:
                offsets[header_values] = self.offsets[header_values]
        return type(self)(stream=self.stream, index_keys=self.index_keys, offsets=offsets)

    def first(self):
        with open(self.stream.path) as file:
            first_offset = next(iter(self.offsets.values()))[0]
            return self.stream.message_class.fromfile(file, offset=first_offset)


@attr.attrs()
class Stream(collections.Iterable):
    path = attr.attrib(type=str)
    mode = attr.attrib(default='r', type=str)
    message_class = attr.attrib(default=Message, type=Message, repr=False)

    def __iter__(self):
        # type: () -> T.Generator[Message, None, None]
        with open(self.path, self.mode) as file:
            while True:
                try:
                    yield self.message_class.fromfile(file=file)
                except EOFError:
                    break

    def first(self):
        # type: () -> Message
        return next(iter(self))

    def index(self, index_keys):
        return Index.fromstream(stream=self, index_keys=index_keys)
