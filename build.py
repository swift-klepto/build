"""
 This source file is part of the Swift.org open source project

 Copyright (c) 2014 - 2021 Apple Inc. and the Swift project authors
 Licensed under Apache License v2.0 with Runtime Library Exception

 See http://swift.org/LICENSE.txt for license information
 See http://swift.org/CONTRIBUTORS.txt for Swift project authors

 -------------------------------------------------------------------------
"""

import json
import platform
import re
import tarfile
from argparse import ArgumentParser
from dataclasses import dataclass
from enum import Enum
from os import getenv, symlink
from pathlib import Path
from shutil import copytree, ignore_patterns, which
from subprocess import DEVNULL, PIPE, STDOUT, CompletedProcess
from subprocess import check_output as check_subprocess_output
from subprocess import run as run_subprocess
from typing import Callable

import lsb_release


# Util functions
def fail(message: str):
    print(f"!!! {message}")
    exit(1)


def run_command(command: list, *args, cwd=None, **kwargs) -> CompletedProcess:
    print(
        f">>> Running '{' '.join(command)}'{(' in ' + str(Path(cwd).absolute())) if cwd else ''}"
    )
    return run_subprocess(command, *args, cwd=cwd, **kwargs)


# Data classes
class Configuration(Enum):
    RELEASE = "release"
    DEBUG = "debug"


@dataclass
class Product:
    name: str
    install_path: str  # install path relative to install destdir

    # (
    #   install_path: Path,
    #   configuration: Configuration,
    #   devkitpro_path: Path,
    #   icu_path: Path,
    #   versions_str: str,
    #   reconfigure: bool,
    # ) -> None
    build_and_install_func: Callable[
        [Path, Configuration, Path],
        None,
    ]


# Products
def build_toolchain(
    install_path: Path,
    configuration: Configuration,
    devkitpro_path: Path,
    icu_path: Path,
    versions_str: str,
    reconfigure: bool,  # unimplemented for toolchain
):
    preset = {
        Configuration.RELEASE.value: "libnx_release",
        Configuration.DEBUG.value: "libnx_debug",
    }[configuration]

    result = run_command(
        [
            "python3",
            "./swift/utils/build-script",
            "-j1",  # for safety, the LLVM and clang linking steps like to eat RAM
            f"--preset={preset}",
            f"devkitpro_path={devkitpro_path.absolute()}",
            f"install_destdir={install_path.absolute()}",
            f"libnx_icu_path={icu_path.absolute()}",
            f"versions_str=klepto-toolchain-{versions_str}",
        ]
    )
    if result.returncode != 0:
        fail(f"Failed to build toolchain with preset {preset}")


def build_swiftpm(
    install_path: Path,
    configuration: Configuration,
    devkitpro_path: Path,
    icu_path: Path,
    versions_str: str,
    reconfigure: bool,
):
    configuration_arg = {
        Configuration.RELEASE.value: "--release",
        Configuration.DEBUG.value: "",
    }[configuration]

    def _bootstrap(command: str):
        return run_subprocess(
            [
                "python3",
                "Utilities/bootstrap",
                command,
                "-v",
                "--prefix",
                install_path.absolute(),
                "--reconfigure" if reconfigure else "",
                configuration_arg,
                # TODO: change build dir to build/swiftpm
            ],
            cwd="klepto-swiftpm",
        )

    if _bootstrap("build").returncode != 0:
        fail("Could not build swiftpm")

    if _bootstrap("install").returncode != 0:
        fail("Could not install swiftpm")


def build_icu(
    install_path: Path,
    configuration: Configuration,
    devkitpro_path: Path,
    icu_path: Path,
    versions_str: str,
    reconfigure: bool,
):
    # TODO: actually build it instead of asking users to manually build and place it

    libicu_path = Path("libicuuc-libnx")
    if not libicu_path.exists():
        fail(
            "libicuuc not found, please build it with devkitA64 + libnx and "
            f"place it at {libicu_path.absolute()}"
        )

    # Only copy useful folders and files for linking (headers are unused)
    useful_folders = ["lib", "stubdata"]
    ignore_files = ["*.so.*", "*.so", "*.ao", "*.o", "*.d", "Makefile"]

    for folder in useful_folders:
        copytree(
            libicu_path / folder,
            install_path / folder,
            ignore=ignore_patterns(*ignore_files),
            dirs_exist_ok=True,
        )


def build_frontend(
    install_path: Path,
    configuration: Configuration,
    devkitpro_path: Path,
    icu_path: Path,
    versions_str: str,
    reconfigure: bool,
):
    # Install frontend
    frontend_path = Path("klepto-frontend")
    copytree(frontend_path, install_path, dirs_exist_ok=True)

    # Make ../klepto -> klepto-frontend symbolic link
    klepto_path = install_path.parent / "klepto"
    if klepto_path.exists():
        klepto_path.unlink()
    symlink(frontend_path / "klepto-frontend", klepto_path)


