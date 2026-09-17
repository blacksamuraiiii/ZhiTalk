# 32秒自动挂断（SIP Timer H）根因与修复

> 这个文档取代旧版 `sip-transport-tcp.md`（曾错误建议 UDP→TCP 解决）。根因是 NAT 地址映射，不是传输层协议。

## 症状

- 通话总是在 **30-32 秒**准时挂断
- FreePBX asterisk/full 日志：
  ```
  Transmitting SIP request ... BYE
  Reason: SIP ;cause=408 ;text="Request Timeout"
  ```
- SIP 日志显示 INVITE 的 200 OK 被重传多次，Contact 头写的是容器内网 IP（如 `172.20.0.3`）

## 根因

三层 NAT 导致 200 OK 响应中的 Contact 头写的是 Asterisk 容器内网地址（`172.20.0.3:5060`），该地址在 Windows 侧不可达：

```
MicroSIP(Windows) → Docker Desktop NAT → FreePBX容器(172.20.0.3:5060)

Asterisk 发 200 OK:
  Contact: <sip:172.20.0.3:5060>    ← Windows 路由表无此网段

MicroSIP 收 ACK → 发 ACK 到 172.20.0.3:5060 → 包丢了 → Asterisk 重传 200 OK
→ 32 秒后 Timer H(64×T1) 到期 → BYE cause=408
```

## 修复

**pjsip.conf transport 段加 NAT 配置**：

```ini
[transport-udp]
type = transport
protocol = udp
bind = 0.0.0.0:5060
local_net = 127.0.0.1/32                     ; 只把本机当内网，不含 Docker bridge
external_media_address = 172.31.33.150       ; RTP 地址改写为 WSL2 IP
external_signaling_address = 172.31.33.150   ; Contact/Via 头改写为 WSL2 IP（核心）
external_signaling_port = 5060
```

**endpoint 段**：
```ini
[100]
type = endpoint
rewrite_contact = yes                         ; 注册响应也改写 Contact
```

## 踩坑记录

- ⚠️ **不要配 `local_net = 172.20.0.0/16`** — Docker 来的请求源 IP 是 172.20.0.1，在此范围内，Asterisk 认为"内网"不触发改写
- ⚠️ **不要改 UDP → TCP** — Contact 地址不可达时 TCP 同样建不起来
- ⚠️ **不要加保活参数** — `rtp_keepalive`、`direct_media=no`、`qualify_frequency` 全是外围手段

## 验证

```bash
docker exec freepbx bash -c 'grep "Contact:" /var/log/asterisk/full'
# 期望: Contact: <sip:172.31.33.150:5060>  （不是 172.20.0.3）

docker exec freepbx bash -c 'grep -i "retransmit" /var/log/asterisk/full'
# 期望: 空
```
