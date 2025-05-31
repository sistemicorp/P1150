#! /usr/bin/env python3

# © 2024 Unit Circle Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from subprocess import Popen, PIPE
import re
import os
from struct import unpack
from operator import itemgetter
import time
import cbor2


# Variables that need to be insync with target config of logging framework
LOG_TYPE_BASIC     = 0x00
LOG_TYPE_MEM       = 0x01
LOG_TYPE_RES       = 0x02
LOG_TYPE_PORT      = 0x03
TARGET_DIGIT_SHIFT = 20
level2str = {
    '0': "INFO",
    '1': "TRACE ",
    '2': "WARN ",
    '3': "ERROR",
    '4': "FATAL",
    '5': "PANIC"
    }

re_header = re.compile(r'^\s*<(\d+)><([0-9a-fA-F]+)>: Abbrev Number: (\d+) \(DW_TAG_(.+)\)')
re_detail = re.compile(r'^\s*<([0-9a-fA-F]+)>\s*DW_AT_(\S+)\s*:(.*)$')

re_addr = re.compile(r'\(DW_OP_addr:\s*([0-9a-fA-F]+)(;.*)?\)')

class Attr(object):
  def __init__(self, tag, detail):
    self.tag = tag
    self.detail = detail

  def __getattr__(self, attr):
    if attr == 'short':
      return self.detail.split(':')[-1].strip()
    elif attr == 'value':
      z = self.detail.strip()
      if z[:2] == '0x':
        return int(z[2:], 16)
      else:
        return int(z)
    elif attr == 'address':
      m = re_addr.search(self.detail)
      if m:
        return int(m.group(1), 16)
      else:
        return None
    elif attr == 'type':
      return self.detail.strip()[3:-1]
    raise AttributeError(attr)

  def __repr__(self):
    return "Attr(%s, %s)" % (self.tag, self.detail)

class Item(object):
  def __init__(self, tag):
    self.tag = tag
    self.attr = {}
    self.children = []

  def add_attr(self, child):
    self.attr[child.tag] = child

  def __getitem__(self, key):
    return self.attr[key]

  def __contains__(self, key):
    return key in self.attr

  def type(self, items):
    return items.get(self['type'].type, None)

  def add_child(self, child):
    self.children.append(child)

  def __repr__(self):
    return "Item(%s, %s, %s, %s)" % (self.tag, len(self.attr), len(self.children))


def parse(filename):
  debug = False
  items = {}  # map from addr->Item
  stack = [{'level': -1, 'item': Item('root')}]

  cmd = ['arm-none-eabi-objdump', '-Wi', '-g', filename]
  proc = Popen(cmd, stdout=PIPE)
  if True:
    cur_header = None
    for lineno, line in enumerate(proc.stdout.readlines()):

      l = line.decode('utf-8').strip()
      if debug: print("Processing:", lineno, l)

      # Check for detail first - there are many more of them
      m = re_detail.match(l)
      if m:
        item = Attr(m.group(2), m.group(3))
        items[m.groups(1)] = item
        stack[-1]['item'].add_attr(item)
        if debug: print("  addr: %s tag: %s detail: %s" % m.groups())
        continue

      # Check for header
      m = re_header.match(l)
      if m:
        item = Item(m.group(4))
        items[m.group(2)] = item

        while stack[-1]['level'] >= int(m.group(1)):
          if debug: print("Poping level", stack[-1]['level'])
          x = stack.pop()
          stack[-1]['item'].add_child(x['item'])

        if stack[-1]['level'] + 1 != int(m.group(1)):
          print("Skipping levels", lineno, l)
          exit(-1)

        if debug: print("Pushing a new level", int(m.group(1)))
        stack.append({'level':int(m.group(1)), 'item': item})
        if debug: print("level: %s addr: %s rev: %s tag: %s" % m.groups())
        continue

  while stack[-1]['level'] >= 0:
    if debug: print("Poping level", stack[-1]['level'])
    x = stack.pop()
    stack[-1]['item'].add_child(x['item'])

  return stack[0]['item'], items

