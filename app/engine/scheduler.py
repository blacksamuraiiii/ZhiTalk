import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ai-backend.scheduler")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.date import DateTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    DateTrigger = None  # type: ignore


def fire_scheduled_call(config: dict, caller: str, content: str, name: str = "", trigger_type: str = "auto"):
    """定时任务回调：联网查询 → LLM 生成 → TTS 合成（含开场白）→ 写共享 sln + 保存上下文 → AMI Originate。
    trigger_type: 'manual'（测试按钮）或 'auto'（调度器触发）。
    """
    if not content:
        logger.warning("[定时任务] 内容为空，跳过")
        return

    import uuid as _uuid
    from engine.call_history import insert_scheduler_run, update_scheduler_run
    run_id = _uuid.uuid4().hex
    _cb_caller = config.get("freepbx", {}).get("callback_caller_id", "AI助手")
    insert_scheduler_run(
        run_id=run_id, job_name=name or caller, trigger_type=trigger_type,
        caller=_cb_caller, callee=caller, content=content, status="executing"
    )

    # 1. 先判断是否需要联网搜索（复用 MCP 垂直域路由逻辑）
    from engine.registry import get_engine
    llm = get_engine("llm")
    if llm is None:
        import sys
        llm = sys.modules["__main__"].get_llm()

    search_client = get_engine("search")
    if search_client is None:
        import sys
        search_client = sys.modules["__main__"].get_search()

    try:
        need, route = search_client.need_search(content)
        if need:
            from engine.search import build_query
            q = build_query(content, [{"role": "user", "content": content}])
            ctx = None
            if route == "weather":
                city = search_client.extract_city(content)
                ctx = search_client.search(city or q, route if city else "general")
            elif route == "finance":
                ftype, fparam = search_client.lookup_stock(content)
                ctx = search_client.search(q, route, finance_type=ftype, finance_param=fparam)
            else:
                ctx = search_client.search(q, route)

            if ctx:
                logger.info(f"[定时任务] 联网搜索 ({route}) → {len(ctx)} 字符")
                reply = llm.chat_with_search(
                    [{"role": "user", "content": content}], ctx, timeout=60
                )
            else:
                # 需要联网但搜索失败 → 放弃本次任务（不瞎编，不外呼）
                logger.warning(f"[定时任务] 联网搜索失败（{route}），放弃本次外呼")
                update_scheduler_run(run_id, "failed", error=f"联网搜索失败({route})")
                return
        else:
            reply = llm.chat([{"role": "user", "content": content}], timeout=60)
    except Exception as e:
        logger.error(f"[定时任务] LLM 生成失败: {e}")
        update_scheduler_run(run_id, "failed", error=f"LLM生成失败: {e}")
        return
    if not reply:
        logger.warning("[定时任务] LLM 返回空，跳过")
        update_scheduler_run(run_id, "failed", error="LLM返回空")
        return
    logger.info(f"[定时任务] LLM 回复: {reply[:80]}...")

    # 1.5. 拼开场白前缀（"您好，我是智话通，每日天气预报。..."）
    full_text = reply
    if name:
        full_text = f"您好，我是智话通，{name}。{reply}"
        logger.info(f"[定时任务] 开场白拼接: {full_text[:80]}...")

    # 2. TTS 合成（引擎单例走 registry，避免 from main 加载第二个模块实例）
    tts = get_engine("tts")
    if tts is None:
        import sys
        tts = sys.modules["__main__"].get_tts()
    slin_data = tts.synthesize(full_text)
    if not slin_data:
        logger.warning("[定时任务] TTS 合成失败（空输出），跳过")
        update_scheduler_run(run_id, "failed", error="TTS合成失败")
        return

    # 3. 写共享目录（后缀 .sln 8kHz，与回拨一致）
    audio_path = Path("/app/shared") / f"callback_{caller}.sln"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(slin_data)
    logger.info(f"[定时任务] TTS 音频已生成: {audio_path} ({len(slin_data)}字节)")

    # 3.5. 保存对话上下文（回拨通话追问时用，格式与 ConversationHandler.to_json() 一致）
    import json
    # assistant 存 full_text（含开场白"您好，我是智话通，任务名"），详情页能显示完整播报
    context = [{"role": "user", "content": content}, {"role": "assistant", "content": full_text}]
    context_path = Path("/app/shared") / f"context_{caller}.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[定时任务] 上下文已保存: {context_path}")

    # 4. AMI Originate → 200 分机（Playback + AudioSocket 对话）
    from asterisk.manager import Manager
    mgr = Manager()
    mgr.connect(config["freepbx"]["host"], config["freepbx"]["ami_port"])
    mgr.login(config["freepbx"]["ami_user"], config["freepbx"]["ami_secret"])
    action = {
        "Action": "Originate",
        "Channel": f"PJSIP/{caller}",
        "Context": "default",
        "Exten": "200",
        "Priority": 1,
        "CallerID": config["freepbx"]["callback_caller_id"],
        "Timeout": 30000,
        "Variable": f"CALLBACK_NUM={caller}",
    }
    r = mgr.send_action(action)
    resp = r.get_header("Response")
    msg = r.get_header("Message")
    try:
        mgr.logoff()
    except Exception:
        pass
    logger.info(f"[定时任务] 外呼 {caller} → {resp} {msg}")
    # 外呼成功
    update_scheduler_run(run_id, "success", reply=full_text)


