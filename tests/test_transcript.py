import json
from pathlib import Path
from stdtel.transcript import read_slice, attribute

def line(t, typ, **msg):
    return json.dumps({"type": typ, "timestamp": t, "message": msg})

def make_transcript(path: Path):
    rows = [
        line(100, "assistant", model="claude-x", usage={"input_tokens": 10, "output_tokens": 5},
             content=[{"type": "tool_use", "name": "Read", "id": "t0", "input": {}}]),
        line(101, "assistant", model="claude-x", usage={"input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 100},
             content=[{"type": "tool_use", "name": "Skill", "id": "t1", "input": {"skill": "structured-logging"}}]),
        line(102, "user", content=[{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 400}]),
        line(103, "assistant", model="claude-x", usage={"input_tokens": 30, "output_tokens": 15}, content=[]),
        line(104, "assistant", model="claude-x", usage={"input_tokens": 5, "output_tokens": 5},
             content=[{"type": "tool_use", "name": "Skill", "id": "t2", "input": {"skill": "other"}}]),
        line(105, "assistant", model="claude-y", usage={"input_tokens": 40, "output_tokens": 20}, content=[]),
    ]
    path.write_text("\n".join(rows) + "\n")

def test_incremental_and_attribution(tmp_path):
    p = tmp_path / "t.jsonl"; make_transcript(p)
    sl = read_slice(p, 0)
    assert len(sl.requests) == 5 and len(sl.skill_loads) == 2
    assert sl.skill_loads[0].load_tokens == 100
    att = {a.skill: a for a in attribute(sl)}
    # structured-logging owns requests at t=101..103 (before 'other' loads at 104)
    assert att["structured-logging"].tail.total == (20+5+100) + (30+15)
    assert att["structured-logging"].tail_first_only.total == (20+5+100)+(30+15)+(5+5)+(40+20)
    assert att["other"].tail.total == (5+5) + (40+20)
    assert att["other"].models == ["claude-x", "claude-y"]
    # second read from new offset yields nothing new
    assert read_slice(p, sl.new_offset).requests == []

def test_partial_trailing_line(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(line(1, "assistant", model="m", usage={"input_tokens": 1}, content=[]) + "\n" + '{"type": "assist')
    sl = read_slice(p, 0)
    assert len(sl.requests) == 1
    assert sl.new_offset == len(line(1, "assistant", model="m", usage={"input_tokens": 1}, content=[])) + 1
