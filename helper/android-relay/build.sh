#!/usr/bin/env bash
# 在 Android SDK 35 与 JDK 17+ 下构建；输出目录和签名密钥由调用方提供。
set -euo pipefail
: "${ANDROID_HOME:?需设置 Android SDK 目录}"
: "${JAVA_HOME:?需设置 JDK 目录}"
: "${OUT_DIR:?需设置工作区缓存内输出目录}"
: "${SIGNING_KEYSTORE:?需指定持久化签名密钥}"
: "${SIGNING_PASSWORD:?需通过环境变量提供签名口令}"
export PATH="$JAVA_HOME/bin:$PATH"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_TOOLS="$ANDROID_HOME/build-tools/35.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-35/android.jar"
mkdir -p "$OUT_DIR/classes" "$OUT_DIR/dex"
"$BUILD_TOOLS/aapt2" link -o "$OUT_DIR/base.apk" --manifest "$SOURCE_DIR/AndroidManifest.xml" -I "$PLATFORM"
find "$SOURCE_DIR/src" -name '*.java' -print > "$OUT_DIR/sources.txt"
javac -source 8 -target 8 -classpath "$PLATFORM" -d "$OUT_DIR/classes" @"$OUT_DIR/sources.txt"
jar cf "$OUT_DIR/classes.jar" -C "$OUT_DIR/classes" .
"$BUILD_TOOLS/d8" --lib "$PLATFORM" --min-api 26 --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar"
cp "$OUT_DIR/base.apk" "$OUT_DIR/unsigned.apk"
jar uf "$OUT_DIR/unsigned.apk" -C "$OUT_DIR/dex" classes.dex
"$BUILD_TOOLS/zipalign" -f 4 "$OUT_DIR/unsigned.apk" "$OUT_DIR/aligned.apk"
"$BUILD_TOOLS/apksigner" sign --ks "$SIGNING_KEYSTORE" --ks-pass env:SIGNING_PASSWORD \
  --key-pass env:SIGNING_PASSWORD --out "$OUT_DIR/phone-adb-relay.apk" "$OUT_DIR/aligned.apk"
"$BUILD_TOOLS/apksigner" verify --verbose "$OUT_DIR/phone-adb-relay.apk"