class SchedulerManager:
    """定时任务管理器：从 config['scheduler']['jobs'] 读任务，注册到 APScheduler。"""

    def __init__(self, config: dict):
        self.config = config
        self._sched = None
        self._lock = threading.Lock()
        if not HAS_APSCHEDULER:
            logger.warning("[定时任务] apscheduler 未安装，定时任务不可用")
            return
        self._sched = BackgroundScheduler()
        self._sched.start()
        logger.info("[定时任务] 调度器已启动")

    def reload(self):
        """清空全部 job，按 config 当前 jobs 重建（每次 CRUD 后调用）。"""
        if self._sched is None:
            return
        with self._lock:
            for job in self._sched.get_jobs():
                try:
                    job.remove()
                except Exception:
                    pass
            scheduler_cfg = self.config.get("scheduler", {}) or {}
            if not scheduler_cfg.get("enabled", False):
                logger.info("[定时任务] 调度器已禁用，清空全部任务")
                return
            jobs = scheduler_cfg.get("jobs", []) or []
            for j in jobs:
                if not j.get("enabled", True):
                    continue
                self._add_job(j)
            logger.info(f"[定时任务] 已重载 {len(jobs)} 条任务")

    def _add_job(self, j: dict):
        trigger = self._make_trigger(j)
        if trigger is None:
            return
        job_id = f"sched-{j.get('id') or j.get('name')}"
        self._sched.add_job(
            self._run_job,
            trigger=trigger,
            args=[j],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60,
        )

    @staticmethod
    def _make_trigger(j: dict):
        freq = j.get("freq", "once")
        t = str(j.get("time", "08:00"))
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except Exception:
            hh, mm = 8, 0

        if freq == "daily":
            return CronTrigger(hour=hh, minute=mm)
        if freq == "weekly":
            wdays = j.get("weekday", [0])
            if not wdays:
                wdays = [0]
            return CronTrigger(day_of_week=",".join(str(d) for d in wdays), hour=hh, minute=mm)
        if freq == "interval":
            return IntervalTrigger(minutes=int(j.get("interval_min", 30) or 30))

        # once：一次性，需 date + time
        d = str(j.get("date", ""))
        try:
            run = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
        except Exception:
            logger.warning(f"[定时任务] 一次性任务日期非法: {d} {t}")
            return None
        if run <= datetime.now():
            logger.warning(f"[定时任务] 一次性任务时间已过，跳过: {d} {t}")
            return None
        return DateTrigger(run_date=run)

    def _run_job(self, j: dict):
        caller = str(j.get("caller", "101") or "101")
        content = str(j.get("content", "") or "")
        name = j.get("name", caller)
        try:
            logger.info(f"[定时任务] 触发: {name} → 拨打 {caller}")
            fire_scheduled_call(self.config, caller, content, name)
        except Exception as e:
            logger.error(f"[定时任务] 触发失败 ({name}): {e}")

    def shutdown(self):
        if self._sched is not None:
            self._sched.shutdown(wait=False)