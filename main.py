# main.py
import asyncio
import yaml
import logging
from skland_api import SklandAPI
from notifier import NotifierManager

# 初始化基础日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SklandStandalone")

async def run_sign_in():
    # 1. 加载配置
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("找不到 config.yaml 文件")
        return []

    # 2. 日志等级控制
    user_log_level = config.get("log_level", "info").lower()
    for lib in ["httpx", "httpcore", "skland_api", "Qmsg"]:
        lib_logger = logging.getLogger(lib)
        lib_logger.setLevel(logging.INFO if user_log_level == "debug" else WARNING)

    users = config.get("users", [])

    if not users:
        logger.warning("配置中没有发现用户信息")
        return []

    api = SklandAPI(max_retries=3)
    notifier = NotifierManager(config)

    notify_lines = ["📅 森空岛签到姬", ""]

    logger.info(f"开始执行签到任务，共 {len(users)} 个账号")

    account_results = []

    for index, user in enumerate(users, 1):
        nickname_cfg = user.get("nickname", "未知用户")
        token = user.get("token")

        user_header = f"🌈 No.{index}({nickname_cfg}):"
        notify_lines.append(user_header)
        logger.info(f"正在处理: {nickname_cfg}")

        games = []

        if not token:
            logger.error(f"  [{nickname_cfg}] 未配置 Token")
            notify_lines.append("❌ 账号配置错误: 缺少Token")
            notify_lines.append("")
            account_results.append({"nickname": nickname_cfg, "error": "缺少Token", "games": []})
            continue

        try:
            results, official_nickname = await api.do_full_sign_in(token)

            if not results:
                notify_lines.append("❌ 未找到绑定角色")
                logger.warning(f"  [{nickname_cfg}] 未找到角色")
                games.append({"name": "绑定角色", "status": "❌ 未找到", "detail": ""})

            for r in results:
                is_signed_already = not r.success and any(k in r.error for k in ["已签到", "重复", "already"])

                if r.success:
                    icon = "✅"
                    status_text = "成功"
                    detail = f" ({', '.join(r.awards)})" if r.awards else ""
                elif is_signed_already:
                    icon = "✅"
                    status_text = "已签"
                    detail = ""
                else:
                    icon = "❌"
                    status_text = "失败"
                    detail = f" ({r.error})"

                line = f"{icon} {r.game}: {status_text}{detail}"
                notify_lines.append(line)
                logger.info(f"  - {line}")
                games.append({"name": r.game, "status": f"{icon} {status_text}", "detail": detail})

        except Exception as e:
            error_msg = str(e)
            logger.error(f"  [{nickname_cfg}] 异常: {error_msg}")
            notify_lines.append(f"❌ 系统错误: {error_msg}")
            games.append({"name": "系统错误", "status": "❌ 异常", "detail": f" ({error_msg})"})

        account_results.append({"nickname": nickname_cfg, "games": games})
        notify_lines.append("")

    await api.close()

    while notify_lines and notify_lines[-1] == "":
        notify_lines.pop()

    final_message = "\n".join(notify_lines)
    await notifier.send_all(final_message)

    logger.info("所有任务已完成")
    return account_results

# 补充缺失的常量定义 (防止上面代码报错)
WARNING = logging.WARNING

if __name__ == "__main__":
    asyncio.run(run_sign_in())