def _get_clang_cflags(devkitpro_path: Path):
    gcc_path = devkitpro_path / "devkitA64" / "bin" / "aarch64-none-elf-gcc"
    gpp_path = devkitpro_path / "devkitA64" / "bin" / "aarch64-none-elf-g++"

    # Run devkitA64 gcc for isystem paths
    gcc_process = run_subprocess(
        [str(gcc_path.absolute()), "-xc++", "-E", "-Wp,-v", "-"],
        stdin=DEVNULL,
        stdout=PIPE,
        stderr=STDOUT,
    )
    include_paths = [
        f"/{path}"  # regex eats first / so put it back in
        for path in re.findall(
            r"^\s+\/(.*?)$", gcc_process.stdout.decode(), flags=re.MULTILINE
        )
    ] + [str((devkitpro_path / "libnx" / "include").absolute())]

    isystems = [f"-isystem{path}" for path in include_paths]

    return [
        "-Wno-gnu-include-next",
        "-D__SWITCH__",
        "-D__DEVKITA64__",
        "-D__unix__",
        "-D__linux__",
        "-fPIE",
        "-nostdinc",
        "-nostdinc++",
        "-D_POSIX_C_SOURCE=200809",
        "-D_GNU_SOURCE",
        # libnx already included in isystem
        f"-I{str(devkitpro_path.absolute())}/portlibs/switch/include/",
        "-mno-tls-direct-seg-refs",
        "-Qunused-arguments",
        "-Xclang",
        "-target-feature",
        "-Xclang",
        "+read-tp-soft",
        "-ftls-model=local-exec",
    ] + isystems


def build_libdispatch(
    install_path: Path,
    configuration: Configuration,
    devkitpro_path: Path,
    icu_path: Path,
    versions_str: str,
    reconfigure: bool,
):
    source_dir = Path("klepto-libdispatch")
    build_dir = Path("build") / "libdispatch"

    # install_path is toolchain path
    toolchain_bindir = install_path / "usr" / "bin"
    clang = toolchain_bindir / "clang"
    clangpp = toolchain_bindir / "clang++"

    build_dir.mkdir(exist_ok=True, parents=True)

    cflags = _get_clang_cflags(devkitpro_path)
    cflags += ["-DDISPATCH_USE_OS_DEBUG_LOG", "-U__linux__"]

    # Run cmake
    run_subprocess(
        [
            "cmake",
            "-G",
            "Ninja",
            str(source_dir.absolute()),
            f"-DCMAKE_C_COMPILER={str(clang.absolute())}",
            f"-DCMAKE_CXX_COMPILER={str(clangpp.absolute())}",
            "-DBUILD_SHARED_LIBS:BOOL=NO",
            f"-DCMAKE_C_FLAGS={' '.join(cflags)}",
            f"-DCMAKE_CXX_FLAGS={' '.join(cflags)}",
        ],
        cwd=str(build_dir.absolute()),
    )

    # Run ninja
    run_subprocess(
        ["ninja", "-v"],
        cwd=str(build_dir.absolute()),
    )


products = [
    # toolchain dependencies
    # TODO: this needs clang to work, which is part of toolchain, find a way to first build llvm+clang then dispatch then swift
    Product("libdispatch", "toolchain", build_libdispatch),
    Product("icu", "icu", build_icu),
    # swift + clang
    Product("toolchain", "toolchain", build_toolchain),
    # host tools
    Product("swiftpm", "swiftpm", build_swiftpm),
    Product("frontend", "klepto-frontend", build_frontend),
]

# Arguments parsing
parser = ArgumentParser(
    description=f"Builds and installs klepto (products: {', '.join([product.name for product in products])})."
)

for product in products:
    parser.add_argument(
        f"--only-{product.name}",
        action="store_true",
        help=f"only build and install {product.name} (can be used with other --only-* flags to build a subset of products)",
        dest=f"only_{product.name}",
    )

parser.add_argument(
    "--install-destdir",
    action="store",
    help="where to install klepto (default: ./dist/klepto-{version string})",
    default=None,
    dest="install_destdir",
)

parser.add_argument(
    "--configuration",
    action="store",
    choices=[conf.value for conf in Configuration],
    default=Configuration.RELEASE.value,
    help="configuration to build (default: release)",
    dest="configuration",
)

parser.add_argument(
    "--package",
    action="store",
    help=(
        "create a .tar.gz package of the installed products. "
        "can optionnally give a location (default: ./dist) (cannot be used with any --only-* flag)"
    ),
    dest="package",
    nargs="?",
    default=False,
    const="dist",
    required=False,
)

parser.add_argument(
    "--dry-run",
    action="store_true",
    help="don't build or install anything but still perform all the checks",
    dest="dry_run",
)

parser.add_argument(
    "--no-reconfigure",
    action="store_false",
    help="don't reconfigure before building",
    dest="reconfigure",
)

args = parser.parse_args()

# Arguments sanity check
if args.package:
    for product in products:
        if getattr(args, f"only_{product.name}"):
            fail(f"Cannot use --package with --only-{product.name}")

