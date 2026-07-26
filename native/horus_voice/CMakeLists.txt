cmake_minimum_required(VERSION 3.25)

project(horus_voice_native VERSION 0.1.0 LANGUAGES CXX)

find_package(LibDataChannel CONFIG REQUIRED)
find_package(Opus CONFIG REQUIRED)

add_library(horus_voice_native SHARED
    include/horus_voice_native.h
    src/horus_voice_native.cpp
)

target_compile_features(horus_voice_native PRIVATE cxx_std_20)
target_compile_definitions(horus_voice_native PRIVATE HORUS_VOICE_NATIVE_EXPORTS=1)
target_include_directories(horus_voice_native PUBLIC include)
target_link_libraries(horus_voice_native PRIVATE
    LibDataChannel::LibDataChannel
    Opus::opus
)

if(ANDROID)
    # Android 15 and current Quest runtimes support 16 KiB memory pages. Keep
    # every load segment compatible instead of relying on the NDK default.
    target_link_options(horus_voice_native PRIVATE
        "LINKER:-z,max-page-size=16384"
        "LINKER:-z,common-page-size=16384"
    )
endif()

if(MSVC)
    target_compile_options(horus_voice_native PRIVATE /W4 /permissive- /EHsc)
else()
    target_compile_options(horus_voice_native PRIVATE -Wall -Wextra -Wpedantic)
endif()

set_target_properties(horus_voice_native PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
)
