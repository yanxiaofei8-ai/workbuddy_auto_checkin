#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双账号云端签到 · 单账号执行入口（GitHub Actions runner 内运行）。

职责：
  1. 从环境变量读取该账号的长效令牌，注入到隔离 HOME；
  2. 调用 3.1.8 签到脚本执行签到 + Buddy 旅行（跳过脚本内置推送）；
  3. 准确标识本次执行的是哪个账号，附结果、积分与令牌剩余天数；
  4. 把同一份汇总并行推送到所有已配置的群机器人（飞书 / 企业微信）；
  5. 输出脱敏结果并写入 GitHub Step Summary。

账号标识策略（准确区分，且默认零配置）：
  - 默认从长效令牌载荷读取 preferred_username，脱敏为「前 3 + **** + 后 4」；
  - 若配置了 ACCOUNT_LABEL（仓库 Variables），则完全采用该自定义别名，
    不显示任何账号标识片段，适合通知群里有其他成员的场景；
  - 两者都不可用时显示「未标识账号」并给出配置提示。

通知渠道：
  - FEISHU_WEBHOOK、WECOM_WEBHOOK 各自独立、可同时配置；
  - 未配置的渠道静默跳过，全部未配置时签到照常执行，只是不发通知；
  - 新增渠道只需在 targets 里加一项 (名称, 环境变量, 报文构造函数)。

安全约定：
  - 令牌明文只存在于本进程内存与 runner 临时目录，不进入日志、命令行或产物；
  - 账号标识只以脱敏形式出现在通知中，不写入日志文件或仓库；
  - 推送状态只记录渠道名与结果，不记录 Webhook 地址与报文内容；
  - 执行结束即清除令牌文件；
  - 不读取、不解密、不导出本机任何登录态。
"""

import base64
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO_CHECKIN = os.path.join(HERE, "auto_checkin.py")
PY = sys.executable
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY")


def token_payload(rt):
    """解析长效令牌载荷；失败返回 None。"""
    try:
        payload = rt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def token_days_left(rt):
    """从长效令牌载荷读取到期时间，返回剩余整天数；解析失败返回 None。"""
    data = token_payload(rt)
    try:
        exp = int((data or {}).get("exp") or 0)
        if exp <= 0:
            return None
        return int((exp - time.time()) / 86400)
    except Exception:
        return None


def account_identity(rt):
    """从长效令牌载荷提取账号标识并脱敏（保留前 3 后 4 位）。

    用于让通知准确区分是哪个账号执行成功；只输出脱敏形式，
    不泄露完整账号标识，也不写入日志与产物。
    """
    data = token_payload(rt)
    try:
        ident = str((data or {}).get("preferred_username") or "").strip()
        if not ident:
            return None
        if len(ident) <= 7:
            return ident[0] + "***"
        return ident[:3] + "*" * (len(ident) - 7) + ident[-4:]
    except Exception:
        return None


def feishu_body(text):
    """飞书自定义机器人文本报文。"""
    return {"msg_type": "text", "content": {"text": text}}


def wecom_body(text):
    """企业微信群机器人文本报文（字段与飞书不同）。"""
    return {"msgtype": "text", "text": {"content": text}}


def push_webhook(url, body, name):
    """向单个群机器人推送；只返回简短状态，不回显 URL 与报文。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
        # 企业微信 / 钉钉即使 HTTP 200，也可能用 errcode 表示业务失败
        try:
            obj = json.loads(raw)
            code = obj.get("errcode") if isinstance(obj, dict) else None
            if code not in (None, 0):
                return "%s:失败(%s)" % (name, str(obj.get("errmsg") or code)[:40])
        except (ValueError, AttributeError):
            pass
        return "%s:已推送" % name
    except Exception as exc:
        return "%s:失败(%s)" % (name, type(exc).__name__)


def notify_all(text, targets):
    """targets: [(名称, webhook, 报文构造函数)]；未配置渠道静默跳过。"""
    results = []
    for name, url, builder in targets:
        if not url:
            results.append("%s:未配置" % name)
            continue
        results.append(push_webhook(url, builder(text), name))
    results.append(wecom_bot_push(text))
    print("推送: " + " | ".join(results))
    return results


def wecom_bot_push(text):
    """企业微信「智能机器人 API 模式」WebSocket 长连接主动推送。

    需要环境变量：
      WB_BOT_ID / WB_BOT_SECRET  机器人凭证（管理后台 API 模式生成）
      WECOM_CHAT_ID              单聊会话 ID（即接收人的 userid）
    未配置时返回未配置；单次失败自动重试一次（规避两个 job 并发抢连接）。
    """
    bot_id = (os.environ.get("WB_BOT_ID") or "").strip()
    secret = (os.environ.get("WB_BOT_SECRET") or "").strip()
    chat_id = (os.environ.get("WECOM_CHAT_ID") or "").strip()
    name = "企业微信机器人"
    if not (bot_id and secret and chat_id):
        return "%s:未配置" % name
    try:
        import websocket  # websocket-client
    except ImportError:
        return "%s:失败(缺少 websocket-client)" % name

    def once():
        ws = websocket.create_connection(
            "wss://openws.work.weixin.qq.com", timeout=15)
        try:
            rid1 = uuid.uuid4().hex
            ws.send(json.dumps({
                "cmd": "aibot_subscribe",
                "headers": {"req_id": rid1},
                "body": {"bot_id": bot_id, "secret": secret},
            }))
            sub = _ws_wait_ack(ws, rid1, timeout=10)
            if not sub or sub.get("errcode") != 0:
                raise RuntimeError("订阅失败(%s)" % (sub or {}).get("errcode"))
            rid2 = uuid.uuid4().hex
            ws.send(json.dumps({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": rid2},
                "body": {"chatid": chat_id, "chat_type": 1,
                         "msgtype": "markdown",
                         "markdown": {"content": text}},
            }, ensure_ascii=False))
            ack = _ws_wait_ack(ws, rid2, timeout=12)
            if not ack or ack.get("errcode") != 0:
                raise RuntimeError("发送失败(%s)" % (ack or {}).get("errcode"))
            return True
        finally:
            try:
                ws.close()
            except Exception:
                pass

    for attempt in (1, 2):
        try:
            once()
            return "%s:已推送" % name
        except Exception as exc:
            if attempt == 2:
                return "%s:失败(%s)" % (name, str(exc)[:40])
            time.sleep(3)


