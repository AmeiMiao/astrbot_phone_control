#!/usr/bin/env bash
# 构建 phone-wg 静态二进制。
#
# 插件只随包分发 Linux x86_64（控制市场包体积）；默认只构建 linux/amd64 并写入
# ../../bin/linux-x86_64/phone-wg。其他平台由用户自行编译，默认输出到
# ./dist/<os>-<arch>/phone-wg[.exe]，再自行复制为插件根目录 bin/phone-wg。
# 本机没有 Go 时自动改用 docker 镜像构建（需 docker 权限）：
#   BUILD_IMAGE=golang:1.26-alpine ./build.sh
# 构建其他平台：
#   TARGETS="linux/arm64" ./build.sh
#   TARGETS="windows/amd64" ./build.sh
set -euo pipefail

cd "$(dirname "$0")"

GO_VERSION="${GO_VERSION:-1.26}"
BUILD_IMAGE="${BUILD_IMAGE:-golang:${GO_VERSION}-alpine}"
PLUGIN_BIN="${PLUGIN_BIN:-../../bin}"
OUT_DIR="${OUT_DIR:-dist}"
TARGETS="${TARGETS:-linux/amd64}"

export CGO_ENABLED=0
export GOFLAGS="${GOFLAGS:--mod=mod}"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export GOSUMDB="${GOSUMDB:-off}"

# 目标平台 -> 产物路径：随包的 linux/amd64 写插件 bin/linux-x86_64/；
# 其他平台写本地 dist/<os>-<arch>/，由用户自行放到插件根目录 bin/phone-wg。
dest_for() {
	case "$1" in
	linux/amd64) echo "${PLUGIN_BIN}/linux-x86_64/phone-wg" ;;
	*) echo "${OUT_DIR}/${1%%/*}-${1##*/}/phone-wg" ;;
	esac
}

build_one() {
	local os="$1" arch="$2" out
	out="$(dest_for "$os/$arch")"
	case "$os" in
	windows) [ "${out##*.}" = "exe" ] || out="${out}.exe" ;;
	esac
	mkdir -p "$(dirname "$out")"
	echo "==> 构建 ${os}/${arch} -> ${out}"
	GOOS="$os" GOARCH="$arch" go build -trimpath -buildvcs=false \
		-ldflags "-s -w" -o "$out" .
	if command -v file >/dev/null 2>&1; then file "$out"; fi
	if command -v sha256sum >/dev/null 2>&1; then sha256sum "$out"; fi
}

run_local() {
	echo "==> 使用本机 go: $(go version)"
	go mod tidy
	go vet ./...
	go test ./...
	local target
	for target in $TARGETS; do
		build_one "${target%%/*}" "${target##*/}"
	done
}

run_docker() {
	echo "==> 本机未找到 go，使用 docker 镜像 ${BUILD_IMAGE}"
	local target os arch out
	for target in $TARGETS; do
		os="${target%%/*}"
		arch="${target##*/}"
		out="$(dest_for "$target")"
		mkdir -p "$(dirname "$out")"
		echo "==> 构建 ${os}/${arch} -> ${out}"
		docker run --rm -v "${PWD}:/src" -w /src \
			-e CGO_ENABLED=0 -e GOOS="$os" -e GOARCH="$arch" \
			-e "GOFLAGS=${GOFLAGS}" -e "GOPROXY=${GOPROXY}" -e "GOSUMDB=${GOSUMDB}" \
			"${BUILD_IMAGE}" sh -c \
			"go mod tidy && go vet ./... && go test ./... && go build -trimpath -buildvcs=false -ldflags '-s -w' -o '${out}' ."
		if command -v file >/dev/null 2>&1; then file "$out"; fi
	done
}

if command -v go >/dev/null 2>&1; then
	run_local
else
	run_docker
fi

echo "==> 构建完成"
