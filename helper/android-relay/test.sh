#!/usr/bin/env bash
# 在宿主 JDK 上核对实际 LibADB 依赖的关闭与缓冲读取语义。
set -euo pipefail
: "${JAVA_HOME:?需设置 JDK 目录}"
: "${OUT_DIR:?需设置构建缓存目录}"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT_DIR/test-classes"
"$JAVA_HOME/bin/javac" -classpath "$OUT_DIR/deps/libadb.jar" -d "$OUT_DIR/test-classes" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/LocalAdbInputStream.java" \
  "$SOURCE_DIR/src/fun/acat/phonerelay/ClipboardInput.java" \
  "$SOURCE_DIR/tests/LocalAdbInputStreamTest.java" \
  "$SOURCE_DIR/tests/ClipboardInputTest.java"
"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes:$OUT_DIR/deps/libadb.jar" \
  fun.acat.phonerelay.LocalAdbInputStreamTest
"$JAVA_HOME/bin/java" -classpath "$OUT_DIR/test-classes" fun.acat.phonerelay.ClipboardInputTest
