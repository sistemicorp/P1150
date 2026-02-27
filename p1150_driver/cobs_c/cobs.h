// © 2022 Unit Circle Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once
#include <stddef.h>
#include <stdint.h>

typedef intmax_t ssize_t;

#define COBS_ENC_SIZE(n_) ((((n_)+253)/254) + (n_))

size_t cobs_enc_size(size_t n);
size_t cobs_enc(uint8_t* out, uint8_t* in, size_t n);
ssize_t cobs_dec(uint8_t* out, uint8_t* in, size_t n);
