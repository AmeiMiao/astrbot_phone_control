"""通过本机 ADB 管理抓包 CA 的独立挂载及所有权收尾。"""

import base64
import re
import shlex
import subprocess
from pathlib import Path

from .errors import ActionFailedError


class AppCaTrust:
    def __init__(self, adb, capture_id, before_install=None):
        self.adb = adb
        self.before_install = before_install
        if not re.fullmatch(r"[0-9a-f]{32}", capture_id):
            raise ValueError("抓包身份无效")
        self.directory = "/data/local/tmp/phone-relay-ca-" + capture_id
        self.prefix = ""
        self.target = ""
        self.cert_hash = ""
        self.prepared = False

    async def root_command(self, command):
        return await self.adb.shell(
            ("su -c " + shlex.quote(command))
            if self.prefix
            else ("sh -c " + shlex.quote(command)),
            timeout=40,
        )

    async def install(self, cert: Path):
        result = await self.adb.shell("id -u; command -v su || true", timeout=5)
        if result.splitlines()[0].strip() != "0":
            if len(result.splitlines()) < 2:
                raise ActionFailedError(
                    "系统 CA 需要本机 adbd root 或 su，代理尚未启动。无 root 时选择手动 CA，并从 Relay App 导出或从插件下载证书，在手机系统设置安装；HTTPS 还需目标 App 信任此 CA"
                )
            check = await self.adb.shell("su -c " + shlex.quote("id -u"), timeout=10)
            if check.strip() != "0":
                raise ActionFailedError(
                    "系统 CA 的 su 授权未获准；请授权或选择手动 CA 信任方式"
                )
            self.prefix = "su -c "
            # su 的参数本身就是完整命令，不再增加一层外部 shell。
        hashed = subprocess.run(
            ["openssl", "x509", "-subject_hash_old", "-noout", "-in", str(cert)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{8}", hashed):
            raise ActionFailedError("抓包 CA 文件名计算失败")
        self.cert_hash = hashed
        self.target = (
            await self.root_command(
                "if [ -d /apex/com.android.conscrypt/cacerts ]; then echo /apex/com.android.conscrypt/cacerts; else echo /system/etc/security/cacerts; fi"
            )
        ).strip()
        if self.target not in (
            "/apex/com.android.conscrypt/cacerts",
            "/system/etc/security/cacerts",
        ):
            raise ActionFailedError("系统 CA 目录无效")
        source, target = shlex.quote(self.directory), shlex.quote(self.target)
        payload = base64.b64encode(cert.read_bytes()).decode()
        self.prepared = True
        if self.before_install is not None:
            await self.before_install(self.cleanup_command())
        command = (
            f"set -e; mkdir -p {source}; cp -a {target}/. {source}/; "
            f"I=0; while [ -e {source}/{hashed}.$I ]; do I=$((I+1)); [ $I -lt 32 ] || exit 1; done; "
            f"echo {hashed}.$I > {source}/.owned-cert; "
            f"printf %s {shlex.quote(payload)} | base64 -d > {source}/{hashed}.$I && "
            f"chmod 755 {source}; chmod 644 {source}/{hashed}.$I; "
            f"CONTEXT=$(ls -Zd {target} | awk '{{print $1}}'); "
            f'case "$CONTEXT" in u:object_r:*) chcon -R "$CONTEXT" {source};; '
            '*) if [ "$(getenforce 2>/dev/null)" = Enforcing ]; then exit 1; fi;; esac; '
            'SEEN=""; for P in 1 $(pidof zygote64 zygote 2>/dev/null); do '
            'N=$(readlink /proc/$P/ns/mnt) || exit 1; case " $SEEN " in *" $N "*) continue;; esac; '
            f'nsenter -t "$P" -m -- mount --bind {source} {target} || exit 1; '
            'SEEN="$SEEN $N"; done'
        )
        try:
            await self.root_command(command)
            visible = await self.root_command(
                f'C=$(cat {source}/.owned-cert); nsenter -t 1 -m -- test -f {target}/"$C" && echo CA_READY'
            )
            if visible.strip() != "CA_READY":
                raise ActionFailedError("系统 CA 挂载未生效")
        except BaseException:
            await self.close()
            raise

    def cleanup_command(self):
        if not self.prepared:
            return "true"
        source, target = shlex.quote(self.directory), shlex.quote(self.target)
        # 先撤回自有证书；仍持有目录挂载的应用保留完整的系统证书副本。
        command = (
            f"[ -d {source} ] || exit 0; C=$(cat {source}/.owned-cert 2>/dev/null); "
            f'case "$C" in {self.cert_hash}.[0-9]|{self.cert_hash}.[0-9][0-9]) rm -f {source}/"$C";; esac; '
            f"D=$(stat -c %d:%i {source}) || exit 1; "
            'SEEN=""; FAILED=0; for F in /proc/[0-9]*/ns/mnt; do '
            'P=${F#/proc/}; P=${P%/ns/mnt}; N=$(readlink "$F" 2>/dev/null) || continue; '
            'case " $SEEN " in *" $N "*) continue;; esac; SEEN="$SEEN $N"; '
            f'CURRENT=$(nsenter -t "$P" -m -- stat -c %d:%i {target} 2>/dev/null) || continue; '
            f'if [ "$CURRENT" = "$D" ]; then nsenter -t "$P" -m -- umount {target} || FAILED=1; fi; '
            'done; test "$FAILED" = 0'
        )
        return (
            ("su -c " + shlex.quote(command))
            if self.prefix
            else ("sh -c " + shlex.quote(command))
        )

    async def close(self):
        if self.prepared:
            await self.adb.shell(self.cleanup_command(), timeout=40)
            self.prepared = False
