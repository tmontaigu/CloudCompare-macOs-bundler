#!/usr/bin/env python3

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Dict, List, Optional, Union, Any, NoReturn
import os
from dataclasses import dataclass
from pathlib import Path
import sys
import logging

import subprocess
import multiprocessing
import platform
import shutil
from subprocess import CalledProcessError
import re
import argparse
import hashlib

# TODO: avoid redoing some steps like extraction, configuration, building if it was already done and
#       shit did not change

# https://cmake.org/cmake/help/latest/manual/cmake-toolchains.7.html
# https://stackoverflow.com/questions/24659753/cmake-find-library-and-cmake-find-root-path
# https://cmake.org/cmake/help/latest/variable/CMAKE_FIND_ROOT_PATH_MODE_LIBRARY.html
# https://clang.llvm.org/docs/CrossCompilation.html

# https://clang.llvm.org/docs/CommandGuide/clang.html


class Config:
    def __init__(
        self,
        target_arch: Optional[str] = None,
        target_os_version: Optional[str] = None,
        num_jobs: Optional[int] = None,
    ) -> None:
        if target_arch is None:
            target_arch = platform.machine()

        if target_os_version is not None and platform.system() != 'Darwin':
            raise ValueError("target_os_version is only for macos")
        elif target_os_version is None and platform.system() == 'Darwin':
            if target_arch == 'x86_64':
                target_os_version = "10.15"
            elif target_arch == "arm64":
                target_os_version = "11.0"
            else:
                raise ValueError(f"Unknown arch {target_arch}")

        if num_jobs is None:
            num_jobs = multiprocessing.cpu_count()

        self.target_arch = target_arch
        self.target_os_version = target_os_version
        self.num_jobs = num_jobs

        # Some important path which will be needed throughout:
        self.script_dir: Path = Path(__file__).parent
        self.working_dir: Path = self.script_dir / "workdir"
        # Where sources will be downloaded and re-used between target-arch build
        self.sources_dir: Path = self.working_dir / "sources"
        self.arch_dir: Path = self.working_dir / self.target_arch
        # Where build folder for each dependencies will be stored
        self.build_dir: Path = self.arch_dir / "builds"

        # Root path where depdencies built will be installed
        self.install_root: Path = self.arch_dir / "install"
        self.install_lib: Path = self.install_root / 'lib'
        self.install_bin: Path = self.install_root / 'bin'
        self.install_include: Path = self.install_root / 'include'
        self.pkg_config_path: Path = self.install_lib / 'pkgconfig'

        self.working_dir.mkdir(exist_ok=True, parents=True)
        self.sources_dir.mkdir(exist_ok=True, parents=True)
        self.arch_dir.mkdir(exist_ok=True, parents=True)
        self.build_dir.mkdir(exist_ok=True, parents=True)

        # The environment variables that should be set when running a command
        self.env_vars: Dict[str, str] = {
            **os.environ,
            "PKG_CONFIG_PATH": str(self.pkg_config_path),
        }
        self.env_vars['PATH'] = f"{str(self.install_bin)}:{self.env_vars['PATH']}"

        # Dict with default config that should be used by the CMake build system
        self.cmake_default_config_opts = {
            "-G": "Ninja",
            "-DCMAKE_BUILD_TYPE": "Release",
            "-DCMAKE_INSTALL_PREFIX": str(self.install_root),
            # Otherwise, on some Linux some lib would be in /lib and others in /lib64
            "-DCMAKE_INSTALL_LIBDIR": str(self.install_lib),
            "-DCMAKE_PREFIX_PATH": str(self.install_lib / "cmake") + ";" + str(self.install_root),
            "-DCMAKE_INCLUDE_PATH": str(self.install_include),
            "-DCMAKE_LIBRARY_PATH": str(self.install_lib),
            # "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM": "NEVER",
            # "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY": "ONLY",
            # "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE": "ONLY",
            # "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE": "ONLY",
            "-DCMAKE_FIND_ROOT_PATH": str(self.install_root),
            # "-DCMAKE_SKIP_RPATH": "TRUE",
        }

        if platform.system() == "Darwin":
            self.cmake_default_config_opts['-DCMAKE_OSX_ARCHITECTURES'] = self.target_arch
            self.cmake_default_config_opts['-DCMAKE_OSX_DEPLOYMENT_TARGET'] = self.target_os_version

        # List of default options for Autotools build system
        self.default_autotools_config_opts: List[str] = [
            f"--prefix={str(self.install_root)}"
        ]

        # aka CPPFLAGS, used by make / autotools
        self.compiler_preprocessor_flags: str = f"-I{self.install_include}"

        # aka CFLAGS (for C) or CXXFLAGS (for C++)
        # used by make / autotools
        self.compiler_flags: str = "-O3"

        # aka LDFLAGS
        self.linker_flags: str = f"-L{self.install_lib}"

        if platform.system() == "Darwin":
            assert self.target_os_version is not None, "Target OS Version not set"
            macos_version_flag = f" -mmacosx-version-min={self.target_os_version}"
            self.compiler_flags += macos_version_flag
            self.linker_flags += macos_version_flag

            if self.target_arch != platform.machine():
                # The 'target-triple' to cross compile with clang
                target_triple = f"{self.target_arch}-apple-darwin"
                self.compiler_flags += f' --target={target_triple}'
                self.default_autotools_config_opts.append(f'--host={target_triple}')

                self.compiler_flags += f' -arch {self.target_arch}'
                self.linker_flags += f' -arch {self.target_arch}'


    @classmethod
    def from_cmdline(cls):
        parser = argparse.ArgumentParser(description="Downloads and builds the dependencies for CloudCompare on macOS")

        parser.add_argument("arch", help="The arch for which cloud compare should be build")
        parser.add_argument("macos_version", help="The minimum macOS version targeted", metavar="macos-version")
        parser.add_argument("--num-jobs", help="Number of jobs / threads for the build", default=None)

        args = parser.parse_args()

        return cls(
            target_arch=args.arch,
            target_os_version=args.macos_version,
            num_jobs=args.num_jobs
        )


