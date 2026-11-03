"""聊天只读分析工具：读取持久结果，不创建任务、不调用后台模型。"""

from typing import Annotated, Literal
from pydantic import Field

from app.models.chat_tools import RunArguments, clip


class AnalysisArguments(RunArguments):
    section: Literal["overview", "facts", "inferences", "recommendations"] = "overview"
    offset: Annotated[int, Field(strict=True, ge=0, le=256)] = 0


ANALYSIS_ARGUMENTS = {"get_run_analysis": AnalysisArguments}
ANALYSIS_DESCRIPTIONS = {"get_run_analysis": "Read an owned run's saved background analysis, including report version and stale flag. Sections are paged. Empty means no saved result; never start analysis or training. Inferences and experiment directions are unverified hypotheses, not paper-backed conclusions."}
ANALYSIS_POLICY = "查询后台分析时调用 get_run_analysis。先确认运行，空结果说明尚无已保存分析；queued/running 不等于完成。is_stale=true 必须说明属于旧报告。事实与推断分开，建议是待验证方向；本工具没有核实论文，不可把它称为有论文依据的修改方案。用户要求分析时，可以直接基于本轮已读证据撰写聊天中的分析，不要把不能触发后台生成接口说成完全不能分析；聊天分析与已保存的后台报告应明确区分。"


def analysis_projection(result, args):
    content = result["content"]
    data = {"task_id": result["task_id"], "report_version": result["report_version"],
            "is_stale": result["is_stale"], "model_name": result["model_name"],
            "section": args.section, "notice": clip(content["notice"], 320)}
    if args.section == "overview":
        data.update({"fact_count": len(content["facts"]), "inference_count": len(content["inferences"]),
                     "recommendation_count": len(content["recommendations"]),
                     "omitted_fact_count": content["omitted_fact_count"],
                     "limitations": [clip(text, 200) for text in content["limitations"][:3]]})
    else:
        items = content[args.section]
        selected = []
        for item in items[args.offset:args.offset + 2]:
            row = dict(item)
            for key in ("text", "verification"):
                if key in row:
                    original = row[key]
                    row[key] = clip(original, 400)
                    if original != row[key]:
                        row["display_truncated"] = True
            selected.append(row)
        data.update({"items": selected, "offset": args.offset, "total": len(items),
                     "next_offset": args.offset + len(selected) if args.offset + len(selected) < len(items) else None})
    return data