def _ws_wait_ack(ws, req_id, timeout=12):
    """按 req_id 关联等待响应帧；忽略中途收到的回调帧。"""
    end = time.time() + timeout
    ws.settimeout(timeout)
    while time.time() < end:
        try:
            raw = ws.recv()
        except Exception:
            return None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        rid = (data.get("headers") or {}).get("req_id")
        if rid == req_id:
            return data
    return None


def wipe(path):
    """把令牌文件内容置空，不删除文件本身。"""
    try:
        if os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
    except Exception:
        pass


def main():
    custom = (os.environ.get("ACCOUNT_LABEL") or "").strip()
    rt = (os.environ.get("WB_RT") or "").strip()
    feishu = (os.environ.get("FEISHU_WEBHOOK") or "").strip()
    wecom = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    wb_dir = os.path.join(home, ".workbuddy")
    rt_file = os.path.join(home, "rt.txt")
    saved_rt = os.path.join(wb_dir, "checkin-rt.json")

    targets = [("飞书", feishu, feishu_body),
               ("企业微信", wecom, wecom_body)]

    # 自定义别名优先；未配置时使用令牌自动识别的脱敏账号标识
    ident = account_identity(rt)
    display = custom or ident or "未标识账号"
    title_ok = "【每日签到成功 ✅】账号 %s" % display
    title_fail = "【每日签到失败 ⚠️】账号 %s" % display

    if not rt:
        text = (title_fail + "\n执行结果: 需要关注\n"
                "原因: 未配置长效令牌 Secret，请检查仓库机密设置。")
        print(text)
        notify_all(text, targets)
        return 1

    try:
        os.makedirs(wb_dir, exist_ok=True)
        with open(rt_file, "w", encoding="utf-8") as f:
            f.write(rt)

        # 步骤 1：录入令牌（--setup-rt-file 避免令牌出现在命令行）
        setup = subprocess.run(
            [PY, AUTO_CHECKIN, "--setup-rt-file", rt_file],
            capture_output=True, text=True, timeout=60,
        )
        if setup.returncode != 0:
            text = (title_fail + "\n执行结果: 需要关注\n"
                    "原因: 令牌录入失败，请确认 Secret 中的长效令牌完整有效。")
            print(text)
            notify_all(text, targets)
            return 1

        # 步骤 2：执行签到 + 旅行；内置推送关闭，由本脚本统一汇总
        run = subprocess.run(
            [PY, AUTO_CHECKIN, "--no-notify"],
            capture_output=True, text=True, timeout=150,
        )
        raw = (run.stdout or "").strip()
        try:
            res = json.loads(raw)
        except Exception:
            res = {"status": "error",
                   "msg": (raw or (run.stderr or ""))[:300] or "无有效输出"}

        status = res.get("status")
        ok = status == "ok"
        travel = res.get("travel") or {}

        # 两个口径严格分开：balance 为账户真实可用积分余额，
        # checkin_total_credits 为本期签到活动累计发放积分（不是余额）。
        balance = res.get("balance")
        ctotal = res.get("checkin_total_credits")
        lines = [title_ok if ok else title_fail,
                 "执行结果: %s" % ("成功" if ok else "需要关注"),
                 "状态: %s / %s" % (status, res.get("action")),
                 "可用积分余额: %s" % (balance if balance is not None else "未获取"),
                 "本期签到累计: %s 积分" % (ctotal if ctotal is not None else "-"),
                 "说明: %s" % (res.get("msg") or "-")]
        if balance is None:
            lines.append("提示: 余额接口未返回，本期累计不等于余额，请勿按累计数理解")
        if travel.get("available"):
            lines.append("Buddy 旅行: %s · %s · %s" % (
                travel.get("state_text") or "-",
                travel.get("location_name") or "-",
                travel.get("remaining_text") or "-",
            ))

        left = token_days_left(rt)
        if left is not None:
            lines.append("长效令牌剩余: %d 天" % left)
            if left <= 7:
                lines.append("⚠️ 令牌即将到期，请重新导出并更新 GitHub Secret")
        if display == "未标识账号":
            lines.append("提示: 无法识别账号，请在仓库 Variables 配置 ACCOUNT_LABEL_A / ACCOUNT_LABEL_B")

        text = "\n".join(lines)
        print(text)
        notify_all(text, targets)

        if SUMMARY:
            with open(SUMMARY, "a", encoding="utf-8") as f:
                f.write("### 账号 %s\n\n```text\n%s\n```\n\n" % (display, text))

        return 0 if ok else 1
    finally:
        wipe(rt_file)
        wipe(saved_rt)


if __name__ == "__main__":
    sys.exit(main())
