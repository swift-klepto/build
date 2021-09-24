# klepto-build

Build script for klepto toolchain

# Dependencies
You need Python as this build script and the Swift build script and the SwiftPM build script are all written in Python.

You need a host Swift toolchain of the same version as the klepto toolchain to build ̀ klepto-swiftpm`. You also need CMake, and all other dependencies of a regular Swift toolchain build.

You also obviously need a devkitPro environment setup with devkitA64 and libnx. The `DEVKITPRO` environment variable must be set.

# How to use
1. Clone every repository in the organization next to each other
2. Go to the folder containing everything and run the build script `python3 klepto-build/build.py`
3. If it whines that something is missing, which it probably will because I didn't fork everything, clone it from the corresponding upstream Swift repository and checkout the right branch (same branch name as the `swift` repository)

There are options to build individual parts of the toolchain, create a targz package, make a dry run... Use `python3 klepto-build/build.py --help` to list them.
