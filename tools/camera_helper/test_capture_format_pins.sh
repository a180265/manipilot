#!/bin/sh
set -eu
cd "$(dirname "$0")"
format_test_dir=$(mktemp -d)
trap 'rm -rf "$format_test_dir"' EXIT HUP INT TERM
swiftc Sources/CameraHelper/CaptureFormatPins.swift Tests/CaptureFormatPinsTests.swift -o "$format_test_dir/check-pins"
"$format_test_dir/check-pins"