CONFIG: Config = Config.from_cmdline()
CONFIG.working_dir.mkdir(exist_ok=True)

# Commands
CMAKE = "cmake"
CURL = "curl"
MAKE = "make"
GIT = 'git'

CAPTURE=False

LOGGER = logging.getLogger(__name__)


def run_command(*args, **kwargs):
    if CAPTURE:
        stdout, stderr = subprocess.PIPE, subprocess.STDOUT
    else:
        stdout, stderr = sys.stdout, sys.stderr

    LOGGER.debug(f"Running Command {args[0]}")
    # print(" ".join(str(args[0])))
    subprocess.run(*args, **kwargs, check=True, stdout=stdout, stderr=stderr, env=CONFIG.env_vars)


def maybe_log_subprocess_error(exc: subprocess.CalledProcessError) -> NoReturn:
    if CAPTURE:
        logging.critical(exc.stdout.decode())
    exit(1)


@contextmanager
def set_directory(path: Union[Path, str]):
    """Sets the cwd within the context

    Args:
        path (Path): The path to the cwd

    Yields:
        None
    """

    origin = Path().absolute()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(origin)


class BuildSystem(ABC):
    @abstractmethod
    def configure(self, *args, **kwargs):
        ...

    @abstractmethod
    def build(self, *args, **kwargs):
        ...

    @abstractmethod
    def install(self, *args, **kwargs):
        ...


class SourceDistribution(ABC):
    @abstractmethod
    def download_to(self, output_dir: str) -> str:
        ...

    @abstractmethod
    def verify_checksum(self):
        ...


def parse_top_level_dir_of_tar(archive_path: str) -> str:
    pc = subprocess.run(["tar", "-tf", archive_path], check=True, stdout=subprocess.PIPE)
    lines = pc.stdout.decode().splitlines()
    regex = re.compile("^[^/]+/?$")
    matches = []
    for line in lines:
        if regex.match(line) is not None:
            matches.append(line)

    # The above may not be correct for some tarballs
    # try something else
    if len(matches) != 1:
        top_levels = set()
        for line in lines:
            top_levels.add(Path(line).parts[0])

        assert len(top_levels) == 1
        matches = list(top_levels)

    assert len(matches) == 1
    return matches[0]