# Required software check
required_software = ["clang", "clang++", "swift", "python3", "cmake"]
for software in required_software:
    if not which(software):
        fail(f"Could not find {software}")


# Get devkitA64 and libnx versions from (dkp-)pacman
using = "dkp-pacman"
try:
    query = run_command(["dkp-pacman", "-Qe"], stdout=PIPE)
except FileNotFoundError:
    try:
        using = "pacman"
        query = check_subprocess_output(["pacman", "-Qe"])
    except FileNotFoundError:
        fail(
            "Could not find dkp-pacman or pacman to determine installed "
            "devkitA64 and libnx versions"
        )

# entry format: "{package name} {version}"
# entries are newline separated
entries = query.stdout.decode().strip().split("\n")
versions = dict(entry.split(" ") for entry in entries)

# Ensure devkitA64 and libnx are installed
if "devkitA64" not in versions:
    fail(f"devkitA64 does not seem to be installed (searched with {using})")

if "libnx" not in versions:
    fail(f"libnx does not seem to be installed (searched with {using})")

# Get swift version from cmake file
swift_cmakelists = Path("swift/CMakeLists.txt")
if not swift_cmakelists.exists():
    fail(f"Did not find swift source code at {swift_cmakelists.absolute()}")

with open(swift_cmakelists, "r") as file:
    result = re.search(r'set\(SWIFT_VERSION "(.*?)"\)', file.read())
    if not result:
        fail(f"Unable to parse SWIFT_VERSION from {swift_cmakelists.absolute()}")
    swift_version = result.group(1)

with open(swift_cmakelists, "r") as file:
    result = re.search(r'set\(KLEPTO_VERSION "(.*?)"\)', file.read())
    if not result:
        fail(f"Unable to parse KLEPTO_VERSION from {swift_cmakelists.absolute()}")
    klepto_version = result.group(1)


# Check for devkitpro and icu
icu_path = Path("libicuuc-libnx")
if not icu_path.exists():
    fail(
        f"Directory {icu_path.absolute()} was not found, please build libicuuc and place it there"
    )

devkitpro_path = getenv("DEVKITPRO")
if not devkitpro_path:
    fail("DEVKITPRO environment variable is not set, cannot continue")

devkitpro_path = Path(devkitpro_path)
if not devkitpro_path.exists():
    fail(
        f"Directory {devkitpro_path.absolute()} was not found, please check the DEVKITPRO environment variable"
    )

versions_str = (
    f"swift[{swift_version}]+dkA64[{versions['devkitA64']}]+lnx[{versions['libnx']}]"
)

# Prepare build
configuration = args.configuration
platform_string = f"{lsb_release.get_distro_information()['ID'].lower()}{lsb_release.get_distro_information()['RELEASE']}-{platform.processor()}"
dist_name = f"klepto-{klepto_version}-{configuration.upper()}-{platform_string}"

install_destdir = args.install_destdir
if not install_destdir:
    install_destdir = Path("dist") / dist_name
else:
    install_destdir = Path(install_destdir)

products_to_build = []

for product in products:
    if getattr(args, f"only_{product.name}"):
        products_to_build += [product]

products_to_build = products_to_build or products

print(
    f">>> Prepared build for {', '.join([product.name for product in products_to_build])} in {install_destdir.absolute()}"
)

# Build and install all products
install_destdir.mkdir(parents=True, exist_ok=True)

for product in products_to_build:
    product_destdir = install_destdir / product.install_path
    product_destdir.mkdir(parents=True, exist_ok=True)

    print(f">>> Building and installing {product.name} in {product_destdir.absolute()}")

    if not args.dry_run:
        product.build_and_install_func(
            product_destdir,
            configuration,
            devkitpro_path,
            icu_path,
            versions_str,
            args.reconfigure,
        )

# Build manifest
manifest_file = install_destdir / "manifest.json"

print(f">>> Writing manifest to {manifest_file.absolute()}")

# Filter out irrelevant versions (deko3d, devkita64-cmake, devkitA64-gdb...)
relevant_packages = ["libnx", "devkitA64"]
versions = {
    package: version
    for package, version in versions.items()
    if package in relevant_packages
}

# Add swift and klepto
versions["swift"] = swift_version
versions["klepto"] = klepto_version

with open(manifest_file, "w") as f:
    manifest = {
        "versions": versions,
    }
    f.write(json.dumps(manifest))

print(
    f">>> Done building {', '.join([product.name for product in products_to_build])} in {install_destdir.absolute()}"
)

# Package if requested
if args.package:
    package_file = Path(args.package) / f"{dist_name}.tar.gz"

    package_file.parent.mkdir(exist_ok=True, parents=True)

    print(f">>> Writing {str(package_file.absolute())}")

    with tarfile.open(package_file, mode="w:gz") as tfile:
        tfile.add(install_destdir, arcname=dist_name)

    print(f">>> Done writing {str(package_file.absolute())}")
