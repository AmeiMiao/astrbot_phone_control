#!/usr/bin/env bash
# 构建 phone-wg 多平台静态二进制并复制到插件 bin/<平台>/。
#
# 默认目标：linux/amd64、linux/arm64、windows/amd64（与插件随包平台一致）。
# 本机没有 Go 时自动改用 docker 镜像构建（需 docker 权限）：
#   BUILD_IMAGE=golang:1.26-alpine ./build.sh
# 只构建部分目标：
#   TARGETS="linux/arm64" ./build.sh
set -euo pipefail

cd "$(dirname "$0")"

GO_VERSION="${GO_VERSION:-1.26}"
BUILD_IMAGE="${BUILD_IMAGE:-golang:${GO_VERSION}-alpine}"
PLUGIN_BIN="${PLUGIN_BIN:-../../bin}"
TARGETS="${TARGETS:-linux/amd64 linux/arm64 windows/amd64}"

export CGO_ENABLED=0
export GOFLAGS="${GOFLAGS:--mod=mod}"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export GOSUMDB="${GOSUMDB:-off}"

# 目标平台 -> 插件 bin 目录（名称与 core/platform_bins.py 的宿主平台识别一致）
dest_for() {
	case "$1" in
	linux/amd64) echo "${PLUGIN_BIN}/linux-x86_64/phone-wg" ;;
	linux/arm64) echo "${PLUGIN_BIN}/linux-aarch64/phone-wg" ;;
	windows/amd64) echo "${PLUGIN_BIN}/windows-x86_64/phone-wg.exe" ;;
	*) echo "bin/$1/phone-wg" ;;
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
