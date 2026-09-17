"""
中屏管理 Web 路由
HTML 模板从 templates/ 目录读取（不再内联）
"""

import os
import logging
import subprocess
import json
from flask import jsonify, request
from pathlib import Path

logger = logging.getLogger("ai-backend.web")
_config = None
_config_path = None

# 全局日志队列（AudioSocket 写入，Web API 读取）
_global_log_queue = []

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _read_template(name: str) -> str:
    """读取 HTML 模板文件"""
    path = _TEMPLATES_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def register_routes(app, config: dict):
    global _config, _config_path
    _config = config
    _find_config_path()

    # ═══ 页面路由 ═══

    @app.route("/")
    def page_overview():
        return _read_template("overview.html")

    @app.route("/logs")
    def page_logs():
        return _read_template("logs.html")

    @app.route("/extensions")
    def page_extensions():
        return _read_template("extensions.html")

    @app.route("/freepbx")
    def page_freepbx():
        return _read_template("freepbx.html")

    @app.route("/ai")
    def page_ai():
        return _read_template("ai.html")

    @app.route("/callback")
    def page_callback():
        return _read_template("callback.html")

    @app.route("/tftp")
    def page_tftp():
        return _read_template("tftp.html")

    @app.route("/schedule")
    def page_schedule():
        return _read_template("schedule.html")

    @app.route("/metrics")
    def page_metrics():
        return _read_template("metrics.html")

    @app.route("/llama-server")
    def page_llama_server():
        return _read_template("llama-server.html")

    @app.route("/api/status")
    def api_status():
        return jsonify({
            "status": "running",
            "version": "1.2",
            "flask_port": config["network"]["flask_port"],
            "audiosocket_port": config["network"]["audiosocket_port"],
            "freepbx_host": config["freepbx"]["host"],
            "freepbx_ami_port": config["freepbx"]["ami_port"],
            "asr_provider": config["asr"]["provider"],
            "vad_provider": config.get("vad", {}).get("vad_engine", "tenvad"),
            "llm_provider": config["llm"]["provider"],
            # 本地 llama 模式下实际生效的是 local_llama 子段，顶层 api_base/model 可能残留旧值
            "llm_api_base": (config["llm"].get("local_llama", {}).get("api_base")
                             if config["llm"].get("provider") == "local_llama"
                             else config["llm"].get("api_base", "")),
            "llm_model": (config["llm"].get("local_llama", {}).get("model")
                          if config["llm"].get("provider") == "local_llama"
                          else config["llm"].get("model", "")),
            "llm_local_online": _check_local_llm(),
            "tts_provider": config["tts"]["provider"],
            "tts_model": config["tts"].get("model_file", ""),
            "search_provider": config.get("search", {}).get("provider", "anysearch"),
        })

    @app.route("/api/metrics")
    def api_metrics():
        """返回通话指标数据：统计表基于全量数据，近期通话限最近10条。数据来自 SQLite call_records。"""
        try:
            from engine.call_history import load_calls, load_all_calls, get_summary
            # 统计表用全量，近期通话只取10条
            all_calls = load_all_calls()
            recent = load_calls(10)
            summary = get_summary(all_calls)
            return jsonify({"calls": recent, "summary": summary})
        except Exception as e:
            logger.warning(f"[METRICS] API读取失败: {e}")
            return jsonify({"calls": [], "summary": {
                "total": 0, "avg_duration_s": 0, "max_duration_s": 0,
                "min_duration_s": 0, "avg_turns": 0, "max_turns": 0,
                "callback_count": 0, "timeout_count": 0, "farewell_count": 0,
                "timeout_rate": 0,
            }})

    @app.route("/api/topology")
    def api_topology():
        """聚合拓扑节点状态：FreePBX/座机在线用 AMI 探测。"""
        import time as _t

        freepbx_host = _config["freepbx"]["host"]
        host_ip = _config["freepbx"].get("host_ip") or freepbx_host
        ata_ip = _config["freepbx"].get("ata_ip", "192.168.2.100")
        ami_port = _config["freepbx"]["ami_port"]
        flask_port = _config["network"]["flask_port"]
        as_port = _config["network"]["audiosocket_port"]

        # FreePBX 在线 + 座机(101) 在线状态（一次 AMI 连接拿两个）
        freepbx_ok = False
        ata_online = False
        try:
            from asterisk.manager import Manager
            mgr = Manager()
            mgr.connect(freepbx_host, ami_port)
            mgr.login(_config["freepbx"]["ami_user"], _config["freepbx"]["ami_secret"])
            freepbx_ok = True
            endpoints = {}

            def on_endpoint_list(evt, manager):
                name = evt.get_header("ObjectName")
                contacts = evt.get_header("Contacts", "")
                endpoints[name] = bool(contacts.strip())

            mgr.register_event("EndpointList", on_endpoint_list)
            mgr.send_action({"Action": "PJSIPShowEndpoints"})
            _t.sleep(2)
            ata_online = endpoints.get("101", False)
            try:
                mgr.logoff()
            except Exception:
                pass
        except Exception:
            freepbx_ok = False

        nodes = [
            {"id": "tftp", "name": "TFTP Server",
             "sub": "ATA 配置下发", "ip": host_ip, "ports": ["UDP 69"],
             "ok": True, "status": "在线"},
            {"id": "ata", "name": "ATA190 座机",
             "sub": "注册到 FreePBX", "ip": ata_ip, "ports": ["SIP 5060"],
             "ok": ata_online, "status": "在线" if ata_online else "离线"},
            {"id": "freepbx", "name": "FreePBX (Asterisk)",
             "sub": "IP-PBX 核心", "ip": host_ip, "ports": ["SIP 5060", "AMI " + str(ami_port)],
             "ok": freepbx_ok, "status": "在线" if freepbx_ok else "离线"},
            {"id": "ai", "name": "AI 后端 (Python)",
             "sub": "推理 + 中屏", "ip": host_ip, "ports": ["AS " + str(as_port), "Web " + str(flask_port)],
             "ok": True, "status": "在线"},
        ]
        return jsonify({"nodes": nodes})

    @app.route("/api/config/freepbx", methods=["GET"])
    def get_freepbx_config():
        return jsonify(config["freepbx"])

    @app.route("/api/config/freepbx", methods=["PUT"])
    def update_freepbx_config():
        data = request.get_json()
        if not data:
            return jsonify({"error": "无效请求"}), 400
        old_host_ip = config["freepbx"].get("host_ip")
        _update_section(config["freepbx"], data)
        _save_config()
        new_host_ip = config["freepbx"].get("host_ip")
        if new_host_ip and new_host_ip != old_host_ip:
            _sync_pjsip_host_ip(new_host_ip)
        return jsonify({"status": "ok"})

    @app.route("/api/config/ai", methods=["GET"])
    def get_ai_config():
        llm = dict(config["llm"])
        if llm.get("api_key"):
            k = llm["api_key"]
            llm["api_key"] = k[:6] + "****" + k[-4:]
        search_cfg = dict(config.get("search", {}))
        if search_cfg.get("api_key") and "****" not in str(search_cfg["api_key"]):
            k = search_cfg["api_key"]
            search_cfg["api_key"] = k[:4] + "****" + k[-4:]
        return jsonify({"asr": config["asr"], "llm": llm, "tts": config["tts"], "search": search_cfg})

    @app.route("/api/config/ai", methods=["PUT"])
    def update_ai_config():
        data = request.get_json()
        if not data:
            return jsonify({"error": "无效请求"}), 400
        import copy
        old_asr = copy.deepcopy(config.get("asr", {}))
        old_llm = copy.deepcopy(config.get("llm", {}))
        old_tts = copy.deepcopy(config.get("tts", {}))
        old_search = copy.deepcopy(config.get("search", {}))
        for section in ("asr", "llm", "tts", "search"):
            if section in data:
                # api_key 是掩码格式（含****）则跳过，不覆盖真实值
                if section in ("llm", "search") and "api_key" in data[section]:
                    if "****" in str(data[section]["api_key"]):
                        data[section].pop("api_key")
                # city_map / stock_map 需整体替换（支持增删条目），不走 _update_section 的"只更新已有 key"逻辑
                if section == "search":
                    for map_key in ("city_map", "stock_map"):
                        if map_key in data[section] and isinstance(data[section][map_key], dict):
                            config[section][map_key] = data[section][map_key]
                            data[section].pop(map_key)
                _update_section(config[section], data[section])
        _save_config()

        # 引擎配置任何变化 → 重置引擎单例，下次通话/请求用新引擎
        # （main.get_asr/get_tts 检测到注册表为空会重建）
        # 修复：原逻辑只对比 provider，切换 voice/speed/noise_scale 等子参数不重置，
        # 导致换 edge 音色后仍用旧 voice。改为深拷贝对比整个段。
        from engine.registry import reset_engine
        if data.get("search") and isinstance(data["search"], dict) \
                and config.get("search", {}) != old_search:
            reset_engine("search")
            logger.info(f"搜索配置变更，引擎已重置（下次通话生效）")
        if data.get("asr") and isinstance(data["asr"], dict) \
                and config.get("asr", {}) != old_asr:
            reset_engine("asr")
            logger.info(f"ASR 配置变更，引擎已重置（下次通话生效）")
        if data.get("tts") and isinstance(data["tts"], dict) \
                and config.get("tts", {}) != old_tts:
            reset_engine("tts")
            logger.info(f"TTS 配置变更，引擎已重置（下次通话生效）")
        if data.get("llm") and isinstance(data["llm"], dict) \
                and config.get("llm", {}) != old_llm:
            reset_engine("llm")
            logger.info(f"LLM 配置变更，引擎已重置（下次通话生效）")
        return jsonify({"status": "ok"})

    @app.route("/api/config/callback", methods=["GET"])
    def get_callback_config():
        return jsonify(config["callback"])

    @app.route("/api/config/callback", methods=["PUT"])
    def update_callback_config():
        data = request.get_json()
        if not data:
            return jsonify({"error": "无效请求"}), 400
        _update_section(config["callback"], data)
        _save_config()
        return jsonify({"status": "ok"})

    @app.route("/api/llm/test", methods=["POST"])
    def test_llm():
        try:
            data = request.get_json() or {}
            provider = data.get("provider") or config["llm"].get("provider", "openai_compat")
            # 构建测试用 config：provider + 对应子段（支持未保存前先测本地）
            test_cfg = {"provider": provider}
            if provider == "local_llama":
                test_cfg = {"provider": provider, "local_llama": config["llm"].get("local_llama", {})}
            else:
                test_cfg.update({k: v for k, v in config["llm"].items() if k != "local_llama"})
            from engine.llm import OpenAILLM
            llm = OpenAILLM(test_cfg)
            reply = llm.chat([{"role": "user", "content": data.get("prompt", "你好")}], timeout=10)
            return jsonify({"status": "ok", "reply": reply})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/llm/local/status")
    def local_llm_status():
        """查询本地 llama-server 运行状态（/v1/models + /metrics 预览）"""
        import requests as _req
        base = config["llm"].get("local_llama", {}).get("api_base", "http://host.docker.internal:8081/v1")
        root = base[:-3] if base.endswith("/v1") else base
        try:
            r = _req.get(base + "/models", timeout=3)
            model_id = r.json().get("data", [{}])[0].get("id", "?") if r.status_code == 200 else "?"
            metrics_preview = ""
            try:
                m = _req.get(root + "/metrics", timeout=3)
                metrics_preview = m.text[:500]
            except Exception:
                pass
            return jsonify({"online": True, "model": model_id, "metrics_preview": metrics_preview})
        except Exception as e:
            return jsonify({"online": False, "error": str(e)})

    @app.route("/api/llm/local/control/<path:action>", methods=["GET", "POST"])
    def local_llm_control(action):
        """代理到 WSL llama-bridge（8082）：启停/状态/日志。桥不可达时返回 online:false。"""
        base = config["llm"].get("local_llama", {}).get("api_base", "http://host.docker.internal:8081/v1")
        bridge = base.replace("/v1", "").replace(":8081", ":8082")
        token = config["llm"].get("local_llama", {}).get("api_key", "")
        url = bridge + "/" + action
        if request.query_string:
            url += "?" + request.query_string.decode("utf-8")
        try:
            import requests as _req
            r = _req.request(request.method, url, headers={"X-LLM-TOKEN": token}, timeout=25)
            try:
                return jsonify(r.json())
            except Exception:
                return jsonify({"online": False, "error": "bridge 返回非 JSON: %s" % r.text[:200]}), 502
        except Exception as e:
            return jsonify({"online": False, "error": str(e)})

    @app.route("/api/tts/test", methods=["POST"])
    def test_tts():
        """用当前表单选择的 TTS（provider+params）合成开场白，返回 base64 WAV 供浏览器播放。

        不依赖已保存配置：前端传 provider+params，后端按需临时建引擎（不污染单例），
        这样用户切换音色/speed 后点「测试TTS」立即听到新参数效果，无需先保存。
        """
        try:
            import struct, base64
            data = request.get_json() or {}
            text = "您好，我是智话通，请问有什么可以帮您"
            provider = data.get("provider") or config["tts"].get("provider", "matcha-zh-en-8k")
            params = data.get("params") or {}
            if not isinstance(params, dict):
                params = {}

            # 临时引擎（不写入 registry，避免污染通话中的引擎单例）。
            # 用 config.yaml 该 provider 的完整子配置打底，再用表单字段覆盖，
            # 保证 model_dir/threads/cache_dir 等非表单字段仍生效。
            base_params = dict(config["tts"].get(provider, {}) or {})
            base_params.update(params)

            from engine.tts import create_tts
            tts = create_tts({"provider": provider, provider: base_params})
            slin_data = tts.synthesize(text)
            if not slin_data:
                return jsonify({"status": "error", "message": "TTS 合成失败"}), 500

            data_len = len(slin_data)
            wav = struct.pack("<4sI4s", b"RIFF", 36 + data_len, b"WAVE")
            wav += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 8000, 16000, 2, 16)
            wav += struct.pack("<4sI", b"data", data_len)
            wav += slin_data
            b64 = base64.b64encode(wav).decode("ascii")

            duration = data_len / (8000 * 2)
            logger.info(f"[TTS测试] {provider}: {len(slin_data)}B ({duration:.1f}s)")
            return jsonify({
                "status": "ok",
                "audio": b64,
                "duration": round(duration, 1),
                "provider": provider,
            })
        except Exception as e:
            logger.error(f"[TTS测试] 失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/logs")
    def get_logs():
        """实时日志。call_id 参数可选：传入则从 call_logs 表查该通话的持久化日志。"""
        call_id = request.args.get("call_id", "").strip()
        if call_id:
            try:
                from engine.call_history import load_logs
                return jsonify({"logs": load_logs(call_id)})
            except Exception as e:
                logger.warning(f"[LOGS] 持久化日志读取失败: {e}")
                return jsonify({"logs": []})
        return jsonify({"logs": _global_log_queue[-50:]})

    @app.route("/api/logs/clear", methods=["POST"])
    def clear_logs():
        _global_log_queue.clear()
        return jsonify({"status": "ok"})

    @app.route("/api/extensions", methods=["GET"])
    def get_extensions():
        """获取分机列表 + 在线状态（通过 AMI）"""
        try:
            from asterisk.manager import Manager
            import time

            mgr = Manager()
            mgr.connect(
                _config["freepbx"]["host"],
                _config["freepbx"]["ami_port"],
            )
            mgr.login(
                _config["freepbx"]["ami_user"],
                _config["freepbx"]["ami_secret"],
            )

            endpoints = {}
            def on_endpoint_list(evt, manager):
                name = evt.get_header("ObjectName")
                contacts = evt.get_header("Contacts")
                device_state = evt.get_header("DeviceState")
                endpoints[name] = {
                    "online": bool(contacts.strip()),
                    "device_state": device_state,
                }

            mgr.register_event("EndpointList", on_endpoint_list)
            mgr.send_action({"Action": "PJSIPShowEndpoints"})
            time.sleep(2)  # 等事件推送完
            mgr.logoff()

            exts = []
            real_exts = _list_pjsip_extensions()
            for name, label in real_exts.items():
                info = endpoints.get(name)
                state = "在线" if (info and info["online"]) else "离线"
                exts.append({"name": name, "label": label, "state": state})

            return jsonify({"extensions": exts})

        except Exception as e:
            logger.error(f"获取分机状态失败: {e}")
            return jsonify({
                "extensions": [
                    {"name": "100", "label": "MicroSIP 软电话", "state": "未知"},
                    {"name": "101", "label": "ATA190 座机", "state": "未知"},
                ]
            })

    @app.route("/api/extensions", methods=["POST"])
    def add_extension():
        """添加分机：拦截保留号（0=AI助手、200=回拨入口）和已注册分机号。"""
        data = request.get_json() or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"error": "分机号不能为空"}), 400

        # 保留号拦截：9 是 AI 助手，200 是回拨入口，已被系统占用
        reserved = {"9": "AI 助手（AudioSocket）", "200": "回拨入口（AMI 外呼）"}
        if name in reserved:
            return jsonify({"error": f"分机 {name} 已被占用为「{reserved[name]}」，不可注册"}), 400

        # 已注册拦截：查 pjsip.conf 现有分机
        existing = _list_pjsip_extensions()
        if name in existing:
            return jsonify({"error": f"分机 {name} 已注册，不可重复注册"}), 400

        password = str(data.get("password", "")).strip()
        try:
            msg = _pjsip_edit(name, "add", password)
            _reload_pjsip()
            logger.info(msg)
            return jsonify({"status": "ok", "message": msg})
        except Exception as e:
            logger.error(f"添加分机失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/extensions/<name>", methods=["PUT"])
    def edit_extension(name):
        """编辑分机：action=label（描述）或 action=password（密码）。"""
        data = request.get_json() or {}
        action = str(data.get("action", "")).strip()
        value = str(data.get("value", "")).strip()
        if action not in ("label", "password"):
            return jsonify({"error": "action 只支持 label（编辑描述）或 password（修改密码）"}), 400
        if not value:
            return jsonify({"error": "值不能为空"}), 400
        try:
            msg = _pjsip_edit(name, action, value)
            _reload_pjsip()
            logger.info(msg)
            return jsonify({"status": "ok", "message": msg})
        except Exception as e:
            logger.error(f"编辑分机失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/extensions/<name>", methods=["DELETE"])
    def delete_extension(name):
        """删除分机：拦截保留号。"""
        reserved = {"9": "AI 助手", "200": "回拨入口"}
        if name in reserved:
            return jsonify({"error": f"分机 {name} 是系统保留号，不可删除"}), 400
        try:
            msg = _pjsip_edit(name, "delete")
            _reload_pjsip()
            logger.info(msg)
            return jsonify({"status": "ok", "message": msg})
        except Exception as e:
            logger.error(f"删除分机失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/calls")
    def get_calls():
        """历史查询：分页 + 模糊搜索（caller/callee/call_id）。"""
        try:
            from engine.call_history import load_calls_paged, count_calls
            page = max(1, int(request.args.get("page", 1)))
            page_size = min(100, max(1, int(request.args.get("page_size", 20))))
            keyword = request.args.get("keyword", "").strip()
            calls = load_calls_paged(page, page_size, keyword)
            total = count_calls(keyword)
            return jsonify({"calls": calls, "total": total, "page": page, "page_size": page_size})
        except Exception as e:
            logger.warning(f"[CALLS] 历史查询失败: {e}")
            return jsonify({"calls": [], "total": 0, "error": str(e)}), 500

    @app.route("/api/calls/<call_id>")
    def get_call_detail(call_id):
        """单条通话详情（元信息 + transcript 解析 + 关联日志）。"""
        try:
            from engine.call_history import load_call_detail
            detail = load_call_detail(call_id)
            if detail is None:
                return jsonify({"error": "未找到该通话"}), 404
            return jsonify(detail)
        except Exception as e:
            logger.warning(f"[CALLS] 详情查询失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/calls/<call_id>/export")
    def export_call(call_id):
        """导出单条通话为 txt（对话全文）。"""
        try:
            from flask import Response
            from engine.call_history import load_call_detail
            detail = load_call_detail(call_id)
            if detail is None:
                return jsonify({"error": "未找到该通话"}), 404
            lines = ["通话记录", "=" * 40]
            lines.append(f"主叫号码: {detail.get('caller') or ''}")
            lines.append(f"被叫号码: {detail.get('callee') or ''}")
            lines.append(f"通话开始: {detail.get('start_time') or ''}")
            lines.append(f"通话结束: {detail.get('end_time') or ''}")
            lines.append(f"通话时长: {detail.get('duration_s') or 0} 秒")
            lines.append(f"通话类型: {'系统回拨' if detail.get('is_callback') else '正常拨入'}")
            lines.append(f"结束原因: {detail.get('outcome') or ''}")
            lines.append("")
            lines.append("-" * 40)
            turns = detail.get("turns") or []
            if turns:
                for t in turns:
                    u = (t.get("user_text") or "").strip()
                    a = (t.get("ai_text") or "").strip()
                    if u:
                        lines.append(f"[用户] {u}")
                    if a:
                        lines.append(f"[AI] {a}")
            else:
                lines.append("（无对话内容）")
            text = "\n".join(lines)
            return Response(
                text,
                mimetype="text/plain; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename=call_{call_id[:8]}.txt"},
            )
        except Exception as e:
            logger.warning(f"[CALLS] 导出失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/callback/test", methods=["POST"])
    def test_callback():
        """主动回拨测试：AI 拨打 100 号码，播放回电语"""
        try:
            from asterisk.manager import Manager
            data = request.get_json() or {}
            target = data.get("target", "100")

            mgr = Manager()
            mgr.connect(
                _config["freepbx"]["host"],
                _config["freepbx"]["ami_port"],
            )
            mgr.login(
                _config["freepbx"]["ami_user"],
                _config["freepbx"]["ami_secret"],
            )

            audio_file = "custom/callback_test"
            channel = f"PJSIP/{target}"
            cid = _config["freepbx"]["callback_caller_id"]

            action = {
                "Action": "Originate",
                "Channel": channel,
                "Application": "Playback",
                "Data": audio_file,
                "CallerID": cid,
                "Timeout": 30000,
            }
            r = mgr.send_action(action)
            resp = r.get_header("Response")
            msg = r.get_header("Message")

            try:
                mgr.logoff()
            except Exception:
                pass

            logger.info(f"回拨测试: {channel} → {resp} {msg}")
            return jsonify({
                "status": resp,
                "message": msg,
                "channel": channel,
                "audio": audio_file,
            })
        except Exception as e:
            logger.error(f"回拨测试失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/callback/make", methods=["POST"])
    def make_callback():
        """指定号码+文本 → TTS生成 → AMI外呼→201分机(Playback+AudioSocket对话)"""
        try:
            from asterisk.manager import Manager
            data = request.get_json() or {}
            target = data.get("target", "100")
            text = data.get("text", "这是一条测试回拨，您好。")

            # 1. 生成 TTS 音频到共享目录（文件名按目标号码命名，200分机Playback会用）
            # ⚠️ 后缀必须是 .sln（8kHz），不是 .sln16！TTS 输出 8kHz，若命名 .sln16 会被 Asterisk 按 16kHz 播放 → 语速翻倍
            # 引擎单例走 registry（避免 from main 加载第二个模块实例）
            from engine.registry import get_engine
            tts = get_engine("tts")
            if tts is None:
                import sys
                tts = sys.modules["__main__"].get_tts()
            slin_data = tts.synthesize(text)

            from pathlib import Path
            audio_path = Path("/app/shared") / f"callback_{target}.sln"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(slin_data)
            logger.info(f"TTS音频已生成: {audio_path} ({len(slin_data)}字节)")

            # 2. AMI Originate → 201 分机（Playback + AudioSocket 对话）
            mgr = Manager()
            mgr.connect(
                _config["freepbx"]["host"],
                _config["freepbx"]["ami_port"],
            )
            mgr.login(
                _config["freepbx"]["ami_user"],
                _config["freepbx"]["ami_secret"],
            )

            channel = f"PJSIP/{target}"
            cid = _config["freepbx"]["callback_caller_id"]

            action = {
                "Action": "Originate",
                "Channel": channel,
                "Context": "default",
                "Exten": "200",
                "Priority": 1,
                "CallerID": cid,
                "Timeout": 30000,
                "Variable": f"CALLBACK_NUM={target}",
            }
            r = mgr.send_action(action)
            resp = r.get_header("Response")
            msg = r.get_header("Message")

            try:
                mgr.logoff()
            except Exception:
                pass

            logger.info(f"回拨: {channel} → 201 ({resp} {msg})")
            return jsonify({
                "status": resp,
                "message": msg,
                "channel": channel,
                "text": text,
                "audio": f"callback_{target}.sln",
            })
        except Exception as e:
            logger.error(f"回拨失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/callback/play", methods=["POST"])
    def play_callback():
        """用当前模型配置的 TTS 合成文本，返回 base64 WAV 供浏览器直接播放。

        默认文本「您好，这里是智话通的回拨测试」；用户改文本即用新文本实时合成。
        """
        try:
            import struct
            import base64
            data = request.get_json() or {}
            text = data.get("text", "您好，这里是智话通的回拨测试")

            # 1. 用当前 TTS 合成
            from engine.registry import get_engine
            tts = get_engine("tts")
            if tts is None:
                import sys
                tts = sys.modules["__main__"].get_tts()
            slin_data = tts.synthesize(text)
            if not slin_data:
                return jsonify({"status": "error", "message": "TTS 合成失败（空输出）"}), 500

            # 2. slin (8kHz int16 mono) → WAV → base64
            data_len = len(slin_data)
            wav = struct.pack("<4sI4s", b"RIFF", 36 + data_len, b"WAVE")
            wav += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 8000, 16000, 2, 16)
            wav += struct.pack("<4sI", b"data", data_len)
            wav += slin_data
            b64 = base64.b64encode(wav).decode("ascii")

            duration = data_len / (8000 * 2)
            logger.info(f"[播放] TTS 合成: {len(slin_data)}B ({duration:.1f}s)")
            return jsonify({
                "status": "ok",
                "audio": b64,
                "duration": round(duration, 1),
                "text": text,
            })
        except Exception as e:
            logger.error(f"[播放] 失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    # ═══ TFTP API ═══
    # 通过 os.path 直接读写 TFTP 配置（容器挂载 /app/data/tftpboot）

    _TFTP_PATH = "/app/data/tftpboot"

    def _tftp_list_files() -> list[dict]:
        """列出 tftpboot 下所有文件"""
        import os as _os
        import time as _tmod
        p = Path(_TFTP_PATH)
        if not p.exists():
            return []
        files = []
        for f in p.iterdir():
            if not f.is_file():
                continue
            st = f.stat()
            files.append({
                "name": f.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime * 1000),
            })
        files.sort(key=lambda x: x["name"])
        return files

    @app.route("/api/tftp/files")
    def api_tftp_files():
        try:
            files = _tftp_list_files()
            return jsonify({"files": files})
        except Exception as e:
            logger.error(f"TFTP 列出文件失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tftp/read")
    def api_tftp_read():
        fname = request.args.get("file", "")
        if not fname:
            return jsonify({"error": "缺少 file 参数"}), 400
        # 仅支持 .xml 文本读取（固件类二进制不返回）
        if not fname.lower().endswith(".xml"):
            return jsonify({"error": "仅支持 .xml 文件内容读取"}), 400
        try:
            fpath = Path(_TFTP_PATH) / fname
            if not fpath.exists() or not fpath.is_file():
                return jsonify({"error": f"文件 {fname} 不存在"}), 404
            # 安全检查：确保路径在 _TFTP_PATH 内
            fpath = fpath.resolve()
            if not str(fpath).startswith(str(Path(_TFTP_PATH).resolve())):
                return jsonify({"error": "非法的文件路径"}), 400
            content = fpath.read_text(encoding="utf-8")
            return jsonify({"content": content, "file": fname})
        except Exception as e:
            logger.error(f"TFTP 读取文件 {fname} 失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tftp/write", methods=["PUT"])
    def api_tftp_write():
        data = request.get_json()
        if not data or "file" not in data:
            return jsonify({"error": "缺少 file 参数"}), 400
        fname = data["file"]
        # 安全检查：防止路径穿越
        if "/" in fname or "\\" in fname or ".." in fname:
            return jsonify({"error": "文件名不能包含路径分隔符"}), 400
        fpath = Path(_TFTP_PATH) / fname
        content = data.get("content")
        if content is None:
            # content=null → 删除文件
            try:
                if fpath.exists():
                    fpath.unlink()
                    logger.info(f"TFTP 删除文件: {fname}")
                    return jsonify({"status": "ok", "action": "deleted", "file": fname})
                else:
                    return jsonify({"error": f"文件 {fname} 不存在"}), 404
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        else:
            # 写入/新建文件
            try:
                fpath.write_text(content, encoding="utf-8")
                logger.info(f"TFTP 写入文件: {fname} ({len(content)} 字节)")
                return jsonify({"status": "ok", "action": "written", "file": fname})
            except Exception as e:
                logger.error(f"TFTP 写入文件 {fname} 失败: {e}")
                return jsonify({"error": str(e)}), 500

    @app.route("/api/tftp/upload", methods=["POST"])
    def api_tftp_upload():
        if "file" not in request.files:
            return jsonify({"error": "缺少文件上传"}), 400
        f = request.files["file"]
        if f.filename == "":
            return jsonify({"error": "空文件名"}), 400
        fname = f.filename
        # 安全检查
        if "/" in fname or "\\" in fname or ".." in fname:
            return jsonify({"error": "文件名不能包含路径分隔符"}), 400
        try:
            data = f.read()
            fpath = Path(_TFTP_PATH) / fname
            fpath.write_bytes(data)
            logger.info(f"TFTP 上传成功: {fname} ({len(data)} 字节)")
            return jsonify({"status": "ok", "file": fname, "size": len(data)})
        except Exception as e:
            logger.error(f"TFTP 上传失败: {e}")
            return jsonify({"error": str(e)}), 500

    # ═══ 定时任务 API ═══

    def _get_scheduler():
        """获取调度器单例（registry 优先，fallback main 全局）。"""
        from engine.registry import get_engine
        sched = get_engine("scheduler")
        if sched is not None:
            return sched
        import sys
        return sys.modules["__main__"].get_scheduler()

    def _ensure_scheduler_cfg():
        cfg = _config.setdefault("scheduler", {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("jobs", [])
        return cfg

    def _reload_scheduler():
        try:
            sched = _get_scheduler()
            if sched is not None:
                sched.reload()
        except Exception as e:
            logger.warning(f"调度器重载失败: {e}")

    @app.route("/api/scheduler/jobs", methods=["GET"])
    def list_scheduler_jobs():
        cfg = _ensure_scheduler_cfg()
        return jsonify({"enabled": cfg.get("enabled", False), "jobs": cfg.get("jobs", [])})

    @app.route("/api/scheduler/jobs", methods=["POST"])
    def create_scheduler_job():
        from datetime import datetime
        data = request.get_json() or {}
        content = str(data.get("content", "")).strip()
        caller = str(data.get("caller", "")).strip()
        if not content:
            return jsonify({"error": "播报内容不能为空"}), 400
        if not caller:
            return jsonify({"error": "主叫号码不能为空"}), 400
        job = {
            "id": "job-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "name": str(data.get("name", "")).strip() or caller,
            "enabled": bool(data.get("enabled", True)),
            "freq": data.get("freq", "daily"),
            "time": data.get("time", "08:00"),
            "date": str(data.get("date", "")),
            "weekday": data.get("weekday", [0]),
            "interval_min": int(data.get("interval_min", 30) or 30),
            "content": content,
            "caller": caller,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        cfg = _ensure_scheduler_cfg()
        cfg["jobs"].append(job)
        _save_config()
        _reload_scheduler()
        return jsonify({"status": "ok", "job": job})

    @app.route("/api/scheduler/jobs/<job_id>", methods=["PUT"])
    def update_scheduler_job(job_id):
        data = request.get_json() or {}
        cfg = _ensure_scheduler_cfg()
        jobs = cfg["jobs"]
        target = next((j for j in jobs if j.get("id") == job_id), None)
        if target is None:
            return jsonify({"error": "任务不存在"}), 404
        editable = ("name", "enabled", "freq", "time", "date", "weekday", "interval_min", "content", "caller")
        for k in editable:
            if k in data and data[k] is not None:
                target[k] = data[k]
        target["content"] = str(target.get("content", "")).strip()
        target["caller"] = str(target.get("caller", "")).strip()
        if not target["content"] or not target["caller"]:
            return jsonify({"error": "播报内容和主叫号码不能为空"}), 400
        _save_config()
        _reload_scheduler()
        return jsonify({"status": "ok", "job": target})

    @app.route("/api/scheduler/jobs/<job_id>", methods=["DELETE"])
    def delete_scheduler_job(job_id):
        cfg = _ensure_scheduler_cfg()
        jobs = cfg["jobs"]
        remaining = [j for j in jobs if j.get("id") != job_id]
        if len(remaining) == len(jobs):
            return jsonify({"error": "任务不存在"}), 404
        cfg["jobs"] = remaining
        _save_config()
        _reload_scheduler()
        return jsonify({"status": "ok"})

    @app.route("/api/scheduler/jobs/<job_id>/run", methods=["POST"])
    def run_scheduler_job(job_id):
        """测试执行：立即触发该任务（LLM→TTS→外呼），不等调度时间。"""
        cfg = _ensure_scheduler_cfg()
        job = next((j for j in cfg.get("jobs", []) if j.get("id") == job_id), None)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        caller = str(job.get("caller", "101") or "101")
        content = str(job.get("content", "") or "")
        if not content:
            return jsonify({"error": "任务内容为空"}), 400
        # 异步执行（LLM 可能耗时数十秒，不阻塞请求）
        import threading as _th
        from engine.scheduler import fire_scheduled_call
        def _run():
            try:
                fire_scheduled_call(_config, caller, content, job.get("name", caller), "manual")
            except Exception as e:
                logger.error(f"[定时任务] 测试执行失败: {e}")
        _th.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "ok", "message": f"已触发任务「{job.get('name', caller)}」，将 LLM 生成后外呼 {caller}"})

    @app.route("/api/scheduler/runs")
    def get_scheduler_runs():
        """近期任务执行记录（手动测试 + 自动触发，近 20 条）。"""
        try:
            from engine.call_history import load_scheduler_runs
            runs = load_scheduler_runs(limit=10)
            return jsonify({"runs": runs, "total": len(runs)})
        except Exception as e:
            logger.warning(f"[SCHEDULER] 读取任务记录失败: {e}")
            return jsonify({"runs": [], "total": 0})

    @app.route("/api/scheduler/jobs/<job_id>/toggle", methods=["POST"])
    def toggle_scheduler_job(job_id):
        """启用/禁用切换。"""
        cfg = _ensure_scheduler_cfg()
        job = next((j for j in cfg.get("jobs", []) if j.get("id") == job_id), None)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        job["enabled"] = not job.get("enabled", True)
        _save_config()
        _reload_scheduler()
        state = "启用" if job["enabled"] else "禁用"
        return jsonify({"status": "ok", "enabled": job["enabled"], "message": f"任务已{state}"})

    logger.info("中屏路由注册完成")

def _check_local_llm() -> bool:
    """探活本地 llama-server：仅当 provider=local_llama 时才探，避免无关请求"""
    if _config["llm"].get("provider") != "local_llama":
        return False
    try:
        import requests
        base = _config["llm"].get("local_llama", {}).get("api_base", "http://host.docker.internal:8081/v1")
        r = requests.get(base + "/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _update_section(section: dict, data: dict):
    """递归更新配置段：只覆盖 data 中存在的 key。

    多引擎结构下 asr/tts 段是 {provider, xasr:{...}, zipformer:{...}, ...}：
    嵌套 dict 递归合并，前端只提交当前 provider 的参数也不会丢其他引擎的
    已有参数（如 tokens_file / sample_rate / enable_endpoint_detection）。
    """
    for k, v in data.items():
        if k in section:
            if isinstance(v, dict) and isinstance(section[k], dict):
                _update_section(section[k], v)
            else:
                section[k] = v


def _save_config():
    import yaml
    try:
        with open(_config_path, "w", encoding="utf-8") as f:
            yaml.dump(_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"配置已保存到 {_config_path}")
    except Exception as e:
        logger.error(f"保存配置失败: {e}")


def _find_config_path():
    global _config_path
    if _config_path:
        return
    env_path = os.environ.get("CONFIG_PATH")
    if env_path and os.path.exists(env_path):
        _config_path = env_path
        return
    dev_path = Path(__file__).parent.parent.parent / "config.yaml"
    if dev_path.exists():
        _config_path = str(dev_path)
        return
    _config_path = "/app/config.yaml"


# ── pjsip.conf 读写（分机管理）──

_PJSIP_CONF = "/app/data/asterisk2/pjsip.conf"


def _read_pjsip_labels() -> dict:
    """从 pjsip.conf 读取所有分机的 callerid（描述标签）。"""
    labels = {}
    try:
        cur_name = None
        cur_type = None
        with open(_PJSIP_CONF, "r") as f:
            for line in f:
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    cur_name = s[1:-1].strip()
                    cur_type = None
                elif cur_name and s.startswith("type") and "=" in s:
                    cur_type = s.split("=", 1)[1].strip()
                elif cur_name and cur_type == "endpoint" and s.startswith("callerid") and "=" in s:
                    labels[cur_name] = s.split("=", 1)[1].strip()
    except Exception:
        pass
    return labels


def _reload_pjsip():
    """通过 AMI 让 Asterisk 重载 PJSIP 配置。"""
    try:
        from asterisk.manager import Manager
        mgr = Manager()
        mgr.connect(_config["freepbx"]["host"], _config["freepbx"]["ami_port"])
        mgr.login(_config["freepbx"]["ami_user"], _config["freepbx"]["ami_secret"])
        mgr.send_action({"Action": "Command", "Command": "module reload res_pjsip.so"})
        mgr.logoff()
        logger.info("PJSIP 配置已重载")
    except Exception as e:
        logger.warning(f"PJSIP 重载失败: {e}")


def _sync_pjsip_host_ip(new_ip: str):
    """host_ip 变更时同步 pjsip.conf 的 external/media address 并重载。

    pjsip.conf 挂在 /app/data/asterisk2/（与 freepbx 容器共享），
    external_media_address / external_signaling_address / media_address
    三处对外 IP 同步替换为新值，否则 ATA190 的 RTP 上行会发到旧 IP。
    """
    import re
    try:
        with open(_PJSIP_CONF, "r") as f:
            content = f.read()
        old = content
        # transport 段的 external 地址
        content = re.sub(
            r"^(external_media_address\s*=\s*)\S+",
            rf"\g<1>{new_ip}",
            content, flags=re.MULTILINE)
        content = re.sub(
            r"^(external_signaling_address\s*=\s*)\S+",
            rf"\g<1>{new_ip}",
            content, flags=re.MULTILINE)
        # 各 endpoint 的 media_address
        content = re.sub(
            r"^(media_address\s*=\s*)\S+",
            rf"\g<1>{new_ip}",
            content, flags=re.MULTILINE)
        if content != old:
            with open(_PJSIP_CONF, "w") as f:
                f.write(content)
            logger.info(f"pjsip.conf external/media address 已同步为 {new_ip}")
            _reload_pjsip()
        else:
            logger.info(f"pjsip.conf 无需同步（已为 {new_ip}）")
    except Exception as e:
        logger.error(f"同步 pjsip.conf host_ip 失败: {e}")


def _pjsip_edit(ext_name: str, action: str, value: str = None) -> str:
    """编辑 pjsip.conf。action='label'/'password'/'add'/'delete'。返回操作描述。"""
    import re
    with open(_PJSIP_CONF, "r") as f:
        content = f.read()

    if action == "delete":
        # 逐行跳过 [ext_name] 的 section 及其后的空行分隔
        lines = content.splitlines(keepends=True)
        out = []
        i, n = 0, len(lines)
        while i < n:
            if lines[i].strip() == f"[{ext_name}]":
                i += 1
                while i < n and not lines[i].strip().startswith("["):
                    i += 1
                while i < n and lines[i].strip() == "":
                    i += 1
                continue
            out.append(lines[i])
            i += 1
        content = "".join(out)
        with open(_PJSIP_CONF, "w") as f:
            f.write(content)
        return f"分机 {ext_name} 已删除"

    if action == "add":
        media = _config.get("freepbx", {}).get("external_media_address", "10.168.2.94")
        if not value:
            raise ValueError("密码不能为空")
        block = (
            f"\n[{ext_name}]\n"
            f"type = aor\nmax_contacts = 1\nremove_existing = yes\n\n"
            f"[{ext_name}]\n"
            f"type = auth\nauth_type = userpass\npassword = {value}\nusername = {ext_name}\n\n"
            f"[{ext_name}]\n"
            f"type = endpoint\ncontext = default\ndisallow = all\nallow = ulaw\nallow = alaw\n"
            f"allow = slin\noutbound_auth = {ext_name}\nauth = {ext_name}\naors = {ext_name}\n"
            f"rewrite_contact = yes\nmedia_address = {media}\n"
        )
        with open(_PJSIP_CONF, "a") as f:
            f.write(block)
        return f"分机 {ext_name} 已添加"

    # edit label / password
    target_type = "endpoint" if action == "label" else "auth"
    target_key = "callerid" if action == "label" else "password"

    # 在 [ext_name] 的 target_type 段内替换/插入 key
    pattern = rf"(\[{re.escape(ext_name)}\]\n.*?type\s*=\s*{target_type}\b.*?\n)(.*?)(\n\[|\n\n|\Z)"
    # 只在 type=target_type 的段内操作
    # 更简单的做法：分段处理
    sections = re.split(r"(?=\n?\[)", content)
    modified = False
    new_sections = []
    for sec in sections:
        if not sec.strip():
            new_sections.append(sec)
            continue
        header = sec.strip().split("\n")[0]
        if header != f"[{ext_name}]":
            new_sections.append(sec)
            continue
        # 检查 type 是否匹配
        type_match = re.search(r"type\s*=\s*(\S+)", sec)
        if not type_match or type_match.group(1) != target_type:
            new_sections.append(sec)
            continue
        # 替换或插入 key
        if re.search(rf"^{target_key}\s*=", sec, re.MULTILINE):
            sec = re.sub(rf"^{target_key}\s*=.*", f"{target_key} = {value}", sec, flags=re.MULTILINE)
        else:
            # 在 type 行后插入
            sec = re.sub(r"(type\s*=\s*" + target_type + r".*\n)", r"\1    " + target_key + f" = {value}\n", sec)
        modified = True
        new_sections.append(sec)
    if not modified:
        raise ValueError(f"未找到分机 {ext_name} 的 {action} 配置段")
    content = "".join(new_sections)
    with open(_PJSIP_CONF, "w") as f:
        f.write(content)
    return f"分机 {ext_name} 的 {target_key} 已更新为 {value}"


def _list_pjsip_extensions() -> dict:
    """返回 pjsip.conf 中真实分机的 {name: label}，排除 200（回拨）和 transport 段。
    label 优先取 callerid，其次取默认值。"""
    default_labels = {"100": "MicroSIP 软电话", "101": "ATA190 座机"}
    labels = _read_pjsip_labels()
    result = {}
    try:
        cur_name = cur_type = None
        with open(_PJSIP_CONF, "r") as f:
            for line in f:
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    cur_name = s[1:-1].strip()
                    cur_type = None
                elif cur_name and s.startswith("type") and "=" in s:
                    cur_type = s.split("=", 1)[1].strip()
                    if cur_type == "aor" and cur_name not in ("200", "transport-udp"):
                        result[cur_name] = labels.get(cur_name) or default_labels.get(cur_name, cur_name)
    except Exception:
        return dict(default_labels)
    return result
