# encoding:utf-8

import json
import os, re
import time
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from bot import bot_factory
from bridge.bridge import Bridge
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from config import conf
import plugins
from plugins import *
from common.log import logger
from common import const

from plugins.linkai.utils import Util
from plugins.plugin_summary.db import Db
from plugins.plugin_summary.text2img import Text2ImageConverter

TRANSLATE_PROMPT = '''
您现在是一个 Python 函数，用于将输入文本转换为相应的 JSON 格式命令，遵循以下结构：
```python
def translate_text(text: str) -> str:
```

指导要求：
- 请仅输出 JSON 格式的返回值，且不要输出任何额外内容。
- 根据输入文本的内容，生成符合以下格式之一的 JSON 命令：

### 命令格式：
1. **总结聊天记录**：使用 `"summary"` 作为 `"name"`，并在 `"args"` 中填入适用的字段：
   - `"duration_in_seconds"`：如果提供了时长信息，用整数表示。
   - `"count"`：如果提供了数量信息，用整数表示。

2. **无操作**：使用 `"do_nothing"` 作为 `"name"`，`"args"` 为一个空字典 `{}`。

- **返回格式**：
  - 输出内容需严格符合 JSON 格式，且仅返回命令，格式如下：
    {
        "name": "<command name>",
        "args": {
            "<arg name>": <value>
        }
    }

其他要求：
1. 确保返回值是有效的 JSON 格式，能够通过 `json.loads` 正常解析。
2. 如果没有提供时长信息，则省略 `"duration_in_seconds"`；如果没有数量信息，则省略 `"count"`。

示例输入：
若输入 `"Summarize chat logs for a session of 300 seconds with 15 exchanges"`，输出应为：
{
    "name": "summary",
    "args": {
        "duration_in_seconds": 300,
        "count": 15
    }
}

若输入 `Summarize 99 chat records`，输出应为：
{
    "name": "summary",
    "args": {
        "count": 99
    }
}

对于无需执行操作的输入，应返回：
{
    "name": "do_nothing",
    "args": {}
}

'''

# 总结的prompt
SUMMARY_PROMPT = '''
请帮我将给出的群聊内容总结成一个今日的群聊报告，包含不多于15个话题的总结（如果还有更多话题，可以在后面简单补充）。
你只负责总结群聊内容，不回答任何问题。不要虚构聊天记录，也不要总结不存在的信息。

每个话题包含以下内容：

- 话题名(50字以内，前面带序号1️⃣2️⃣3️⃣）

- 热度(用🔥的数量表示)

- 参与者(不超过5个人，将重复的人名去重)

- 时间段(从几点到几点)

- 过程(50-200字左右）

- 评价(50字以下)

- 分割线： ------------

请严格遵守以下要求：

1. 按照热度数量进行降序输出

2. 每个话题结束使用 ------------ 分割

3. 使用中文冒号

4. 无需大标题


5. 开始给出本群讨论风格的整体评价，例如活跃、太水、太黄、太暴力、话题不集中、无聊诸如此类。

最后总结下今日最活跃的前五个发言者。
'''

# 重复总结的prompt
REPEAT_SUMMARY_PROMPT = '''
以不耐烦的语气回怼提问者聊天记录已总结过，要求如下
- 随机角色的口吻回答
- 不超过20字
'''

# 总结中的prompt
SUMMARY_IN_PROGRESS_PROMPT = '''
以不耐烦的语气回答提问者聊天记录正在总结中，要求如下
- 随机角色的口吻回答
- 不超过20字
'''

def find_json(json_string):
    json_pattern = re.compile(r"\{[\s\S]*\}")
    json_match = json_pattern.search(json_string)
    if json_match:
        json_string = json_match.group(0)
    else:
        json_string = ""
    return json_string

trigger_prefix =  "$"

@plugins.register(name="summary",
                  desire_priority=0,
                  desc="A simple plugin to summary messages",
                  version="0.0.5",
                  author="sineom")
