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
mkdir -p "$OUT_DIR/classes" "$OUT_DIR/dex" "$OUT_DIR/assets"
python3 "$SOURCE_DIR/prepare-dependencies.py" "$OUT_DIR/deps"
cp "$SOURCE_DIR/../../bin/scrcpy-server-v5.0.1" "$OUT_DIR/assets/scrcpy-server"
CLASSPATH="$PLATFORM"
for dependency in "$OUT_DIR"/deps/*.jar; do CLASSPATH="$CLASSPATH:$dependency"; done
"$BUILD_TOOLS/aapt2" link -o "$OUT_DIR/base.apk" --manifest "$SOURCE_DIR/AndroidManifest.xml" -I "$PLATFORM" -A "$OUT_DIR/assets"
find "$SOURCE_DIR/src" -name '*.java' -print > "$OUT_DIR/sources.txt"
javac -source 8 -target 8 -classpath "$CLASSPATH" -d "$OUT_DIR/classes" @"$OUT_DIR/sources.txt"
jar cf "$OUT_DIR/classes.jar" -C "$OUT_DIR/classes" .
"$BUILD_TOOLS/d8" --lib "$PLATFORM" --min-api 26 --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar" "$OUT_DIR"/deps/*.jar
cp "$OUT_DIR/base.apk" "$OUT_DIR/unsigned.apk"
for dex in "$OUT_DIR"/dex/*.dex; do jar uf "$OUT_DIR/unsigned.apk" -C "$OUT_DIR/dex" "$(basename "$dex")"; done
jar uf "$OUT_DIR/unsigned.apk" -C "$OUT_DIR/deps/native" lib
"$BUILD_TOOLS/zipalign" -f 4 "$OUT_DIR/unsigned.apk" "$OUT_DIR/aligned.apk"
"$BUILD_TOOLS/apksigner" sign --ks "$SIGNING_KEYSTORE" --ks-pass env:SIGNING_PASSWORD \
  --key-pass env:SIGNING_PASSWORD --out "$OUT_DIR/phone-adb-relay.apk" "$OUT_DIR/aligned.apk"
"$BUILD_TOOLS/apksigner" verify --verbose "$OUT_DIR/phone-adb-relay.apk"

python3 - "$OUT_DIR/phone-adb-relay.apk" "$SOURCE_DIR/AndroidManifest.xml" <<'PYINFO'
import hashlib, json, sys, xml.etree.ElementTree as ET
from pathlib import Path
apk = Path(sys.argv[1]); manifest = ET.parse(sys.argv[2]).getroot()
prefix = '{http://schemas.android.com/apk/res/android}'
info = {'package': manifest.attrib['package'], 'versionCode': int(manifest.attrib[prefix+'versionCode']),
        'versionName': manifest.attrib[prefix+'versionName'], 'size': apk.stat().st_size,
        'sha256': hashlib.sha256(apk.read_bytes()).hexdigest(), 'protocol': 1}
apk.with_suffix('.json').write_text(json.dumps(info, indent=2)+'\n')
PYINFO