def extract_archive(archive_path: str, dest_folder):

    if archive_path.endswith(".tar.gz"):
        subprocess.run(['tar', '-xzf', archive_path, '-C', dest_folder])
        extract_dir_name = parse_top_level_dir_of_tar(archive_path)
        return str(Path(dest_folder) / extract_dir_name)

    if archive_path.endswith(".tar.xz"):
        subprocess.run(['tar', '-xf', archive_path, '-C', dest_folder])
        extract_dir_name = parse_top_level_dir_of_tar(archive_path)
        return str(Path(dest_folder) / extract_dir_name)

    if archive_path.endswith(".tar.bz2"):
        subprocess.run(['tar', '-xjf', archive_path, '-C', dest_folder])
        extract_dir_name = parse_top_level_dir_of_tar(archive_path)
        return str(Path(dest_folder) / extract_dir_name)

    raise NotImplementedError


@dataclass
class Dependency:
    name: str
    # version: str
    source: SourceDistribution
    build_system: BuildSystem

    def handle(self) -> None:
        our_source_dir = CONFIG.sources_dir / self.name
        our_source_dir.mkdir(exist_ok=True)

        try:
            extracted_dir = self.source.download_to(str(our_source_dir))
            LOGGER.debug(f"Sources are ready at: {extracted_dir}")
        except CalledProcessError as e:
            # maybe LOGGER.exception ?
            LOGGER.critical("Failed to download sources")
            maybe_log_subprocess_error(e)

        our_build_dir = CONFIG.build_dir / self.name
        our_build_dir.mkdir(exist_ok=True)

        # if not is_dir_empty(our_build_dir):
        #     LOGGER.debug("Build dir not empty, doing nothing")
        #     return

        LOGGER.debug("Configuring")
        try:
            self.build_system.configure(
                source_dir=str(extracted_dir),
                build_dir=str(our_build_dir)
            )
        except CalledProcessError as e:
            LOGGER.critical(f"Failed to configure {self.name}")
            maybe_log_subprocess_error(e)

        LOGGER.debug("Building")
        self.build_system.build(
                build_dir=str(our_build_dir)
        )

        LOGGER.debug("Installing")
        try:
            self.build_system.install(
                build_dir=str(our_build_dir)
            )
        except CalledProcessError as e:
            LOGGER.critical(f"Failed to install {self.name}")
            maybe_log_subprocess_error(e)


def is_dir_empty(path: str) -> bool:
    iter = Path(path).iterdir()

    try:
        next(iter)
    except StopIteration:
        return True
    else:
        return False


class InternetArchive(SourceDistribution):
    def __init__(self, url: str, expected_hash: Optional[str]) -> None:
        self.url = url
        self.expected_hash = expected_hash

    def download_to(self, output_dir: str) -> str:
        # --location permet de récupérer le fichier même si la page a changé de location
        # subprocess.run(['wget', '-P', output_dir, self.url], check=True)

        archive_name = Path(self.url).name
        local_archive_path = Path(output_dir) / archive_name

        if local_archive_path.exists():
            LOGGER.debug('Sources are already downloaded')
        else:
            LOGGER.debug('Start downloading sources')
            run_command([CURL, '--location', self.url, '-o', local_archive_path])
            LOGGER.debug('Sources successfully downloaded')

        expected_extracted_dir = Path(output_dir) / parse_top_level_dir_of_tar(str(local_archive_path))
        if expected_extracted_dir.exists():
            LOGGER.debug("Archive already extracted")
            return str(expected_extracted_dir)
        else:
            LOGGER.debug("Extracting sources")
            extracted_dir = extract_archive(str(local_archive_path), output_dir)
            LOGGER.debug(f"Sources extracted to {extracted_dir}")
            assert extracted_dir == str(expected_extracted_dir), f"{extracted_dir} is not the same as {expected_extracted_dir}"
            return str(extracted_dir)

    def verify_checksum(self):
        return NotImplementedError
        # if self.expected_hash is not None:
        #     algo, expected_hash = self.expected_hash.split(':')
        #     if algo == 'sha256':
        #         hashlib.sha256()
        #     else:
        #         raise NotImplementedError


