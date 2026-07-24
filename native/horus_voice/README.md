# HORUS Voice Native

`horus_voice_native` is the open media plugin shared by HORUS MR and HORUS Field. It wraps libdatachannel WebRTC media transport and libopus behind a small stable C ABI suitable for Unity IL2CPP `DllImport`.

The plugin:

- negotiates one send/receive Opus track;
- uses direct ICE first and accepts optional STUN/TURN URLs;
- exposes SDP and ICE callbacks to the authenticated HORUS signaling client;
- encodes and decodes 48 kHz mono PCM;
- enables Opus FEC, DTX, voice tuning, and packet-loss concealment;
- never records audio.

## UWP ARM64

Install CMake, Visual Studio UWP/ARM64 build tools, and vcpkg. From this directory:

```powershell
cmake -S . -B build/uwp-arm64 `
  -G "Visual Studio 17 2022" -A ARM64 `
  -DCMAKE_SYSTEM_NAME=WindowsStore `
  -DCMAKE_SYSTEM_VERSION=10.0 `
  -DCMAKE_TOOLCHAIN_FILE=<VCPKG_ROOT>/scripts/buildsystems/vcpkg.cmake `
  -DVCPKG_TARGET_TRIPLET=arm64-uwp

cmake --build build/uwp-arm64 --config Release
```

Copy `horus_voice_native.dll` plus the vcpkg runtime DLLs for libdatachannel, libjuice, OpenSSL, and Opus into `Assets/Plugins/WSA/ARM64` in HORUS Field. Unity plugin import settings must be restricted to WSA Player and ARM64.

The project was dependency-probed successfully on 2026-07-18 with libdatachannel 0.24.5 and Opus for the `arm64-uwp` triplet. A successful library build is not a substitute for the physical-HoloLens duplex/latency/thermal acceptance test.

## Android ARM64

Install the manifest once with vcpkg's Android triplet, then configure the wrapper against the Unity Android NDK and the installed static libraries:

```powershell
$env:ANDROID_NDK_HOME = '<UNITY_EDITOR>/Data/PlaybackEngines/AndroidPlayer/NDK'
vcpkg install --triplet arm64-android --x-manifest-root .

cmake -S . -B build/android-arm64 -G Ninja `
  -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_TOOLCHAIN_FILE="$env:ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" `
  -DANDROID_ABI=arm64-v8a `
  -DANDROID_PLATFORM=android-28 `
  -DCMAKE_FIND_ROOT_PATH=<VCPKG_INSTALLED>/arm64-android `
  -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH `
  -DLibDataChannel_DIR=<VCPKG_INSTALLED>/arm64-android/share/libdatachannel `
  -DOpus_DIR=<VCPKG_INSTALLED>/arm64-android/share/opus

cmake --build build/android-arm64 --config Release
```

If the Unity editor is installed below `Program Files`, expose its NDK through a short junction before building; OpenSSL's MSYS build does not reliably handle a compiler path containing spaces. Strip the release library with the NDK's `llvm-strip`, then copy `libhorus_voice_native.so` into `Assets/Plugins/Android/arm64-v8a` in HORUS MR. The Android build statically links its third-party code and depends only on Android system libraries (`libm`, `libdl`, and `libc`).

The Android wrapper is linked with 16 KiB load-segment alignment so it remains compatible with Android 15+ devices that use 16 KiB memory pages. Verify packaged binaries with `llvm-readelf -lW`; every `LOAD` segment should report an alignment of at least `0x4000`.

The Android AArch64 library and the UWP ARM64 library both built successfully on 2026-07-18. Physical-device call quality and lifecycle tests are still required.

## License notes

The HORUS wrapper follows the connector's Apache-2.0 license. Redistributed dependency licenses and notices must accompany application builds. Current dependencies include MPL-2.0, BSD, MIT, Apache-2.0, and LGPL components; preserve their notices and comply with their relinking/source requirements.
