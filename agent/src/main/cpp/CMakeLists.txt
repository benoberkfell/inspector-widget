# ViewSpector — native JVMTI agent build.
#
# Produces libviewspector.so from viewspector_agent.cpp. The .so exports
# Agent_OnAttach / Agent_OnLoad (CONTRACT §3); it calls
# AddToBootstrapClassLoaderSearch(bootstrap.dex), FindClass Bootstrap, and
# invokes the static Bootstrap.initialize(payloadPath, socketName).
#
# IMPORTANT: jvmti.h is NOT shipped in the Android NDK sysroot (only jni.h is).
# This is a known constraint; the canonical fix — used verbatim by AOSP's own
# androidx inspection native build at
# support/inspection/inspection/src/main/native/CMakeLists.txt:45-52 — is to
# copy the JDK's jvmti.h into a private include dir and compile against it. The
# struct/ABI layout JVMTI relies on (jvmtiEnv vtable, jvmtiError, the
# AddToBootstrapClassLoaderSearch slot) is stable across the JDK and Android ART
# implementations for the functions we use. scripts/build.sh exports JAVA_HOME
# to JDK 21 before invoking Gradle, so $ENV{JAVA_HOME} resolves here.

cmake_minimum_required(VERSION 3.22.1)

project(viewspector_agent LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Vendor the JDK's jvmti.h. We copy it into a separate folder (not its source
# dir) so that the JDK's own neighbouring jni.h is NOT picked up — we want the
# NDK's jni.h. include-with-quotes inside jvmti.h would otherwise prefer the
# JDK's jni.h; isolating the copied header avoids that.
if(NOT DEFINED ENV{JAVA_HOME})
    message(FATAL_ERROR
        "JAVA_HOME is not set. scripts/build.sh must export JAVA_HOME to a JDK "
        "(e.g. JDK 21) so the JDK's jvmti.h can be vendored for the NDK build.")
endif()

configure_file(
    "$ENV{JAVA_HOME}/include/jvmti.h"
    "${CMAKE_CURRENT_BINARY_DIR}/external_jvmti/jvmti.h"
    COPYONLY)

add_library(viewspector SHARED viewspector_agent.cpp)

# The vendored jvmti.h directory is added PRIVATE so #include <jvmti.h> resolves
# to the copy while jni.h still resolves from the NDK sysroot.
target_include_directories(viewspector
    PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/external_jvmti")

# Only -llog is needed beyond the standard NDK libs (CONTRACT note). Android ART
# provides the JVMTI implementation in-process; we link nothing for it.
find_library(log-lib log)
target_link_libraries(viewspector ${log-lib})

# 16 KB page alignment — required for app compatibility on recent devices and a
# no-op on older ones. Mirrors the inspection native build.
target_link_options(viewspector PRIVATE "-Wl,-z,max-page-size=16384")