class GitRepo(SourceDistribution):
    def __init__(self, url: str, ref: str, after_commands: Optional[List[str]] = None):
        self.url = url
        self.ref = ref
        self.after_commands = after_commands if after_commands is not None else []

    def download_to(self, output_dir: str) -> str:
        base_name = Path(self.url).name

        output_dir = Path(output_dir) / base_name
        if output_dir.exists():
            LOGGER.debug('Project already cloned')
            return str(output_dir)

        LOGGER.debug('Cloning project')
        run_command([GIT, 'clone', self.url, str(output_dir)])

        saved_cwd = os.getcwd()
        os.chdir(str(output_dir))
        run_command(
            [GIT, 'checkout', self.ref],
        )

        for command in self.after_commands:
            run_command(command.split())
        os.chdir(saved_cwd)

        return str(output_dir)

    def verify_checksum(self):
        pass


class CMake(BuildSystem):
    def __init__(self, configure_options: Dict[str, str] = None) -> None:
        if configure_options is None:
            self.configure_options = {**CONFIG.cmake_default_config_opts}
        else:
            self.configure_options = {**CONFIG.cmake_default_config_opts, **configure_options}

    def configure(self, source_dir: str, build_dir: str):
        options_as_cmd_args = []
        for (key, value) in self.configure_options.items():
            options_as_cmd_args.append(f"{key}={value}")
        run_command([CMAKE, '-S', source_dir, '-B', build_dir] + options_as_cmd_args)

    def build(self, build_dir: str):
        run_command([CMAKE, '--build', build_dir, f'-j{CONFIG.num_jobs}'])

    def install(self, build_dir: str):
        run_command([CMAKE, '--install', build_dir])
 

class Autotools(BuildSystem):
    def __init__(self, configure_options: List[str] = None, supports_out_of_tree_build=True) -> None:
        if configure_options is not None:
            self.configure_options = [*CONFIG.default_autotools_config_opts, *configure_options]
        else:
            self.configure_options = [*CONFIG.default_autotools_config_opts]
        self.supports_out_of_tree_build = supports_out_of_tree_build

    def configure(self, source_dir: str, build_dir: str):
        if self.supports_out_of_tree_build:
            with set_directory(build_dir):
                run_command(
                    [
                        f"{source_dir}/configure",
                        f"CPPFLAGS={CONFIG.compiler_preprocessor_flags}",
                        f"CXXFLAGS={CONFIG.compiler_flags}",
                        f"CFLAGS={CONFIG.compiler_flags}",
                        f"LDFLAGS={CONFIG.linker_flags}",
                    ] + self.configure_options,
                )
        else:
            if is_dir_empty(build_dir): 
                LOGGER.debug("Out of tree build not supported, copying sources to build dir")
                shutil.copytree(src=source_dir, dst=build_dir, dirs_exist_ok=True)
            with set_directory(build_dir):
                run_command(
                    [
                        f"./configure",
                        f"CPPFLAGS={CONFIG.compiler_preprocessor_flags}",
                        f"CXXFLAGS={CONFIG.compiler_flags}",
                        f"CFLAGS={CONFIG.compiler_flags}",
                        f"LDFLAGS={CONFIG.linker_flags}",
                    ] + self.configure_options,
                )

    def build(self, build_dir: str):
        with set_directory(build_dir):
            run_command([MAKE, f'-j{CONFIG.num_jobs}'])

    def install(self, build_dir: str):
        with set_directory(build_dir):
            run_command([MAKE, 'install'])


