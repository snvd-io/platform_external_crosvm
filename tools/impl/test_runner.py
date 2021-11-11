#!/usr/bin/env python3
# Copyright 2021 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import functools
import json
import os
import random
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Set
import typing

import test_target
from test_target import TestTarget
import testvm
from test_config import CRATE_OPTIONS, TestOption

USAGE = """\
Runs tests for crosvm locally, in a vm or on a remote device.

To build and run all tests locally:

    $ ./tools/run_tests --target=host

To cross-compile tests for aarch64 and run them on a built-in VM:

    $ ./tools/run_tests --target=vm:aarch64

The VM will be automatically set up and booted. It will remain running between
test runs and can be managed with `./tools/aarch64vm`.

Tests can also be run on a remote device via SSH. However it is your
responsiblity that runtime dependencies of crosvm are provided.

    $ ./tools/run_tests --target=ssh:hostname

The default test target can be managed with `./tools/set_test_target`

To see full build and test output, add the `-v` or `--verbose` flag.
"""

Arch = test_target.Arch

# Print debug info. Overriden by -v
VERBOSE = False

# Kill a test after 60 seconds to prevent frozen tests from running too long.
TEST_TIMEOUT_SECS = 60

# Number of parallel processes for executing tests.
PARALLELISM = 4

CROSVM_ROOT = Path(__file__).parent.parent.parent.resolve()
COMMON_ROOT = CROSVM_ROOT / "common"


class ExecutableResults(object):
    """Container for results of a test executable."""

    def __init__(self, name: str, success: bool, test_log: str):
        self.name = name
        self.success = success
        self.test_log = test_log


class Executable(NamedTuple):
    """Container for info about an executable generated by cargo build/test."""

    binary_path: Path
    crate_name: str
    cargo_target: str
    is_test: bool
    is_fresh: bool

    @property
    def name(self):
        return f"{self.crate_name}:{self.cargo_target}"


def should_build_crate(crate: str, target_arch: Arch):
    options = CRATE_OPTIONS.get(crate, [])
    if TestOption.DO_NOT_BUILD in options:
        return False
    if TestOption.BUILD_ARM_ONLY in options:
        return target_arch == "aarch64" or target_arch == "armhf"
    if TestOption.BUILD_X86_ONLY in options:
        return target_arch == "x86_64"
    return True


def should_run_executable(executable: Executable, target_arch: Arch):
    options = CRATE_OPTIONS.get(executable.crate_name, [])
    if TestOption.DO_NOT_RUN in options:
        return False
    if TestOption.RUN_ARM_ONLY in options:
        return target_arch == "aarch64" or target_arch == "armhf"
    if TestOption.RUN_X86_ONLY in options:
        return target_arch == "x86_64"
    return True


def list_main_crates():
    yield "crosvm"
    for path in CROSVM_ROOT.glob("*/Cargo.toml"):
        yield path.parent.name


def list_common_crates():
    for path in COMMON_ROOT.glob("*/Cargo.toml"):
        yield path.parent.name


def cargo(
    cargo_command: str, cwd: Path, flags: list[str], env: dict[str, str]
) -> Iterable[Executable]:
    """
    Executes a cargo command and returns the list of test binaries generated.

    The build log will be hidden by default and only printed if the build
    fails. In VERBOSE mode the output will be streamed directly.

    Note: Exits the program if the build fails.
    """
    cmd = [
        "cargo",
        cargo_command,
        "--message-format=json-diagnostic-rendered-ansi",
        *flags,
    ]
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    messages: List[str] = []

    # Read messages as cargo is running.
    assert process.stdout
    for line in iter(process.stdout.readline, ""):
        # any non-json line is a message to print
        if not line.startswith("{"):
            if VERBOSE:
                print(line.rstrip())
            messages.append(line.rstrip())
            continue
        json_line = json.loads(line)

        # 'message' type lines will be printed
        if json_line.get("message"):
            message = json_line.get("message").get("rendered")
            if VERBOSE:
                print(message)
            messages.append(message)

        # Collect info about test executables produced
        elif json_line.get("executable"):
            yield Executable(
                Path(json_line.get("executable")),
                crate_name=json_line.get("package_id", "").split(" ")[0],
                cargo_target=json_line.get("target").get("name"),
                is_test=json_line.get("profile", {}).get("test", False),
                is_fresh=json_line.get("fresh", False),
            )

    if process.wait() != 0:
        if not VERBOSE:
            for message in messages:
                print(message)
        sys.exit(-1)


def cargo_build_executables(
    crates: List[str] = [],
    cwd: Path = Path("."),
    features: Set[str] = set(),
    env: Dict[str, str] = {},
) -> Iterable[Executable]:
    """Build all test binaries for the given list of crates."""
    flags: list[str] = []
    if features:
        flags += [
            "--no-default-features",
            "--features",
            ",".join(features),
        ]
    for crate in crates:
        flags += ["-p", crate]

    # Run build first, to make sure compiler errors of building non-test
    # binaries are caught.
    yield from cargo("build", cwd, flags, env)

    # Build all tests and return the collected executables
    yield from cargo("test", cwd, ["--no-run", *flags], env)