def process_subprogram(item, functions):
  if 'low_pc' in item and 'name' in item:
    low_pc = item['low_pc'].value
    high_pc = item['high_pc'].value
    if low_pc > 0:
      if high_pc < low_pc:
        high_pc = low_pc + high_pc
      functions[(low_pc, high_pc)] = item['name'].short
      #print(f"tag: {item.tag} name: {item['name'].short} low_pc: {item['low_pc'].value:08x} high_pc: {item['high_pc'].value:08x}")
    elif 'external' in item:
      pass
    elif 'inline' in item:
      pass
    elif 'abstract_origin' in item:
      pass
    elif 'prototyped' in item:
      pass
    else:
      print(f'unknown subprogram tag: {item.tag} attr: {item.attr}')

def process_variable(item, variables, cu_name):
  #print(item.tag, item.attr, item.children)
  if 'location' in item and 'name' in item:
    addr =item['location'].address
    if addr is not None:
      if addr > 0:
        variables[addr] = cu_name + ':' + item['name'].short
    else:
      print(cu_name + ':' + item['name'].short)

def process_enum(item, enums):
  if 'name' in item:
    name = item['name'].short
    if name in enums: return
    mapping  = {}
    for e in item.children:
      mapping[e['const_value'].value] = e['name'].short
    enums[name] = mapping

def process_typedef(item, items, tdenums):
  #print("  ", item.tag, item['name'].short)
  if 'type' not in item:
    return
  c = item.type(items)
  name = item['name'].short
  mapping = {}

  if name in tdenums: return

  while c:
    if c.tag == 'base_type':
      break
    elif c.tag == 'typedef':
      c = c.type(items)
    elif c.tag == 'enumeration_type':
      for e in c.children:
        mapping[e['const_value'].value] = e['name'].short
        #print("    ", e['name'].short, ":", e['const_value'].value)
      break
    elif c.tag == 'structure_type':
      break
    elif c.tag == 'pointer_type':
      break
    elif c.tag == 'subroutine_type':
      break
    elif c.tag == 'array_type':
      break
    elif c.tag == 'union_type':
      break
    elif c.tag == 'volatile_type':
      break
    elif c.tag == 'const_type':
      break
    else:
      print("Unknown typedef child", c.tag)
  if len(mapping) > 0:
    #print(name, mapping)
    tdenums[name] = mapping

def extract(root, items):
  enums = {}
  tdenums = {}
  variables = {}
  functions = {}

  # Run through the compilation units
  for cu in root.children:
    cu_name = cu['name'].short
    #print("Compilation unit: ", cu_name)

    # Run through the items in each compilation unit
    for item in cu.children:
      #if 'name' in item:
      #  print("  ", item.tag, item['name'].short)
      if item.tag == 'subprogram':
        process_subprogram(item, functions)

      elif item.tag == 'variable':
        process_variable(item, variables, cu_name)

      elif item.tag == 'enumeration_type':
        process_enum(item, enums)

      elif item.tag == 'typedef':
        process_typedef(item, items, tdenums)

      elif item.tag in ['base_type', 'structure_type', 'pointer_type',
                        'subroutine_type', 'array_type', 'union_type',
                        'volatile_type', 'const_type', 'dwarf_procedure',
                        'restrict_type']:
        pass
      else:
        print('unhandled type (ignoring):', item.tag, item.attr, item.children)

  return (enums, tdenums, variables, functions)

def lookup_func(functions, a):
  a = a & ~1
  for low, hi in functions.keys():
    if a >= low and a < hi:
      return (functions[(low, hi)], a-low)
  return None

def lookup_var(variables, a):
  # Only consider valid if 0 <= offset < 0x3000
  d = [(variables[addr], a - addr) for idx, addr in enumerate(variables.keys()) if a >= addr and a - addr < 0x3000]
  if len(d) == 0:
    return None
  else:
    return min(d, key=itemgetter(1))

# Parsers for format strings
def parse_int32(b, s = None, t = None):
  if len(b) < 4: return ('<missing int32>', b'')
  return (unpack('<i', b[:4])[0], b[4:])

def parse_uint32(b, s = None, t = None):
  if len(b) < 4: return ('<missing uint32>', b'')
  return (unpack('<I', b[:4])[0], b[4:])

def parse_int64(b, s = None, t = None):
  if len(b) < 8: return ('<missing int64>', b'')
  return (unpack('<q', b[:8])[0], b[8:])

