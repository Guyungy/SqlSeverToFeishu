#!/usr/bin/env python3
"""一个会说 TDS 前两步、但不说 TDS 登录的桩服务，供 Rust 集成测试使用。

为什么是这个粒度：用真实 SQL Server 才能端到端验，本地没有可连的库，
于是把桩做到「够触发我们要验的那一层」为止。

TDS 连接建立顺序（这里修正了我最初的两处误判）：
  1. 客户端发明文 PRELOGIN 询问服务器支持什么
  2. 服务器回 PRELOGIN，其中 ENCRYPTION 字段决定后续行为
  3. 值若为 ENCRYPT_ON / ENCRYPT_REQ，双方**随即**开始 TLS 握手
  4. 握手字节并不是裸 TLS 记录，而是**再包一层 TDS 包头**
     （类型 PreLogin=0x12），两个方向都是如此 —— 见 tiberius 的
     `TlsPreloginWrapper`：读时按包头切分并剥离，写时补上包头。
     所以桩必须自己完成这层封装，否则 TLS 解析器看到的第一个字节
     是 0x12 而不是 0x16，直接握手失败。
  5. TLS 之后客户端才发加密的 LOGIN7

用 Python 的 MemoryBIO 手工驱动握手，就是为了控制第 4 步的封装。

两种模式（argv[4]）：
  encrypt (默认)  ENCRYPTION=0x01，随后做 TLS 握手；握手完成且收到了客户端
                  的后续数据记 ok，否则记 fail。这是 SQL Server 的常规配置。
  plain            ENCRYPTION=0x02（服务器不支持加密），客户端应当拒绝继续。
                  用来验证「客户 SQL Server 没关加密」时报错是否清晰。

用法: tds_stub.py <日志文件> <证书目录> [<openssl 路径>] [<模式>]
启动后在标准输出打印一行 `PORT <n>`。
"""

import os
import socket
import ssl
import subprocess
import sys
import threading

LOG_PATH = sys.argv[1]
CERT_DIR = sys.argv[2]
OPENSSL = sys.argv[3] if len(sys.argv) > 3 else "openssl"
MODE = sys.argv[4] if len(sys.argv) > 4 else "encrypt"

CERT = os.path.join(CERT_DIR, "cert.pem")
KEY = os.path.join(CERT_DIR, "key.pem")
LOG_LOCK = threading.Lock()

HEADER_BYTES = 8
PACKET_TYPE_RESPONSE = 0x04
PACKET_TYPE_PRELOGIN = 0x12
PACKET_STATUS_EOM = 0x01

ENCRYPT_ON = 0x01
ENCRYPT_NOT_SUP = 0x02


def note(text):
    with LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()


def ensure_cert():
    """生成自签证书。优先带 SAN；老 openssl 不支持 -addext 时退回无 SAN。"""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    base = [
        OPENSSL, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY, "-out", CERT, "-days", "2", "-nodes",
        "-subj", "/CN=sqlserver-local-test",
    ]
    with_san = base + ["-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"]
    if subprocess.run(with_san, capture_output=True).returncode != 0:
        subprocess.run(base, capture_output=True, check=True)


