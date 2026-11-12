# Airtest environment for this board (source me before using airtest)
# Board is arm64: the adb bundled inside the airtest package is x86_64 / armhf, so it cannot run.
# Point Airtest at the system arm64 adb instead.
export ANDROID_HOME=/usr/lib/android-sdk
export PATH="$ANDROID_HOME/platform-tools:$PATH"
export AIRTEST_VENV=/root/.openclaw/workspace/Airtest/.venv
