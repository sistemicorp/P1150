#! /usr/bin/env python3
# vim: set fileencoding=utf-8:

# © 2023 Unit Circle Inc.
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

# Implementation of COBS - http://www.stuartcheshire.org/papers/COBSforToN.pdf

def enc(data):
  out = b''
  data = data + b'\0' # Add "fake" zero
  while len(data) > 0:
    i = data.index(b'\0')
    if i >= 254:
      out, data = out + bytes((255,)) + data[:254], data[254:]

      # Early exit if we only have the added "fake" zero
      # No need to send extra \x01 byte - receiver can infer
      if data == b'\x00':
        break
    else:
      out, data = out + bytes((i+1,)) + data[:i], data[i+1:]
  return out

def dec_x(data):
  out = b''
  while len(data) > 0:
    code = data[0]
    seg, data = data[1:code], data[code:]
    if code == 255 and len(data) == 0:
      # Add back "fake" zero removed by sender
      seg += b'\0'
    elif code < 255:
      seg += b'\0'
    out += seg
  return out[:-1] # Remove "fake" zero


def dec(dd):
  data, idx, dellist = list(dd), 0, []
  n = len(data)
  out = [0]*(n + 1)
  out[:-1] = list(data)
  while idx < n:
    code = data[idx]
    if code == 255 and idx == len(data):
      pass # Zero at end already added
    elif code < 255:
      out[idx + code] = 0
    else:
      dellist.append(idx+code)
    idx += code
  r =  bytes(out[1:-1])
  dellist.reverse()
  for idx in dellist:
    r = r[:idx-1] + r[idx:]
  return r

# TODO Remove extra "fake" zero if we end on a length == 0xdf
def enc_zpe(data):
  out = b''
  data = data + b'\0' # Add "fake" zero
  while len(data) > 0:
    i = data.index(b'\0')
    if i >= 0xdf:
      out, data = out + bytes((0xe0,)) + data[:0xdf], data[0xdf:]
    elif len(data) >= i+2 and data[i+1] == 0 and i <= 30:
      out, data = out + bytes((i + 0xe1,)) + data[:i], data[i+2:]
    else:
      out, data = out + bytes((i+1,)) + data[:i], data[i+1:]
  return out

def dec_zpe(data):
  out = b''
  while len(data) > 0:
    code = data[0]
    if code < 0xe0:
      seg, data = data[1:code] + b'\0', data[code:]
    elif code == 0xe0:
      seg, data = data[1:code], data[code:]
    else:
      seg, data = data[1:code-0xe0] + b'\0\0', data[code-0xe0:]
    out += seg
  return out[:-1] # Remove "fake" zero


tcs = [
    # From the paper
    (bytes.fromhex('4500002C4C79000040064F37'),
      bytes.fromhex('024501042C4C79010540064F37')),

    # Empty string
    (b'', b'\x01'),

    # Single null byte string
    (b'\x00', b'\x01\x01'),

    # String that ends with null
    (b'123\x00', b'\x04123\x01'),

    # String with no null
    (b'123', b'\x04123'),

    # String of length 1 with no null
    (b'1', b'\x021'),

    # String with null in middle
    (b'123\x00456', b'\x04123\x04456'),

    # String with only nulls
    (b'\x00'*10, b'\x01' * 11),

    # String that starts with null and has null in middle
    (b'\x00'+b'1'*254+b'\x00123456', b'\x01\xff'+b'1'*254+b'\x01\x07123456'),

    # Very long string
    (b'0123456789'*150,
      (b'\xff0123456789012345678901234567890123456789012345678901234567890' +
       b'12345678901234567890123456789012345678901234567890123456789012345' +
       b'67890123456789012345678901234567890123456789012345678901234567890' +
       b'123456789012345678901234567890123456789012345678901234567890123' +
       b'\xff4567890123456789012345678901234567890123456789012345678901234' +
       b'56789012345678901234567890123456789012345678901234567890123456789' +
       b'01234567890123456789012345678901234567890123456789012345678901234' +
       b'567890123456789012345678901234567890123456789012345678901234567' +
       b'\xff8901234567890123456789012345678901234567890123456789012345678' +
       b'90123456789012345678901234567890123456789012345678901234567890123' +
       b'45678901234567890123456789012345678901234567890123456789012345678' +
       b'901234567890123456789012345678901234567890123456789012345678901' +
       b'\xff2345678901234567890123456789012345678901234567890123456789012' +
       b'34567890123456789012345678901234567890123456789012345678901234567' +
       b'89012345678901234567890123456789012345678901234567890123456789012' +
       b'345678901234567890123456789012345678901234567890123456789012345' +
       b'\xff6789012345678901234567890123456789012345678901234567890123456' +
       b'78901234567890123456789012345678901234567890123456789012345678901' +
       b'23456789012345678901234567890123456789012345678901234567890123456' +
       b'789012345678901234567890123456789012345678901234567890123456789' +
       b'\xe70123456789012345678901234567890123456789012345678901234567890' +
       b'12345678901234567890123456789012345678901234567890123456789012345' +
       b'67890123456789012345678901234567890123456789012345678901234567890' +
       b'123456789012345678901234567890123456789'
       )),

      # String that is 1 short of 254 length boundary no nulls
      (bytes(range(1,254)), b'\xfe' + bytes(range(1,254))),

      # String that is 1 short of 254 length boundary ending in null
      (bytes(range(1,254)) + b'\x00', b'\xfe' + bytes(range(1,254)) + b'\x01'),

      # String that is at 254 length boundary no nulls
      (bytes(range(1,255)), b'\xff' + bytes(range(1,255))),

      # String that is at 254 length boundary ending in null
      (bytes(range(1,255)) + b'\x00', b'\xff' + bytes(range(1,255)) + b'\x01\x01'),

      # String that is 1 beyond 254 length boundary no nulls
      (bytes(range(1,256)), b'\xff' + bytes(range(1,255)) + b'\x02\xff'),

      # String that is 1 beyond 254 length boundary ending in null
      (bytes(range(1,256)) + b'\x00',  b'\xff' + bytes(range(1,255)) + b'\x02\xff\x01'),
]