class Qt5Build(Autotools):
    # https://wiki.qt.io/Building_Qt_5_from_Git#Getting_the_source_code
    def __init__(self):
        super().__init__()
    
    def configure(self, source_dir: str, build_dir: str):
        # https://github.com/qbittorrent/qBittorrent/wiki/Compilation:-macOS-(x86_64,-arm64,-cross-compilation)
        with set_directory(build_dir):
            command = [
                f"{source_dir}/configure",
                "-release",
                "-nomake", "examples",
                "-nomake", "tests",
                "-opensource",
                "-confirm-license",
                "-skip", "qtwebengine",
                "-skip", "qt3d",
                "-qt-pcre",
                "-qt-libjpeg",
                "-qt-freetype",
                "-platform", "macx-clang",
                "-prefix", str(CONFIG.install_root),
            ]

            if platform.system() == "Darwin":
                command.append(f"QMAKE_APPLE_DEVICE_ARCHS={CONFIG.target_arch}")
                command.append(f"QMAKE_MACOSX_DEPLOYMENT_TARGET={CONFIG.target_os_version}")

            run_command(command)


class BoostBuildSystem(BuildSystem):
    # Useful for cross compilation
    def configure(self, source_dir: str, build_dir: str):
        if is_dir_empty(build_dir):
            LOGGER.debug("Out of tree build not supported, copying sources to build dir")
            shutil.copytree(src=source_dir, dst=build_dir, dirs_exist_ok=True)

        with set_directory(build_dir):
            cxx_flags_value = f"-arch {CONFIG.target_arch}"
            c_flags = cxx_flags_value
            linkflags = f"-arch {CONFIG.target_arch}"
            run_command([
                f'./bootstrap.sh',
                f'cxxflags={cxx_flags_value}',
                f'cflags={c_flags}',
                f'linkflags={linkflags}',
                f"--prefix={CONFIG.install_root}"
            ])

    def create_b2_command(self) -> List[str]:
        # arm64 -> arm, x86_64 -> x86
        architecture = CONFIG.target_arch[:3]

        command = [
            './b2',
            f'cxxflags={CONFIG.compiler_flags}',
            f'cflags={CONFIG.compiler_flags}',
            f'linkflags={CONFIG.linker_flags}',
            'target-os=darwin',
            f'architecture={architecture}',
        ]

        if CONFIG.target_arch == 'x86_64' and CONFIG.target_arch != platform.machine():
            # We are cross compiling to x86_64
            command.append('abi=sysv')
            command.append('binary-format=mach-o')
            command.append('-a')

        return command

    def build(self, build_dir: str):
        command = self.create_b2_command()
        with set_directory(build_dir):
            run_command(command)

    def install(self, build_dir: str):
        command = self.create_b2_command()
        command.append("install")
        with set_directory(build_dir):
            run_command(command)


DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="Qt5",
        source=GitRepo(
            url="git://code.qt.io/qt/qt5.git",
            ref="v5.15.2",
            # TODO: move this to configure step of build system ?
            after_commands=[
                "./init-repository --module-subset=default,-qtwebengine",
            ]
        ),
        build_system=Qt5Build()
    ),
    Dependency(
        name="GMP",
        source=InternetArchive(
            url="https://gmplib.org/download/gmp/gmp-6.2.1.tar.xz",
            expected_hash=None,
        ),
        build_system=Autotools(),
    ),
    Dependency(
        name="MPFR",
        source=InternetArchive(
            url='https://www.mpfr.org/mpfr-current/mpfr-4.1.0.tar.xz',
            expected_hash=None,
        ),
        build_system=Autotools()
    ),
    Dependency(
        name="boost",
        source=InternetArchive(
            url='https://boostorg.jfrog.io/artifactory/main/release/1.78.0/source/boost_1_78_0.tar.gz',
            # expected_hash='94ced8b72956591c4775ae2207a9763d3600b30d9d7446562c552f0a14a63be7' sha256
            expected_hash=None,
        ),
        build_system=BoostBuildSystem()
    ),
    Dependency(
        # https://doc.cgal.org/latest/Manual/installation.html#installation_configwithcmake
        name="CGAL",
        source=InternetArchive(
            url="https://github.com/CGAL/cgal/releases/download/v5.4/CGAL-5.4-library.tar.xz",
            expected_hash=None,
        ),
        build_system=CMake()
    ),
    Dependency(
        name="libtiff",
        source=InternetArchive(
            url="http://download.osgeo.org/libtiff/tiff-4.3.0.tar.gz",
            expected_hash=None
        ),
        build_system=CMake()
    ),
    Dependency(
        name='sqlite',
        source=InternetArchive(
            url='https://sqlite.org/2021/sqlite-autoconf-3360000.tar.gz',
            expected_hash=None,
        ),
        build_system=Autotools(),
    ),
    Dependency(
        name='proj',
        source=InternetArchive(
            url="http://download.osgeo.org/proj/proj-8.1.0.tar.gz",
            expected_hash=None,
        ),
        build_system=Autotools(
            configure_options=['--without-curl']
        ),
        # source=InternetArchive(
        #     # url="http://download.osgeo.org/proj/proj-8.1.0.tar.gz",
        #     url="http://download.osgeo.org/proj/proj-9.0.0.tar.gz",
        #     expected_hash=None,
        # ),
        # build_system=CMake(
        #   configure_options={
        #     "-DBUILD_APPS": "OFF",
        #     "-DENABLE_CURL": "OFF",
        #     "-DBUILD_TESTING": "OFF"
        #   }
        # )
    ),
    Dependency(
        name='libgeotiff',
        source=InternetArchive(
            url="http://download.osgeo.org/geotiff/libgeotiff/libgeotiff-1.7.0.tar.gz",
            expected_hash=None,
        ),
        build_system=Autotools(),
    ),
    Dependency(
        name="png",
        source=InternetArchive(
            url='http://prdownloads.sourceforge.net/libpng/libpng-1.6.37.tar.xz',
            expected_hash=None,
        ),
        build_system=Autotools()
    ),
    Dependency(
        name='gdal',
        source=InternetArchive(
            url='https://github.com/OSGeo/gdal/releases/download/v3.3.1/gdal-3.3.1.tar.gz',
            expected_hash=None,
        ),
        build_system=Autotools(
            configure_options=['--with-python=no'],
            supports_out_of_tree_build=False,
        )
    ),
    Dependency(
        name="eigen",
        source=InternetArchive(
            url='https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz',
            expected_hash=None,
        ),
        build_system=CMake(),

    ),
    Dependency(
        name='laz-perf',
        source=GitRepo(
            url="https://github.com/hobu/laz-perf",
            ref="2.1.0",
        ),
        build_system=CMake(
          configure_options={
            "-DWITH_TESTS": "FALSE",
          }
        )
    ),
    Dependency(
        name='LASzip',
        source=InternetArchive(
            url="https://github.com/LASzip/LASzip/releases/download/3.4.3/laszip-src-3.4.3.tar.gz",
            expected_hash=None,
        ),
        build_system=CMake()
    ),
    # pdal does not seems to be able to find lazperf properly
    Dependency(
      name='pdal',
      # # Source disrib seems to cause some weird errors at configure time
      # source=InternetArchive(
      #   url='https://github.com/PDAL/PDAL/releases/download/2.3.0/PDAL-2.3.0-src.tar.gz',
      #   expected_hash=None,
      # ),
      source=GitRepo(
        url="https://github.com/PDAL/PDAL",
        ref="2.3.0",
      ),
      build_system=CMake(
          configure_options={
              "-DWITH_TESTS": "OFF",
              # "-DWITH_LAZPERF": "ON",
              "-DCMAKE_FIND_FRAMEWORK": "NEVER",
              "-DWITH_LASZIP": "ON",
              # "-DLazperf_DIR": f"{CONFIG.install_lib / 'cmake' / 'lazperf' }",

              # "-DCMAKE_MACOSX_RPATH": "OFF",
          }
      )
    ),
    Dependency(
        name="dlib",
        source=InternetArchive(
            url='http://dlib.net/files/dlib-19.23.tar.bz2',
            expected_hash=None,
        ),
        build_system=CMake(
            configure_options={
                "-DCMAKE_CXX_FLAGS": "-DDLIB_NO_GUI_SUPPORT",
            }
        ),
    ),
    Dependency(
        name="flann",
        source=InternetArchive(
            url="https://github.com/flann-lib/flann/archive/refs/tags/1.9.1.tar.gz",
            expected_hash="sha256:b23b5f4e71139faa3bcb39e6bbcc76967fbaf308c4ee9d4f5bfbeceaa76cc5d3",
        ),
        build_system=CMake(
            configure_options={
                "-DBUILD_C_BINDINGS": "OFF",
                "-DBUILD_EXAMPLES": "OFF",
                "-DBUILD_TESTS": "OFF",
                "-DBUILD_DOC": "OFF",
            }
        )
    ),
    Dependency(
        name="PCL",
        source=InternetArchive(
            url='https://github.com/PointCloudLibrary/pcl/archive/pcl-1.11.0.tar.gz',
            expected_hash="sha256:4255c3d3572e9774b5a1dccc235711b7a723197b79430ef539c2044e9ce65954"
        ),
        build_system=CMake(
            configure_options={
                "-DWITH_LIBUSB": 'OFF',
                "-DWITH_QT": "OFF",
                "-DDWITH_VTK": "OFF",
                "-DDWITH_PCAP": "OFF",
                "-DPCL_ONLY_CORE_POINT_TYPES": 'ON',
                "-DBUILD_2d": "ON",
                "-DBUILD_CUDA": "OFF",
                "-DBUILD_GPU": "ON",
                "-DBUILD_apps": "OFF",
                "-DBUILD_examples": "OFF",
                "-DBUILD_common": "ON",
                "-DBUILD_geometry": "OFF",
                "-DBUILD_stereo": "OFF",
                "-DBUILD_registration": "OFF",
                "-DBUILD_recognition": "OFF",
                "-DBUILD_segmentation": "OFF",
                "-DBUILD_simulation": "OFF",
            }
        )
    ),
    # Needed for E57 plugin
    Dependency(
        name="Xerces-C",
        source=InternetArchive(
            url="https://dlcdn.apache.org//xerces/c/3/sources/xerces-c-3.2.3.tar.gz",
            expected_hash="sha256:fb96fc49b1fb892d1e64e53a6ada8accf6f0e6d30ce0937956ec68d39bd72c7e",
        ),
        build_system=CMake()
    )
]


