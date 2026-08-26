# google3 build rules for the AV2 research agent.
#
# The package has no required third-party dependency; yaml and the Anthropic
# client are optional and are declared here because they are available in
# google3 and make the fast paths available.

load("//third_party/bazel_rules/rules_python/python:py_binary.bzl", "py_binary")
load("//third_party/bazel_rules/rules_python/python:py_library.bzl", "py_library")
load("//third_party/bazel_rules/rules_python/python:py_test.bzl", "py_test")

package(default_visibility = ["//visibility:public"])

licenses(["notice"])

py_library(
    name = "av2ra",
    srcs = glob(["av2ra/**/*.py"]),
    data = glob([
        "config/*.yaml",
        "config/sites/*.yaml",
        "docs/*.md",
    ]),
    deps = [
        # Optional at runtime; av2ra.util.yamlish falls back to a bundled parser.
        "//third_party/py/yaml",
    ],
)

py_binary(
    name = "av2ra_cli",
    srcs = ["av2ra/cli.py"],
    main = "av2ra/cli.py",
    deps = [":av2ra"],
)

py_test(
    name = "tests",
    size = "medium",
    srcs = glob(["tests/*.py"]),
    main = "tests/run_all.py",
    deps = [":av2ra"],
)
