# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: pogoprotos/enums/ar_mode.proto

import sys
_b=sys.version_info[0]<3 and (lambda x:x) or (lambda x:x.encode('latin1'))
from google.protobuf.internal import enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
from google.protobuf import descriptor_pb2
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor.FileDescriptor(
  name='pogoprotos/enums/ar_mode.proto',
  package='pogoprotos.enums',
  syntax='proto3',
  serialized_pb=_b('\n\x1epogoprotos/enums/ar_mode.proto\x12\x10pogoprotos.enums*5\n\x06\x41rMode\x12\x0f\n\x0b\x41RMODE_NONE\x10\x00\x12\x0e\n\nARSTANDARD\x10\x01\x12\n\n\x06\x41RPLUS\x10\x02\x62\x06proto3')
)
_sym_db.RegisterFileDescriptor(DESCRIPTOR)

_ARMODE = _descriptor.EnumDescriptor(
  name='ArMode',
  full_name='pogoprotos.enums.ArMode',
  filename=None,
  file=DESCRIPTOR,
  values=[
    _descriptor.EnumValueDescriptor(
      name='ARMODE_NONE', index=0, number=0,
      options=None,
      type=None),
    _descriptor.EnumValueDescriptor(
      name='ARSTANDARD', index=1, number=1,
      options=None,
      type=None),
    _descriptor.EnumValueDescriptor(
      name='ARPLUS', index=2, number=2,
      options=None,
      type=None),
  ],
  containing_type=None,
  options=None,
  serialized_start=52,
  serialized_end=105,
)
_sym_db.RegisterEnumDescriptor(_ARMODE)

ArMode = enum_type_wrapper.EnumTypeWrapper(_ARMODE)
ARMODE_NONE = 0
ARSTANDARD = 1
ARPLUS = 2


DESCRIPTOR.enum_types_by_name['ArMode'] = _ARMODE


# @@protoc_insertion_point(module_scope)