def recv_exact(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def tds_packet(packet_type, payload):
    total = HEADER_BYTES + len(payload)
    header = (
        bytes([packet_type, PACKET_STATUS_EOM])
        + total.to_bytes(2, "big")
        + b"\x00\x00\x01\x00"
    )
    return header + payload


def read_tds_packet(sock):
    """返回 (类型, 负载)；连接提前结束返回 None。"""
    header = recv_exact(sock, HEADER_BYTES)
    if len(header) < HEADER_BYTES:
        return None
    total = int.from_bytes(header[2:4], "big")
    payload = recv_exact(sock, max(total - HEADER_BYTES, 0))
    if len(payload) < max(total - HEADER_BYTES, 0):
        return None
    return header[0], payload


def prelogin_response(encryption_byte):
    """构造 PRELOGIN 应答：选项表 3 项 + 终止符，数据紧随其后。"""
    options = [
        (0x00, bytes([0x0E, 0x00, 0x00, 0x00, 0x00, 0x00])),  # VERSION 14.0
        (0x01, bytes([encryption_byte])),                      # ENCRYPTION
        (0x04, bytes([0x00])),                                 # MARS 关闭
    ]
    table_len = len(options) * 5 + 1
    table = b""
    data = b""
    offset = table_len
    for token, value in options:
        table += bytes([token]) + offset.to_bytes(2, "big") + len(value).to_bytes(2, "big")
        data += value
        offset += len(value)
    return table + b"\xff" + data


def serve_one(raw):
    try:
        raw.settimeout(10.0)

        prelogin = read_tds_packet(raw)
        if prelogin is None or prelogin[0] != PACKET_TYPE_PRELOGIN:
            note("no-prelogin")
            return

        if MODE == "plain":
            raw.sendall(tds_packet(PACKET_TYPE_RESPONSE, prelogin_response(ENCRYPT_NOT_SUP)))
            # 先记日志再等客户端收尾：客户端读到「不支持加密」就立刻报错了，
            # 若把 note 放在后面的 recv 之后，测试端读日志时会读到空文件
            # （这与 feishu_mock 里那处竞态是同一类问题）。
            note("plain")
            # 多读一会儿，避免带着未读数据关闭触发 RST，
            # 把「明确拒绝」变成看起来像网络故障。
            try:
                raw.recv(4096)
            except OSError:
                pass
            return

        raw.sendall(tds_packet(PACKET_TYPE_RESPONSE, prelogin_response(ENCRYPT_ON)))

        incoming = ssl.MemoryBIO()
        outgoing = ssl.MemoryBIO()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        tls = context.wrap_bio(incoming, outgoing, server_side=True)

        def flush_out():
            total_sent = 0
            data = outgoing.read()
            while data:
                chunk, data = data[:4096], data[4096:]
                raw.sendall(tds_packet(PACKET_TYPE_PRELOGIN, chunk))
                total_sent += len(chunk)
            if total_sent:
                note(f"trace:flushed {total_sent}")
            return total_sent

        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                flush_out()
                packet = read_tds_packet(raw)
                if packet is None:
                    note("fail:peer-closed-during-handshake")
                    return
                note(f"trace:read type=0x{packet[0]:02x} len={len(packet[1])}")
                incoming.write(packet[1])
            except ssl.SSLError as error:
                # 客户端不信任自签证书时会走到这里，属于预期路径。
                note(f"fail:{type(error).__name__}")
                return

        # 握手成功后必须把服务端最后一段握手消息发出去，
        # 否则客户端还在等它，会以为连接被中断。
        flush_out()
        note(
            f"trace:handshake-done version={tls.version()} "
            f"cipher={tls.cipher()[0] if tls.cipher() else '?'}"
        )

        # 判定标准：do_handshake() 返回成功，说明客户端的 Finished 已经
        # 验签通过；此后只要再收到任何字节，就说明客户端确实认为握手完成
        # 并继续往下走了。
        #
        # 这里刻意不去解密应用数据：LOGIN7 的内容不是本轮要验的东西
        # （tiberius 的登录报文格式不需要我们复现），多解一层只会引入
        # 与目标无关的失败点。
        raw.settimeout(15.0)
        try:
            tail = raw.recv(65536)
        except OSError:
            tail = b""
        note("ok" if tail else "fail:no-bytes-after-tls-handshake")
    except (ssl.SSLError, OSError) as error:
        note(f"fail:{type(error).__name__}")
    finally:
        try:
            raw.close()
        except OSError:
            pass


def main():
    ensure_cert()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    print(f"PORT {server.getsockname()[1]}", flush=True)
    while True:
        raw, _ = server.accept()
        threading.Thread(target=serve_one, args=(raw,), daemon=True).start()


if __name__ == "__main__":
    main()