def build_common_crate(build_env: dict[str, str], crate_name: str):
    print(f"Building tests for: common/{crate_name}")
    return list(
        cargo_build_executables(
            [],
            env=build_env,
            cwd=COMMON_ROOT / crate_name,
        )
    )


def build_all_binaries(target: TestTarget, target_arch: Arch):
    """Discover all crates and build them."""
    build_env = os.environ.copy()
    build_env.update(test_target.get_cargo_env(target, target_arch))

    main_crates = [
        crate
        for crate in list_main_crates()
        if should_build_crate(crate, target_arch)
    ]

    print("Building tests for:", ", ".join(main_crates))
    yield from cargo_build_executables(
        main_crates, env=build_env, features=set(["all-linux"])
    )

    common_crates = [
        crate
        for crate in list_common_crates()
        if should_build_crate(crate, target_arch)
    ]

    with Pool(PARALLELISM) as pool:
        for executables in pool.imap(
            functools.partial(build_common_crate, build_env), common_crates
        ):
            yield from executables


def execute_test(target: TestTarget, executable: Executable):
    """
    Executes a single test on the given test targed

    Note: This function is run in a multiprocessing.Pool.

    Test output is hidden unless the test fails or VERBOSE mode is enabled.
    """
    options = CRATE_OPTIONS.get(executable.crate_name, [])
    args: list[str] = []
    if TestOption.SINGLE_THREADED in options:
        args += ["--test-threads=1"]

    if VERBOSE:
        print(f"Running test {executable.name}...")
    try:
        # Pipe stdout/err to be printed in the main process if needed.
        test_process = test_target.exec_file_on_target(
            target,
            executable.binary_path,
            args=args,
            timeout=TEST_TIMEOUT_SECS,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return ExecutableResults(
            executable.name,
            test_process.returncode == 0,
            test_process.stdout,
        )
    except subprocess.TimeoutExpired as e:
        # Append a note about the timeout to the stdout of the process.
        msg = f"\n\nProcess timed out after {e.timeout}s\n"
        return ExecutableResults(
            executable.name,
            False,
            e.stdout.decode("utf-8") + msg,
        )


def execute_all(
    executables: list[Executable],
    target: test_target.TestTarget,
    arch: Arch,
    repeat: int,
):
    """Executes all tests in the `executables` list in parallel."""
    executables = [e for e in executables if should_run_executable(e, arch)]
    if repeat > 1:
        executables = executables * repeat
        random.shuffle(executables)

    sys.stdout.write(f"Running {len(executables)} test binaries on {target}")
    sys.stdout.flush()
    with Pool(PARALLELISM) as pool:
        for result in pool.imap(
            functools.partial(execute_test, target), executables
        ):
            if not result.success or VERBOSE:
                msg = "passed" if result.success else "failed"
                print()
                print("--------------------------------")
                print("-", result.name, msg)
                print("--------------------------------")
                print(result.test_log)
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
            yield result
    print()


def find_crosvm_binary(executables: list[Executable]):
    for executable in executables:
        if not executable.is_test and executable.cargo_target == "crosvm":
            return executable
    raise Exception("Cannot find crosvm executable")


def main():
    parser = argparse.ArgumentParser(usage=USAGE)
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Print all test output.",
    )
    parser.add_argument(
        "--target",
        help="Execute tests on the selected target. See ./tools/set_test_target",
    )
    parser.add_argument(
        "--arch",
        choices=typing.get_args(Arch),
        help="Target architecture to build for.",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Repeat each test N times to check for flakes.",
    )
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose  # type: ignore
    os.environ["RUST_BACKTRACE"] = "1"

    target = (
        test_target.TestTarget(args.target)
        if args.target
        else test_target.TestTarget.default()
    )
    print("Test target:", target)

    arch = args.arch
    if not arch:
        arch = test_target.get_target_arch(target)
    print("Building for architecture:", arch)

    # Start booting VM while we build
    if target.vm:
        testvm.build_if_needed(target.vm)
        testvm.up(target.vm)

    executables = list(build_all_binaries(target, arch))

    if args.build_only:
        print("Not running tests as requested.")
        sys.exit(0)

    # Upload dependencies plus the main crosvm binary for integration tests
    test_target.prepare_target(
        target, extra_files=[find_crosvm_binary(executables).binary_path]
    )

    # Execute all test binaries
    test_executables = [e for e in executables if e.is_test]
    all_results = list(
        execute_all(test_executables, target, arch, repeat=args.repeat)
    )

    failed = [r for r in all_results if not r.success]
    if len(failed) == 0:
        print("All tests passed.")
        sys.exit(0)
    else:
        print(f"{len(failed)} of {len(all_results)} tests failed:")
        for result in failed:
            print(f"  {result.name}")
        sys.exit(-1)


def verify_crate_options():
    """Verify that CRATE_OPTIONS are for existing crates."""
    all_crates = list(list_main_crates()) + list(list_common_crates())
    for crate, _ in CRATE_OPTIONS.items():
        if crate not in all_crates:
            raise Exception("No such crate: %s" % crate)


if __name__ == "__main__":
    try:
        verify_crate_options()
        main()
    except subprocess.CalledProcessError as e:
        print("Command failed:", e.cmd)
        print(e.stdout)
        print(e.stderr)
        sys.exit(-1)