def parse_uint64(b, s = None, t = None):
  if len(b) < 8: return ('<missing uint64>', b'')
  return (unpack('<Q', b[:8])[0], b[8:])

def parse_double(b, s = None, t = None):
  if t is None:
    if len(b) < 8: return ('<missing double>', b'')
    return (unpack('<d', b[:8])[0], b[8:])
  else:
    if len(b) < 4: return ('<missing float>', b'')
    return (unpack('<f', b[:4])[0], b[4:])

def parse_pointer(b, s = None, t = None):
  if len(b) < 4: return ('<missing pointer>', b'')
  return (unpack('<I', b[:4])[0], b[4:])

def parse_bytes(b, s = None):
  # Just return the remaining bytes as a memory byte array
  return (b, b'')

def parse_string(b, s = None):
  # Extract a NULL terminated list of chars
  try:
    i = b.index(0)
    return (repr(b[:i].decode('utf-8'))[1:-1], b[i+1:])
  except ValueError:
    return ('<missing string>', b'')

def parse_enum(enum_t):
  def parser(b, s = None, t = None):
    if b is None and s is None and t is None:
      return enum_t
    (r, b) = parse_int32(b, s, t)
    if enum_t in s.enums:
      e = s.enums[enum_t].get(r, "<%s:%d>" % (enum_t, r))
    elif enum_t in s.tdenums:
      e = s.tdenums[enum_t].get(r, "<%s:%d>" % (enum_t, r))
    else:
      e = "<!%s:%d>" % (enum_t, r)
    return (e, b)
  return parser

def parse_sym(b, s = None, t = None, functions = None, variables = None):
  (r, b) = parse_uint32(b, s, t)
  f = lookup_func(s.functions, r)
  if f:
    return ('%s+0x%x' % f, b)
  v = lookup_var(s.variables, r)
  if v:
    return ('%s+0x%x' % v, b)
  return ('0x%08x' % r, b)

# List of printf format specifiers supported and the corresponding parsing
# function to extract the value to be formatted from the incoming byte stream.
types = {
 'd': parse_int32,
 'i': parse_int32,
 'o': parse_int32,
 'u': parse_uint32,
 'x': parse_uint32,
 'X': parse_uint32,
 'c': parse_uint32,
 'ld': parse_int32,
 'li': parse_int32,
 'lo': parse_int32,
 'lu': parse_uint32,
 'lx': parse_uint32,
 'lX': parse_uint32,
 'lld': parse_int64,
 'lli': parse_int64,
 'llo': parse_int64,
 'llu': parse_uint64,
 'llx': parse_uint64,
 'llX': parse_uint64,
 'f': parse_double,
 'F': parse_double,
 'e': parse_double,
 'E': parse_double,
 'g': parse_double,
 'G': parse_double,
 'a': parse_double,
 'A': parse_double,
 'p': parse_pointer,
 's': parse_string,
}


re_fmt = re.compile(r'(((?<!%)|(?P<sym>{sym})|({enum:(?P<enum>[^}]*)}))%[#+\- 0]*[0-9]*\.?[0-9]*(?:lu[fFeEgGaA]|[l]*[diouxXcfFeEgGaApsb]|z[douxX]))')

def cfmt2pfmt(fmt):
  #print(fmt)
  args = []
  m = True
  s = fmt
  r = ''
  while m is not None:
    m = re_fmt.search(fmt)
    if m:
      (start, end) = m.span()
      prefix = fmt[0:start]
      infix = fmt[start:end]
      postfix = fmt[end:]

      if infix[-3:-1] == 'll':
        specifier = infix[-3:]
      elif infix[-2:-1] == 'l':
        specifier = infix[-2:]
      elif infix[-3:-1] == 'lu':
        specifier = infix[-3:]
      else:
        specifier = infix[-1]

      if m.group('sym'):
        args.append(parse_sym)
        infix = '%s'
      elif m.group('enum'):
        #print(m.group('enum'))
        args.append(parse_enum(m.group('enum').strip()))
        infix = '%s'
      else:
        args.append(types[specifier])

      # Make fmt compatible with python
      if infix[-3:-1] == 'll':
        # Python print only handles one l
        infix = infix[:-2] + infix[-1]
      elif infix[-2:] == '%p':
        # Replace %p with something nice
        infix = infix[:-2] + '0x%08x'
      elif infix[-1] == 'p':
        # Python doesn't understand p so try %x
        infix = infix[:-1] + 'x'
      elif infix[-2] == 'z':
         # .....%z[douxX] -> .....%[douxX]
         infix = infix[:-2] + infix[-1]

      r = r + prefix + infix
      fmt = postfix
    else:
      r = r + fmt
  return (r, args)