def test_enc_dec():
  for v, e in tcs:
    if enc(v) != e:
      print(f'v:      <{v.hex()}>')
      print(f'e:      <{e.hex()}>')
      print(f'enc(v): <{enc(v).hex()}>')
      raise ValueError
    if dec(e) != v:
      print(f'v:      <{v.hex()}>')
      print(f'e:      <{e.hex()}>')
      print(f'dec(e): <{dec(e).hex()}>')
      raise ValueError

tcs_zpe = [
    (bytes.fromhex('4500002C4C79000040064F37'),
     bytes.fromhex('E245E42C4C790540064F37')),
]

def test_enc_dec_zpe():
  for v, e in tcs_zpe:
    #print('<%s> <%s>' % (v.hex(), enc_zpe(v).hex()))
    #print('<%s> <%s>' % (e.hex(), dec_zpe(e).hex()))
    assert enc_zpe(v) == e
    assert dec_zpe(e) == v

tcs_dec = [
  (bytes.fromhex('01 01 02 02 01 01 01 01 01'), None),
  (bytes.fromhex('02 24 02 02 01'), None),
  (bytes.fromhex('02 55 02 02 0a 3c 75 6e 6b 6e 6f 77 6e 3e'), None),
  (bytes.fromhex('01 01 02 02 02 01 01 01 01'), None),
  (bytes.fromhex('02 24 02 02 01'), None),
  (bytes.fromhex('02 55 02 02 0a 3c 75 6e 6b 6e 6f 77 6e 3e'), None),
  (bytes.fromhex('01 01 02 02 02 02 01 01 01'), None),
  (bytes.fromhex('02 24 02 02 01'), None),
  (bytes.fromhex('02 55 02 02 0a 3c 75 6e 6b 6e 6f 77 6e 3e'), None),
  (bytes.fromhex('01 01 02 02 02 03 01 01 01'), None),
  (bytes.fromhex('02 24 02 02 01'), None),
  (bytes.fromhex('02 55 02 02 0a 3c 75 6e 6b 6e 6f 77 6e 3e'), None),
  (bytes.fromhex('01 01 02 02 02 04 01 01 01'), None),
  (bytes.fromhex('02 24 02 02 01'), None),
  (bytes.fromhex('02 55 02 02 0a 3c 75 6e 6b 6e 6f 77 6e 3e'), None),
  (bytes.fromhex('01 01 02 02 02 05 01 01 01'), None),
  ]

def test_dec():
  for v, e in tcs_dec:
    #print('<%s> <%s>' % (v.hex(), enc_zpe(v).hex()))
    #print('<%s> <%s>' % (e.hex(), dec_zpe(e).hex()))
    print('<%s>' % (dec(v).hex(),))
    #assert dec(v) == e

if __name__ == '__main__':
  test_enc_dec()
  test_enc_dec_zpe()
  test_dec()
