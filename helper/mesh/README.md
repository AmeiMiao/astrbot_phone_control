# 用户态组网组件

Tailscale CLI/tailscaled 在启用并重载时按 Linux 宿主架构从 https://pkgs.tailscale.com/stable/ 下载官方稳定版。归档与 SHA256 校验后保存到插件数据目录，manifest.json 记录实际版本与来源；手动路径优先。

ZeroTier 使用官方 ZeroTier Sockets SDK 固定提交 `a707ea6ae0910efdc1125d04758c411e2e9ea4f9`，在独立工作进程运行节点、回环 SOCKS 和来自指定手机 IP 的组网代理转发。`libzt` 仅支持手动提供，在配置中填写动态库绝对路径。请在目标宿主系统构建，以匹配架构和运行库；以下脚本静态链接 C++ 运行库。

构建需要 Git、CMake、C/C++ 编译器、Make 与 strip：

```bash
MESH_BUILD_DIR=/path/to/cache/mesh MESH_OUTPUT=/path/to/output ./build-zerotier.sh
```

编译选项 `-include stdexcept` 为上游指标库声明 `std::invalid_argument` 所需标准头。Central API 编译关闭；插件通过 Network ID 与本地节点身份加入网络，成员授权由 ZeroTier 控制台完成。保留原始 SDK/ZeroTierOne/lwIP 许可证，位于 `bin/linux-x86_64/ZEROTIER-LICENSE.txt` 和 `zerotier-licenses/`。

SDK 的虚拟接口 MTU 为 1280，TCP MSS 随之计算，使封装后的流量适合常见 1500 MTU 链路。构建脚本应用同目录 `zerotier-mtu.patch`；每个 SDK socket 的收发与关闭由同一线程处理，代理保留 TCP 半关闭并使用有界缓冲。

ZeroTier 的强制 TCP 中继由 `zts_init_force_tcp_relay` 在节点启动前设置，默认按 SDK 自动策略运行。开启时要求动态库提供此接口；能力不足会明确报错。所选传输策略写入节点状态，配置变更重建工作进程并复用节点身份。

SDK共享进程级`zts_errno`，原生套接字调用与错误读取通过同一互斥保护。套接字以非阻塞方式运行；连接采用BSD接口、可写状态与`SO_ERROR`核对，10秒连接期限内轮询，每次调用后释放互斥，数据与其他连接可继续处理。

SDK 根节点按14秒阈值调度心跳，保持在30秒的节点活动判定期限内。构建脚本在固定 ZeroTierOne 源码上应用 `zerotier-heartbeat.patch`，与 MTU 补丁共同生成动态库；手动配置的动态库须由此脚本构建。网络就绪和节点离线判断仍使用 SDK 的实际事件。
