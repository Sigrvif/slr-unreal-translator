# Third-party notices — SLR Unreal Translator

SLR Unreal Translator is licensed under the GNU General Public License,
version 3 or later — see [LICENSE](LICENSE). The release packages bundle the
following third-party components:

| Component | License | Notes |
|---|---|---|
| CPython 3.10.11 embeddable, Windows payload (`runtime/win/python/`) | Python Software Foundation License | fetched at build time by `build/fetch_runtime.py`; the payload carries its own `LICENSE.txt` |
| CPython 3.10.13 (python-build-standalone, Linux payload, `runtime/linux/`) | Python Software Foundation License | same fetch script |
| Oodle Core (`oo2core_9_win64.dll` / `liboo2corelinux64.so.9`) | (c) Epic Games, Inc. (RAD Game Tools) — proprietary; **not bundled** (the UE EULA permits redistribution only inside UE products). Discovered at runtime from the user's game folder or a local Unreal Engine install; env override `SLRUNREAL_OODLE_DLL` (`backend/ut/oodle.py`) | discovered at scan time — never staged into packages (`build/build_release.py`) |
| CityHash port (`backend/ut/cityhash.py`) | MIT (c) Google | from-scratch port of CityHash v1.0.3 `city.cc` (algorithm + test vectors) |
| DirectXTex-derived BC codecs (`backend/ut/bcdec.py`, `backend/ut/_bc7tables.py`) | MIT (c) Microsoft | decode logic follows DirectXTex (`BC.cpp` / `BC6HBC7.cpp`) and the Microsoft BC7 mode reference; the partition/fixup tables are generated from the same source. Encoders are first-party approximations |

Nothing else is bundled: the backend is deliberately stdlib-only (there is no
pip stage). BLAKE3 (`backend/ut/blake3.py`) is an original implementation
from the published specification, and AES (`backend/ut/aes.py`) uses the
Windows BCrypt OS API with an in-house pure-Python fallback.

MIT license text (for the CityHash and DirectXTex rows above):

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions: The above copyright
> notice and this permission notice shall be included in all copies or
> substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS",
> WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
> TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT.
