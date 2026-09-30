"""现役研究流程的确定性时间提示；不把时间基准当作事实证据。"""

from datetime import datetime


def current_research_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def time_hint(anchor: str) -> str:
    return (
        f"\n\n【时间基准】本阶段开始于 {anchor}（含时区 UTC 偏移）。"
        "涉及今天、今年、最新、近期或现状时，以此解释相对时间，不要默认停留在模型知识截止年份。"
        "用户明确指定的历史日期、时间范围或时区优先；不要把历史问题改成现状问题。"
        "检索词按问题需要加入年份或时间范围，不要给所有检索机械追加年份。"
        "核对事件发生时间、来源发布或更新时间及适用版本；检索到旧材料不能证明当前仍成立。"
        "此时间不是来源更新时间，也不是事实证据；最新情况无法核实时明确说明缺口。"
    )