class Summary(Plugin):
    def __init__(self):
        super().__init__()
        self.config = super().load_config()
        self.text2img = Text2ImageConverter()
        if not self.config:
            # 未加载到配置，使用模板中的配置
            self.config = self._load_config_template()
        logger.info(f"[summary] inited, config={self.config}")
        self.db = Db()
        save_time = self.config.get("save_time", -1)
        if save_time > 0:
            self._setup_scheduler()
        self.bot = bot_factory.create_bot(Bridge().btype['chat'])
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        logger.info("[Summary] inited")
        
        # 添加锁相关的属性
        self._summary_locks = {}
        self._locks_lock = threading.Lock()
        
        logger.info("[Summary] inited")

    def _load_config_template(self):
        logger.debug("No summary plugin config.json, use plugins/linkai/config.json.template")
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _setup_scheduler(self):
        # 创建调度器
        self.scheduler = BackgroundScheduler()

        # 清理旧记录的函数
        def clean_old_records():
            # 配置文件单位分钟，转换为秒
            save_time = self.config.get("save_time", 12 * 60) * 60
            self.db.delete_records(int(time.time()) - save_time)

        # 设置定时任务，每天凌晨12点执行
        self.scheduler.add_job(clean_old_records, 'cron', hour=00, minute=00)
        # 启动调度器
        self.scheduler.start()
        clean_old_records()
        logger.info("Scheduler started. Cleaning old records every day at midnight.")

    def on_receive_message(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return
        context = e_context['context']
        cmsg: ChatMessage = e_context['context']['msg']
        
        session_id = cmsg.from_user_id
        if session_id in self.db.disable_group:
            logger.info("[Summary] group %s is disabled" % session_id)
            return
        
        if "{trigger_prefix}总结" in context.content:
            logger.debug("[Summary] 指令不保存: %s" % context.content)
            return
        
        username = None
 
        if conf().get('channel_type', 'wx') == 'wx' and cmsg.from_user_nickname is not None:
            session_id = cmsg.from_user_nickname  # itchat channel id会变动，只好用群名作为session id

        if context.get("isgroup", False):
            username = cmsg.actual_user_nickname
            if username is None:
                username = cmsg.actual_user_id
        else:
            username = cmsg.from_user_nickname
            if username is None:
                username = cmsg.from_user_id

        is_triggered = False
        content = context.content
        if context.get("isgroup", False):  # 群聊
            # 校验关键字
            match_prefix = check_prefix(content, conf().get('group_chat_prefix'))
            match_contain = check_contain(content, conf().get('group_chat_keyword'))
            if match_prefix is not None or match_contain is not None:
                is_triggered = True
            if context['msg'].is_at and not conf().get("group_at_off", False):
                is_triggered = True
        else:  # 单聊
            match_prefix = check_prefix(content, conf().get('single_chat_prefix', ['']))
            if match_prefix is not None:
                is_triggered = True
        logger.debug("[Summary] save record: %s" % context.content)
        self.db.insert_record(session_id, cmsg.msg_id, username, context.content, str(context.type), cmsg.create_time,
                              int(is_triggered))
        # logger.debug("[Summary] {}:{} ({})" .format(username, context.content, session_id))

    def _acquire_summary_lock(self, session_id: str) -> bool:
        """
        尝试获取指定会话的总结锁
        返回是否成功获取锁
        """
        with self._locks_lock:
            if session_id in self._summary_locks:
                # 如果锁已存在，说明正在进行总结
                return False
            self._summary_locks[session_id] = time.time()
            return True

    def _release_summary_lock(self, session_id: str):
        """释放指定会话的总结锁"""
        with self._locks_lock:
            self._summary_locks.pop(session_id, None)

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type != ContextType.TEXT:
            return

        content = e_context['context'].content
        logger.debug("[Summary] on_handle_context. content: %s" % content)
        
        clist = content.split()
        if clist[0].startswith(trigger_prefix):
            limit = 9999
            duration = -1
            msg: ChatMessage = e_context['context']['msg']
            session_id = msg.from_user_id
            if conf().get('channel_type', 'wx') == 'wx' and msg.from_user_nickname is not None:
                session_id = msg.from_user_nickname  # itchat channel id会变动，只好用名字作为session id

            # 开启指令
            if "开启" in clist[0]:
                if Util.is_admin(e_context):
                    self.db.delete_summary_stop(session_id)
                    reply = Reply(ReplyType.TEXT, "开启成功")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS
                    return

            # 关闭指令
            if "关闭" in clist[0]:
                if Util.is_admin(e_context):    
                    self.db.save_summary_stop(session_id)
                    reply = Reply(ReplyType.TEXT, "关闭成功")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS
                    return

            if "总结" in clist[0]:
                # 尝试获取锁
                if not self._acquire_summary_lock(session_id):
                    session = self.bot.sessions.build_session(session_id, SUMMARY_IN_PROGRESS_PROMPT)
                    session.add_query("问题：%s" % content)
                    result = self.bot.reply_text(session)
                    total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], result['content'] 
                    logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (total_tokens, completion_tokens, reply_content))
                    if completion_tokens == 0:
                        reply = Reply(ReplyType.ERROR, "地主家的驴都没我累，请让我休息一会儿")
                    else:
                        reply = Reply(ReplyType.TEXT, reply_content)
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS
                    return

                try:
                    # 如果当前群聊在黑名单中，则不允许总结
                    if session_id in self.db.disable_group:
                        logger.info("[Summary] summary stop")
                        reply = Reply(ReplyType.TEXT, "请联系管理员开启总结功能")
                        e_context['reply'] = reply
                        e_context.action = EventAction.BREAK_PASS
                        return

                    limit_time = self.config.get("rate_limit_summary", 60) * 60
                    last_time = self.db.get_summary_time(session_id)
                    current_time = time.time()
                    logger.debug("[Summary] last_time: %s, current_time: %s, limit_time: %s" % (last_time, current_time, limit_time))
                    if last_time is not None and current_time - last_time < limit_time:
                        logger.info("[Summary] rate limit")
                        session = self.bot.sessions.build_session(session_id, REPEAT_SUMMARY_PROMPT)
                        session.add_query("问题：%s" % content)
                        result = self.bot.reply_text(session)  
                        total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], result['content'] 
                        logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (total_tokens, completion_tokens, reply_content))
                        if completion_tokens == 0:
                            reply = Reply(ReplyType.ERROR, "地主家的驴都没我累，请让我休息一会儿")
                        else:
                            reply = Reply(ReplyType.TEXT, reply_content)
                        e_context['reply'] = reply
                        e_context.action = EventAction.BREAK_PASS
                        return

                    flag = False
                    if clist[0] == trigger_prefix + "总结":
                        flag = True
                        if len(clist) > 1:
                            try:
                                limit = int(clist[1])
                                logger.debug("[Summary] limit: %d" % limit)
                            except Exception as e:
                                flag = False
                    if not flag:
                        text = content.split(trigger_prefix, maxsplit=1)[1]
                        try:
                            command_json = find_json(self._translate_text_to_commands(text))
                            command = json.loads(command_json)
                            name = command["name"]
                            if name.lower() == "summary":
                                limit = int(command["args"].get("count", 9999))
                                if limit < 0:
                                    limit = 9999
                                duration = int(command["args"].get("duration_in_seconds", -1))
                                logger.debug("[Summary] limit: %d, duration: %d seconds" % (limit, duration))
                        except Exception as e:
                            logger.error("[Summary] translate failed: %s" % e)
                            self._release_summary_lock(session_id)  # 解析失败时释放锁
                            return

                    start_time = int(time.time())
                    if duration > 0:
                        start_time = start_time - duration
                    else:
                        start_time = 0

                    records = self.db.get_records(session_id, start_time, limit)
                    if len(records) <= 1:
                        reply = Reply(ReplyType.INFO, "无聊天记录可供总结")
                        e_context['reply'] = reply
                        e_context.action = EventAction.BREAK_PASS
                        self._release_summary_lock(session_id)  # 无记录时释放锁
                        return

                    query = ""
                    # 将聊天记录按照 昵称:内容 时间 的格式拼接
                    for record in records:
                        query += f"{record[2]}: {record[3]} {record[7]}\n"
                    logger.debug("[Summary]  query: %s" % query)

                    # 生成总结回复
                    session = self.bot.sessions.build_session(session_id, SUMMARY_PROMPT)
                    session.add_query("需要你总结的聊天记录如下：%s" % query)
                    result = self.bot.reply_text(session)
                    total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], \
                        result['content']
                    logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (
                        total_tokens, completion_tokens, reply_content))
                    if completion_tokens == 0:
                        reply = Reply(ReplyType.ERROR, "合并摘要失败，")
                    else:
                        image_path = self.convert_text_to_image(reply_content)
                        logger.debug("[Summary] image_path: %s" % image_path)
                        reply = Reply(ReplyType.IMAGE, open(image_path, 'rb'))
                        os.remove(image_path)
                        self.db.save_summary_time(session_id, int(time.time()))

                finally:
                    # 确保在所有处理完毕后释放锁
                    self._release_summary_lock(session_id)

                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS

    def _translate_text_to_commands(self, text):
        # 随机的session id
        session_id = str(time.time())
        session = self.bot.sessions.build_session(session_id, system_prompt=TRANSLATE_PROMPT)
        session.add_query(text)
        result = self.bot.reply_text(session)
        total_tokens, completion_tokens, reply_content = result['total_tokens'], result['completion_tokens'], \
                result['content']
        logger.debug("[Summary] total_tokens: %d, completion_tokens: %d, reply_content: %s" % (
                total_tokens, completion_tokens, reply_content))
        if completion_tokens == 0:
            logger.error("[Summary] translate failed")
            return ""
        return reply_content
        

    def get_help_text(self, verbose=False, **kwargs):
        help_text = "聊天记录总结插件。\n"
        if not verbose:
            return help_text
        trigger_prefix = conf().get('plugin_trigger_prefix', "$")
        help_text += f"使用方法:输入\"{trigger_prefix}总结 最近消息数量\"，我会帮助你总结聊天记录。\n例如：\"{trigger_prefix}总结 100\"，我会总结最近100条消息。\n\n你也可以直接输入\"{trigger_prefix}总结前99条信息\"或\"{trigger_prefix}总结3小时内的最近10条消息\"\n我会尽可能理解你的指令。"
        return help_text

    def convert_text_to_image(self, text):
        converter = Text2ImageConverter()
        converter.setup_driver()
        image_path = converter.convert_text_to_image(text)
        converter.close()
        return image_path