def parse_prefix(prefix):
  if prefix.count(':') >= 3:
    (level, fname, line, fmt) = prefix.split(':', 3)
    (clean, parser) = cfmt2pfmt(fmt)
    return (level, fname, line, clean, parser)
  elif prefix.count(':') == 2:
    (level, fname, line) = prefix.split(':', 2)
    return (level, fname, line)
  else:
    return prefix

def load_logdata(fname):
  from elftools.elf.elffile import ELFFile
  p = re.compile(b'([^\x00]*)\x00+')
  s = None
  with open(fname, 'rb') as f:
    elf = ELFFile(f)
    sdata = elf.get_section_by_name('.logdata').data()
    if len(sdata) == 0: return None, {}

    s = elf.get_section_by_name('.symtab')
    saddr = s.get_symbol_by_name('_slogstr')
    if saddr == None: return None, {}
    saddr = saddr[0].entry.st_value

    strings = p.finditer(sdata)
    fmt    = {m.start()+saddr: parse_prefix(m.group(1).decode('utf-8')) for m in strings}

  return (saddr, fmt)

def hex2str(b, sep = ' '):
  return sep.join(['%02x' % v for v in b])

def extract_vals(frame, parser, logdata):
  vals = []
  for p in parser:
    (v, frame) = p(frame, logdata)
    if v is None:
      #logging.debug(f'parser: {parser}, p: {p}, frame: {frame.hex()}')
      return (None,
       "Unable to decode parameter: %s near: %s" % (p, hex2str(frame)))
    vals.append(v)
  if len(frame) > 0: return (None, "Extra data in frame %s" % (hex2str(frame)))
  return (tuple(vals), None)


def fnencode(p):
  enc = {
      parse_int32 : 'int32',
      parse_uint32 : 'uint32',
      parse_int64 : 'int64',
      parse_uint64 : 'uint64',
      parse_double : 'double',
      parse_pointer : 'pointer',
      parse_bytes: 'bytes',
      parse_string: 'string',
      parse_sym: 'sym',
      }
  if p in enc:
    return enc[p]
  elif repr(p).startswith('<function parse_enum.<locals>.parser'):
    return ('enum', p(None))
  else:
    raise ValueError(f'unknown parser function {p}')

def fndecode(p):
  dec = {
      'int32': parse_int32,
      'uint32': parse_uint32,
      'int64': parse_int64,
      'uint64': parse_uint64,
      'double': parse_double,
      'pointer': parse_pointer,
      'bytes': parse_bytes,
      'string': parse_string,
      'sym': parse_sym,
      }
  if isinstance(p, type([])) and len(p) == 2 and p[0] == 'enum':
    return parse_enum(p[1])
  elif p in dec:
    return dec[p]
  else:
    raise ValueError(f'unknown parser function {p}')

def load_from_cbor(data):
  a = cbor2.loads(data)
  enums = a['enums']
  tdenums = a['tdenums']
  variables = a['vars']
  functions = a['fns']
  saddr = a['saddr']

  old_fmts = a['fmts']
  fmts = {}
  for fmt in sorted(old_fmts.keys()):
    x = old_fmts[fmt]
    if len(x) == 5:
      level, fname, line, clean, parser = x
      fmts[fmt] = level, fname, line, clean, [fndecode(p) for p in parser]
    elif len(x) == 3:
      level, fname, line = x
      fmts[fmt] = level, fname, line
  return enums, tdenums, variables, functions, saddr, fmts

def load_cbor_from_elf(filename):
  from elftools.elf.elffile import ELFFile
  with open(filename, 'rb') as f:
    elf = ELFFile(f)
    s = elf.get_section_by_name('.logdata_cbor')
    if s:
      data = s.data()
      if len(data) > 0:
        return data
    return None

