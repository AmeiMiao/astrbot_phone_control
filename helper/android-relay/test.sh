#!/usr/bin/env bash
# 在宿主 JDK 上核对实际 LibADB 依赖的关闭与缓冲读取语义。
set -euo pipefail
: "${JAVA_HOME:?需设置 JDK 目录}"
: "${OUT_DIR:?需设置构建缓存目录}"
: "${ANDROID_HOME:?需设置 Android SDK 目录}"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT_DIR/test-classes"
"$JAVA_HOME/bin/javac" -classpath "$ANDROID_HOME/platforms/android-35/android.jar:$OUT_DIR/deps/annotations.jar:$OUT_DIR/deps/libadb.jar:$OUT_DIR/deps/bcprov.jar" -d "$OUT_DIR/test-classes" \
  "$SOURCE_DIR/src/io/github/muntashirakon/adb/AdbConnection.java" \
  "$SOURCE_DIR/tests/AdbOpenTest.java" \
  "$SOURCE_DIR/tests/android/os/Build.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/LocalAdbInputStream.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/ClipboardInput.java" \
  "$SOURCE_DIR/tests/LocalAdbInputStreamTest.java" \
  "$SOURCE_DIR/tests/ClipboardInputTest.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/ScrcpyPackets.java" \
  "$SOURCE_DIR/tests/ScrcpyPacketsTest.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/YuvPlanes.java" \
  "$SOURCE_DIR/tests/YuvPlanesTest.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/H264Config.java" \
  "$SOURCE_DIR/tests/H264ConfigTest.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/ProxyCertificates.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/PhoneHttpProxy.java" \
  "$SOURCE_DIR/tests/PhoneHttpProxyTest.java"
"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes:$OUT_DIR/deps/libadb.jar" \
  fun.acat.phonerelay.LocalAdbInputStreamTest
"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes:$OUT_DIR/deps/libadb.jar" io.github.muntashirakon.adb.AdbOpenTest
"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes" fun.acat.phonerelay.ClipboardInputTest

"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes" fun.acat.phonerelay.ScrcpyPacketsTest

"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes:$OUT_DIR/deps/bcprov.jar" fun.acat.phonerelay.PhoneHttpProxyTest "$OUT_DIR"

"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes" fun.acat.phonerelay.YuvPlanesTest

"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes" fun.acat.phonerelay.H264ConfigTest
