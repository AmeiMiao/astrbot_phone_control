#!/usr/bin/env bash
set -euo pipefail
: "${MESH_BUILD_DIR:?设置工作区缓存目录 MESH_BUILD_DIR}"
: "${MESH_OUTPUT:?设置输出目录 MESH_OUTPUT}"
mesh_helper_dir="$(cd "$(dirname "$0")" && pwd)"
mesh_source_commit=a707ea6ae0910efdc1125d04758c411e2e9ea4f9
mkdir -p "$MESH_BUILD_DIR" "$MESH_OUTPUT"
if [ ! -d "$MESH_BUILD_DIR/libzt/.git" ]; then
  git clone --no-checkout https://github.com/zerotier/libzt.git "$MESH_BUILD_DIR/libzt"
fi
git -C "$MESH_BUILD_DIR/libzt" checkout --detach "$mesh_source_commit"
git -C "$MESH_BUILD_DIR/libzt" submodule update --init --recursive
if git -C "$MESH_BUILD_DIR/libzt" apply --reverse --check "$mesh_helper_dir/zerotier-mtu.patch" 2>/dev/null; then
  :
else
  git -C "$MESH_BUILD_DIR/libzt" apply --check "$mesh_helper_dir/zerotier-mtu.patch"
  git -C "$MESH_BUILD_DIR/libzt" apply "$mesh_helper_dir/zerotier-mtu.patch"
fi
mesh_core_dir="$MESH_BUILD_DIR/libzt/ext/ZeroTierOne"
if git -C "$mesh_core_dir" apply --reverse --check "$mesh_helper_dir/zerotier-heartbeat.patch" 2>/dev/null; then
  :
else
  git -C "$mesh_core_dir" apply --check "$mesh_helper_dir/zerotier-heartbeat.patch"
  git -C "$mesh_core_dir" apply "$mesh_helper_dir/zerotier-heartbeat.patch"
fi
cmake -S "$MESH_BUILD_DIR/libzt" -B "$MESH_BUILD_DIR/build" \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIB=ON -DBUILD_STATIC_LIB=ON -DBUILD_HOST_SELFTEST=OFF \
  -DBUILD_HOST_EXAMPLES=OFF -DZTS_DISABLE_CENTRAL_API=ON \
  -DCMAKE_CXX_FLAGS='-include stdexcept' \
  -DCMAKE_SHARED_LINKER_FLAGS='-static-libstdc++ -static-libgcc'
cmake --build "$MESH_BUILD_DIR/build" --target zt-shared --parallel 4
cp "$MESH_BUILD_DIR/build/lib/libzt.so" "$MESH_OUTPUT/libzt.so"
strip "$MESH_OUTPUT/libzt.so"
sha256sum "$MESH_OUTPUT/libzt.so"
