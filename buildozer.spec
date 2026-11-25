[app]
title = Tactical Neural Matrix
package.name = tacticalneuralmatrix
package.domain = org.tdn
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = bin, tests, venv, .git, .github, .buildozer
version = 5.7.0

# All app modules use only the Python standard library. certifi gives the phone the HTTPS certificates it needs
# to reach the broker servers. plyer needs pyjnius and android on the phone (text-to-speech).
requirements = python3,kivy==2.3.0,plyer,pyjnius,android,certifi

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,RECORD_AUDIO
android.api = 33
android.minapi = 21
android.ndk = 25b
android.ndk_api = 21
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.wakelock = False
android.presplash_color = #010103
android.logcat_filters = *:S python:D
# filled in by the GitHub workflow so no path mismatch is possible
android.sdk_path =
android.ndk_path =

p4a.branch = v2023.09.16
p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 0
bin_dir = ./bin
build_dir = ./.buildozer