class LogData(object):
  def __init__(self, filename, verbose = False):
    if verbose:
      print(f"Loading {filename}")
    if filename.endswith('.cbor') or filename.endswith('.logdata'):
      with open(filename, 'rb') as f:
        data = f.read()
      self.enums, self.tdenums, self.variables, \
          self.functions, self.saddr, self.fmts = load_from_cbor(data)
    else:
      data = load_cbor_from_elf(filename)
      if data:
        self.enums, self.tdenums, self.variables, \
            self.functions, self.saddr, self.fmts = load_from_cbor(data)
      else:
        self.enums, self.tdenums, self.variables, self.functions = \
            extract(*parse(filename))
        self.saddr, self.fmts = load_logdata(filename)
    self.filename = filename
    self.ts = os.stat(filename).st_mtime
    self.count = 0
    self.start_time = time.time()
    if verbose:
      print(f"Loaded target: {self.filename} target: {self.target()}")

  def target(self):
    return (self.saddr >> TARGET_DIGIT_SHIFT) & 0xf;

  def _check_for_reload(self):
    ts = os.stat(self.filename).st_mtime
    if ts != self.ts:
      old_target = self.target()
      self.enums, self.tdenums, self.variables, self.functions = \
          extract(*parse(self.filename))
      self.saddr, self.fmts = load_logdata(self.filename)
      self.ts = ts

      if self.target() != old_target:
        print("Target changed from %d to %d" % (old_target, self.target()))
      print("Reloaded:", self.filename)

  def decode(self, item):
    self._check_for_reload()
    target, addr, frame = item
    kind = addr & 3
    addr = addr & ~3
    (level, fname, line, clean, parser) = self.fmts.get(addr, (None, None, None, None, None))
    ts = int((time.time() - self.start_time)*1000+.5)/1000.
    self.count += 1
    if level is None or kind not in [LOG_TYPE_BASIC, LOG_TYPE_MEM]:
      return (self.count, ts, target, addr, frame)
    else:
      if kind == LOG_TYPE_MEM:
        parser = [parse_pointer, parse_bytes]
      (vals, error) = extract_vals(frame, parser, self)
      #print(f'vals: {vals} error: {error}')
      if vals is not None:
        if kind == LOG_TYPE_MEM:
          text = f'{clean} {vals[0]:08x}: {hex2str(vals[1])}'
        else:
          text = clean % vals
        level = level2str.get(level, f"<bad level {level}>")
        return (self.count, ts, level, fname, line, text)
      else:
        return (self.count, ts, level, fname, line, f'{clean} [{frame.hex()} - {error}]')

  def dump_fmts(self):
    for a in sorted(self.fmts.keys()):
      x = self.fmts[a]
      if len(x) == 5:
        level, fname, line, clean, parser = x
        print('0x%08x' % a, level, fname, line, clean, parser)
      elif len(x) == 3:
        level, fname, line = x
        print('0x%08x' % a, level, fname, line)

  def dump(self):
    for name in sorted(self.enums.keys()):
      print(name)
      for k, v in self.enums[name].items():
        print("  ", k, v)

    for name in sorted(self.tdenums.keys()):
      print(name)
      for k, v in self.tdenums[name].items():
        print("  ", k, v)

    for addr in sorted(self.variables.keys()):
      print('0x%08x: %s' % (addr, self.variables[addr]))

    for (low, hi) in sorted(self.functions.keys(), key=itemgetter(0)):
      print("[0x%08x, 0x%08x) - %s" % (low, hi, self.functions[(low, hi)]))

  def save(self, ofname):
    fmts = {}
    for a in sorted(self.fmts.keys()):
      x = self.fmts[a]
      if len(x) == 5:
        level, fname, line, clean, parser = x
        fmts[a] = level, fname, line, clean, [fnencode(p) for p in parser]
      elif len(x) == 3:
        level, fname, line = x
        fmts[a] = level, fname, line

    a = cbor2.dumps({'enums': self.enums, 'tdenums': self.tdenums, 'vars': self.variables, 'fns': self.functions, 'saddr': self.saddr, 'fmts': fmts})
    with open(ofname, 'wb') as f:
      f.write(a)