def main():
    logging.basicConfig(
        level=logging.DEBUG
    )

    for dependency in DEPENDENCIES:
        LOGGER.info(f"Handling dependency named '{dependency.name}'")
        dependency.handle()

    run_command([
        'install_name_tool',
        '-change',
        '@executable_path/../lib/liblaszip.8.dylib',
        f"{CONFIG.install_lib / 'liblaszip.8.dylib'}",
        f"{CONFIG.install_lib / 'libpdalcpp.dylib'}",
    ])

    pcl_libs = CONFIG.install_lib.glob('libpcl_*')
    for lib in pcl_libs:
        run_command([
            'install_name_tool',
            '-change',
            'libflann_cpp.1.9.dylib',
            f'{CONFIG.install_lib / "libflann_cpp.1.9.dylib"}',
            str(lib)
        ])


if __name__ == '__main__':
    main()
    
    
    
    
    
    
    
    

'''
    If you ever happen to want to link against installed libraries
in a given directory, LIBDIR, you must either use libtool, and
specify the full pathname of the library, or use the '-LLIBDIR'
flag during linking and do at least one of the following:
   - add LIBDIR to the 'LD_LIBRARY_PATH' environment variable
     during execution
   - add LIBDIR to the 'LD_RUN_PATH' environment variable
     during linking
   - use the '-Wl,-rpath -Wl,LIBDIR' linker flag
   - have your system administrator add LIBDIR to '/etc/ld.so.conf'

See any operating system documentation about shared libraries for
more information, such as the ld(1) and ld.so(8) manual pages.
'''
